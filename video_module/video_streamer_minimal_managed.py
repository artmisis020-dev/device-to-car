#!/usr/bin/env python3
"""Managed minimal H.264 SRT video streamer with runtime env controls."""

import math
import os
import signal
import subprocess
import sys
import threading
import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst

Gst.init(None)


VIDEO_DEVICE = os.environ.get("VIDEO_DEVICE", "/dev/video0")


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        print(f"[WARN] Invalid {name}={value}, using {default}", flush=True)
        return default


STREAM_FPS = env_int("STREAM_FPS", 30)
STREAM_WIDTH = env_int("SIRENA_VIDEO_WIDTH", 640)
STREAM_HEIGHT = env_int("SIRENA_VIDEO_HEIGHT", 512)
SRT_LATENCY_MS = env_int("SRT_LATENCY_MS", 0)
BITRATE_KBPS = env_int("BITRATE_KBPS", 2500)
KEYINT = env_int("KEYINT", 30)
MAVLINK_ENDPOINT = os.environ.get(
    "MAVLINK_ENDPOINT",
    os.environ.get("SIRENA_MAVLINK_TELEMETRY_URL", "udp:127.0.0.1:14562"),
)
SIRENA_RELAY_TARGET = os.environ.get("SIRENA_RELAY_TARGET", "").strip().strip('"')
OSD_RATE_HZ = max(1, env_int("OSD_RATE_HZ", 5))

_legacy_osd_enabled = os.environ.get("OSD_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on")
OSD_MODE = os.environ.get("OSD_MODE", "").strip().lower()
if OSD_MODE not in ("off", "text", "hud-lite"):
    OSD_MODE = "text" if _legacy_osd_enabled else "off"


