"""InertiaReplayWorker — один потік на пристрій: одноразовий (не циклічний,
на відміну від InferenceWorker) прогін offline-replay EKF
(vision_module/inertia/ekf_replay.py) над парою відео нижньої камери
(качається з РПі за посиланням) + inertia CSV (текстом у тілі запиту від
адмінки, вже маленький — не якає окремого HTTP-виклику назад).

vision_module/inertia/ — не Python-пакет (плоскі імпорти на кшталт
`import airframe`, розраховані на python3 ekf_replay.py напряму з тієї
директорії) — тож `import ekf_replay` тут працює лише додавши цю
директорію в sys.path (одноразово, при першому імпорті цього файлу)."""

from __future__ import annotations

import logging
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

import numpy as np
import requests

_INERTIA_DIR = Path(__file__).resolve().parent / "inertia"
if str(_INERTIA_DIR) not in sys.path:
    sys.path.append(str(_INERTIA_DIR))
import ekf_replay  # noqa: E402

logger = logging.getLogger(__name__)

VIDEO_DOWNLOAD_TIMEOUT_S = 30
VIDEO_DOWNLOAD_CHUNK = 1024 * 1024


def _summarize(raw: dict) -> dict:
    """Ті самі підсумкові цифри, що друкує `python3 ekf_replay.py` в CLI —
    сирі numpy-масиви (позиції/помилки по кожному фрейму) тут НЕ віддаємо:
    для довгого польоту це можуть бути десятки тисяч точок, а панелі
    потрібен лише підсумок, не графік.

    has_ground_truth=False (немає GPS/local_position у лозі — типово для
    бенч-тесту/приміщення) — GPS тут ЛИШЕ еталон для звірки дрейфу, не
    вхід самого розрахунку інерції, тож EKF (IMU+баро+потік) і без нього
    рахує повноцінну траєкторію; interval_pct/interval_max_err просто
    відсутні, звіт натомість дає зміщення/пройдений шлях."""
    summary = {
        "has_ground_truth": bool(raw.get("has_ground_truth")),
        "flow_applied_count": int(raw.get("flow_applied_count", 0)),
    }
    if not summary["has_ground_truth"]:
        summary["displacement_m"] = float(raw.get("displacement_m", 0.0))
        summary["path_length_m"] = float(raw.get("path_length_m", 0.0))
        return summary

    pct = raw["interval_pct"]
    err = raw["interval_max_err"]
    summary["n_intervals"] = int(len(pct))
    if len(pct):
        summary["median_pct"] = float(np.median(pct))
        summary["p95_pct"] = float(np.percentile(pct, 95))
        summary["max_pct"] = float(pct.max())
        summary["median_err_m"] = float(np.median(err))
        summary["max_err_m"] = float(err.max())
    return summary


class InertiaReplayWorker(threading.Thread):
    def __init__(self, device_id: str, video_url: str, csv_text: str):
        super().__init__(name=f"inertia-replay-{device_id}", daemon=True)
        self.device_id = device_id
        self.video_url = video_url
        self.csv_text = csv_text

        self.started_ts = time.time()
        self.done = False
        self.result: dict | None = None
        self.error: str | None = None

    def status(self) -> dict:
        return {
            "success": True,
            "device_id": self.device_id,
            "active": self.is_alive(),
            "done": self.done,
            "started_ts": self.started_ts,
            "result": self.result,
            "error": self.error,
        }

    def run(self) -> None:
        tmp_dir = tempfile.mkdtemp(prefix="sirena-inertia-")
        try:
            csv_path = os.path.join(tmp_dir, "log.csv")
            Path(csv_path).write_text(self.csv_text)

            # Оригінальна назва файлу з URL, НЕ фіксована — video_sync.py
            # парсить rec_YYYYMMDD_HHMMSS саме з імені для синхронізації з
            # CSV; фіксована назва (напр. "lowercam.h264") ламала цей парсинг
            # (перевірено наживо: "не вдалось розпізнати timestamp").
            video_name = unquote(Path(urlparse(self.video_url).path).name) or "lowercam.mp4"
            video_path = os.path.join(tmp_dir, video_name)
            with requests.get(self.video_url, stream=True, timeout=VIDEO_DOWNLOAD_TIMEOUT_S) as resp:
                resp.raise_for_status()
                with open(video_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=VIDEO_DOWNLOAD_CHUNK):
                        if chunk:
                            f.write(chunk)

            # GPS/local_position у лозі — лише опційний еталон для звірки
            # дрейфу, не вхід розрахунку: EKF (IMU+баро+потік) рахує
            # траєкторію і без нього (весь сенс інерціальної навігації —
            # саме НЕ залежати від GPS). run() тому більше не повертає
            # None — завжди або сира траєкторія, або звірена проти GPS.
            raw = ekf_replay.run(csv_path, video_path=video_path, verbose=False)
            self.result = _summarize(raw)
        except Exception as exc:
            logger.exception("[%s] inertia replay провалився", self.device_id)
            self.error = str(exc)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            self.done = True
