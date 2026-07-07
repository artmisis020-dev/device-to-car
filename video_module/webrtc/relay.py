import asyncio
import json
import logging
from pathlib import Path
import webrtc.config as config

_relay_recorder = None


def _relay_bitrate_kbps() -> int:
    """Бітрейт з конфігу video-service-manager, фолбек — env/дефолт."""
    try:
        cfg = json.loads(Path(config.VIDEO_CONFIG_PATH).read_text())
        bitrate = int(cfg.get("bitrate") or 0)
        if bitrate > 0:
            return bitrate
    except Exception:
        pass
    return config.RELAY_BITRATE_KBPS


class RelayRecorder:
    def __init__(self, width, height, fps, srt_target, bitrate_kbps):
        self._w = width
        self._h = height
        self._fps = fps
        self._target = srt_target
        self._bitrate = bitrate_kbps
        self._proc = None
        self.active = False
        # Дозволяємо накопичити максимум ~4 сирі кадри у stdin-буфері: якщо
        # SRT-лінк не встигає, кадри дропаємо, інакше буфер росте необмежено
        # (затримка повзе вгору, далі OOM).
        self._max_buffered = width * height * 3 // 2 * 4
        self._dropped = 0

    async def start(self):
        cmd = [
            "gst-launch-1.0", "-q", "fdsrc", "fd=0",
            "!", "rawvideoparse", "use-sink-caps=false",
            f"width={self._w}", f"height={self._h}", "format=i420", f"framerate={self._fps}/1",
            "!", "x264enc", "tune=zerolatency", f"bitrate={self._bitrate}",
            "speed-preset=ultrafast", f"key-int-max={self._fps}",
            "!", "h264parse", "config-interval=1",
            "!", "mpegtsmux", "alignment=7",
            "!", "srtsink", f"uri={self._target}", "sync=false"
        ]
        self._proc = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        self.active = True
        logging.info(f"[Relay] Started PID={self._proc.pid} bitrate={self._bitrate}kbps -> {self._target}")

    def push_frame(self, data):
        if not self.active or not self._proc or self._proc.returncode is not None:
            return
        try:
            buffered = self._proc.stdin.transport.get_write_buffer_size()
            if buffered > self._max_buffered:
                self._dropped += 1
                if self._dropped == 1 or self._dropped % 300 == 0:
                    logging.warning(
                        f"[Relay] SRT-лінк не встигає — дропнуто {self._dropped} кадрів "
                        f"(у буфері {buffered} байт)")
                return
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
                    rec = RelayRecorder(config.WIDTH, config.HEIGHT, config.FPS,
                                        target, _relay_bitrate_kbps())
                    await rec.start()
                    _relay_recorder = rec
            elif not flag.exists() and _relay_recorder is not None:
                await _relay_recorder.stop()
                _relay_recorder = None
        except Exception as e:
            logging.warning(f"[Relay] poll error: {e}")