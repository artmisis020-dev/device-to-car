#!/usr/bin/env python3
"""
Sirena SRT Relay Capture — один нативний GStreamer-пайплайн V4L2 → H264 → SRT.

Без локального переглядача, без окремих підпроцесів: v4l2src йде прямо в
енкодер і srtsink в одному пайплайні (PyGObject, GLib.MainLoop) — на відміну
від попередньої версії, де capture і encode/send були двома gst-launch-1.0
підпроцесами, склеєними побайтовим читанням/записом через Python.
"""
import sys
import signal
import threading
import time
import logging
import json
import urllib.request
from pathlib import Path

import gi
from cameras_services import resolve_video_device, video_nodes, list_formats, list_cameras, supports_mode

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst

Gst.init(None)

current_dir = Path(__file__).resolve().parent
if str(current_dir) not in sys.path:
    sys.path.insert(0, str(current_dir))

import capture_relay.config as config
import capture_relay.registry as registry
import capture_relay.timestamp_overlay as timestamp_overlay
import capture_relay.ts_packer as ts_packer
import capture_relay.adaptive_bitrate as adaptive_bitrate

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [srt-relay-capture]: %(message)s")
log = logging.getLogger(__name__)

_CONFIGURED_DEVICE = config.DEVICE
config.DEVICE = resolve_video_device(_CONFIGURED_DEVICE)
if config.DEVICE != _CONFIGURED_DEVICE:
    log.warning(f"VIDEO_DEVICE={_CONFIGURED_DEVICE} не знайдено, використовую {config.DEVICE}")

if not config.SIRENA_RELAY_TARGET:
    # Без цілі — стрімити нікуди. Чистий вихід (exit 0), щоб Restart=on-failure
    # не крутив crash-loop; video_relay.py сам перезапустить юніт, коли
    # запише SIRENA_RELAY_TARGET у /etc/default/sirena-relay.
    log.info("SIRENA_RELAY_TARGET порожній — стрім не потрібен, чистий вихід.")
    sys.exit(0)

if not registry.run_video_handshake():
    log.error("Video service not authorized — exiting")
    sys.exit(1)

if not config.check_device_exists():
    available = ", ".join(video_nodes()) or "немає"
    log.error(f"Відеокамеру не знайдено: VIDEO_DEVICE={config.DEVICE}. Доступні /dev/video*: {available}")
    sys.exit(1)

if not supports_mode(config.DEVICE, config.INPUT_FORMAT, config.WIDTH, config.HEIGHT, config.FPS):
    # Налаштований режим (з панелі/env) орієнтувався на іншу камеру — різні
    # пристрої мають різні моделі й кількість камер, тож НЕ падаємо в
    # crash-loop, а самі підбираємо найкращий режим, який ця конкретна
    # камера реально вміє (той самий принцип, що вже є в
    # sirena_manager.SirenaSupervisor.set_camera() для ручного перемикання).
    _camera = next((c for c in list_cameras(verbose=False) if c.path == config.DEVICE), None)
    _modes = _camera.modes if _camera else []
    if not _modes:
        log.error(
            f"Камера {config.DEVICE} не підтримує {config.INPUT_FORMAT} "
            f"{config.WIDTH}x{config.HEIGHT}@{config.FPS}fps і не має жодного "
            f"сумісного режиму для цього пайплайна. Доступні режими:\n{list_formats(config.DEVICE)}"
        )
        sys.exit(1)
    _fourcc, _w, _h, _fps = _modes[0]  # найбільша площа, тоді fps — modes вже відсортовані
    _new_format = {"YUYV": "YUY2", "MJPG": "MJPG"}.get(_fourcc, _fourcc)
    log.warning(
        f"Камера {config.DEVICE} не підтримує налаштований режим "
        f"{config.INPUT_FORMAT} {config.WIDTH}x{config.HEIGHT}@{config.FPS}fps — "
        f"автоматично перемикаюсь на {_new_format} {_w}x{_h}@{_fps}fps "
        "(найкращий доступний режим цієї камери)."
    )
    config.INPUT_FORMAT, config.WIDTH, config.HEIGHT, config.FPS = _new_format, _w, _h, _fps
    config.resync_keyint(_fps)
    config.persist_auto_mode(_new_format, _w, _h, _fps)


