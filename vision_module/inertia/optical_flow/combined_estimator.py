"""Одометрія (keypoint_odometry.py) + оптичний потік (flow_estimator.py)
разом — незалежні оцінки того самого: горизонтальної швидкості дрона з
камери вниз. "Непогано працюють в парі" (з постановки задачі) саме тому,
що в них РІЗНІ слабкі місця:

  - OpticalFlowEstimator (LK): швидкий, але per-feature трекінг ламається
    при різкій зміні освітлення/великому зсуві між кадрами.
  - KeypointOdometryEstimator (ORB+ratio-test): повільніший, але
    дескриптори стійкіші до освітлення й дозволяють більший зсув між
    кадрами (не потребує "сусідніх" кадрів, як LK).

Коли обидва згодні — висока довіра (менший std для EKF). Коли
розходяться чи один "загубився" — покладаємось на інший або знижуємо
довіру, а не сліпо віримо єдиному джерелу.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from flow_config import FlowConfig, CFG
from flow_estimator import OpticalFlowEstimator, FlowResult
from keypoint_odometry import KeypointOdometryEstimator, KeypointOdometryResult


@dataclass
class CombinedResult:
    velocity_body: np.ndarray
    mode: str            # "flow+keypoint" | "flow" | "keypoint" | "none"
    quality: float
    std: float
    flow_result: FlowResult
    keypoint_result: KeypointOdometryResult
    agreement_m_s: float | None  # |v_flow - v_keypoint|, None якщо лише одне джерело


class CombinedMotionEstimator:
    def __init__(self, config: FlowConfig | None = None,
                 disagreement_thresh_m_s: float = 1.5):
        self.cfg = config or CFG
        self.flow_est = OpticalFlowEstimator(self.cfg)
        self.kp_est = KeypointOdometryEstimator(self.cfg)
        self.disagreement_thresh = disagreement_thresh_m_s

    def estimate(self, prev_gray: np.ndarray, curr_gray: np.ndarray,
                 altitude_m: float, gyro_body_rads: np.ndarray, dt: float) -> CombinedResult:
        flow_res = self.flow_est.estimate(prev_gray, curr_gray, altitude_m, gyro_body_rads, dt)
        kp_res = self.kp_est.estimate(prev_gray, curr_gray, altitude_m, gyro_body_rads, dt)

        flow_ok = flow_res.mode != "none"
        kp_ok = kp_res.mode != "none"

        if flow_ok and kp_ok:
            diff = float(np.linalg.norm(flow_res.velocity_body - kp_res.velocity_body))
            # Зважене середнє за оберненою дисперсією (менший std -> більша вага).
            w_flow = 1.0 / (flow_res.std ** 2)
            w_kp = 1.0 / (kp_res.std ** 2)
            velocity = (flow_res.velocity_body * w_flow + kp_res.velocity_body * w_kp) / (w_flow + w_kp)
            combined_std = 1.0 / np.sqrt(w_flow + w_kp)
            if diff > self.disagreement_thresh:
                # Джерела не згодні — це сигнал "не довіряй сліпо", а не
                # просто взяти середнє: роздуваємо std пропорційно розбіжності.
                combined_std = max(combined_std, diff / 2.0)
            quality = min(flow_res.quality, kp_res.quality)
            return CombinedResult(velocity, "flow+keypoint", quality, combined_std,
                                   flow_res, kp_res, diff)

        if flow_ok:
            return CombinedResult(flow_res.velocity_body, "flow", flow_res.quality,
                                   flow_res.std, flow_res, kp_res, None)
        if kp_ok:
            return CombinedResult(kp_res.velocity_body, "keypoint", kp_res.quality,
                                   kp_res.std, flow_res, kp_res, None)

        return CombinedResult(np.zeros(2), "none", 0.0, self.cfg.velocity_std_high_alt,
                               flow_res, kp_res, None)
