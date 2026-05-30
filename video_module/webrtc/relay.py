import asyncio
import logging
from pathlib import Path
import webrtc.config as config

_relay_recorder = None


class RelayRecorder:
    def __init__(self, width, height, fps, srt_target):
        self._w = width
        self._h = height
        self._fps = fps
        self._target = srt_target
        self._proc = None
        self.active = False

    async def start(self):
        cmd = [
            "gst-launch-1.0", "-q", "fdsrc", "fd=0",
            "!", "rawvideoparse", "use-sink-caps=false",
            f"width={self._w}", f"height={self._h}", "format=i420", f"framerate={self._fps}/1",
            "!", "x264enc", "tune=zerolatency", "bitrate=500", "speed-preset=ultrafast", f"key-int-max={self._fps}",
            "!", "h264parse", "config-interval=1",
            "!", "mpegtsmux", "alignment=7",
            "!", "srtsink", f"uri={self._target}", "sync=false"
        ]
        self._proc = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        self.active = True
        logging.info(f"[Relay] Started PID={self._proc.pid} -> {self._target}")

    def push_frame(self, data):
        if not self.active or not self._proc or self._proc.returncode is not None:
            return
        try:
            self._proc.stdin.write(data)
        except Exception:
            self.active = False

    async def stop(self):
        self.active = False
        if self._proc:
            try:
                self._proc.stdin.close()
                await asyncio.wait_for(self._proc.wait(), timeout=3.0)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None
        logging.info("[Relay] Stopped")


def get_current_relay():
    global _relay_recorder
    return _relay_recorder


def push_to_relay(data):
    global _relay_recorder
    if _relay_recorder and _relay_recorder.active:
        _relay_recorder.push_frame(data)


async def relay_poll_task(camera_available_check_func):
    global _relay_recorder
    flag = Path(config.RELAY_FLAG_FILE)

    while True:
        await asyncio.sleep(5)
        if not camera_available_check_func():
            continue
        try:
            if flag.exists() and _relay_recorder is None:
                target = flag.read_text().strip()
                if target:
                    rec = RelayRecorder(config.WIDTH, config.HEIGHT, config.FPS, target)
                    await rec.start()
                    _relay_recorder = rec
            elif not flag.exists() and _relay_recorder is not None:
                await _relay_recorder.stop()
                _relay_recorder = None
        except Exception as e:
            logging.warning(f"[Relay] poll error: {e}")