"""Завантажує сирі Starlink-точки (з логів `sirena-gps-hub.service` на РПі,
файл data/starlink_raw_data.json) і швидкість FC (з телеметрії адмін-сервера,
data/fc_velocity_data.json) для одного вікна польоту.

Обидва JSON — вже готові вивантаження (не тягнуть нічого по мережі самі);
цей файл лише парсить їх у зручну для обчислень форму (epoch-час, м/с)."""

from __future__ import annotations

import bisect
import csv
import json
import sys
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import geo_utils

DATA_DIR = Path(__file__).resolve().parent / "data"


class RawPoint(NamedTuple):
    t: float  # unix epoch UTC
    lat: float
    lon: float
    alt: float


class VelSample(NamedTuple):
    t: float  # unix epoch UTC
    lat: float
    lon: float
    vx: float  # м/с, NED North
    vy: float  # м/с, NED East
    vz: float  # м/с, NED Down


def load_raw_starlink(flight: str) -> list[RawPoint]:
    data = json.loads((DATA_DIR / "starlink_raw_data.json").read_text())
    pts = []
    for row in data[flight]:
        t = geo_utils.parse_iso_to_epoch(row["ts"])
        pts.append(RawPoint(t=t, lat=row["lat"], lon=row["lon"], alt=row["alt"]))
    pts.sort(key=lambda p: p.t)
    return pts


class VelocityLookup:
    """Найближчий за часом семпл реальної швидкості FC (GLOBAL_POSITION_INT).

    FC-телеметрія йде набагато частіше (~5Гц) за Starlink (1Гц), тому для
    кожної Starlink-мітки часу береться найближчий FC-семпл — це і є
    "інерційна" швидкість, яку ми фюзимо (вона вже включає реальний
    гіро+акс з FC, не з нашого власного датчика)."""

    def __init__(self, samples: list[VelSample]):
        self._samples = sorted(samples, key=lambda s: s.t)
        self._times = [s.t for s in self._samples]

    def nearest(self, t: float) -> VelSample | None:
        if not self._samples:
            return None
        i = bisect.bisect_left(self._times, t)
        candidates = [c for c in (i - 1, i) if 0 <= c < len(self._samples)]
        if not candidates:
            return None
        return min((self._samples[c] for c in candidates), key=lambda s: abs(s.t - t))


def load_velocity(flight: str) -> VelocityLookup:
    data = json.loads((DATA_DIR / "fc_velocity_data.json").read_text())
    samples = [
        VelSample(t=row["ts"], lat=row["lat"], lon=row["lon"], vx=row["vx"], vy=row["vy"], vz=row["vz"])
        for row in data[flight]
    ]
    return VelocityLookup(samples)


class ImuRow(NamedTuple):
    t: float
    acc_x: float
    acc_y: float
    acc_z: float
    gyro_x: float
    gyro_y: float
    gyro_z: float
    roll: float
    pitch: float
    yaw: float
    baro_alt: float


_INERTIA_DIR = Path(__file__).resolve().parent.parent.parent / "vision_module" / "inertia"


def load_imu_rows(flight: str) -> list[ImuRow]:
    """Сирі IMU-рядки з data/<flight>_inertia.csv (вирізка з реального
    безперервного логу admin_module/services/inertia_log_service.py —
    той самий формат, що читає vision_module/inertia/replay.py). Це ЛИШЕ
    дані — сам EKF (EKFEstimator з vision_module/inertia/ekf_estimator.py)
    кожен route_*_ourinertia.py жене сам, покроково, синхронно з фільтром
    Starlink (не через пакетний ekf_replay.run(), бо той скидається на
    GPS через фіксований інтервал, а нам треба скидатись саме на кожен
    ПРИЙНЯТИЙ Starlink-фікс і вільно інтегрувати лише на самому викиді —
    так це й буде працювати в продакшні)."""
    with open(DATA_DIR / f"{flight}_inertia.csv", newline="") as f:
        reader = csv.DictReader(f)
        rows = [
            ImuRow(
                t=float(r["timestamp"]),
                acc_x=float(r["acc_x"]), acc_y=float(r["acc_y"]), acc_z=float(r["acc_z"]),
                gyro_x=float(r["gyro_x"]), gyro_y=float(r["gyro_y"]), gyro_z=float(r["gyro_z"]),
                roll=float(r["roll"]), pitch=float(r["pitch"]), yaw=float(r["yaw"]),
                baro_alt=float(r["baro_alt"] or 0.0),
            )
            for r in reader
        ]
    rows.sort(key=lambda r: r.t)
    return rows


def new_ekf_estimator():
    """Створює чистий EKFEstimator з vision_module/inertia — БЕЗ жодного
    зв'язку з EKF ArduPilot/FC: рахує сам, лише з сирих IMU-рядків вище."""
    sys.path.insert(0, str(_INERTIA_DIR))
    sys.path.insert(0, str(_INERTIA_DIR / "optical_flow"))
    from ekf_estimator import EKFConfig, EKFEstimator  # noqa: E402

    return EKFEstimator(EKFConfig())
