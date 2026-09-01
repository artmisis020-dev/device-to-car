#!/usr/bin/env python3
"""Managed minimal H.264 SRT video streamer with runtime env controls."""

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import gi
from cameras_services import resolve_video_device, video_nodes

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst

Gst.init(None)

_CONFIGURED_VIDEO_DEVICE = os.environ.get("VIDEO_DEVICE", "/dev/video0")
VIDEO_DEVICE = resolve_video_device(_CONFIGURED_VIDEO_DEVICE)

print(f"[INFO] Використовується відеопристрій: {VIDEO_DEVICE}", flush=True)

if VIDEO_DEVICE != _CONFIGURED_VIDEO_DEVICE:
    print(f"[WARN] VIDEO_DEVICE={_CONFIGURED_VIDEO_DEVICE} не знайдено, використовую {VIDEO_DEVICE}", flush=True)

if not os.path.exists(VIDEO_DEVICE):
    available = ", ".join(video_nodes()) or "немає"
    raise RuntimeError(
        f"Відеокамеру не знайдено: VIDEO_DEVICE={VIDEO_DEVICE}. "
        f"Доступні /dev/video*: {available}. "
        "Перевірте USB camera або оновіть VIDEO_DEVICE в /opt/sirena/.env."
    )


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        print(f"[WARN] Invalid {name}={value}, using {default}", flush=True)
        return default


VIDEO_MANAGER_CONFIG_PATH = os.environ.get("SIRENA_VIDEO_CONFIG_PATH", "/opt/sirena-video/sirena_video_config.json")


def _load_manager_config() -> dict:
    try:
        return json.loads(Path(VIDEO_MANAGER_CONFIG_PATH).read_text())
    except Exception:
        return {}


def _cfg_int(cfg: dict, json_key: str, env_name: str, default: int) -> int:
    """Значення з панелі керування (sirena_video_config.json) має пріоритет
    над env — так fps/bitrate/роздільність, задані в UI, реально
    застосовуються при наступному старті/рестарті сервісу."""
    value = cfg.get(json_key)
    if value is not None:
        try:
            return int(value)
        except (TypeError, ValueError):
            pass
    return env_int(env_name, default)


_MANAGER_CFG = _load_manager_config()

STREAM_FPS = _cfg_int(_MANAGER_CFG, "fps", "SIRENA_VIDEO_FPS", 30)
STREAM_WIDTH = _cfg_int(_MANAGER_CFG, "width", "SIRENA_VIDEO_WIDTH", 640)
STREAM_HEIGHT = _cfg_int(_MANAGER_CFG, "height", "SIRENA_VIDEO_HEIGHT", 512)
BITRATE_KBPS = _cfg_int(_MANAGER_CFG, "bitrate", "SIRENA_VIDEO_BITRATE", 1000)
# Коротший GOP, ніж у srt-relay-capture (там дефолт ~STREAM_FPS): цей шлях
# штовхає RTSP без ARQ, тож швидше відновлення після втрати пакета важливіше
# за компресію.
KEYINT = env_int("KEYINT", 15)
SIRENA_RELAY_TARGET = os.environ.get("SIRENA_RELAY_TARGET", "").strip().strip('"')
VIDEO_ENCODER = os.environ.get("VIDEO_ENCODER", "auto").strip().lower()

# baseline | main | high — main дає CABAC (краща компресія за той самий
# бітрейт) без доданої затримки; B-frames лишаються вимкненими окремо
# (bframes=0 у x264enc), бо саме вони додають latency, а не профіль.
_H264_PROFILE_MAP = {"baseline": (0, "baseline"), "main": (2, "main"), "high": (4, "high")}
H264_PROFILE = os.environ.get("H264_PROFILE", "main").strip().lower()
if H264_PROFILE not in _H264_PROFILE_MAP:
    print(f"[WARN] Invalid H264_PROFILE={H264_PROFILE}, using main", flush=True)
    H264_PROFILE = "main"

# ─── Локальний запис відео ────────────────────────────────────────────────────
# RECORD_ENABLED=1 — увімкнути запис на диск (паралельно зі стрімом)
# RECORD_DIR — куди писати файли
# RECORD_SEGMENT_SEC — тривалість одного файлу-сегмента (сек). Сегментація
#                           важлива: при раптовому вимкненні живлення втрачається
#                           лише поточний сегмент, а не весь запис.
# RECORD_MIN_FREE_MB — мінімум вільного місця; якщо менше — старі сегменти
#                           видаляються перед стартом (0 = не чистити)
RECORD_ENABLED = os.environ.get("RECORD_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on")
RECORD_DIR = os.environ.get("RECORD_DIR", "/opt/sirena-video/recordings/local").rstrip("/")
RECORD_SEGMENT_SEC = max(5, env_int("RECORD_SEGMENT_SEC", 60))
RECORD_MIN_FREE_MB = env_int("RECORD_MIN_FREE_MB", 500)


