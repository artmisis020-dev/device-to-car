import asyncio
import logging
from fractions import Fraction
import av
from aiortc import VideoStreamTrack
import webrtc.config as config
import webrtc.relay as relay


class ThermalTrack(VideoStreamTrack):
    """Single-reader design: _background_reader() is the ONLY coroutine reading from
    GStreamer stdout. recv() picks frames from a queue — no pipe-alignment races."""

    def __init__(self, device, width, height, fps):
        super().__init__()
        self.device = device
        self.width = width
        self.height = height
        self.fps = fps
        self.proc = None
        self.frame_size = width * height * 3 // 2
        self._n = 0
        self._pts = 0
        self._time_base = Fraction(1, self.fps)
        self._frame_q = asyncio.Queue(maxsize=3)
        self._reader_task = None
        self._lut = None                          # b"" = контраст не потрібен
        self._lut_refresh = max(1, fps // 2)      # перерахунок ~2 рази/с

    def _build_cmd(self):
        if config.INPUT_FORMAT in ("MJPG", "JPEG"):
            return [
                "gst-launch-1.0", "-q",
                "v4l2src", f"device={self.device}",
                "!", f"image/jpeg,width={self.width},height={self.height},framerate={self.fps}/1",
                "!", "jpegdec", "!", "videoconvert",
                "!", f"video/x-raw,format=I420,width={self.width},height={self.height}",
                "!", "fdsink", "fd=1", "sync=false",
            ]
        cmd = [
            "gst-launch-1.0", "-q",
            "v4l2src", f"device={self.device}", "io-mode=mmap",
            "!", f"video/x-raw,format=YUY2,width={self.width},height={self.height},framerate={self.fps}/1",
            "!", "queue", "max-size-buffers=2", "leaky=downstream",
            "!", "videoconvert",
            "!", f"video/x-raw,format=I420,width={self.width},height={self.height}",
            "!", "fdsink", "fd=1", "sync=false",
        ]
        logging.info(f"Launching GStreamer pipeline: {' '.join(cmd)}")
        return cmd

    async def _start(self):
        if self.proc is not None and self.proc.returncode is not None:
            logging.warning("GStreamer exited (rc=%d)", self.proc.returncode)
            self.proc = None

        if self.proc is None:
            cmd = self._build_cmd()
            self.proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            logging.info("GStreamer PID=%d", self.proc.pid)
            asyncio.create_task(self._read_stderr(self.proc.stderr))

        if self._reader_task is None or self._reader_task.done():
            self._reader_task = asyncio.ensure_future(self._background_reader())

    async def _read_stderr(self, stderr_stream):
        try:
            while True:
                line = await stderr_stream.readline()
                if not line:
                    break
                decoded_line = line.decode('utf-8', errors='ignore').strip()
                if decoded_line:
                    logging.error(f"[GStreamer Error] {decoded_line}")
        except Exception as e:
            logging.error(f"Error reading GStreamer stderr: {e}")

    async def _background_reader(self):
        logging.info("Background frame reader started")
        while True:
            if self.proc is None or self.proc.returncode is not None:
                self.proc = None
                await asyncio.sleep(0.5)
                try:
                    cmd = self._build_cmd()
                    self.proc = await asyncio.create_subprocess_exec(
                        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
                    logging.info("GStreamer restarted PID=%d", self.proc.pid)
                except Exception as e:
                    logging.error("GStreamer restart failed: %s", e)
                    await asyncio.sleep(1)
                continue

            data = b""
            while len(data) < self.frame_size:
                try:
                    chunk = await asyncio.wait_for(
                        self.proc.stdout.read(self.frame_size - len(data)), timeout=2.0)
                except asyncio.TimeoutError:
                    stderr_err = ""
                    if self.proc and self.proc.stderr:
                        try:
                            if hasattr(self.proc.stderr, 'read_nowait'):
                                raw_stderr = self.proc.stderr.read_nowait()
                            else:
                                raw_stderr = await asyncio.wait_for(self.proc.stderr.read(1024), timeout=0.1)
                            stderr_err = raw_stderr.decode('utf-8', errors='ignore').strip()
                        except Exception:
                            pass

                    logging.error(
                        f"GStreamer read timeout! Expected: {self.frame_size}, got: {len(data)}. Stderr: [{stderr_err}]"
                    )
                    try:
                        self.proc.terminate()
                        await self.proc.wait()
                    except Exception:
                        pass
                    self.proc = None
                    data = b""
                    break

                if not chunk:
                    stderr_err = ""
                    if self.proc and self.proc.stderr:
                        try:
                            raw_stderr = await asyncio.wait_for(self.proc.stderr.read(2048), timeout=0.2)
                            stderr_err = raw_stderr.decode('utf-8', errors='ignore').strip()
                        except Exception:
                            pass
                    logging.error(f"GStreamer EOF reached unexpectedly! Stderr: [{stderr_err}]")
                    try:
                        self.proc.terminate()
                        await self.proc.wait()
                    except Exception:
                        pass
                    self.proc = None
                    data = b""
                    break

                data += chunk

            if len(data) < self.frame_size:
                continue

            if self._frame_q.full():
                try:
                    self._frame_q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                self._frame_q.put_nowait(data)
            except asyncio.QueueFull:
                pass

            relay.push_to_relay(data)

    async def recv(self):
        if self._reader_task is None or self._reader_task.done():
            await self._start()

        try:
            data = await asyncio.wait_for(self._frame_q.get(), timeout=5.0)
        except asyncio.TimeoutError:
            raise RuntimeError("No frame received from GStreamer within 5s")

        self._n += 1
        y_size = self.width * self.height
        uv_size = y_size >> 2
        y_bytes = bytes(data[:y_size])

        # min/max по всьому Y-плейну на кожному кадрі дорогі для CPU RPi, а
        # теплова картина міняється повільно — перераховуємо LUT раз на
        # _lut_refresh кадрів, між ними користуємось кешованим.
        if self._lut is None or self._n % self._lut_refresh == 1:
            y_min = min(y_bytes)
            y_max = max(y_bytes)
            if y_max > y_min + 2:
                span = y_max - y_min
                self._lut = bytes(max(0, min(255, int((i - y_min) * 255 / span))) for i in range(256))
            else:
                self._lut = b""
            if self._n % 60 == 1:
                logging.info("Frame %d Y min=%d max=%d", self._n, y_min, y_max)

        if self._lut:
            y_bytes = y_bytes.translate(self._lut)

        frame = av.VideoFrame(self.width, self.height, "yuv420p")
        frame.planes[0].update(y_bytes)
        uv_neutral = bytes([128]) * uv_size
        frame.planes[1].update(uv_neutral)
        frame.planes[2].update(uv_neutral)
        frame.pts = self._pts
        frame.time_base = self._time_base
        self._pts += 1
        return frame

    def stop(self):
        super().stop()
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
        if self.proc:
            try:
                self.proc.terminate()
            except Exception:
                pass
            self.proc = None