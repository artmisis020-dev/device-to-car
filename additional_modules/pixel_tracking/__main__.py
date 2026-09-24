"""
Run the tracker on a camera or a video file without the drone.

    python -m pixel_tracking                      # camera 0
    python -m pixel_tracking --source video.mp4
    python -m pixel_tracking --source 0 --headless --lock-on-start

Keys: space - lock ROI at center, mouse click - lock ROI at cursor,
      w/a/s/d - nudge ROI by 1px, r - reset, q/esc - quit.
"""
import argparse
import time

import cv2

from . import PixelTracker, TrackerConfig, center_roi, draw


def parse_args():
    p = argparse.ArgumentParser(description="Standalone pixel tracker")
    p.add_argument("--source", default="0", help="camera index or video path")
    p.add_argument("--roi-size", type=int, default=40)
    p.add_argument("--offset-y", type=int, default=20, help="ROI offset from frame center")
    p.add_argument("--threshold", type=float, default=TrackerConfig.match_threshold)
    p.add_argument("--lock-on-start", action="store_true", help="lock center ROI on first frame")
    p.add_argument("--headless", action="store_true", help="no window, print results")
    p.add_argument("--output", help="save annotated video to this file")
    return p.parse_args()


def main():
    args = parse_args()
    source = int(args.source) if args.source.isdigit() else args.source
    cap = cv2.VideoCapture(source)
    if not cap.isOpened():
        raise SystemExit(f"Could not open source {args.source}")

    tracker = PixelTracker(TrackerConfig(match_threshold=args.threshold))
    writer = None
    click = []
    window = "pixel_tracking"
    if not args.headless:
        cv2.namedWindow(window)
        cv2.setMouseCallback(window, lambda e, x, y, *_: click.append((x, y))
                             if e == cv2.EVENT_LBUTTONDOWN else None)

    nudges = {ord("w"): (0, -1), ord("s"): (0, 1), ord("a"): (-1, 0), ord("d"): (1, 0)}
    fps, frames, t0 = 0.0, 0, time.time()

    while True:
        ok, frame = cap.read()
        if not ok:
            break

        if args.lock_on_start and not tracker.initialized:
            tracker.init(frame, center_roi(frame.shape, args.roi_size, (0, args.offset_y)))
        if click:
            x, y = click.pop()
            s = args.roi_size
            tracker.init(frame, (x - s // 2, y - s // 2, s, s))

        result = tracker.update(frame)

        frames += 1
        if time.time() - t0 >= 1.0:
            fps, frames, t0 = frames / (time.time() - t0), 0, time.time()

        if args.headless:
            print(f"{result.status:9s} center={result.center} score={result.score:.2f} "
                  f"scale={result.scale:.2f} fps={fps:.1f}")
        draw(frame, result)
        cv2.putText(frame, f"FPS {fps:.1f}", (10, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)

        if args.output:
            if writer is None:
                h, w = frame.shape[:2]
                writer = cv2.VideoWriter(args.output, cv2.VideoWriter_fourcc(*"MJPG"),
                                         cap.get(cv2.CAP_PROP_FPS) or 30, (w, h))
            writer.write(frame)

        if args.headless:
            continue
        if not tracker.initialized:
            x, y, w, h = center_roi(frame.shape, args.roi_size, (0, args.offset_y))
            cv2.rectangle(frame, (x, y), (x + w, y + h), (255, 255, 255), 1)
        cv2.imshow(window, frame)
        key = cv2.waitKey(1) & 0xFF
        if key in (ord("q"), 27):
            break
        if key == ord(" "):
            tracker.init(frame, center_roi(frame.shape, args.roi_size, (0, args.offset_y)))
        elif key == ord("r"):
            tracker.reset()
        elif key in nudges:
            tracker.nudge(frame, *nudges[key])

    cap.release()
    if writer is not None:
        writer.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