def get_encoder_chain(bitrate_kbps: int) -> "tuple[str, bool]":
    # v4l2h264enc реєструється лише коли в системі реально є V4L2 M2M
    # енкодер (RPi4 має, RPi5 — ні), тож find() тут — перевірка заліза.
    use_hw = config.VIDEO_ENCODER in ("auto", "v4l2") and Gst.ElementFactory.find("v4l2h264enc") is not None
    if config.VIDEO_ENCODER == "v4l2" and not use_hw:
        log.warning("VIDEO_ENCODER=v4l2, але v4l2h264enc недоступний — fallback на x264enc")
    profile_id, caps_profile = config.H264_PROFILE_INFO

    if use_hw:
        log.info(f"Using hardware encoder: v4l2h264enc (profile={config.H264_PROFILE})")
        chain = (
            "v4l2h264enc name=video_encoder "
            f"extra-controls=\"controls,repeat_sequence_header=1,h264_profile={profile_id},"
            f"video_bitrate={bitrate_kbps * 1000},h264_i_frame_period={config.KEYINT}\" ! "
            "capsfilter caps=\"video/x-h264,level=(string)4,stream-format=byte-stream,alignment=au\""
        )
        return chain, False

    if Gst.ElementFactory.find("x264enc") is None:
        raise RuntimeError("No H264 encoder found: x264enc")

    log.info(f"Using software encoder: x264enc (preset={config.X264_SPEED_PRESET}, profile={config.H264_PROFILE})")
    chain = (
        "x264enc name=video_encoder "
        "tune=zerolatency "
        f"speed-preset={config.X264_SPEED_PRESET} "
        f"bitrate={bitrate_kbps} "
        f"key-int-max={config.KEYINT} "
        "bframes=0 "
        "sliced-threads=true "
        "rc-lookahead=0 "
        f"vbv-buf-capacity={config.X264_VBV_BUF_MS} "
        "byte-stream=true "
        "option-string=repeat-headers=1 ! "
        f"capsfilter caps=\"video/x-h264,profile={caps_profile},stream-format=byte-stream,alignment=au\""
    )
    return chain, True


def create_pipeline_string() -> "tuple[str, bool]":
    encoder_chain, is_software_encoder = get_encoder_chain(config.bitrate_kbps())
    encode_and_send = (
        # Одна конвертація в I420 (з потоками) перед міткою часу; мітку
        # змішує overlaycomposition прямо в I420 — див. timestamp_overlay.py
        # (раніше cairooverlay вимагав BGRx і тягнув дві повні конвертації).
        f"videoconvert n-threads={config.VIDEOCONVERT_THREADS} ! "
        f"video/x-raw,format=I420,width={config.WIDTH},height={config.HEIGHT} ! "
        "overlaycomposition name=ts_overlay ! "
        f"{encoder_chain} ! "
        "h264parse config-interval=1 ! "
        # alignment=0 + ts_packer: весь кадр іде в мережу одразу, без
        # очікування наступного кадру (див. capture_relay/ts_packer.py).
        "mpegtsmux alignment=0 ! "
        f"{ts_packer.APPSINK} "
        f"{ts_packer.APPSRC} ! "
        f"srtsink name=srt_sink uri=\"{config.SIRENA_RELAY_TARGET}\" sync=false processing-deadline=0"
    )

    if config.INPUT_FORMAT in ("MJPG", "JPEG"):
        raw_chain = (
            f"v4l2src device={config.DEVICE} ! "
            f"image/jpeg,width={config.WIDTH},height={config.HEIGHT},framerate={config.FPS}/1 ! "
            # як і для YUY2: якщо кодер/мережа не встигають — відкидаємо старий
            # кадр тут, а не копичимо чергу (затримку) в драйвері камери
            "queue max-size-buffers=1 leaky=downstream ! "
            "jpegdec ! "
        )
    else:
        raw_chain = (
            f"v4l2src device={config.DEVICE} io-mode=mmap ! "
            f"video/x-raw,format=YUY2,width={config.WIDTH},height={config.HEIGHT},framerate={config.FPS}/1 ! "
            "queue max-size-buffers=1 leaky=downstream ! "
        )

    if not config.TRACK_TAP_ENABLED:
        # Дефолтний шлях — байт-в-байт той самий рядок, що й до появи
        # піксель-трекінгу: жодного tee, жодного track_sink, нуль ризику.
        pipeline_str = f"{raw_chain}{encode_and_send}"
        return pipeline_str, is_software_encoder

    # SIRENA_TRACK_TAP=1 (пише additional_modules/pixel_tracking/control.py
    # перед перезапуском цього сервісу) — той самий сирий кадр іде у ДВІ
    # гілки через tee: (1) кодування+SRT без змін, (2) BGR-appsink для
    # track_tap.py (лінивий імпорт cv2/трекера — див. той модуль). Один
    # v4l2src, один процес, без v4l2loopback — усуває race, знайдений живим
    # тестуванням у попередній (loopback) ітерації цієї фічі.
    # raw_chain для YUY2 вже закінчується власним queue (decoupling від
    # v4l2src) — другого перед tee не треба; для MJPG його не було й раніше.
    pipeline_str = (
        f"{raw_chain}"
        "tee name=track_tee "
        f"track_tee. ! queue max-size-buffers=1 leaky=downstream ! {encode_and_send} "
        "track_tee. ! queue max-size-buffers=1 leaky=downstream ! videoconvert ! "
        "video/x-raw,format=BGR ! "
        "appsink name=track_sink emit-signals=true max-buffers=1 drop=true sync=false"
    )
    return pipeline_str, is_software_encoder