def prepare_record_dir() -> str:
    """Створює папку сесії з міткою часу і за потреби чистить старі записи."""
    session = time.strftime("%Y-%m-%d_%H-%M-%S")
    session_dir = os.path.join(RECORD_DIR, session)
    os.makedirs(session_dir, exist_ok=True)

    if RECORD_MIN_FREE_MB > 0:
        try:
            st = os.statvfs(RECORD_DIR)
            free_mb = st.f_bavail * st.f_frsize // (1024 * 1024)
            if free_mb < RECORD_MIN_FREE_MB:
                # Видаляємо найстаріші сесії, поки не звільниться місце
                sessions = sorted(
                    d for d in os.listdir(RECORD_DIR)
                    if os.path.isdir(os.path.join(RECORD_DIR, d)) and d != session
                )
                for old in sessions:
                    shutil.rmtree(os.path.join(RECORD_DIR, old), ignore_errors=True)
                    st = os.statvfs(RECORD_DIR)
                    free_mb = st.f_bavail * st.f_frsize // (1024 * 1024)
                    print(f"[REC] Видалено стару сесію {old}, вільно {free_mb} MB", flush=True)
                    if free_mb >= RECORD_MIN_FREE_MB:
                        break
        except Exception as exc:
            print(f"[WARN] Не вдалось перевірити/звільнити місце: {exc}", flush=True)

    return session_dir

# ultrafast/superfast/veryfast/... — швидші пресети не додають затримки
# (без lookahead/B-frames), лише гірша якість/бітрейт-ефективність за
# той самий CPU-бюджет.
X264_SPEED_PRESET = os.environ.get("X264_SPEED_PRESET", "superfast").strip().lower()

V4L2_IO_MODE = os.environ.get("V4L2_IO_MODE", os.environ.get("SIRENA_V4L2_IO_MODE", "rw")).strip().lower()


if not SIRENA_RELAY_TARGET and not RECORD_ENABLED:
    # Без цілі ретрансляції стрімити нікуди. Виходимо чисто (exit 0), щоб
    # Restart=on-failure не крутив crash-loop до StartLimitBurst і юніт не
    # лягав у failed; video-relay сам рестартне сервіс, коли запише
    # /etc/default/sirena-relay з таргетом.
    print("[INFO] SIRENA_RELAY_TARGET порожній — стрім не потрібен, чистий вихід.", flush=True)
    sys.exit(0)


