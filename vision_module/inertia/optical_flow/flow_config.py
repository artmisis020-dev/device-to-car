"""Конфігурація оптичного потоку — камера, пороги режимів, осі.

Дивись README.md цієї теки для фізичного обґрунтування кожного рішення
(чому два режими, чому пірамідальний LK, чому homography на висоті).
"""
from dataclasses import dataclass


@dataclass
class CameraConfig:
    # Горизонтальне поле зору камери вниз, градуси. НЕПЕРЕВІРЕНЕ значення-
    # заглушка — заміни на реальне з калібрування/специфікації камери.
    hfov_deg: float = 90.0
    # 1920x1080 — реальна роздільність запису нижньої камери
    # (additional_modules/lowercam/lowercam_capture.py, SIRENA_LOWERCAM_
    # WIDTH/HEIGHT). Раніше тут стояло 640x480 (застарілий плейсхолдер) —
    # focal_px() рахувала фокусну відстань під ЦЮ ширину, а реальні кадри
    # (RecordingFrameSource читає нативну роздільність без ресайзу) — 1920px,
    # тобто кутовий переклад пікселів у flow_math.py був системно втричі
    # неправильний (focal_px занижена в 3 рази -> кутовий зсув/швидкість
    # завищені в 3 рази). hfov_deg і далі неперевірене — лише ширина/висота
    # тепер відповідає реальності.
    width: int = 1920
    height: int = 1080

    def focal_px(self) -> float:
        """Фокальна відстань у пікселях (pinhole-модель) — потрібна для
        переведення кутового зсуву (рад) у зсув у пікселях і навпаки."""
        import math
        return (self.width / 2.0) / math.tan(math.radians(self.hfov_deg) / 2.0)


@dataclass
class FlowConfig:
    camera: CameraConfig = None

    # Нижче цієї висоти (м) — режим per-feature Lucas-Kanade (помітний
    # паралакс, малі об'єкти трекаються добре). Вище — homography
    # (земля майже плоска здалеку, менше локальних точок, дешевше й
    # стійкіше рахувати одне глобальне перетворення кадру).
    altitude_mode_threshold_m: float = 50.0

    # Lucas-Kanade (низька висота). lk_min_distance знижено з 7 (було
    # занадто рідко для 1920x1080 — мало точок -> низька quality, багато
    # кадрів відкидались update-порогом нижче навіть при чистому треку).
    lk_max_corners: int = 300
    lk_quality_level: float = 0.01
    lk_min_distance: int = 5
    lk_win_size: int = 21
    lk_max_pyr_level: int = 3          # пірамідальність — головний засіб
    # покриття широкого діапазону зсуву кадру (див. README, розділ 1)

    # Homography (висока висота)
    orb_n_features: int = 500
    ransac_reproj_thresh: float = 3.0
    min_homography_matches: int = 15

    # Фільтрація викидів при агрегації сирого потоку в один вектор
    flow_outlier_trim_frac: float = 0.2  # відкинути 20% найбільш "інакших"

    # Довіра до вимірювання (std для EKF update_velocity), м/с —
    # орієнтовно; уточнити на реальних даних, коли вони з'являться.
    velocity_std_low_alt: float = 0.3
    velocity_std_high_alt: float = 1.0

    def __post_init__(self):
        if self.camera is None:
            self.camera = CameraConfig()


CFG = FlowConfig()
