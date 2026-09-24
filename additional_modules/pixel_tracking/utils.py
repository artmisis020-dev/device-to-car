import cv2

from .tracker import TRACKING, SEARCHING

STATUS_COLORS = {TRACKING: (0, 255, 0), SEARCHING: (0, 255, 255)}


def center_roi(frame_shape, size=40, offset=(0, 20)):
    """ROI of given size at the frame center (+offset), same default as the drone code."""
    h, w = frame_shape[:2]
    return (w // 2 - size // 2 + offset[0], h // 2 - size // 2 + offset[1], size, size)


def rotate_frame(frame, roll_deg):
    """Level the horizon by rotating the frame against the drone roll (degrees)."""
    h, w = frame.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2, h / 2), -roll_deg, 1.0)
    return cv2.warpAffine(frame, m, (w, h))


def stick_to_step(value, low=1400, high=1600, step=1):
    """RC channel PWM -> ROI shift in px: -step, 0 or +step (0 for missing channel)."""
    if value <= 0:
        return 0
    if value < low:
        return -step
    if value > high:
        return step
    return 0


def draw(frame, result):
    """Draw the tracking result on frame in place."""
    if result.bbox is None:
        return frame
    color = STATUS_COLORS.get(result.status, (0, 0, 255))
    x, y, w, h = result.bbox
    cv2.rectangle(frame, (x, y), (x + w, y + h), color, 2)
    cv2.circle(frame, result.center, 2, (0, 0, 255), cv2.FILLED)
    cv2.putText(frame, f"{result.status} {result.score:.2f} x{result.scale:.2f}", (10, 20),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)
    return frame