# ТИМЧАСОВО (діагностика мережі, не постійна фіча): fire-and-forget звіт
# внутрішнього стану AdaptiveBitrateRunner на admin-сервер, щоб звести
# його в один лог разом зі стороною MediaMTX і клієнта. Видалити разом з
# відповідним ендпоінтом/сервісом на адмін-стороні, коли аналіз завершено.
_REPORT_FAIL_LOG_EVERY_S = 60.0
_last_report_fail_log = 0.0


def _report_bitrate_state(payload: dict) -> None:
    def _send():
        global _last_report_fail_log
        try:
            device_id = registry.get_hardware_id()
            data = json.dumps({**payload, "device_id": device_id}).encode()
            req = urllib.request.Request(
                f"{config.REGISTRY_URL}/api/video/report-bitrate/{device_id}",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5):
                pass
        except Exception as exc:
            # best-effort — не має права зачепити живий пайплайн, але й не
            # мовчки: раніше будь-який збій тут був невидимий (rpi_* колонки
            # діагностичного CSV порожні без жодного сліду в журналі).
            now = time.monotonic()
            if now - _last_report_fail_log >= _REPORT_FAIL_LOG_EVERY_S:
                _last_report_fail_log = now
                log.warning(f"[AdaptiveBitrate] звіт на адмін-сервер не вдався: {exc}")

    threading.Thread(target=_send, daemon=True).start()


