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
import logging
from pathlib import Path

import gi
from cameras_services import resolve_video_device, video_nodes, list_formats, supports_mode

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst

Gst.init(None)

current_dir = Path(__file__).resolve().parent
if str(current_dir) not in sys.path:
    sys.path.insert(0, str(current_dir))

import capture_relay.config as config
import capture_relay.registry as registry

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
    log.error(
        f"Камера {config.DEVICE} не підтримує {config.INPUT_FORMAT} "
        f"{config.WIDTH}x{config.HEIGHT}@{config.FPS}fps — v4l2src не зможе "
        f"домовитись про caps. Доступні режими:\n{list_formats(config.DEVICE)}"
    )
    sys.exit(1)


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
        "byte-stream=true "
        "option-string=repeat-headers=1 ! "
        f"capsfilter caps=\"video/x-h264,profile={caps_profile},stream-format=byte-stream,alignment=au\""
    )
    return chain, True


def create_pipeline_string() -> "tuple[str, bool]":
    if config.INPUT_FORMAT in ("MJPG", "JPEG"):
        input_chain = (
            f"v4l2src device={config.DEVICE} ! "
            f"image/jpeg,width={config.WIDTH},height={config.HEIGHT},framerate={config.FPS}/1 ! "
            "jpegdec ! videoconvert ! "
        )
    else:
        input_chain = (
            f"v4l2src device={config.DEVICE} io-mode=mmap ! "
            f"video/x-raw,format=YUY2,width={config.WIDTH},height={config.HEIGHT},framerate={config.FPS}/1 ! "
            "queue max-size-buffers=1 leaky=downstream ! "
            "videoconvert ! "
        )

    encoder_chain, is_software_encoder = get_encoder_chain(config.bitrate_kbps())

    pipeline_str = (
        f"{input_chain}"
        f"video/x-raw,format=I420,width={config.WIDTH},height={config.HEIGHT} ! "
        f"{encoder_chain} ! "
        "h264parse config-interval=1 ! "
        "mpegtsmux alignment=7 ! "
        f"srtsink name=srt_sink uri=\"{config.SIRENA_RELAY_TARGET}\" sync=false"
    )
    return pipeline_str, is_software_encoder


class AdaptiveBitrateController:
    """Тримає bitrate живого x264enc у межах, які реально проходять через
    SRT-лінк. Орієнтується на власну оцінку пропускної здатності SRT
    (bandwidth-mbps) і на факт реальних втрат (packets-sent-dropped) —
    SRT вже рахує це сам, не треба вигадувати власну евристику з нуля."""

    BANDWIDTH_HEADROOM = 0.7       # цільовий bitrate <= 70% оцінки SRT bandwidth
    DROP_CUT_FACTOR = 0.5          # реальна втрата даних -> різко вдвічі вниз
    RETRANSMIT_CUT_FACTOR = 0.85   # ретрансмісії без втрат -> обережний крок вниз
    RECOVERY_STEP_FACTOR = 1.1     # все ок -> плавний крок вгору (до target_kbps)

    def __init__(self, encoder: Gst.Element, sink: Gst.Element, target_kbps: int, min_kbps: int):
        self.encoder = encoder
        self.sink = sink
        self.target_kbps = target_kbps
        self.min_kbps = min_kbps
        self.current_kbps = target_kbps
        self._last_dropped = None
        self._last_retransmitted = None

    def _apply(self, new_kbps: float):
        new_kbps = int(max(self.min_kbps, min(self.target_kbps, new_kbps)))
        if new_kbps == self.current_kbps:
            return
        self.current_kbps = new_kbps
        self.encoder.set_property("bitrate", new_kbps)
        log.info(f"[AdaptiveBitrate] -> {new_kbps} kbps")

    def tick(self) -> bool:
        stats = self.sink.get_property("stats")
        if stats is None:
            return True  # ще не з'єднано, чекаємо наступного тіку

        bandwidth_mbps = stats.get_value("bandwidth-mbps") or 0.0
        dropped = stats.get_value("packets-sent-dropped") or 0
        retransmitted = stats.get_value("packets-retransmitted") or 0

        # Лічильники в stats кумулятивні — рахуємо приріст за інтервал.
        if self._last_dropped is None:
            self._last_dropped = dropped
            self._last_retransmitted = retransmitted
            return True

        new_dropped = dropped - self._last_dropped
        new_retransmitted = retransmitted - self._last_retransmitted
        self._last_dropped = dropped
        self._last_retransmitted = retransmitted

        if new_dropped > 0:
            log.warning(f"[AdaptiveBitrate] +{new_dropped} втрачених пакетів за інтервал — різко знижую")
            self._apply(self.current_kbps * self.DROP_CUT_FACTOR)
            return True

        if bandwidth_mbps <= 0:
            return True  # SRT ще не встиг оцінити лінк

        bw_kbps = bandwidth_mbps * 1000 * self.BANDWIDTH_HEADROOM

        if new_retransmitted > 0:
            self._apply(min(bw_kbps, self.current_kbps * self.RETRANSMIT_CUT_FACTOR))
        else:
            self._apply(min(bw_kbps, self.current_kbps * self.RECOVERY_STEP_FACTOR, self.target_kbps))

        return True


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
        min_kbps = config.adaptive_bitrate_min_kbps(target_kbps)
        controller = AdaptiveBitrateController(
            pipeline.get_by_name("video_encoder"),
            pipeline.get_by_name("srt_sink"),
            target_kbps,
            min_kbps,
        )
        GLib.timeout_add_seconds(config.ADAPTIVE_BITRATE_INTERVAL_SEC, controller.tick)
        log.info(
            f"[AdaptiveBitrate] enabled: target={target_kbps}kbps min={min_kbps}kbps "
            f"interval={config.ADAPTIVE_BITRATE_INTERVAL_SEC}s"
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
