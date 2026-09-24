"""Самотест без реального відео: беремо текстуроване зображення
(big.png з visual_navigation, якщо є в репо — та сама супутникова карта;
інакше генеруємо синтетичну текстуру, щоб тест лишався запускним і без
цього модуля), штучно зсуваємо його на ВІДОМУ величину, перевіряємо, чи
естіматор відновлює правильну швидкість. Так само перевіряємо
гіро-компенсацію: "обертання без руху" має давати швидкість ~0 після
компенсації.

Запуск: python3 test_synthetic.py
"""
import os

import cv2
import numpy as np

from flow_config import FlowConfig, CameraConfig
from flow_estimator import OpticalFlowEstimator

# optical_flow/ — підпакет vision_module/inertia/, тому "../.." до кореня
# репо, звідти вниз у visual_navigation/ (модуль ще не доданий у це репо
# на момент написання — див. фолбек у load_source()).
MAP_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "..", "visual_navigation", "big.png")


def load_source():
    img = cv2.imread(MAP_PATH, cv2.IMREAD_GRAYSCALE)
    if img is not None:
        return img
    # visual_navigation/big.png ще нема в цьому репо — синтетична текстура
    # (шум + кілька плям розміром з "об'єкти на землі") цілком достатня для
    # перевірки самої математики зсуву/гіро-компенсації, не потребує
    # реального супутникового знімка.
    print(f"Увага: {MAP_PATH} не знайдено — використовую синтетичну текстуру.")
    rng = np.random.default_rng(42)
    img = rng.integers(0, 255, (600, 600), dtype=np.uint8)
    img = cv2.GaussianBlur(img, (3, 3), 0)
    for _ in range(40):
        cx, cy = rng.integers(50, 550, 2)
        r = rng.integers(5, 20)
        cv2.circle(img, (int(cx), int(cy)), int(r), int(rng.integers(0, 255)), -1)
    return img


def make_pair(src, crop=320, shift_px=(6.0, -4.0), rotate_deg=0.0, pad=60):
    """prev — вирізка crop x crop з центру src; curr — та сама ділянка
    з відомим зсувом/поворотом. `big` — вирізка з запасом pad з УСІХ
    боків навколо (x0,y0), щоб warpAffine мав звідки брати контент і не
    тягнув чорні/відбиті краї в корисну область; асиметричне клампування
    краю зламало б відповідність регіонів prev/curr, тому перевіряємо
    явно (раніше тут був саме цей баг: crop-регіон стояв біля краю
    карти через w//2-crop замість w//2-crop//2, і pad клампився)."""
    h, w = src.shape
    x0, y0 = w // 2 - crop // 2, h // 2 - crop // 2
    assert y0 - pad >= 0 and x0 - pad >= 0 and y0 + crop + pad <= h and x0 + crop + pad <= w, (
        f"crop+pad не вміщається в джерело {w}x{h}: спробуй менший crop/pad"
    )
    prev = src[y0:y0 + crop, x0:x0 + crop].copy()

    big = src[y0 - pad:y0 + crop + pad, x0 - pad:x0 + crop + pad]
    Mc = cv2.getRotationMatrix2D((big.shape[1] / 2, big.shape[0] / 2), rotate_deg, 1.0)
    Mc[0, 2] += shift_px[0]
    Mc[1, 2] += shift_px[1]
    shifted_big = cv2.warpAffine(big, Mc, (big.shape[1], big.shape[0]), borderMode=cv2.BORDER_REFLECT)
    curr = shifted_big[pad:pad + crop, pad:pad + crop]
    return prev, curr


def check(name, expected_vx, expected_vy, got_vx, got_vy, tol=0.35):
    ok = abs(expected_vx - got_vx) < tol and abs(expected_vy - got_vy) < tol
    status = "OK " if ok else "FAIL"
    print(f"[{status}] {name}: очікувано vx={expected_vx:.2f} vy={expected_vy:.2f} м/с, "
          f"отримано vx={got_vx:.2f} vy={got_vy:.2f} м/с")
    return ok


def main():
    src = load_source()
    cfg = FlowConfig(camera=CameraConfig(hfov_deg=90.0, width=320, height=320))
    est = OpticalFlowEstimator(cfg)

    altitude_m = 30.0
    dt = 0.1
    focal_px = cfg.camera.focal_px()

    print(f"focal_px={focal_px:.1f}, altitude={altitude_m}м, dt={dt}с\n")

    all_ok = True

    # --- 1. Низька висота (LK): чистий зсув вперед (по Y кадру -> +Y потік -> vx_forward>0) ---
    shift = (0.0, 8.0)  # (dx_px, dy_px) зсув КОНТЕНТУ кадру
    prev, curr = make_pair(src, shift_px=shift)
    res = est.estimate(prev, curr, altitude_m=20.0, gyro_body_rads=np.zeros(3), dt=dt)
    # очікувана швидкість: угловой = dy_px/focal, metric = angular*alt, v = metric/dt
    # мапінг: dy_px (зсув контенту вперед по кадру) -> vx_forward (за формулою в flow_math)
    expected_angular = np.array(shift) / focal_px
    expected_metric = expected_angular * 20.0
    expected_v_frame = expected_metric / dt
    expected_vx = expected_v_frame[1]
    expected_vy = -expected_v_frame[0]
    print(f"Режим отримано: {res.mode}, якість={res.quality:.2f}, точок={res.n_points}")
    all_ok &= check("LK, чистий рух", expected_vx, expected_vy, res.velocity_body[0], res.velocity_body[1])

    # --- 2. Висока висота (homography): той самий тест, інший режим ---
    prev2, curr2 = make_pair(src, shift_px=shift)
    res2 = est.estimate(prev2, curr2, altitude_m=200.0, gyro_body_rads=np.zeros(3), dt=dt)
    expected_metric2 = expected_angular * 200.0
    expected_v_frame2 = expected_metric2 / dt
    expected_vx2 = expected_v_frame2[1]
    expected_vy2 = -expected_v_frame2[0]
    print(f"\nРежим отримано: {res2.mode}, якість={res2.quality:.2f}, точок={res2.n_points}")
    all_ok &= check("Homography, чистий рух", expected_vx2, expected_vy2,
                     res2.velocity_body[0], res2.velocity_body[1], tol=1.5)

    # --- 3. Гіро-компенсація: рух кадру ЛИШЕ від обертання (як реальний rotate),
    #        гіроскоп повідомляє ту саму кутову швидкість -> після компенсації v~0 ---
    pitch_rate = 0.05  # рад/с
    rot_shift_px = pitch_rate * dt * focal_px  # той самий зсув, що дає обертання
    prev3, curr3 = make_pair(src, shift_px=(0.0, rot_shift_px), rotate_deg=0.0)
    res3 = est.estimate(prev3, curr3, altitude_m=20.0,
                         gyro_body_rads=np.array([0.0, pitch_rate, 0.0]), dt=dt)
    print(f"\nРежим отримано: {res3.mode}, якість={res3.quality:.2f}, точок={res3.n_points}")
    all_ok &= check("LK, лише обертання (гіро-компенсація)", 0.0, 0.0,
                     res3.velocity_body[0], res3.velocity_body[1], tol=0.4)

    print(f"\n{'='*50}\n{'УСІ ТЕСТИ OK' if all_ok else 'Є ПРОВАЛЕНІ ТЕСТИ'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