class TelemetryState:
    def __init__(self):
        self.lock = threading.Lock()
        self.mode = "N/A"
        self.armed = "N/A"
        self.batt_v = None
        self.batt_pct = None
        self.alt_m = None
        self.ground_kmh = None
        self.heading = None
        self.sats = None
        self.fix = None
        self.roll_deg = 0.0
        self.pitch_deg = 0.0
        self.last_update_ts = 0.0
        self.error = ""

    def update_from_msg(self, msg):
        mtype = msg.get_type()
        with self.lock:
            if mtype == "HEARTBEAT":
                self.mode = str(getattr(msg, "custom_mode", 0))
                self.armed = "ARMED" if (getattr(msg, "base_mode", 0) & 128) else "DISARMED"
            elif mtype == "SYS_STATUS":
                voltage_mv = getattr(msg, "voltage_battery", -1)
                battery_rem = getattr(msg, "battery_remaining", -1)
                if isinstance(voltage_mv, int) and voltage_mv >= 0:
                    self.batt_v = voltage_mv / 1000.0
                if isinstance(battery_rem, int) and battery_rem >= 0:
                    self.batt_pct = battery_rem
            elif mtype == "GLOBAL_POSITION_INT":
                rel_alt_mm = getattr(msg, "relative_alt", None)
                vx = getattr(msg, "vx", None)
                vy = getattr(msg, "vy", None)
                hdg = getattr(msg, "hdg", None)
                if isinstance(rel_alt_mm, int):
                    self.alt_m = rel_alt_mm / 1000.0
                if isinstance(vx, int) and isinstance(vy, int):
                    speed_ms = ((vx * vx + vy * vy) ** 0.5) / 100.0
                    self.ground_kmh = speed_ms * 3.6
                if isinstance(hdg, int) and hdg != 65535:
                    self.heading = hdg / 100.0
            elif mtype == "GPS_RAW_INT":
                fix_type = getattr(msg, "fix_type", None)
                sats = getattr(msg, "satellites_visible", None)
                if isinstance(fix_type, int):
                    self.fix = fix_type
                if isinstance(sats, int):
                    self.sats = sats
            elif mtype == "ATTITUDE":
                roll = getattr(msg, "roll", None)
                pitch = getattr(msg, "pitch", None)
                yaw = getattr(msg, "yaw", None)
                if isinstance(roll, (int, float)):
                    self.roll_deg = math.degrees(roll)
                if isinstance(pitch, (int, float)):
                    self.pitch_deg = math.degrees(pitch)
                # Fallback heading from attitude yaw (updated continuously).
                if isinstance(yaw, (int, float)):
                    self.heading = math.degrees(yaw) % 360.0
            elif mtype == "VFR_HUD":
                vfr_hdg = getattr(msg, "heading", None)
                if isinstance(vfr_hdg, int) and 0 <= vfr_hdg <= 360:
                    self.heading = float(vfr_hdg)

            self.last_update_ts = time.time()
            self.error = ""

    def set_error(self, text: str):
        with self.lock:
            self.error = text

    def snapshot(self):
        with self.lock:
            age = time.time() - self.last_update_ts if self.last_update_ts else None
            return {
                "mode": self.mode,
                "armed": self.armed,
                "batt_v": self.batt_v,
                "batt_pct": self.batt_pct,
                "alt_m": self.alt_m,
                "ground_kmh": self.ground_kmh,
                "heading": self.heading,
                "sats": self.sats,
                "fix": self.fix,
                "roll_deg": self.roll_deg,
                "pitch_deg": self.pitch_deg,
                "age": age,
                "error": self.error,
            }

    def osd_blocks(self):
        s = self.snapshot()
        mode_s = f"MODE: {s['mode']}"
        arm_s = f"STATE: {s['armed']}"
        alt_s = f"ALT: {s['alt_m']:.1f} m" if s["alt_m"] is not None else "ALT: --"
        spd_s = f"GS: {s['ground_kmh']:.1f} km/h" if s["ground_kmh"] is not None else "GS: --"
        hdg_s = f"HDG: {s['heading']:.0f}" if s["heading"] is not None else "HDG: --"

        if s["batt_v"] is not None and s["batt_pct"] is not None:
            batt_s = f"BATT: {s['batt_v']:.2f}V ({s['batt_pct']}%)"
        elif s["batt_v"] is not None:
            batt_s = f"BATT: {s['batt_v']:.2f}V"
        else:
            batt_s = "BATT: --"

        gps_s = f"GPS: fix {s['fix']}, sats {s['sats']}" if s["fix"] is not None and s["sats"] is not None else "GPS: --"
        stale_s = ""
        if s["age"] is None:
            stale_s = "MAVLINK: waiting..."
        elif s["age"] > 2.0:
            stale_s = f"MAVLINK: stale ({s['age']:.1f}s)"
        err_s = f"MAVLINK ERR: {s['error']}" if s["error"] else ""

        tl = "\n".join([mode_s, arm_s])
        tr = "\n".join([alt_s, spd_s])
        bl = "\n".join([batt_s, gps_s])
        br_lines = [hdg_s]
        if stale_s:
            br_lines.append(stale_s)
        if err_s:
            br_lines.append(err_s)
        return {"tl": tl, "tr": tr, "bl": bl, "br": "\n".join(br_lines)}


