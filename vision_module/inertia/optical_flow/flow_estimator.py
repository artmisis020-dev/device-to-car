"""OpticalFlowEstimator — оцінка горизонтальної швидкості дрона з камери
вниз, з перемиканням режиму за висотою (README.md, розділ "Діапазон
висот 15-500м — це два різні режими"):

  - Низька висота (< altitude_mode_threshold_m): Lucas-Kanade,
    пірамідальний, per-feature — помітний паралакс, трекінг точок.
  - Висока висота (>= порогу): homography всього кадру — земля майже
    плоска здалеку, дешевше й стійкіше при рідкій текстурі.

Вхід — пара grayscale-кадрів, поточна баро-висота, гіроскоп, dt.
Вихід — [vx_forward, vy_right] м/с у body-фреймі + якість/режим, готове
для InertialEstimator/EKFEstimator.update_velocity() з vision_module/inertia.
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from flow_config import FlowConfig, CFG
from flow_math import (
    compensate_rotation,
    flow_to_body_velocity,
    robust_mean_flow,
    homography_translation_px,
)


@dataclass
class FlowResult:
    velocity_body: np.ndarray   # [vx_forward, vy_right], м/с
    mode: str                   # "lk" | "homography" | "none"
    quality: float              # 0..1, орієнтовна довіра
    n_points: int                # скільки точок/матчів узято до уваги
    std: float                   # рекомендований std для EKF update_velocity


class OpticalFlowEstimator:
    def __init__(self, config: FlowConfig | None = None):
        self.cfg = config or CFG
        self._orb = cv2.ORB_create(nfeatures=self.cfg.orb_n_features)
        self._bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)

    def estimate(self, prev_gray: np.ndarray, curr_gray: np.ndarray,
                 altitude_m: float, gyro_body_rads: np.ndarray, dt: float) -> FlowResult:
        if altitude_m is None or altitude_m <= 0 or dt <= 0:
            return FlowResult(np.zeros(2), "none", 0.0, 0, self.cfg.velocity_std_high_alt)

        if altitude_m < self.cfg.altitude_mode_threshold_m:
            flow_px, n_points, quality = self._lk_flow(prev_gray, curr_gray)
            std = self.cfg.velocity_std_low_alt
            mode = "lk"
        else:
            flow_px, n_points, quality = self._homography_flow(prev_gray, curr_gray)
            std = self.cfg.velocity_std_high_alt
            mode = "homography"

        if flow_px is None:
            return FlowResult(np.zeros(2), "none", 0.0, 0, std)

        focal_px = self.cfg.camera.focal_px()
        flow_px = compensate_rotation(flow_px, gyro_body_rads, dt, focal_px)
        velocity = flow_to_body_velocity(flow_px, altitude_m, dt, focal_px)

        # Довіра нижча, якщо мало точок/матчів підтверджують оцінку.
        std_scaled = std / max(quality, 0.15)
        return FlowResult(velocity, mode, quality, n_points, std_scaled)

    # ------------------------------------------------------------ низька висота

    def _lk_flow(self, prev_gray: np.ndarray, curr_gray: np.ndarray):
        cfg = self.cfg
        pts_prev = cv2.goodFeaturesToTrack(
            prev_gray, maxCorners=cfg.lk_max_corners, qualityLevel=cfg.lk_quality_level,
            minDistance=cfg.lk_min_distance,
        )
        if pts_prev is None or len(pts_prev) < 5:
            return None, 0, 0.0

        win = (cfg.lk_win_size, cfg.lk_win_size)
        pts_curr, status, _err = cv2.calcOpticalFlowPyrLK(
            prev_gray, curr_gray, pts_prev, None,
            winSize=win, maxLevel=cfg.lk_max_pyr_level,
            criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.01),
        )
        status = status.reshape(-1).astype(bool)
        good_prev = pts_prev.reshape(-1, 2)[status]
        good_curr = pts_curr.reshape(-1, 2)[status]
        if len(good_prev) < 5:
            return None, len(good_prev), 0.0

        flow_px = robust_mean_flow(good_prev, good_curr, cfg.flow_outlier_trim_frac)
        quality = min(1.0, len(good_prev) / cfg.lk_max_corners)
        return flow_px, len(good_prev), quality

    # ------------------------------------------------------------ висока висота

    def _homography_flow(self, prev_gray: np.ndarray, curr_gray: np.ndarray):
        cfg = self.cfg
        kp1, des1 = self._orb.detectAndCompute(prev_gray, None)
        kp2, des2 = self._orb.detectAndCompute(curr_gray, None)
        if des1 is None or des2 is None or len(kp1) < cfg.min_homography_matches:
            return None, 0, 0.0

        matches = self._bf.match(des1, des2)
        if len(matches) < cfg.min_homography_matches:
            return None, len(matches), 0.0
        matches = sorted(matches, key=lambda m: m.distance)

        src = np.float32([kp1[m.queryIdx].pt for m in matches]).reshape(-1, 1, 2)
        dst = np.float32([kp2[m.trainIdx].pt for m in matches]).reshape(-1, 1, 2)
        H, mask = cv2.findHomography(src, dst, cv2.RANSAC, cfg.ransac_reproj_thresh)
        if H is None:
            return None, len(matches), 0.0

        inliers = int(mask.sum()) if mask is not None else 0
        h, w = prev_gray.shape[:2]
        flow_px = homography_translation_px(H, (w, h))
        quality = min(1.0, inliers / max(cfg.min_homography_matches, 1) / 3.0)
        return flow_px, inliers, quality


if __name__ == "__main__":
    print("Дивись test_synthetic.py для самотесту без реального відео.")