def supports_mjpeg(video_device: str) -> bool:
    try:
        result = subprocess.run(
            ["v4l2-ctl", "-d", video_device, "--list-formats"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return False

    return "MJPG" in (result.stdout or "")


def get_encoder_chain() -> str:
    # v4l2h264enc реєструється GStreamer'ом лише коли в системі реально є
    # V4L2 M2M енкодер (RPi4 має, RPi5 — ні), тож find() тут — перевірка заліза.
    use_hw = VIDEO_ENCODER in ("auto", "v4l2") and Gst.ElementFactory.find("v4l2h264enc") is not None
    if VIDEO_ENCODER == "v4l2" and not use_hw:
        print("[WARN] VIDEO_ENCODER=v4l2, але v4l2h264enc недоступний — fallback на x264enc", flush=True)

    profile_id, caps_profile = _H264_PROFILE_MAP[H264_PROFILE]

    if use_hw:
        print(f"[INFO] Using hardware encoder: v4l2h264enc (profile={H264_PROFILE})", flush=True)
        return (
            "v4l2h264enc "
            f"extra-controls=\"controls,repeat_sequence_header=1,h264_profile={profile_id},"
            f"video_bitrate={BITRATE_KBPS * 1000},h264_i_frame_period={KEYINT}\" ! "
            "capsfilter caps=\"video/x-h264,level=(string)4,stream-format=byte-stream,alignment=au\""
        )

    if Gst.ElementFactory.find("x264enc") is None:
        raise RuntimeError("No H264 encoder found: x264enc")

    print(f"[INFO] Using software encoder: x264enc (preset={X264_SPEED_PRESET}, profile={H264_PROFILE})", flush=True)
    return (
        "x264enc "
        "tune=zerolatency "
        f"speed-preset={X264_SPEED_PRESET} "
        f"bitrate={BITRATE_KBPS} "
        f"key-int-max={KEYINT} "
        "bframes=0 "
        "sliced-threads=true "
        "byte-stream=true "
        "option-string=repeat-headers=1 ! "
        f"capsfilter caps=\"video/x-h264,profile={caps_profile},stream-format=byte-stream,alignment=au\""
    )


def create_pipeline_string() -> str:
    output_caps = (
        "capsfilter caps=\""
        f"video/x-raw,width={STREAM_WIDTH},height={STREAM_HEIGHT},"
        f"framerate={STREAM_FPS}/1"
        "\""
    )
    encoder_chain = get_encoder_chain()

    return (
        f"v4l2src device={VIDEO_DEVICE} ! "
        "capsfilter caps=\"video/x-raw\" ! "
        "queue leaky=downstream max-size-buffers=1 ! "
        "videoconvert n-threads=2 ! "
        "videoscale ! "
        "videorate drop-only=true ! "
        f"{output_caps} ! "
        "capsfilter caps=\"video/x-raw,format=I420\" ! "
        f"{encoder_chain} ! "
        "h264parse config-interval=1 ! "
        f"{build_sink_chain()}"
    )


def build_sink_chain() -> str:
    """Кінець пайплайна: стрім, запис"""
    branches = []

    if SIRENA_RELAY_TARGET:
        branches.append(
            "queue max-size-buffers=0 max-size-bytes=0 max-size-time=1000000000 ! "
            f"rtspclientsink location=\"{SIRENA_RELAY_TARGET}\" protocols=udp latency=0"
        )

    if RECORD_ENABLED:
        session_dir = prepare_record_dir()
        location = os.path.join(session_dir, "seg_%05d.mp4")
        print(f"[REC] Запис увімкнено -> {location} (сегменти по {RECORD_SEGMENT_SEC}s)", flush=True)
        branches.append(
            "queue max-size-buffers=0 max-size-bytes=0 max-size-time=3000000000 ! "
            "h264parse ! "
            "splitmuxsink name=recsink async-finalize=true "
            f"location=\"{location}\" "
            f"max-size-time={RECORD_SEGMENT_SEC * 1_000_000_000} "
            "muxer-factory=mp4mux"
        )

    if len(branches) == 1:
        return branches[0]

    # Обидві гілки: tee розгалужує один H.264 потік на стрім і на запис.
    return (
            "tee name=out_tee "
            + " ".join(f"out_tee. ! {b}" for b in branches)
    )


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
            print(f"[ERROR] {err.message}", file=sys.stderr, flush=True)
            if debug:
                print(f"[DEBUG] {debug}", file=sys.stderr, flush=True)
            self.runtime_state["restart_requested"] = True
            self.loop.quit()
        elif message.type == Gst.MessageType.EOS:
            print("[INFO] Got end-of-stream", flush=True)
            if not self.runtime_state["stop_requested"]:
                self.runtime_state["restart_requested"] = True
            self.loop.quit()
        elif message.type == Gst.MessageType.STATE_CHANGED:
            if message.src != self.pipeline:
                return True
            old_state, new_state, pending_state = message.parse_state_changed()
            print(
                f"[STATE] {old_state.value_name} -> {new_state.value_name} "
                f"(pending: {pending_state.value_name})",
                flush=True,
            )
        elif message.type == Gst.MessageType.APPLICATION:
            struct = message.get_structure()
            if not (struct and struct.has_name("application/min-streamer-interrupt")):
                return True
            if self.shutting_down:
                self.loop.quit()
                return True
            print("[INFO] Interrupt received, sending EOS", flush=True)
            self.pipeline.send_event(Gst.Event.new_eos())
            self.shutting_down = True
        return True


def request_shutdown(signum, frame):
    del signum, frame
    print("\n[SHUTDOWN] Interrupt requested", flush=True)
    runtime_state["stop_requested"] = True
    if pipeline is None:
        loop.quit()
        return
    bus = pipeline.get_bus()
    if bus is None:
        loop.quit()
        return
    structure = Gst.Structure.new_empty("application/min-streamer-interrupt")
    bus.post(Gst.Message.new_application(pipeline, structure))


pipeline = None
loop = GLib.MainLoop()
PIPELINE_STR = create_pipeline_string()
runtime_state = {"stop_requested": False, "restart_requested": False}

print("[INIT] Запускається GStreamer SRT streamer...", flush=True)
print(
    "[CONF] "
    f"VIDEO_DEVICE={VIDEO_DEVICE} STREAM_FPS={STREAM_FPS} "
    f"BITRATE_KBPS={BITRATE_KBPS} VIDEO_ENCODER={VIDEO_ENCODER} "
    f"V4L2_IO_MODE={V4L2_IO_MODE}",
    flush=True,
)
print(f"[PIPE] {PIPELINE_STR}", flush=True)

try:
    pipeline = Gst.parse_launch(PIPELINE_STR)
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

    ret, state, pending = pipeline.get_state(3 * Gst.SECOND)
    print(
        f"[STATE] startup check result={ret.value_name} "
        f"state={state.value_name} pending={pending.value_name}",
        flush=True,
    )
    if ret == Gst.StateChangeReturn.FAILURE:
        raise RuntimeError("Не вдалося отримати стан пайплайну під час запуску")
    if ret == Gst.StateChangeReturn.SUCCESS and state != Gst.State.PLAYING:
        raise RuntimeError(
            f"Пайп не досягнув стану PLAYING: {state.value_name}, очікується: {pending.value_name}"
        )

    print("[READY] Пайп запущений успішно.", flush=True)
    loop.run()

    if runtime_state["restart_requested"] and not runtime_state["stop_requested"]:
        raise RuntimeError("Пайп зупинено аварійно, потребує systemd restart")
except Exception as exc:
    print(f"[FATAL] {exc}", file=sys.stderr, flush=True)
    sys.exit(1)
finally:
    if pipeline is not None:
        bus = pipeline.get_bus()
        pipeline.set_state(Gst.State.NULL)
        if bus is not None:
            bus.remove_signal_watch()
    print("[DONE] Стрімер зупинено.", flush=True)