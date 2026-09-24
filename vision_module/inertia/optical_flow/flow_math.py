"""Математика: піксельний потік/гомографія -> метрична швидкість.

Фізика (детальніше в README.md):
  1. Потік = потік_від_руху + потік_від_обертання. Другий — шум, який
     треба відняти через гіроскоп ПЕРЕД переведенням у швидкість.
  2. Кутовий_потік [рад] = зсув [px] / фокальна_відстань [px].
  3. Метричний_зсув [м] = Кутовий_потік [рад] * Висота [м].
  4. Швидкість [м/с] = Метричний_зсув / dt.

Мапінг осей — PX4-конвенція (README.md, таблиця "Рух дрона -> Потік"):
  вперед(X) -> +Y потік, вправо(Y) -> -X потік. Тобто в body-фреймі:
  vx_body (вперед) залежить від flow_y, vy_body (вправо) — від flow_x
  (з відповідними знаками).
"""
from __future__ import annotations

import numpy as np


def compensate_rotation(flow_px: np.ndarray, gyro_body_rads: np.ndarray,
                         dt: float, focal_px: float) -> np.ndarray:
    """Віднімає з сирого потоку [dx_px, dy_px] складову від обертання
    корпуса (roll rate, pitch rate) за час dt. yaw (обертання навколо
    оптичної осі) сюди свідомо не включений — це не зсув, а поворот
    кадру, окрема (нехтувано мала для малих dt) поправка.

    gyro_body_rads — [gx, gy, gz] (roll, pitch, yaw rate), рад/с, той
    самий гіроскоп, що й у vision_module/inertia/estimator.py.
    """
    gx, gy = gyro_body_rads[0], gyro_body_rads[1]
    # Кутовий зсув кадру від обертання за dt, у пікселях (мала кутова
    # апроксимація: зсув_px = кут_рад * focal_px).
    rot_dx_px = gy * dt * focal_px   # pitch rate -> зсув по Y-осі кадру (вперед/назад)
    rot_dy_px = gx * dt * focal_px   # roll rate  -> зсув по X-осі кадру (вліво/вправо)
    return flow_px - np.array([rot_dy_px, rot_dx_px])


def flow_to_body_velocity(flow_px: np.ndarray, altitude_m: float, dt: float,
                           focal_px: float) -> np.ndarray:
    """[dx_px, dy_px] (уже без обертання) -> [vx_forward, vy_right] м/с.

    altitude_m — висота над поверхнею (не над домом/launch — саме
    відстань до того, що бачить камера; баро підійде як наближення на
    рівній місцевості, rangefinder точніший на малій висоті)."""
    if dt <= 0 or altitude_m <= 0:
        return np.zeros(2)
    angular = flow_px / focal_px             # рад
    metric_shift = angular * altitude_m       # м (уздовж X,Y кадру)
    velocity_frame = metric_shift / dt        # м/с, у системі координат кадру

    dx_px, dy_px = flow_px
    # Мапінг з таблиці README: forward(X body) -> +Y потік, right(Y body) -> -X потік.
    # Обертаємо назад: vx_forward = velocity_frame[1], vy_right = -velocity_frame[0].
    vx_forward = velocity_frame[1]
    vy_right = -velocity_frame[0]
    return np.array([vx_forward, vy_right])


def robust_mean_flow(points_prev: np.ndarray, points_curr: np.ndarray,
                      trim_frac: float = 0.2) -> np.ndarray:
    """Агрегує набір точкових зсувів (Lucas-Kanade) в один вектор,
    відкидаючи trim_frac найбільш відхилених (типово — рухомі об'єкти
    в кадрі: машини, хвилі, а не сама земля)."""
    diffs = points_curr - points_prev  # Nx2, [dx,dy] по кожній точці
    if len(diffs) == 0:
        return np.zeros(2)
    med = np.median(diffs, axis=0)
    dist = np.linalg.norm(diffs - med, axis=1)
    keep_n = max(1, int(len(diffs) * (1 - trim_frac)))
    keep_idx = np.argsort(dist)[:keep_n]
    return diffs[keep_idx].mean(axis=0)


def homography_translation_px(H: np.ndarray, image_size: tuple[int, int]) -> np.ndarray:
    """Зсув центру кадру під дією гомографії H (prev -> curr), у пікселях.
    Правильніше за читання H[0,2]/H[1,2] напряму, бо враховує й
    масштаб/поворот, які теж входять у H при неідеально плоскій/дальній
    сцені."""
    w, h = image_size
    center = np.array([w / 2.0, h / 2.0, 1.0])
    warped = H @ center
    warped = warped[:2] / warped[2]
    return warped - center[:2]
