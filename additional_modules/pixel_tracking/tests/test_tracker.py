import cv2
import numpy as np

from pixel_tracking import PixelTracker, TRACKING, SEARCHING, LOST

W, H = 640, 480
rng = np.random.default_rng(0)
BACKGROUND = cv2.GaussianBlur(rng.integers(0, 255, (H, W, 3), dtype=np.uint8), (7, 7), 0)
TARGET = rng.integers(0, 255, (40, 40, 3), dtype=np.uint8)


def render(cx, cy, size=40, visible=True):
    frame = BACKGROUND.copy()
    if visible:
        patch = cv2.resize(TARGET, (size, size), interpolation=cv2.INTER_LINEAR)
        x, y = int(cx - size / 2), int(cy - size / 2)
        frame[y:y + size, x:x + size] = patch
    return frame


def start(cx=200, cy=200):
    tracker = PixelTracker()
    tracker.init(render(cx, cy), (cx - 20, cy - 20, 40, 40))
    return tracker


def test_follows_moving_target():
    tracker = start()
    for i in range(1, 100):
        cx, cy = 200 + 3 * i, 200 + int(40 * np.sin(i / 10))
        result = tracker.update(render(cx, cy))
        assert result.status == TRACKING
        assert abs(result.center[0] - cx) <= 2 and abs(result.center[1] - cy) <= 2


def test_follows_scale_change():
    tracker = start(320, 240)
    for size in range(40, 81, 2):
        result = tracker.update(render(320, 240, size))
        assert result.status == TRACKING
    assert abs(result.bbox[2] - 80) <= 8


def test_reacquires_after_occlusion():
    tracker = start(200, 200)
    for _ in range(5):
        tracker.update(render(200, 200, visible=False))
    assert tracker.last_result.status == SEARCHING
    for _ in range(10):
        tracker.update(render(200, 200, visible=False))
    assert tracker.last_result.status == LOST
    # target reappears far away: found by the whole-frame search
    result = tracker.update(render(500, 380))
    assert result.status == TRACKING
    assert abs(result.center[0] - 500) <= 2 and abs(result.center[1] - 380) <= 2


def test_nudge_moves_roi():
    tracker = start(200, 200)
    result = tracker.nudge(render(200, 200), 5, -3)
    assert result.center == (205, 197)
