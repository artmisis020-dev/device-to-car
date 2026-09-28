"""Keypoint-одометрія — те, що мало бути реалізовано в старому проекті
як keypoint_detection.py + matching.py + homography.py + drone_position.py
(усі чотири файли лишились порожніми заглушками — код не переніссь /
не був дописаний). Тут ці чотири кроки — детекція, матчинг, гомографія,
позиція — реалізовані як один модуль, під сучасну задачу: не "кадр
дрона проти супутникової карти" (та задача — у visual_navigation, і має
ту саму проблему розриву доменів, що ми там діагностували), а
**кадр-до-кадру одометрія**, паралельна до OpticalFlowEstimator і
призначена working в парі з ним (README.md, розділ нижче).

Відмінність від flow_estimator.py._homography_flow (яка теж ORB+homography):
  - Тут ratio-test матчинг (Lowe's ratio) замість crossCheck — типово
    точніший відбір матчів при більшій кількості фіч.
  - Більше ORB-фіч за замовчуванням (це "повільніший, але надійніший"
    шлях — призначений для КРОС-перевірки основного flow, не для
    кожного кадру на максимальній швидкості).
  - Явно повертає H і matched keypoints — придатне і для майбутньої
    задачі "де я на карті" (не лише "як я рухався"), якщо колись
    знадобиться повернутись до оригінального задуму main.py.
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from flow_config import FlowConfig, CFG
from flow_math import (
    compensate_rotation,
    flow_to_body_velocity,
    homography_translation_px,
    verify_homography,
)


@dataclass
class KeypointOdometryResult:
    velocity_body: np.ndarray   # [vx_forward, vy_right], м/с
    mode: str                   # "keypoint" | "none"
    quality: float               # 0..1
    n_matches: int
    std: float
    homography: np.ndarray | None = None  # для майбутнього "де я на карті"


def detect_keypoints(gray: np.ndarray, n_features: int):
    """Крок 1 (був keypoint_detection.py): ORB — швидкий, бінарний
    дескриптор, придатний для RPi (на відміну від SIFT з gps_fixed_algo.py,
    який для real-time одометрії надто важкий)."""
    orb = cv2.ORB_create(nfeatures=n_features)
    return orb.detectAndCompute(gray, None)


def match_keypoints(desc1, desc2, ratio: float = 0.75):
    """Крок 2 (був matching.py): ratio test (Lowe) — для кожної точки
    беремо 2 найближчих сусіди і лишаємо матч, лише якщо перший значно
    кращий за другий. Точніший відбір, ніж простий crossCheck, коли
    матчів багато (типово для ORB із великою кількістю фіч)."""
    if desc1 is None or desc2 is None or len(desc1) < 2 or len(desc2) < 2:
        return []
    bf = cv2.BFMatcher(cv2.NORM_HAMMING)
    raw = bf.knnMatch(desc1, desc2, k=2)
    good = []
    for pair in raw:
        if len(pair) < 2:
            continue
        m, n = pair
        if m.distance < ratio * n.distance:
            good.append(m)
    return good


def estimate_homography(kp1, kp2, matches, ransac_thresh: float = 3.0,
                         min_matches: int = 15):
    """Крок 3 (був homography.py): гомографія + перевірка реалістичності
    (verify_homography з flow_math.py — та сама ідея, що в
    gps_positions/gps_fixed_algo.py.verify_homography, перенесена сюди)."""
    if len(matches) < min_matches:
        return None, 0
    src = np.float32([kp1[m.queryIdx].pt for m in matches]).reshape(-1, 1, 2)
    dst = np.float32([kp2[m.trainIdx].pt for m in matches]).reshape(-1, 1, 2)
    H, mask = cv2.findHomography(src, dst, cv2.RANSAC, ransac_thresh)
    if H is None or not verify_homography(H):
        return None, 0
    inliers = int(mask.sum()) if mask is not None else 0
    return H, inliers


def homography_to_position_shift(H: np.ndarray, image_size: tuple[int, int],
                                  altitude_m: float, focal_px: float) -> np.ndarray:
    """Крок 4 (був drone_position.py): зсув центру кадру під дією H ->
    метричний зсув на землі. Той самий математичний хід, що для
    flow-режиму (flow_math.homography_translation_px +
    flow_to_body_velocity), винесений тут окремо, бо саме це — та
    функція, яку б використовував і майбутній "де я на карті" режим
    (там H рахується між кадром дрона і тайлом карти, а не між двома
    послідовними кадрами — решта математики та сама)."""
    dx_px_dy_px = homography_translation_px(H, image_size)
    angular = dx_px_dy_px / focal_px
    return angular * altitude_m  # метричний зсув [x,y] у системі кадру


class KeypointOdometryEstimator:
    """Публічний інтерфейс — той самий патерн виклику, що
    OpticalFlowEstimator.estimate(), для сумісності з
    combined_estimator.py та ekf_bridge.py."""

    def __init__(self, config: FlowConfig | None = None, n_features: int = 800,
                 match_ratio: float = 0.75, min_matches: int = 15,
                 velocity_std: float = 0.7):
        self.cfg = config or CFG
        self.n_features = n_features
        self.match_ratio = match_ratio
        self.min_matches = min_matches
        self.velocity_std = velocity_std

    def estimate(self, prev_gray: np.ndarray, curr_gray: np.ndarray,
                 altitude_m: float, gyro_body_rads: np.ndarray, dt: float) -> KeypointOdometryResult:
        if altitude_m is None or altitude_m <= 0 or dt <= 0:
            return KeypointOdometryResult(np.zeros(2), "none", 0.0, 0, self.velocity_std)

        kp1, des1 = detect_keypoints(prev_gray, self.n_features)
        kp2, des2 = detect_keypoints(curr_gray, self.n_features)
        matches = match_keypoints(des1, des2, self.match_ratio)
        H, n_inliers = estimate_homography(kp1, kp2, matches, self.cfg.ransac_reproj_thresh,
                                            self.min_matches)
        if H is None:
            return KeypointOdometryResult(np.zeros(2), "none", 0.0, len(matches), self.velocity_std)

        h, w = prev_gray.shape[:2]
        focal_px = self.cfg.camera.focal_px()
        flow_px = homography_translation_px(H, (w, h))
        flow_px = compensate_rotation(flow_px, gyro_body_rads, dt, focal_px)
        velocity = flow_to_body_velocity(flow_px, altitude_m, dt, focal_px)

        quality = min(1.0, n_inliers / max(self.min_matches, 1) / 3.0)
        std = self.velocity_std / max(quality, 0.15)
        return KeypointOdometryResult(velocity, "keypoint", quality, n_inliers, std, H)
