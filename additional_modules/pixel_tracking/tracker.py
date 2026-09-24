"""
Standalone pixel (template matching) tracker.

Independent of camera, MAVLink and display: feed it BGR frames, get back
the target position. Based on util/cameraPixelTracking.py with these changes:

- fixed-size base template blended over time (EMA) instead of replacing it
  every frame, so the ROI drifts much less;
- template is updated only on confident matches;
- scale is searched around the current scale (3 candidates per frame)
  instead of 14 absolute scales, so it is faster and scale does not compound;
- a constant-velocity Kalman filter predicts where to center the search
  window each frame (same cost as the naive velocity-EMA it replaces, but
  properly damps jitter/acceleration instead of a fixed smoothing factor;
  the reported position is still the raw match, the filter only drives
  where to look next);
- template updates require several consecutive confident matches
  (update_confirm_frames) before blending in, so one spurious high-score
  match to background clutter can't poison the template;
- coarse-to-fine correlation (downscaled pass to find an approximate peak,
  then a full-resolution pass only in a small window around it) kicks in
  once the search area is much bigger than the template — mainly the
  whole-frame LOST search, not the small per-frame margin search;
- when the match is lost the tracker keeps searching with a wider window and
  then over the whole frame, instead of jumping back to the frame center.
"""
from dataclasses import dataclass
from typing import Optional, Tuple

import cv2
import numpy as np

IDLE = "idle"            # not initialized
TRACKING = "tracking"    # target found on this frame
SEARCHING = "searching"  # target missed for a few frames, searching nearby
LOST = "lost"            # target missed for too long, searching whole frame


@dataclass
class TrackerConfig:
    match_threshold: float = 0.7    # min TM_CCOEFF_NORMED score to accept a match
    update_threshold: float = 0.85  # adapt template only when the match is this confident
    template_alpha: float = 0.15    # weight of the new appearance when adapting template
    search_margin: int = 30         # px around predicted ROI while tracking
    lost_search_margin: int = 80    # px around last ROI while searching
    max_searching_frames: int = 10  # after this many misses status becomes LOST
    global_search_on_lost: bool = True
    scale_step: float = 0.08        # relative scale change tested per frame
    min_scale: float = 0.5
    max_scale: float = 3.0
    min_roi_size: int = 16
    blur_ksize: int = 3
    # Kalman-фільтр (constant-velocity), що прогнозує центр вікна пошуку.
    kf_process_noise: float = 1e-2      # довіра до моделі сталої швидкості (менше -> плавніший прогноз)
    kf_measurement_noise: float = 1e-1  # довіра до виміряної позиції (менше -> прогноз тісніше йде за свіжим матчем)
    # Захист від "отруєння" темплейта: скільки підряд впевнених кадрів
    # (score >= update_threshold) потрібно, перш ніж дозволити оновлення —
    # один випадковий збіг з фоном більше не псує темплейт назавжди.
    update_confirm_frames: int = 3
    # Coarse-to-fine: грубий прохід на зменшеній у coarse_downscale разів
    # копії (регіон+темплейт), потім точний matchTemplate лише у вузькому
    # вікні навколо грубого піку. Вмикається лише коли область пошуку у
    # coarse_min_ratio разів більша за темплейт — типово тільки LOST-пошук
    # по всьому кадру, не звичайний margin-пошук при стабільному трекінгу.
    coarse_downscale: int = 4
    coarse_min_ratio: float = 3.0


@dataclass
class TrackResult:
    status: str
    bbox: Optional[Tuple[int, int, int, int]] = None  # x, y, w, h
    center: Optional[Tuple[int, int]] = None
    score: float = 0.0
    scale: float = 1.0

    @property
    def found(self):
        return self.status == TRACKING


