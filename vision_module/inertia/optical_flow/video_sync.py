"""Синхронізація запису нижньої камери з epoch-часом CSV-логу
(vision_module/inertia/inertia_log_service.py на адмін-сервері) — ЗА
ІМЕНЕМ ФАЙЛУ, а не по годиннику самого відео (у сирому .h264/copy-mp4 без
надійного демуксингу немає гарантованих wall-clock міток).

Продюсер файлу — admin_module/services/lowercam_recording_service.py
(на адмін-сервері, НЕ на РПі — lowercam_capture.py на РПі лише стрімить,
локально нічого не пише): `rec_{datetime.now():%Y%m%d_%H%M%S}.mp4`.
Адмін-сервер живе в UTC (перевірено `timedatectl` наживо: Etc/UTC) — на
відміну від старого /home/manager/record.sh на РПі (Europe/Kyiv), тому
ім'я файлу тепер інтерпретується як UTC напряму, без конвертації. CSV
"timestamp" — `time.time()`-епоха (UTC-байдужа секунда з 1970), тож
розбіжність у трактуванні тут дала б систематичний зсув на весь TZ-офсет."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np

_REC_FILENAME_RE = re.compile(r"rec_(\d{8})_(\d{6})")


def video_start_epoch(video_path) -> float:
    """rec_YYYYMMDD_HHMMSS(...).mp4 -> epoch секунда старту запису (UTC)."""
    name = Path(video_path).name
    m = _REC_FILENAME_RE.search(name)
    if not m:
        raise ValueError(
            f"не вдалось розпізнати timestamp у імені файлу запису: {name!r} "
            "(очікується формат rec_YYYYMMDD_HHMMSS.mp4, як пише lowercam_recording_service.py)"
        )
    dt_utc = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    return dt_utc.timestamp()


class RecordingFrameSource:
    """Послідовний (лише вперед, без seek назад) читач кадрів additional-lowercam.service
    .h264, вирівняний з epoch-часом CSV-рядків через timestamp у імені
    файлу. Призначений для одного проходу replay-циклу в порядку зростання
    часу — той самий порядок, що вже й так у ekf_replay.py::run()."""

    def __init__(self, video_path):
        self.start_epoch = video_start_epoch(video_path)
        self.cap = cv2.VideoCapture(str(video_path))
        if not self.cap.isOpened():
            raise RuntimeError(f"не вдалось відкрити відео: {video_path}")
        self.fps = self.cap.get(cv2.CAP_PROP_FPS) or 30.0
        self._last_frame_idx = -1
        self._last_gray: np.ndarray | None = None
        self._last_frame_t: float | None = None

    def close(self) -> None:
        self.cap.release()

    def frame_pair_at(self, t_epoch: float):
        """Повертає (prev_gray, curr_gray, dt) для НАЙСВІЖІШОГО кадру, що
        настав не пізніше t_epoch, порівняно з попереднім разом, коли
        такий кадр був. None — якщо: до старту відео, немає нового кадру
        відносно минулого виклику (кілька CSV-рядків між кадрами відео —
        нормально, бо запис 30fps, CSV пише раз на 0.1с), відео
        скінчилось, або це перший кадр (нема з чим порівнювати)."""
        target_idx = int((t_epoch - self.start_epoch) * self.fps)
        if target_idx < 0 or target_idx <= self._last_frame_idx:
            return None

        gray = None
        while self._last_frame_idx < target_idx:
            ok, frame = self.cap.read()
            if not ok:
                return None
            self._last_frame_idx += 1
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        frame_t = self.start_epoch + self._last_frame_idx / self.fps
        prev_gray, prev_t = self._last_gray, self._last_frame_t
        self._last_gray, self._last_frame_t = gray, frame_t

        if prev_gray is None:
            return None
        return prev_gray, gray, frame_t - prev_t