class AdaptiveBitrateRunner:
    """Під'єднує capture_relay.adaptive_bitrate.AdaptiveBitrate до живого
    пайплайна: раз на інтервал бере статистику srtsink, виставляє bitrate
    x264enc і (не частіше ніж раз на секунду) звітує стан на адмін-сервер."""

    REPORT_EVERY_S = 1.0
    STATUS_LOG_EVERY_S = 10.0

    def __init__(self, encoder: Gst.Element, sink: Gst.Element, target_kbps: int):
        self.encoder = encoder
        self.sink = sink
        self.abr = adaptive_bitrate.AdaptiveBitrate(target_kbps, adaptive_bitrate.AbrParams(
            interval_s=config.ADAPTIVE_BITRATE_INTERVAL_MS / 1000.0,
            min_kbps=config.ADAPTIVE_BITRATE_MIN_KBPS,
            starlink_guard=config.ADAPTIVE_BITRATE_STARLINK_GUARD,
        ))
        self.applied_kbps = None
        self.last_state = None
        self.last_report = 0.0
        self.last_status_log = 0.0
        self.prev = None
        self._apply(self.abr.current_kbps)

    def _apply(self, kbps: int):
        if kbps == self.applied_kbps:
            return
        self.encoder.set_property("bitrate", kbps)
        self.applied_kbps = kbps

    def tick(self) -> bool:
        stats = self.sink.get_property("stats")
        if stats is None:
            return True  # ще не з'єднано
        fields = {k: stats.get_value(k) for k in (
            "packets-sent", "packets-sent-lost", "packets-retransmitted",
            "packets-sent-dropped", "rtt-ms", "bandwidth-mbps")}
        now = time.time()
        kbps = self.abr.tick(fields, now=now)
        self._apply(kbps)
        if self.abr.cut or (self.abr.state == "start" and self.last_state is None):
            s = self.abr.last_signals
            log.info(f"[AdaptiveBitrate] {self.abr.state}: -> {kbps} kbps "
                     f"(rtt {s['rtt_ms']:.0f}ms, черга {s['queue_ms']:.0f}ms, втрати {s['loss'] * 100:.1f}%, "
                     f"оцінка ємності {s['capacity_kbps']:.0f} kbps)")
        self.last_state = self.abr.state
        if now - self.last_status_log >= self.STATUS_LOG_EVERY_S:
            self.last_status_log = now
            s = self.abr.last_signals
            log.info(f"[AdaptiveBitrate] {kbps}/{self.abr.target_kbps} kbps, стан {self.abr.state}, "
                     f"rtt {s['rtt_ms']:.0f}ms (база {s['base_rtt_ms']:.0f}), втрати {s['loss'] * 100:.1f}%")
        if now - self.last_report >= self.REPORT_EVERY_S:
            self._report(fields, now)
        return True

    def _report(self, fields: dict, now: float) -> None:
        cur = (fields.get("packets-sent-dropped") or 0, fields.get("packets-retransmitted") or 0)
        prev = self.prev or cur
        self.prev = cur
        self.last_report = now
        s = self.abr.last_signals
        _report_bitrate_state({
            "bandwidth_mbps": fields.get("bandwidth-mbps") or 0.0,
            "dropped_delta": cur[0] - prev[0],
            "retransmitted_delta": cur[1] - prev[1],
            "current_kbps": self.abr.current_kbps,
            "target_kbps": self.abr.target_kbps,
            "min_kbps": self.abr.min_kbps,
            "rtt_ms": s["rtt_ms"],
            "queue_ms": s["queue_ms"],
            "loss_pct": s["loss"] * 100,
            "state": self.abr.state,
        })


class BusMessageHandler:
    def __init__(self, loop: GLib.MainLoop, pipeline: Gst.Element, runtime_state):
        self.loop = loop
        self.pipeline = pipeline
        self.runtime_state = runtime_state
        self.shutting_down = False

    def __call__(self, bus: Gst.Bus, message: Gst.Message):
        del bus
        if message.type == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            log.error(err.message)
            if debug:
                log.debug(debug)
            self.runtime_state["restart_requested"] = True
            self.loop.quit()
        elif message.type == Gst.MessageType.EOS:
            log.info("Got end-of-stream")
            if not self.runtime_state["stop_requested"]:
                self.runtime_state["restart_requested"] = True
            self.loop.quit()
        elif message.type == Gst.MessageType.APPLICATION:
            struct = message.get_structure()
            if not (struct and struct.has_name("application/srt-relay-interrupt")):
                return True
            if self.shutting_down:
                self.loop.quit()
                return True
            log.info("Interrupt received, sending EOS")
            self.pipeline.send_event(Gst.Event.new_eos())
            self.shutting_down = True
        return True


def _post_interrupt():
    if pipeline is None:
        loop.quit()
        return
    bus = pipeline.get_bus()
    if bus is None:
        loop.quit()
        return
    structure = Gst.Structure.new_empty("application/srt-relay-interrupt")
    bus.post(Gst.Message.new_application(pipeline, structure))


def request_shutdown(signum, frame):
    del signum, frame
    log.info("Shutdown requested")
    runtime_state["stop_requested"] = True
    _post_interrupt()


def on_registry_revoke():
    log.warning("Access revoked — stopping stream")
    runtime_state["revoked"] = True
    _post_interrupt()


pipeline = None
loop = GLib.MainLoop()
runtime_state = {"stop_requested": False, "restart_requested": False, "revoked": False}