class HudLiteDrawer:
    def __init__(self, telemetry: TelemetryState):
        self.telemetry = telemetry
        self.width = 640
        self.height = 480

    def on_caps_changed(self, overlay, caps):
        del overlay
        try:
            struct = caps.get_structure(0)
            self.width = int(struct.get_value("width"))
            self.height = int(struct.get_value("height"))
        except Exception:
            pass

    def _draw_label(self, cr, x, y, text, align="left", size=24):
        cr.select_font_face("Sans", 0, 1)
        cr.set_font_size(size)
        ext = cr.text_extents(text)
        tx = x
        if align == "center":
            tx = x - (ext.width / 2) - ext.x_bearing
        elif align == "right":
            tx = x - ext.width - ext.x_bearing
        ty = y - ext.y_bearing
        cr.set_source_rgba(0.0, 0.0, 0.0, 0.6)
        cr.move_to(tx + 1, ty + 1)
        cr.show_text(text)
        cr.set_source_rgba(0.95, 0.95, 0.95, 0.9)
        cr.move_to(tx, ty)
        cr.show_text(text)

    def _heading_label(self, deg):
        d = int(deg) % 360
        return {
            0: "N", 45: "NE", 90: "E", 135: "SE",
            180: "S", 225: "SW", 270: "W", 315: "NW",
        }.get(d, str(d))

    def _draw_heading_tape(self, cr, heading_deg, cx, w, h):
        """Horizontal heading tape at the very top, similar to Mission Planner."""
        del h
        tape_top = 4.0
        tape_h = 30.0
        left = 28.0
        right = w - 28.0
        px_per_deg = 5.0
        span_deg = int((right - left) / (2.0 * px_per_deg)) + 5

        # Background strip
        cr.set_source_rgba(0.0, 0.0, 0.0, 0.28)
        cr.rectangle(left, tape_top, right - left, tape_h)
        cr.fill()

        # Border
        cr.set_source_rgba(0.85, 0.85, 0.85, 0.55)
        cr.set_line_width(1.0)
        cr.rectangle(left, tape_top, right - left, tape_h)
        cr.stroke()

        # Ticks and labels
        base = int(round(heading_deg / 5.0)) * 5
        for d in range(base - span_deg, base + span_deg + 1, 5):
            x = cx + (d - heading_deg) * px_per_deg
            if x < left or x > right:
                continue
            is_major = (d % 10 == 0)
            tlen = 10 if is_major else 6
            cr.set_source_rgba(0.92, 0.92, 0.92, 0.9)
            cr.set_line_width(1.2)
            cr.move_to(x, tape_top + tape_h)
            cr.line_to(x, tape_top + tape_h - tlen)
            cr.stroke()
            if is_major:
                lbl = self._heading_label(d)
                self._draw_label(cr, x, tape_top + 4, lbl, align="center", size=11)

        # Center index line + numeric heading
        cr.set_source_rgba(0.95, 0.95, 0.95, 0.95)
        cr.set_line_width(1.8)
        cr.move_to(cx, tape_top + tape_h)
        cr.line_to(cx, tape_top + tape_h - 13)
        cr.stroke()
        self._draw_label(cr, cx, tape_top + 2, f"{int(round(heading_deg)) % 360}", align="center", size=12)

    def _draw_roll_scale(self, cr, roll_deg, cx, w, h):
        """Top roll scale like Mission Planner: fixed arc/marks + moving bank pointer."""
        top_y = h * 0.04
        radius = w * 0.60
        circle_cy = radius + top_y
        span_deg = 60

        a1 = -math.pi / 2 + math.radians(-span_deg)
        a2 = -math.pi / 2 + math.radians(span_deg)
        cr.set_source_rgba(0.9, 0.9, 0.9, 0.65)
        cr.set_line_width(1.5)
        cr.arc(cx, circle_cy, radius, a1, a2)
        cr.stroke()

        major_marks = {-60, -45, -30, -20, -10, 10, 20, 30, 45, 60}
        for d in range(-span_deg, span_deg + 1, 5):
            d_rad = math.radians(d)
            px = cx + radius * math.sin(d_rad)
            py = circle_cy - radius * math.cos(d_rad)
            if py < 0 or py > h * 0.45:
                continue

            dist = math.hypot(cx - px, circle_cy - py)
            nx = (cx - px) / dist
            ny = (circle_cy - py) / dist
            is_major = (d in major_marks)
            tick_len = 16 if is_major else 8

            cr.set_source_rgba(0.95, 0.95, 0.95, 0.9)
            cr.set_line_width(1.5)
            cr.move_to(px, py)
            cr.line_to(px + nx * tick_len, py + ny * tick_len)
            cr.stroke()

            if is_major:
                label = str(abs(d))
                lx = px + nx * (tick_len + 2)
                ly = py + ny * (tick_len + 2) + 7
                self._draw_label(cr, lx, ly, label, align="center", size=15)

        # Moving bank pointer on the arc (follows roll).
        bank = max(-60.0, min(60.0, roll_deg))
        bank_rad = math.radians(bank)
        px = cx + radius * math.sin(bank_rad)
        py = circle_cy - radius * math.cos(bank_rad)
        dist = math.hypot(cx - px, circle_cy - py)
        nx = (cx - px) / dist
        ny = (circle_cy - py) / dist
        cr.set_source_rgba(0.95, 0.95, 0.95, 0.95)
        cr.set_line_width(3.0)
        cr.move_to(px, py)
        cr.line_to(px + nx * 20, py + ny * 20)
        cr.stroke()

        # Fixed red zero marker at arc apex.
        tip_x = cx
        tip_y = top_y + 2
        cr.set_source_rgba(1.0, 0.2, 0.2, 0.95)
        cr.move_to(tip_x, tip_y)
        cr.line_to(tip_x - 8, tip_y + 16)
        cr.line_to(tip_x + 8, tip_y + 16)
        cr.close_path()
        cr.fill()

    def _draw_vtape(self, cr, x, tape_y, tape_w, tape_h, value, unit,
                    scale_px=12.0, tick_interval=5, side="right"):
        """Vertical scrolling tape. side='right' → ticks on the left edge of box;
        side='left' → ticks on the right edge of box."""
        cy_tape = tape_y + tape_h / 2.0

        # Semi-transparent background
        cr.set_source_rgba(1.0, 1.0, 1.0, 0.12)
        cr.rectangle(x, tape_y, tape_w, tape_h)
        cr.fill()
        cr.set_source_rgba(0.75, 0.75, 0.75, 0.5)
        cr.set_line_width(1.0)
        cr.rectangle(x, tape_y, tape_w, tape_h)
        cr.stroke()

        # Clipping region
        cr.save()
        cr.rectangle(x, tape_y, tape_w, tape_h)
        cr.clip()

        visible_range = tape_h / scale_px
        val_min = value - visible_range / 2.0
        start_val = math.floor(val_min / tick_interval) * tick_interval
        cr.set_line_width(1.5)

        v = start_val
        while v <= value + visible_range / 2.0 + tick_interval:
            py = cy_tape - (v - value) * scale_px
            if tape_y <= py <= tape_y + tape_h:
                is_major = (int(round(v)) % (tick_interval * 2) == 0)
                tick_len = 15 if is_major else 8
                cr.set_source_rgba(0.9, 0.9, 0.9, 0.85)
                if side == "right":
                    cr.move_to(x, py)
                    cr.line_to(x + tick_len, py)
                else:
                    cr.move_to(x + tape_w, py)
                    cr.line_to(x + tape_w - tick_len, py)
                cr.stroke()
                if is_major:
                    label = str(int(round(v)))
                    if side == "right":
                        self._draw_label(cr, x + tape_w - 4, py + 6, label,
                                         align="right", size=13)
                    else:
                        self._draw_label(cr, x + 4, py + 6, label,
                                         align="left", size=13)
            v += tick_interval

        cr.restore()

        # Current-value highlight box
        box_h = 22
        box_y = cy_tape - box_h / 2.0
        cr.set_source_rgba(0.0, 0.0, 0.0, 0.85)
        cr.rectangle(x, box_y, tape_w, box_h)
        cr.fill()
        val_text = f"{value:.0f} {unit}"
        cr.set_source_rgba(0.95, 0.95, 0.95, 1.0)
        cr.select_font_face("Sans", 0, 1)
        cr.set_font_size(13)
        ext = cr.text_extents(val_text)
        cr.move_to(x + (tape_w - ext.width) / 2.0 - ext.x_bearing,
                   cy_tape + ext.height / 2.0)
        cr.show_text(val_text)

    def on_draw(self, overlay, cr, timestamp, duration):
        del overlay, timestamp, duration
        s = self.telemetry.snapshot()
        w = float(self.width)
        h = float(self.height)
        cx = w / 2.0
        cy = h / 2.0

        roll = math.radians(s["roll_deg"] or 0.0)
        pitch = max(-30.0, min(30.0, s["pitch_deg"] or 0.0))
        px_per_deg = h / 90.0

        # --- Artificial horizon + pitch ladder ---
        cr.set_line_width(2.0)
        cr.set_source_rgba(0.95, 0.95, 0.95, 0.85)
        cr.save()
        cr.translate(cx, cy)
        cr.rotate(-roll)
        horizon_y = pitch * px_per_deg
        # Main horizon line (shorter, centered)
        cr.move_to(-w * 0.35, horizon_y)
        cr.line_to(w * 0.35, horizon_y)
        cr.stroke()
        # Pitch ladder marks with degree labels
        for mark in (-20, -10, 10, 20):
            y = horizon_y - (mark * px_per_deg)
            span = 55 if abs(mark) == 10 else 35
            cr.set_line_width(1.5)
            cr.move_to(-span, y)
            cr.line_to(span, y)
            cr.stroke()
            # Degree label on both sides
            lbl = str(mark)
            cr.select_font_face("Sans", 0, 1)
            cr.set_font_size(13)
            ext = cr.text_extents(lbl)
            for sx in (-span - 4, span + 4):
                align_x = sx - ext.width - ext.x_bearing if sx > 0 else sx - ext.x_bearing
                cr.set_source_rgba(0.0, 0.0, 0.0, 0.6)
                cr.move_to(align_x + 1, y - ext.y_bearing + 1)
                cr.show_text(lbl)
                cr.set_source_rgba(0.95, 0.95, 0.95, 0.9)
                cr.move_to(align_x, y - ext.y_bearing)
                cr.show_text(lbl)
                cr.set_source_rgba(0.95, 0.95, 0.95, 0.85)
        cr.restore()

        # --- Aircraft V-chevron symbol (red) ---
        cr.set_source_rgba(1.0, 0.15, 0.15, 0.92)
        cr.set_line_width(3.0)
        # Left wing
        cr.move_to(cx - 32, cy)
        cr.line_to(cx - 10, cy + 12)
        cr.line_to(cx, cy + 6)
        # Right wing
        cr.move_to(cx + 32, cy)
        cr.line_to(cx + 10, cy + 12)
        cr.line_to(cx, cy + 6)
        cr.stroke()
        # Green centre dot
        cr.set_source_rgba(0.1, 0.9, 0.1, 0.9)
        cr.arc(cx, cy + 6, 3, 0, 2 * math.pi)
        cr.fill()

        # --- Top heading tape (yaw) ---
        hdg = s["heading"] if s["heading"] is not None else 0.0
        self._draw_heading_tape(cr, hdg, cx, w, h)

        # --- Top roll scale ---
        self._draw_roll_scale(cr, math.degrees(roll), cx, w, h)

        # --- Side tapes ---
        tape_w = 72
        tape_h = h * 0.60
        tape_top = cy - tape_h / 2.0

        # Altitude tape (right side)
        alt = s["alt_m"] if s["alt_m"] is not None else 0.0
        tape_x_r = w - tape_w - 6
        self._draw_vtape(cr, tape_x_r, tape_top, tape_w, tape_h,
                         alt, "m", scale_px=12.0, tick_interval=5, side="right")
        mode_text = s["mode"] or "--"
        self._draw_label(cr, tape_x_r + tape_w / 2, tape_top + tape_h + 20,
                         mode_text, align="center", size=15)
        self._draw_label(cr, tape_x_r + tape_w / 2, tape_top + tape_h + 40,
                         f"{alt:.1f}m>0", align="center", size=13)

        # Speed tape (left side, ground speed in m/s)
        gs_ms = (s["ground_kmh"] or 0.0) / 3.6
        tape_x_l = 6
        self._draw_vtape(cr, tape_x_l, tape_top, tape_w, tape_h,
                         gs_ms, "m/s", scale_px=12.0, tick_interval=5, side="left")
        self._draw_label(cr, tape_x_l + tape_w / 2, tape_top + tape_h + 20,
                         "AS 0.0m/s", align="center", size=13)
        self._draw_label(cr, tape_x_l + tape_w / 2, tape_top + tape_h + 40,
                         f"GS {gs_ms:.1f}m/s", align="center", size=13)

        # --- Armed / Disarmed in centre (large, coloured) ---
        armed_text = s["armed"] or "--"
        cr.select_font_face("Sans", 0, 1)
        cr.set_font_size(36)
        ext = cr.text_extents(armed_text)
        tx = cx - (ext.width / 2) - ext.x_bearing
        ty = cy - tape_h / 2 + 36 - ext.y_bearing
        cr.set_source_rgba(0.0, 0.0, 0.0, 0.55)
        cr.move_to(tx + 2, ty + 2)
        cr.show_text(armed_text)
        if "ARMED" in armed_text and "DIS" not in armed_text:
            cr.set_source_rgba(0.1, 0.85, 0.1, 0.95)
        else:
            cr.set_source_rgba(0.95, 0.15, 0.15, 0.95)
        cr.move_to(tx, ty)
        cr.show_text(armed_text)

        # --- Battery + GPS (bottom centre) ---
        batt = "BATT --"
        if s["batt_v"] is not None and s["batt_pct"] is not None:
            batt = f"BATT {s['batt_v']:.2f}V {s['batt_pct']}%"
        gps = (f"GPS {s['fix']}/{s['sats']}"
               if s["fix"] is not None and s["sats"] is not None else "GPS --")
        self._draw_label(cr, cx, h - 24, f"{batt}   {gps}", align="center", size=16)

        # --- Stale / error ---
        if s["age"] is None or s["age"] > 2.0:
            wait_text = ("MAVLINK WAIT" if s["age"] is None
                         else f"MAVLINK STALE {s['age']:.1f}s")
            self._draw_label(cr, cx, cy + tape_h / 2 + 60, wait_text,
                             align="center", size=16)
        if s["error"]:
            self._draw_label(cr, cx, cy + tape_h / 2 + 82,
                             f"ERR: {s['error']}", align="center", size=14)