class PixelTracker:
    def __init__(self, config: Optional[TrackerConfig] = None):
        self.config = config or TrackerConfig()
        self.reset()

    def reset(self):
        self.template = None       # float32 gray, fixed base size
        self.base_size = None      # (w, h) of the template at scale 1.0
        self.center = None         # float (cx, cy)
        self.scale = 1.0
        self.missed_frames = 0
        self.last_result = TrackResult(IDLE)
        self._confident_streak = 0
        self._kf = self._build_kalman_filter()

    def _build_kalman_filter(self):
        cfg = self.config
        kf = cv2.KalmanFilter(4, 2)  # state: x, y, vx, vy; measurement: x, y
        kf.transitionMatrix = np.array([
            [1, 0, 1, 0],
            [0, 1, 0, 1],
            [0, 0, 1, 0],
            [0, 0, 0, 1],
        ], dtype=np.float32)
        kf.measurementMatrix = np.array([
            [1, 0, 0, 0],
            [0, 1, 0, 0],
        ], dtype=np.float32)
        kf.processNoiseCov = np.eye(4, dtype=np.float32) * cfg.kf_process_noise
        kf.measurementNoiseCov = np.eye(2, dtype=np.float32) * cfg.kf_measurement_noise
        kf.errorCovPost = np.eye(4, dtype=np.float32)
        return kf

    @property
    def initialized(self):
        return self.template is not None

    def init(self, frame, bbox):
        """Start tracking the region bbox=(x, y, w, h) of frame."""
        gray = self._preprocess(frame)
        x, y, w, h = self._clip_bbox(bbox, gray.shape)
        self.reset()
        self.template = gray[y:y + h, x:x + w].copy()
        self.base_size = (w, h)
        self.center = np.array([x + w / 2.0, y + h / 2.0], dtype=np.float32)
        self._kf.statePost = np.array([[self.center[0]], [self.center[1]], [0], [0]], dtype=np.float32)
        self.last_result = self._result(TRACKING, 1.0)
        return self.last_result

    def nudge(self, frame, dx, dy):
        """Move the ROI by (dx, dy) px and re-take the template there (pilot correction)."""
        if not self.initialized:
            return self.last_result
        w, h = self._scaled_size(self.scale)
        cx, cy = self.center + (dx, dy)
        return self.init(frame, (int(round(cx - w / 2)), int(round(cy - h / 2)), w, h))

    def update(self, frame):
        if not self.initialized:
            return self.last_result

        gray = self._preprocess(frame)
        cfg = self.config

        predicted_state = self._kf.predict()
        if self.missed_frames == 0:
            predicted = np.array([predicted_state[0, 0], predicted_state[1, 0]], dtype=np.float32)
            margin = cfg.search_margin
        else:
            predicted = self.center
            margin = cfg.lost_search_margin

        whole_frame = cfg.global_search_on_lost and self.missed_frames > cfg.max_searching_frames
        if whole_frame:
            scales = [self.scale]
        else:
            step = 1.0 + cfg.scale_step
            scales = [self.scale / step, self.scale, self.scale * step]

        best = None  # (score, center, scale)
        for scale in scales:
            if not cfg.min_scale <= scale <= cfg.max_scale:
                continue
            match = self._match(gray, predicted, margin, scale, whole_frame)
            if match is not None and (best is None or match[0] > best[0]):
                best = match

        if best is not None and best[0] >= cfg.match_threshold:
            score, center, scale = best
            if whole_frame:
                # Реаквізиція після LOST — розрив траєкторії, а не її
                # продовження: прогноз від старої (хибної) позиції тут
                # нерелевантний, тож скидаємо фільтр на нову точку замість
                # того, щоб "коригувати" ним застарілий стан.
                self._kf.statePost = np.array([[center[0]], [center[1]], [0], [0]], dtype=np.float32)
                self._kf.errorCovPost = np.eye(4, dtype=np.float32)
            else:
                self._kf.correct(np.array([[center[0]], [center[1]]], dtype=np.float32))
            self.center = center
            self.scale = scale
            self.missed_frames = 0
            self._confident_streak = self._confident_streak + 1 if score >= cfg.update_threshold else 0
            if self._confident_streak >= cfg.update_confirm_frames:
                self._adapt_template(gray)
            self.last_result = self._result(TRACKING, score)
        else:
            self.missed_frames += 1
            self._confident_streak = 0
            status = SEARCHING if self.missed_frames <= cfg.max_searching_frames else LOST
            self.last_result = self._result(status, best[0] if best else 0.0)

        return self.last_result

    def _match(self, gray, predicted, margin, scale, whole_frame):
        tw, th = self._scaled_size(scale)
        frame_h, frame_w = gray.shape
        if tw < self.config.min_roi_size or th < self.config.min_roi_size:
            return None
        if tw > frame_w or th > frame_h:
            return None

        if whole_frame:
            x0, y0, x1, y1 = 0, 0, frame_w, frame_h
        else:
            cx, cy = predicted
            x0 = max(int(cx - tw / 2 - margin), 0)
            y0 = max(int(cy - th / 2 - margin), 0)
            x1 = min(int(cx + tw / 2 + margin), frame_w)
            y1 = min(int(cy + th / 2 + margin), frame_h)
        search = gray[y0:y1, x0:x1]
        if search.shape[0] < th or search.shape[1] < tw:
            return None

        template = self.template
        if (tw, th) != self.base_size:
            template = cv2.resize(template, (tw, th), interpolation=cv2.INTER_LINEAR)

        found = self._correlate(search, template, tw, th)
        if found is None:
            return None
        score, (loc_x, loc_y) = found
        center = np.array([x0 + loc_x + tw / 2.0, y0 + loc_y + th / 2.0], dtype=np.float32)
        return score, center, scale

    def _correlate(self, search, template, tw, th):
        """NCC search на region/template. Коли область пошуку значно більша
        за темплейт (типово — LOST-пошук по всьому кадру, іноді SEARCHING з
        lost_search_margin) — спершу грубий прохід на зменшеній копії
        (дешево, бо площа кореляції падає квадратично від масштабу), а
        точний matchTemplate у повній роздільності робимо лише у вузькому
        вікні навколо знайденого приблизного піку, а не по всій області.
        Для звичайного (малого) вікна при стабільному трекінгу поріг
        coarse_min_ratio просто не спрацьовує — рахуємо, як і раніше."""
        cfg = self.config
        sh, sw = search.shape
        downscale = cfg.coarse_downscale
        if (downscale > 1 and sw >= tw * cfg.coarse_min_ratio and sh >= th * cfg.coarse_min_ratio):
            coarse_tw = max(1, tw // downscale)
            coarse_th = max(1, th // downscale)
            coarse_sw = max(1, sw // downscale)
            coarse_sh = max(1, sh // downscale)
            if coarse_tw >= 4 and coarse_th >= 4 and coarse_tw < coarse_sw and coarse_th < coarse_sh:
                coarse_search = cv2.resize(search, (coarse_sw, coarse_sh), interpolation=cv2.INTER_AREA)
                coarse_template = cv2.resize(template, (coarse_tw, coarse_th), interpolation=cv2.INTER_AREA)
                coarse_res = cv2.matchTemplate(coarse_search, coarse_template, cv2.TM_CCOEFF_NORMED)
                _, _, _, coarse_loc = cv2.minMaxLoc(coarse_res)

                # Приблизна позиція грубого піку в координатах search (повна
                # роздільність) + запас на похибку огрублення — щоб точний
                # прохід не промахнувся повз справжній пік через downscale.
                approx_x, approx_y = coarse_loc[0] * downscale, coarse_loc[1] * downscale
                pad = downscale + 4
                rx0, ry0 = max(0, approx_x - pad), max(0, approx_y - pad)
                rx1, ry1 = min(sw, approx_x + tw + pad), min(sh, approx_y + th + pad)
                refine = search[ry0:ry1, rx0:rx1]
                if refine.shape[0] >= th and refine.shape[1] >= tw:
                    res = cv2.matchTemplate(refine, template, cv2.TM_CCOEFF_NORMED)
                    _, score, _, loc = cv2.minMaxLoc(res)
                    if not np.isfinite(score):
                        return None
                    return score, (rx0 + loc[0], ry0 + loc[1])
                # Точне вікно вийшло замалим (крайовий випадок) — фолбек нижче.

        res = cv2.matchTemplate(search, template, cv2.TM_CCOEFF_NORMED)
        _, score, _, loc = cv2.minMaxLoc(res)
        if not np.isfinite(score):
            return None
        return score, loc

    def _adapt_template(self, gray):
        x, y, w, h = self._current_bbox(gray.shape)
        patch = gray[y:y + h, x:x + w]
        if patch.size == 0:
            return
        if (w, h) != self.base_size:
            patch = cv2.resize(patch, self.base_size, interpolation=cv2.INTER_AREA)
        a = self.config.template_alpha
        self.template = cv2.addWeighted(self.template, 1 - a, patch, a, 0)

    def _preprocess(self, frame):
        if frame.ndim == 3:
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        k = self.config.blur_ksize
        if k > 1:
            frame = cv2.GaussianBlur(frame, (k, k), 0)
        return frame.astype(np.float32)

    def _scaled_size(self, scale):
        w, h = self.base_size
        return max(int(round(w * scale)), 1), max(int(round(h * scale)), 1)

    def _current_bbox(self, shape):
        w, h = self._scaled_size(self.scale)
        cx, cy = self.center
        return self._clip_bbox((int(round(cx - w / 2)), int(round(cy - h / 2)), w, h), shape)

    def _clip_bbox(self, bbox, shape):
        frame_h, frame_w = shape[:2]
        x, y, w, h = (int(v) for v in bbox)
        w = max(1, min(w, frame_w))
        h = max(1, min(h, frame_h))
        x = min(max(x, 0), frame_w - w)
        y = min(max(y, 0), frame_h - h)
        return x, y, w, h

    def _result(self, status, score):
        w, h = self._scaled_size(self.scale)
        cx, cy = self.center
        bbox = (int(round(cx - w / 2)), int(round(cy - h / 2)), w, h)
        return TrackResult(status, bbox, (int(round(cx)), int(round(cy))), float(score), self.scale)