log.info(
    f"VIDEO_DEVICE={config.DEVICE} {config.WIDTH}x{config.HEIGHT}@{config.FPS} "
    f"VIDEO_ENCODER={config.VIDEO_ENCODER} target={config.SIRENA_RELAY_TARGET}"
)

if config.REGISTRY_ENABLED:
    heartbeat_thread = threading.Thread(target=registry.heartbeat_loop, args=(on_registry_revoke,), daemon=True)
    heartbeat_thread.start()

try:
    pipeline_str, is_software_encoder = create_pipeline_string()
    log.info(f"[PIPE] {pipeline_str}")
    pipeline = Gst.parse_launch(pipeline_str)
    if pipeline is None:
        raise RuntimeError("Не вдалося створити GStreamer пайплайн")

    if config.TRACK_TAP_ENABLED:
        import capture_relay.track_tap as track_tap
        track_tap.start(pipeline, config)

    if not ts_packer.attach(pipeline):
        raise RuntimeError("ts_sink/ts_src не знайдено в пайплайні")

    if timestamp_overlay.attach(pipeline):
        log.info("[TimestampOverlay] мітка часу увімкнена — доступна наскрізна затримка в плеєрі")
    else:
        log.warning("[TimestampOverlay] елемент ts_overlay не знайдено в пайплайні — наскрізна затримка недоступна")

    # Примусово нульова базова затримка пайплайна — інакше GStreamer
    # автоузгоджує latency з розрахунку на мінімальну затримку живих джерел
    # (v4l2src звітує ~1 кадр, 33мс на 30fps — фізично невід'ємна частина
    # захоплення кадру) і додає ще певний запас поверх цього для
    # планування; GstAggregator (mpegtsmux) притримує вихід відповідно до
    # цього розрахунку, незалежно від sync=false на самому srtsink (той
    # стосується лише сінка). Виміряно емпірично (браузер, наскрізна
    # затримка): дає ~10-15мс покращення без побічних ефектів.
    pipeline.set_latency(0)

    bus = pipeline.get_bus()
    if bus is None:
        raise RuntimeError("Не вдається отримати шину повідомлень GStreamer")
    bus.add_signal_watch()
    bus.connect("message", BusMessageHandler(loop, pipeline, runtime_state))

    signal.signal(signal.SIGINT, request_shutdown)
    signal.signal(signal.SIGTERM, request_shutdown)

    ret = pipeline.set_state(Gst.State.PLAYING)
    if ret == Gst.StateChangeReturn.FAILURE:
        raise RuntimeError("Failed to start GStreamer pipeline")

    log.info("Pipeline PLAYING")

    if config.ADAPTIVE_BITRATE_ENABLED and is_software_encoder:
        target_kbps = config.bitrate_kbps()
        runner = AdaptiveBitrateRunner(
            pipeline.get_by_name("video_encoder"),
            pipeline.get_by_name("srt_sink"),
            target_kbps,
        )
        GLib.timeout_add(config.ADAPTIVE_BITRATE_INTERVAL_MS, runner.tick)
        log.info(
            f"[AdaptiveBitrate] enabled: target={target_kbps}kbps min={runner.abr.min_kbps}kbps "
            f"start={runner.abr.current_kbps}kbps interval={config.ADAPTIVE_BITRATE_INTERVAL_MS}ms "
            f"starlink_guard={config.ADAPTIVE_BITRATE_STARLINK_GUARD}"
        )
    elif config.ADAPTIVE_BITRATE_ENABLED:
        log.info("[AdaptiveBitrate] увімкнено в конфізі, але апаратний енкодер не підтримує live bitrate — пропускаю")

    loop.run()

    if runtime_state["revoked"]:
        raise RuntimeError("Access revoked — потребує повторного handshake при рестарті")
    if runtime_state["restart_requested"] and not runtime_state["stop_requested"]:
        raise RuntimeError("Пайп зупинено аварійно, потребує systemd restart")
except Exception as exc:
    log.error(f"[FATAL] {exc}")
    sys.exit(1)
finally:
    if pipeline is not None:
        bus = pipeline.get_bus()
        pipeline.set_state(Gst.State.NULL)
        if bus is not None:
            bus.remove_signal_watch()
    log.info("Стрімер зупинено.")