def start_telemetry_reader(state: TelemetryState):
    if OSD_MODE == "off":
        return None

    def worker():
        try:
            from pymavlink import mavutil
        except Exception as exc:
            state.set_error(f"pymavlink import failed: {exc}")
            return

        while True:
            try:
                print(f"[OSD] Connecting MAVLink endpoint: {MAVLINK_ENDPOINT}", flush=True)
                conn = mavutil.mavlink_connection(MAVLINK_ENDPOINT, source_system=250)
                if MAVLINK_ENDPOINT.startswith("udpout:"):
                    import threading as _t
                    def _hb():
                        import time as _time
                        while True:
                            try:
                                conn.mav.heartbeat_send(0, 0, 0, 0, 0)
                            except Exception:
                                pass
                            _time.sleep(1.0)
                    _t.Thread(target=_hb, daemon=True).start()
                # Request all telemetry streams (needed for TCP/UDP connections)
                try:
                    conn.mav.request_data_stream_send(1, 0, mavutil.mavlink.MAV_DATA_STREAM_ALL, 4, 1)
                    print("[OSD] Requested MAVLink data streams", flush=True)
                except Exception:
                    pass
                while True:
                    msg = conn.recv_match(blocking=True, timeout=1)
                    if msg is not None:
                        state.update_from_msg(msg)
            except Exception as exc:
                state.set_error(str(exc))
                time.sleep(1.0)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return thread


def is_mjpeg_only_device(video_device: str) -> bool:
    try:
        result = subprocess.run(
            ["v4l2-ctl", "-d", video_device, "--list-formats"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return False

    formats = []
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("[") and "]: '" in line:
            parts = line.split("'")
            if len(parts) >= 2:
                formats.append(parts[1].strip())
    return len(formats) == 1 and formats[0] == "MJPG"


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


def get_v4l2_src_caps(video_device: str) -> str:
    if supports_mjpeg(video_device):
        print("[INFO] Camera supports MJPEG, using jpegdec source path", flush=True)
        return "capsfilter caps=\"image/jpeg\" ! jpegdec"
    return "capsfilter caps=\"video/x-raw\""


def get_encoder_chain() -> str:
    if Gst.ElementFactory.find("x264enc") is None:
        raise RuntimeError("No H264 encoder found: x264enc")

    print("[INFO] Using software encoder: x264enc", flush=True)
    return (
        "x264enc "
        "tune=zerolatency "
        "speed-preset=ultrafast "
        f"bitrate={BITRATE_KBPS} "
        f"key-int-max={KEYINT} "
        "bframes=0 "
        "sliced-threads=true "
        "byte-stream=true "
        "option-string=repeat-headers=1 ! "
        "capsfilter caps=\"video/x-h264,profile=baseline,stream-format=byte-stream,alignment=au\""
    )


def create_pipeline_string() -> str:
    output_caps = (
        "capsfilter caps=\""
        f"video/x-raw,width={STREAM_WIDTH},height={STREAM_HEIGHT},"
        f"framerate={STREAM_FPS}/1"
        "\""
    )
    encoder_chain = get_encoder_chain()

    osd_chain = ""

    return (
        f"v4l2src device={VIDEO_DEVICE} ! "
        "capsfilter caps=\"video/x-raw\" ! "
        "queue leaky=downstream max-size-buffers=1 ! "
        "videoconvert ! "
        "videoscale ! "
        "videorate drop-only=true ! "
        f"{output_caps} ! "
        "capsfilter caps=\"video/x-raw,format=I420\" ! "
        f"{encoder_chain} ! "
        "h264parse config-interval=1 ! "
        f"rtspclientsink location=\"{SIRENA_RELAY_TARGET}\" protocols=udp latency=0"
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
telemetry_state = TelemetryState()
runtime_state = {"stop_requested": False, "restart_requested": False}

print("[INIT] Starting managed GStreamer SRT streamer...", flush=True)
print(
    "[CONF] "
    f"VIDEO_DEVICE={VIDEO_DEVICE} STREAM_FPS={STREAM_FPS} "
    f"SRT_LATENCY_MS={SRT_LATENCY_MS} BITRATE_KBPS={BITRATE_KBPS} "
    f"OSD_MODE={OSD_MODE} MAVLINK_ENDPOINT={MAVLINK_ENDPOINT}",
    flush=True,
)
print(f"[PIPE] {PIPELINE_STR}", flush=True)

try:
    pipeline = Gst.parse_launch(PIPELINE_STR)
    if pipeline is None:
        raise RuntimeError("Failed to parse pipeline")

    bus = pipeline.get_bus()
    if bus is None:
        raise RuntimeError("Could not get pipeline bus")
    bus.add_signal_watch()
    bus.connect("message", BusMessageHandler(loop, pipeline, runtime_state))

    start_telemetry_reader(telemetry_state)

    if OSD_MODE == "text":
        osd_tl = pipeline.get_by_name("osd_tl")
        osd_tr = pipeline.get_by_name("osd_tr")
        osd_bl = pipeline.get_by_name("osd_bl")
        osd_br = pipeline.get_by_name("osd_br")
        if all(x is not None for x in (osd_tl, osd_tr, osd_bl, osd_br)):
            interval_ms = max(100, int(1000 / OSD_RATE_HZ))

            def update_osd_text():
                blocks = telemetry_state.osd_blocks()
                osd_tl.set_property("text", blocks["tl"])
                osd_tr.set_property("text", blocks["tr"])
                osd_bl.set_property("text", blocks["bl"])
                osd_br.set_property("text", blocks["br"])
                return True

            GLib.timeout_add(interval_ms, update_osd_text)
        else:
            print("[WARN] Text OSD overlays were not found", flush=True)
    elif OSD_MODE == "hud-lite":
        hud = pipeline.get_by_name("osd_hud")
        if hud is None:
            print("[WARN] hud-lite is enabled but cairooverlay was not found", flush=True)
        else:
            drawer = HudLiteDrawer(telemetry_state)
            hud.connect("caps-changed", drawer.on_caps_changed)
            hud.connect("draw", drawer.on_draw)

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
        raise RuntimeError("Failed to get pipeline state while starting")
    if ret == Gst.StateChangeReturn.SUCCESS and state != Gst.State.PLAYING:
        raise RuntimeError(
            f"Pipeline did not reach PLAYING state: {state.value_name}, pending: {pending.value_name}"
        )

    print("[READY] Pipeline started successfully. Streaming via configured relay target...", flush=True)
    loop.run()

    if runtime_state["restart_requested"] and not runtime_state["stop_requested"]:
        raise RuntimeError("Pipeline stopped unexpectedly, requesting systemd restart")
except Exception as exc:
    print(f"[FATAL] {exc}", file=sys.stderr, flush=True)
    sys.exit(1)
finally:
    if pipeline is not None:
        bus = pipeline.get_bus()
        pipeline.set_state(Gst.State.NULL)
        if bus is not None:
            bus.remove_signal_watch()
    print("[DONE] Streamer stopped.", flush=True)
