#!/usr/bin/env python3
"""
Головний оркестратор GPS Hub системи — Enhanced.

Базується на sirena/main.py + інтеграція StarGPSHandler з satellite-gps-.

Нові можливості (відносно оригінального Sirena):
  1. StarGPSHandler  — sliding window (N=30) зі швидкістю, азимутом,
                       кружною статистикою; O(1) running stats.
  2. Статистичний фільтр якості Starlink — 4 умови:
       а) поточна швидкість в межах 1.5σ від середньої
       б) σ швидкості < 15 м/с
       в) відхилення азимуту від середнього < 20°
       г) σ азимуту < 8°
  3. Spoof-детекція — стрибок > 7 км → toggle-флаг.
  4. Forecast fallback — коли Starlink нестабільний, замість
     негайного переходу на Beitian система 5 секунд
     використовує мертве числення (dead reckoning) з останньою
     стабільною швидкістю та азимутом.

Пріоритет джерел (розширений):
  Manual(60s) > Starlink(стабільний) > Forecast(5s) > Beitian
"""

import math
import time
import threading
import signal
import logging
import datetime
from collections import deque
from pathlib import Path
from typing import Optional, Dict, Any, List
import json
# Локальні модулі (оригінальний Sirena)
import starlink
import mavlink_bridge
import gps_priority
import config
import datetime
try:
    from telemetry_snapshot import TelemetrySnapshotPublisher
except ImportError:
    TelemetrySnapshotPublisher = None

# StarGPSHandler з satellite-gps-
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
# from stargps_handler import StarGPSHandler, GPSRecord, Arc4Hysteresis, run_cmad
from stargps_handler import StarGPSHandler, GPSRecord

# 2026-09-28: наш власний EKF (vision_module/inertia/ekf_estimator.py) для
# докочування Starlink-викидів — НЕ EKF ArduPilot/FC. Причина: ArduPilot-EKF
# сам може "зійти з розуму" (спостерігались повідомлення про несправний
# EKF в mavlink-логах, GPS-спуф чи інші фактори здатні його зіпсувати), а
# ми хочемо, щоб докочування на це не залежало. Підхід підтверджено
# візуально на 2 реальних польотах (navigation_module/gps_smoothing_eval,
# route_filtered_visioninertia.py) — EKF скидається на кожній прийнятій
# Starlink-точці (дрейф лише на самому провалі, не увесь політ) і показав
# ту саму якість, що й проста швидкість FC, без залежності від ArduPilot.
#
#
# 2026-09-30: InertialNavigator (vision_module/inertia/inertial_nav.py) —
# інерція працює САМА, без Starlink (чиста інерція: IMU + баро + ZUPT);
# Starlink — опційне абсолютне джерело: коригує окремий aided-фільтр
# (замість "ковзне середнє + скид EKF") і дає звірку чистої інерції.
# Далі те саме місце займе візуальна навігація / GPS. Цифри —
# vision_module/inertia/starlink_eval.py, plot_inertia.py.
#
# ВАЖЛИВО (repo vs deployed): ekf_estimator.py, imu_math.py, integrity.py,
# inertial_nav.py — канонічні файли з vision_module/inertia/. install.sh
# тепер сам копіює їх поряд з main.py (/opt/sirena-navigation/); у репо
# вони імпортуються по шляху нижче.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "vision_module" / "inertia"))
from inertial_nav import InertialNavigator, NavConfig
from pymavlink import mavutil
# Налаштування логування
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [main]: %(message)s"
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Константи статистичного фільтру (з satellite-gps-)
# ---------------------------------------------------------------------------
GPS_QUEUE_SIZE         = 30      # розмір sliding window
GPS_SPOOF_METERS_THRE  = 7000.0  # стрибок > 7 км → підозра на спуфінг
GPS_SPOOF_BLOCK_SEC    = 60.0    # блок Starlink після спуф-стрибка (кожен новий стрибок перезаряджає)
STARLINK_FAIL_THRESHOLD = 3      # к-ть фейлів gRPC поспіль до перемикання на fallback
# 2026-09-26: send_starlink_to_server() слав _stargps._data[-1] в адмінку
# КОЖНІ 0.5с БЕЗУМОВНО, не дивлячись, чи цей запис взагалі свіжий. Якщо
# Starlink-дишка тимчасово не відповідає (gRPC-збій), _stargps просто
# перестає поповнюватись — а сюди й далі летів той самий останній запис,
# роблячи вигляд живих даних. Звідси "координати весь час одні й ті ж".
# Поріг узятий з запасом над STARLINK_POLL_SEC (1с) — кілька пропущених
# опитувань це вже не джиттер.
STARLINK_STALE_SEC = 4.0
# 2026-09-28: наш EKF (зараз — self._nav) рахує позицію лише поки реально
# прилітають ATTITUDE+RAW_IMU від FC. Якщо FC-лінк відвалиться — предикт
# просто перестає викликатись, і `.position` тихо застигає на місці
# (0,0,0 відносно останнього скиду) — це не гірше за старий "freeze", АЛЕ
# явно перевіряємо вік останнього предикту, щоб не докочуватись на
# завідомо мертвих даних без жодного попередження в лог.
OUR_EKF_STALE_SEC = 2.0
# Частоти, які навігація просить у FC для власного EKF (MAV_CMD_SET_MESSAGE_INTERVAL).
# До 2026-09-30 RAW_IMU ішов 2Гц (mavlink_client.MESSAGE_RATES) — це
# миттєвий аліасований семпл, з якого інтегрування давало більшу похибку,
# ніж стала швидкість. Бюджет UART FC (115200 бод ≈ 11.5КБ/с): RAW_IMU
# 50Гц ≈ 2КБ/с + ATTITUDE 25Гц ≈ 1КБ/с + решта телеметрії ≈ 1-1.5КБ/с.
IMU_STREAM_RATES = (
    ("ATTITUDE", config.NAV_ATTITUDE_RATE_HZ),
    ("RAW_IMU", config.NAV_RAW_IMU_RATE_HZ),
    ("SCALED_PRESSURE", config.NAV_PRESSURE_RATE_HZ),
    # Лише для сирого логу / офлайн-аналізу (в фільтри поки не йдуть):
    # друга/третя IMU, мотори, оберти ESC, вібрація. Разом з рядками вище
    # ≈ 6.4КБ/с — ~55% UART 115200. Якщо IMU/ESC-телеметрії на FC немає,
    # запит просто нічого не дає.
    ("SCALED_IMU2", config.NAV_AUX_IMU_RATE_HZ),
    ("SCALED_IMU3", config.NAV_AUX_IMU_RATE_HZ),
    ("SERVO_OUTPUT_RAW", config.NAV_MOTOR_RATE_HZ),
    ("ESC_TELEMETRY_1_TO_4", config.NAV_MOTOR_RATE_HZ),
    ("VIBRATION", config.NAV_VIBRATION_RATE_HZ),
)
IMU_STREAM_REQUEST_EVERY_SEC = 30.0
GPS_AZ_DIFF_THRE       = 20.0    # max відхилення азимуту від середнього (°)
GPS_STDEV_AZ_THRE      = 8.0     # max кружне σ азимуту (°)
GPS_STDEV_SPEED_THRE   = 15.0    # max σ швидкості (м/с)
GPS_STDEV_KOE          = 1.5     # поточна швидкість має бути в межах 1.5σ
FORECAST_SECONDS       = 5       # тривалість forecast вікна (сек)
FORECAST_HZ            = 5       # дискретизація forecast (точок/сек)

class MavLinkGPSHub:
    """
    Головна система управління GPS та MAVLink комунікацією.

    Координує діяльність:
    - Starlink worker потоку
    - Beitian GPS reader потоку
    - MAVLink GCS/FC комунікації
    - NMEA генерування та розповсюджування
    - [NEW] StarGPSHandler — статистичний фільтр + forecast
    """

    def __init__(self, starlink_service):
        self.running = False
        self.last_manual_coords: Optional[Dict[str, float]] = None
        self.last_manual_time: float = 0.0
        self.last_starlink_check = 0.0
        self.starlink_available = False
        self.last_starlink_data: Optional[Dict[str, Any]] = None
        self.last_beitian_nmea: Optional[str] = None
        self.last_manual_marker: Optional[Dict[str, float]] = None
        self.source_mode: str = "AUTO"
        self._seen_gcs_types = set()
        self._fc_heartbeat_seen = False
        self.uart_tx_ok = 0
        self.uart_tx_fail = 0
        self.manual_hold_sec = config.MANUAL_HOLD_SEC
        self.manual_sats = config.MANUAL_SATS
        self.manual_alt_max_delta_m = config.MANUAL_ALT_MAX_DELTA_M
        self.last_output_alt: Optional[float] = None
        self.starlink_filter_window = config.STARLINK_FILTER_WINDOW
        self.starlink_pos_jump_max_m = config.STARLINK_POS_JUMP_MAX_M
        self.starlink_alt_jump_max_m = config.STARLINK_ALT_JUMP_MAX_M
        self.starlink_poll_sec = config.STARLINK_POLL_SEC
        self.starlink_retry_sec = config.STARLINK_RETRY_SEC

        # --- [NEW] StarGPSHandler стан ---
        self._stargps: StarGPSHandler = StarGPSHandler(maxlen=GPS_QUEUE_SIZE)
        self.starlink_stable: bool = False
        self.starlink_spoofed: bool = False
        self._spoof_block_until: float = 0.0
        self._algo_warned: bool = False
        self._starlink_fail_count: int = 0
        self._starlink_stale_logged: bool = False
        self._forecast_list: List[GPSRecord] = []
        self._forecast_time: float = 0.0

        self.starlink_algo = config.STARLINK_ALGO
        self.starlink_max_speed_mps = config.STARLINK_MAX_SPEED_MPS
        # self.arc4_hyst = Arc4Hysteresis()
        self._starlink_samples = deque(maxlen=self.starlink_filter_window)

        # Наша інерційна навігація (НЕ EKF ArduPilot) — працює і без Starlink;
        # Starlink лише коригує aided-фільтр і дає звірку (inertial_nav.py).
        raw_log = None
        if config.INERTIA_RAW_LOG_DIR:
            raw_log = str(Path(config.INERTIA_RAW_LOG_DIR) /
                          f"nav_inertia_{datetime.datetime.now():%Y-%m-%d_%H-%M-%S}.csv")
        self._nav = InertialNavigator(NavConfig(
            accel_model=config.NAV_ACCEL_MODEL,
            drag_damping=config.NAV_DRAG_DAMPING,
            use_fixes=config.NAV_USE_STARLINK_FIXES,
            imu_stale_s=OUR_EKF_STALE_SEC,
            raw_log_path=raw_log,
        ), logger=logger)
        self._last_nav_status_log = 0.0
        self._unknown_stream_msgs = set()
        self._fc_target = None            # (system, component) FC — для запиту частот
        self._last_stream_request = 0.0
        self._last_integrity_warn = 0.0
        self._alt_outlier_streak = 0

        self.priority = gps_priority.GPSPriority(
            manual_timeout_sec=self.manual_hold_sec,
            talker=config.NMEA_TALKER
        )
        if config.SOURCE_MODE in ("AUTO", "STARLINK", "BEITIAN"):
            self.source_mode = config.SOURCE_MODE
        self.bridge = None
        self.telemetry_snapshot = TelemetrySnapshotPublisher() if TelemetrySnapshotPublisher else None

        self.starlink_thread = None
        self.beitian_thread = None
        self.mavlink_thread = None

        self.lock = threading.Lock()
        self.starlink_service = starlink_service
        self.is_moving = False

    def _store_manual_coords(self, msg_data: Dict[str, Any], raw_alt: float) -> Dict[str, float]:
        lat = msg_data.get("latitude")
        lon = msg_data.get("longitude")
        if lat is None or lon is None:
            raise ValueError("manual GPS command missing latitude/longitude")

        with self.lock:
            prev_alt = self.last_output_alt

        if prev_alt is not None and abs(raw_alt - prev_alt) > self.manual_alt_max_delta_m:
            manual_alt = prev_alt
            logger.warning(
                f"MANUAL altitude clamped: raw={raw_alt:.1f}m, prev={prev_alt:.1f}m"
            )
        elif raw_alt == 0.0 and prev_alt is not None:
            manual_alt = prev_alt
        else:
            manual_alt = raw_alt

        manual_coords = {
            "latitude": float(lat),
            "longitude": float(lon),
            "altitude": float(manual_alt),
            "sats": float(self.manual_sats),
        }
        with self.lock:
            self.last_manual_coords = manual_coords
            self.last_manual_marker = dict(manual_coords)
            self.last_manual_time = time.time()
        logger.info(f"Manual GPS встановилось: {manual_coords}")
        return manual_coords

    def _send_manual_status(self, manual_coords: Dict[str, float]) -> None:
        if not self.bridge:
            return
        self.bridge.send_statustext(
            f"GPS Origin Set: {manual_coords['latitude']:.6f}, "
            f"{manual_coords['longitude']:.6f} alt={manual_coords['altitude']:.1f}",
            severity=0
        )

    def connect_hardware(self) -> bool:
        try:
            uart_in_port = config.UART_GPS_PORT
            uart_in_baud = config.UART_GPS_BAUD
            uart_out_port = config.UART_FC_PORT
            uart_out_baud = config.UART_FC_BAUD

            self.bridge = mavlink_bridge.MAVLinkBridge()

            logger.info("Підключення до MAVLink (FC: 14551, GCS: 14550)...")
            mav_ok = self.bridge.connect_mavlink()
            if not mav_ok:
                logger.warning("MAVLink недоступний: працюємо в обмеженому режимі")

            logger.info(f"Підключення до UART IN (Beitian): {uart_in_port} @ {uart_in_baud} baud...")
            self.bridge.connect_uart_gps(uart_in_port, uart_in_baud)

            logger.info(f"Підключення до UART OUT (FC): {uart_out_port} @ {uart_out_baud} baud...")
            self.bridge.connect_uart_fc_output(uart_out_port, uart_out_baud)

            self.bridge.set_gcs_callback(self._handle_gcs_command)

            logger.info("Всі пристрої успішно підключена ✓")
            return True

        except Exception as e:
            logger.error(f"Помилка підключення до апаратури: {e}")
            return False

    def _handle_gcs_command(self, msg: Any):
        try:
            msg_type = msg.get_type()

            if msg_type == "SET_GPS_GLOBAL_ORIGIN":
                msg_data = self.bridge.handle_set_gps_global_origin(msg) if self.bridge else {}
                raw_alt = float(msg_data.get("altitude", 0.0) or 0.0)
                manual_coords = self._store_manual_coords(msg_data, raw_alt)
                self._send_manual_status(manual_coords)

            elif msg_type == "COMMAND_INT":
                command = int(getattr(msg, "command", -1))
                if command == 179:
                    lat_i = getattr(msg, "x", 0)
                    lon_i = getattr(msg, "y", 0)
                    alt = float(getattr(msg, "z", 0.0) or 0.0)
                    if lat_i and lon_i:
                        msg_data = {
                            "latitude": float(lat_i) / 1e7,
                            "longitude": float(lon_i) / 1e7,
                            "altitude": alt,
                        }
                        raw_alt = float(msg_data.get("altitude", 0.0) or 0.0)
                        manual_coords = self._store_manual_coords(msg_data, raw_alt)
                        self._send_manual_status(manual_coords)

            elif msg_type == "LED_CONTROL":
                msg_data = self.bridge.handle_led_control(msg) if self.bridge else {}
                source = msg_data.get("source", "beitian")
                logger.info(f"LED_CONTROL: GPS source → {source}")
                with self.lock:
                    if source == "starlink":
                        self.source_mode = "STARLINK"
                    elif source == "beitian":
                        self.source_mode = "BEITIAN"
                    manual_remaining = 0.0
                    if self.last_manual_time > 0:
                        manual_remaining = max(0.0, self.manual_hold_sec - (time.time() - self.last_manual_time))
                if self.bridge and source in ("starlink", "beitian"):
                    if manual_remaining > 0:
                        self.bridge.send_statustext(
                            f"GPS Mode: {self.source_mode} (MANUAL {manual_remaining:.0f}s)",
                            severity=6
                        )
                    else:
                        self.bridge.send_statustext(f"GPS Mode: {self.source_mode}", severity=6)

            elif msg_type == "COMMAND_LONG":
                command = getattr(msg, "command", None)
                p1 = getattr(msg, "param1", None)
                p5 = getattr(msg, "param5", None)
                p6 = getattr(msg, "param6", None)
                p7 = getattr(msg, "param7", None)
                if int(command or -1) == 179 and (p5 is not None) and (p6 is not None):
                    msg_data = {
                        "latitude": float(p5),
                        "longitude": float(p6),
                        "altitude": float(p7 or 0.0),
                    }
                    raw_alt = float(msg_data.get("altitude", 0.0) or 0.0)
                    manual_coords = self._store_manual_coords(msg_data, raw_alt)
                    self._send_manual_status(manual_coords)

        except Exception as e:
            logger.error(f"Помилка обробки команди GCS: {e}")

    # -----------------------------------------------------------------------
    # [NEW] StarGPSHandler: статистичний фільтр якості Starlink
    # -----------------------------------------------------------------------

    def _is_starlink_stable(self) -> bool:
        """
        Перевіряє якість Starlink GPS за допомогою обраного алгоритму фільтрації.
        """
        h = self._stargps
        now = time.time()

        # Spoof-детекція: великий стрибок між двома сусідніми точками блокує
        # Starlink на GPS_SPOOF_BLOCK_SEC; кожен новий стрибок перезаряджає
        # таймер, після спокійного таймауту блок знімається автоматично.
        if h.last_dist_diff > GPS_SPOOF_METERS_THRE and len(h) > 10:
            self.starlink_spoofed = True
            self._spoof_block_until = now + GPS_SPOOF_BLOCK_SEC
            logger.warning(
                f"[SPOOF] Starlink jump {h.last_dist_diff:.0f}m > {GPS_SPOOF_METERS_THRE:.0f}m — "
                f"блокую Starlink на {GPS_SPOOF_BLOCK_SEC:.0f}s"
            )

        if self.starlink_spoofed:
            if now < self._spoof_block_until:
                return False
            self.starlink_spoofed = False
            logger.info("[SPOOF] Стрибків не було впродовж таймауту — блок Starlink знято")

        algo = getattr(self, "starlink_algo", "ARC4")
        if algo == "NO_FILTER":
            return True

        if algo in ("ARC4", "CMAD"):
            # Реалізації ARC4/CMAD закоментовані. Раніше ці гілки «провалювались»
            # без return → None → Starlink назавжди лишався нестабільним.
            # Поки алгоритми не повернуто — поводимось як NO_FILTER.
            if not self._algo_warned:
                self._algo_warned = True
                logger.warning(
                    f"STARLINK_ALGO={algo} не реалізовано — фільтр стабільності вимкнено (як NO_FILTER)"
                )
            return True

        # LINEAR_STD (оригінальний алгоритм)
        if len(h) < 7:
            return True   # вікно щойно стартувало — довіряємо (історична поведінка)
        if len(h) < GPS_QUEUE_SIZE:
            return False

        last = h.latest()
        if last is None:
            return False

        std_spd = h.std_speed
        std_az  = h.std_azimuth
        avg_spd = h.avg_speed
        avg_az  = h.avg_azimuth

        if any(math.isnan(x) for x in (std_spd, std_az, avg_spd, avg_az)):
            return False

        ch1 = abs(last.speed - avg_spd) < GPS_STDEV_KOE * std_spd
        ch2 = std_spd < GPS_STDEV_SPEED_THRE

        if avg_spd < 0.5:
            ch3 = True
            ch4 = True
        else:
            ch3 = StarGPSHandler._bearing_difference_deg(last.az, avg_az) < GPS_AZ_DIFF_THRE
            ch4 = std_az < GPS_STDEV_AZ_THRE

        return ch1 and ch2 and ch3 and ch4

    # -----------------------------------------------------------------------
    # Starlink worker
    # -----------------------------------------------------------------------

    def starlink_worker(self):
        """
        Потік для опитування Starlink.

        Додатково до оригінального фільтру (moving average):
          - подає RAW точки в StarGPSHandler
          - обчислює стабільність та forecast
        """
        logger.info("Starlink worker запущен")

        session_ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        motion_log_path = f"/home/sirena/logs/starlink_motion_{session_ts}.jsonl"

        while self.running:
            try:
                now = time.time()
                current_interval = self.starlink_poll_sec if self.starlink_available else self.starlink_retry_sec

                if now - self.last_starlink_check >= current_interval:
                    self.last_starlink_check = now

                    try:
                        # location = starlink.get_location()
                        location = starlink.starlink_client.get_location()

                        self.is_moving = self._stargps.kalman_awake

                        # get_status() повертає None при збої (той самий контракт,
                        # що get_location()) — раніше тут беззастережно читались
                        # атрибути status.gps_stats.* на можливому None/dict-
                        # заглушці, і справжня причина збою (напр. gRPC Deadline
                        # Exceeded — dish не відповідає) губилась за
                        # незрозумілим AttributeError. Так само location
                        # перевіряємо перед .get(), а не лише нижче — обидва
                        # виклики можуть провалитись одночасно (той самий
                        # недоступний dish).
                        status = starlink.starlink_client.get_status()
                        if status is not None:
                            data = {
                                "pnt_filter": str(status.gps_stats.pnt_filter_convergence_state),
                                "downlink_bps": status.downlink_throughput_bps,
                                "uplink_bps": status.uplink_throughput_bps,
                                "ping_ms": status.pop_ping_latency_ms,
                                "obstruction": status.obstruction_stats.fraction_obstructed,
                                "tilt_deg": status.alignment_stats.tilt_angle_deg,
                                "azimuth_deg": status.alignment_stats.boresight_azimuth_deg,
                                "elevation_deg": status.alignment_stats.boresight_elevation_deg,
                                "lat": location.get("latitude") if location else None,
                                "lon": location.get("longitude") if location else None,
                                "alt": location.get("altitude") if location else None,
                                "quaternion": {
                                    "scalar": status.ned2dish_quaternion.q_scalar,
                                    "x": status.ned2dish_quaternion.q_x,
                                    "y": status.ned2dish_quaternion.q_y,
                                    "z": status.ned2dish_quaternion.q_z,
                                }
                            }

                            with open(motion_log_path, "a") as f:
                                f.write(json.dumps(data) + "\n")
                            # орієнтація тарілки — у сирий лог інерції (перевірка курсу)
                            q = data["quaternion"]
                            self._nav.log_raw("DISH", None, q["scalar"], q["x"], q["y"], q["z"],
                                              data["tilt_deg"], data["azimuth_deg"], data["elevation_deg"])

                        if location and location.get("available"):
                            # Ковзне середнє + відсічення викидів + інерційне
                            # докочування на викидах — увімкнено 2026-09-28,
                            # див. navigation_module/gps_smoothing_eval.
                            filtered = self._filter_starlink_location(location)
                            with self.lock:
                                self.starlink_available = True
                                self.last_starlink_data = location
                                self._starlink_fail_count = 0

                            #self.bridge.send_gps_input(lat=location["latitude"], lon=location["longitude"], alt=filtered.get('altitude', 0.0), sats=12)

                            logger.info(
                                f"Starlink OK(raw→flt): "
                                f"{location['latitude']:.6f},{location['longitude']:.6f},{float(location.get('altitude', 0.0)):.1f}m"
                                f" → "
                                f"{filtered['latitude']:.6f},{filtered['longitude']:.6f},{float(filtered.get('altitude', 0.0)):.1f}m "
                                f"(sats={filtered.get('gps_sats', '?')}, win={len(self._starlink_samples)})"
                            )

                            # 2. [NEW] Подаємо RAW точку в StarGPSHandler
                            if location.get("latitude") is not None:
                                rec = GPSRecord(
                                    timestamp=datetime.datetime.now(datetime.UTC),
                                    lat=float(location["latitude"]),
                                    lon=float(location["longitude"]),
                                    alt=float(location.get("altitude", 0.0)),
                                )
                                with self.lock:
                                    self._stargps.append(rec)
                                    stable = self._is_starlink_stable()
                                    self.starlink_stable = stable

                                    if stable:
                                        # Оновлюємо forecast з поточної позиції
                                        last = self._stargps.latest()
                                        spd  = self._stargps.span_speed
                                        az   = self._stargps.span_azimuth
                                        if not math.isnan(spd) and not math.isnan(az) and spd > 0.1:
                                            self._forecast_list = self._stargps.forecast_track(
                                                last.lat, last.lon, az, spd,
                                                FORECAST_SECONDS, FORECAST_HZ
                                            )
                                            self._forecast_time = time.time()

                                logger.debug(
                                    f"StarGPS: stable={stable} "
                                    f"spd={self._stargps.avg_speed:.1f}±{self._stargps.std_speed:.1f} m/s "
                                    f"az={self._stargps.avg_azimuth:.1f}±{self._stargps.std_azimuth:.1f}° "
                                    f"len={len(self._stargps)}"
                                )

                        else:
                            with open(motion_log_path, "a") as f:
                                f.write(f"Starlink worker помилка: location unavailable\n")
                            reason = "location unavailable" if status is not None else "status and location unavailable"
                            self._register_starlink_failure(reason)

                    except Exception as e:
                        with open(motion_log_path, "a") as f:
                            f.write(f"Starlink worker помилка: {str(e)}\n")
                        self._register_starlink_failure(str(e))

                time.sleep(1.0)

            except Exception as e:
                with open(motion_log_path, "a") as f:
                    f.write(f"Starlink worker помилка: {e}\n")
                logger.error(f"Starlink worker помилка: {e}")
                time.sleep(5.0)

    def _register_starlink_failure(self, reason: str) -> None:
        """Один збій gRPC не вимикає Starlink: перемикаємось на fallback лише
        після STARLINK_FAIL_THRESHOLD фейлів поспіль (до того тримаємо останні
        дані — це секунди, а не хвилина RETRY-блекауту)."""
        with self.lock:
            self._starlink_fail_count += 1
            fails = self._starlink_fail_count
            if fails >= STARLINK_FAIL_THRESHOLD:
                self.starlink_available = False
        if fails >= STARLINK_FAIL_THRESHOLD:
            logger.warning(f"Starlink недоступний (збій #{fails}): {reason} — джерело вимкнено")
        else:
            logger.warning(f"Starlink збій #{fails}/{STARLINK_FAIL_THRESHOLD}: {reason}")

    def _approx_distance_m(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        dlat_m = (lat2 - lat1) * 111320.0
        dlon_m = (lon2 - lon1) * 111320.0
        return (dlat_m * dlat_m + dlon_m * dlon_m) ** 0.5

    def _filter_starlink_location(self, location: Dict[str, Any]) -> Dict[str, Any]:
        """
        Starlink-точка як ОПЦІЙНИЙ абсолютний фікс для нашої інерції
        (InertialNavigator.aided); чиста інерція (без корекцій) рахується
        незалежно й лише звіряється зі Starlink.

        2026-09-30: замінено схему "ковзне середнє 15 точок + скид EKF на
        кожному фіксі + докочування anchor += ekf.position". Причини
        (vision_module/inertia/starlink_eval.py, польоти 26.09.2026):
          - ковзне середнє 15 точок відстає на ~7с (≈70м на 10м/с) — навіть
            "стояти на місці" було точніше (2с провалу: 42м проти 10м);
          - на СЕРІЇ викидів anchor += ekf.position додавав зміщення з
            моменту скиду повторно (подвійний облік, за 30с — до 2.5км);
          - тепер: Starlink — Kalman-оновлення з χ²-гейтом (поодинокі
            стрибки відкидаються, серія ≥3 — перезахоп), на викиді/провалі
            вихід = прогноз того самого фільтра (без подвійного обліку);
            2с провалу — ~4м, 10с — ~25м.
        Висота: середнє по вікну прийнятих точок (висота Starlink дуже
        шумна, у фільтр іде лише горизонталь); STARLINK_ALT_JUMP_MAX_M
        відсіює точку лише з усереднення висоти. Грубий горизонтальний
        поріг (STARLINK_POS_JUMP_MAX_M + STARLINK_MAX_SPEED_MPS·вік фіксу)
        лишився запобіжником поверх гейта — діє лише поки останній
        прийнятий фікс свіжий, тож заблокувати фільтр назавжди не може.
        """
        try:
            lat  = float(location.get("latitude"))
            lon  = float(location.get("longitude"))
            alt  = float(location.get("altitude", 0.0))
            sats = int(location.get("gps_sats", 0))
            now_t = time.time()

            if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
                raise ValueError("invalid lat/lon")
            if alt < -200.0 or alt > 10000.0:
                raise ValueError("invalid altitude")

            # Грубий запобіжник поверх χ²-гейта: фізично неможливий стрибок
            # відносно поточної оцінки фільтра (а не відносно сирої
            # попередньої точки — та сама могла бути викидом).
            est = self._nav.estimate(now_t)
            coarse_outlier = False
            if (est is not None and est["fix_age_s"] is not None
                    and est["fix_age_s"] < 2.0 * OUR_EKF_STALE_SEC and est["aided"]["latitude"] is not None):
                dist_m = self._approx_distance_m(est["aided"]["latitude"], est["aided"]["longitude"], lat, lon)
                if dist_m > self.starlink_pos_jump_max_m + self.starlink_max_speed_mps * est["fix_age_s"]:
                    coarse_outlier = True
            # Висота Starlink дуже шумна (на польотах 26.09 — розмах до 240м) і
            # у фільтр не йде: стрибок висоти лише виключає точку з
            # усереднення висоти, а НЕ відкидає горизонтальний фікс (інакше
            # стійкий зсув висоти заблокував би всі фікси назавжди).
            alt_outlier = False
            if self._starlink_samples:
                alt_ref = sum(float(p.get("altitude", 0.0)) for p in self._starlink_samples) / len(self._starlink_samples)
                alt_outlier = abs(alt - alt_ref) > self.starlink_alt_jump_max_m

            if coarse_outlier:
                result = {"accepted": False, "fresh": True, "integrity": None, "nis": None, "pure_error_m": None}
                logger.warning(f"Starlink outlier (грубий поріг): {lat:.6f},{lon:.6f} alt={alt:.1f} — докочуюсь інерційно")
            else:
                result = self._nav.on_fix(lat, lon, wall=now_t)
                if result["fresh"] and not result["accepted"]:
                    logger.warning(f"Starlink outlier (χ²-гейт, NIS={result['nis']:.1f}) — докочуюсь інерційно")

            if result["accepted"] and not alt_outlier:
                self._starlink_samples.append({"latitude": lat, "longitude": lon,
                                                "altitude": alt, "gps_sats": sats, "time": now_t})
            elif result["accepted"] and alt_outlier:
                self._alt_outlier_streak += 1
                if self._alt_outlier_streak >= self.starlink_filter_window:
                    # висота стабільно "переїхала" — приймаємо новий рівень
                    self._starlink_samples.clear()
                    self._starlink_samples.append({"latitude": lat, "longitude": lon,
                                                    "altitude": alt, "gps_sats": sats, "time": now_t})
            if not alt_outlier:
                self._alt_outlier_streak = 0
            integ = result.get("integrity")
            if integ is not None and integ.any_alarm and now_t - self._last_integrity_warn > 10.0:
                self._last_integrity_warn = now_t
                logger.warning(f"Starlink integrity: {integ} — координатам Starlink зараз не довіряти сліпо")

            if result.get("pure_error_m") is not None and now_t - self._last_nav_status_log > 10.0:
                self._last_nav_status_log = now_t
                logger.info(f"Інерція: чиста (без корекцій) vs Starlink — {result['pure_error_m']:.1f}м")

            est = self._nav.estimate(now_t)
            if est is None or est["aided"]["latitude"] is None or not est["imu_alive"]:
                # інерція ще не стартувала / немає IMU — віддаємо сиру точку
                return location

            if self._starlink_samples:
                n = len(self._starlink_samples)
                alt_out = sum(float(p.get("altitude", 0.0)) for p in self._starlink_samples) / n
                sats_out = int(round(sum(int(p.get("gps_sats", 0)) for p in self._starlink_samples) / n))
            else:
                alt_out, sats_out = alt, sats
            a = est["aided"]
            return {"latitude": a["latitude"], "longitude": a["longitude"],
                    "altitude": alt_out, "gps_sats": sats_out, "available": True,
                    "vn": a["vn"], "ve": a["ve"], "horiz_accuracy_m": a["horiz_accuracy_m"],
                    "accepted": result["accepted"],
                    "pure_inertia": est["pure"]}

        except Exception as e:
            logger.warning(f"Starlink filter fallback: {e}")
            return self.last_starlink_data or location

    def _request_imu_streams(self) -> None:
        """MAV_CMD_SET_MESSAGE_INTERVAL для ATTITUDE/RAW_IMU/SCALED_PRESSURE.
        Повторюється раз на IMU_STREAM_REQUEST_EVERY_SEC: інші клієнти
        (telemetry_daemon) при своєму старті теж шлють інтервали на той
        самий порт FC — останній запит перемагає."""
        if not (self.bridge and self.bridge.mav_fc and self._fc_target):
            return
        now = time.time()
        if now - self._last_stream_request < IMU_STREAM_REQUEST_EVERY_SEC:
            return
        self._last_stream_request = now
        sysid, compid = self._fc_target
        for name, rate_hz in IMU_STREAM_RATES:
            if not rate_hz or rate_hz <= 0:
                continue
            msg_id = getattr(mavutil.mavlink, f"MAVLINK_MSG_ID_{name}", None)
            if msg_id is None:
                if name not in self._unknown_stream_msgs:
                    self._unknown_stream_msgs.add(name)
                    logger.info(f"{name} невідоме цій версії pymavlink — не запитую")
                continue
            try:
                self.bridge.mav_fc.mav.command_long_send(
                    sysid, compid, mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL, 0,
                    msg_id, int(1_000_000 / rate_hz), 0, 0, 0, 0, 0)
            except Exception as e:
                logger.warning(f"Запит частоти {name} не вдався: {e}")

    # -----------------------------------------------------------------------
    # Beitian worker / Any standart GPS should here implemented
    # -----------------------------------------------------------------------

    def beitian_worker(self):
        logger.info("Beitian worker запущен")
        while self.running:
            try:
                if not self.bridge:
                    time.sleep(0.1)
                    continue
                nmea = self.bridge.read_uart_nmea()
                if nmea:
                    # Зберігаємо тільки GGA або RMC — інші типи (GSA, GSV, VTG...)
                    # не парсяться і викликають флап BEITIAN→NONE
                    if 'GGA' in nmea or 'RMC' in nmea:
                        with self.lock:
                            self.last_beitian_nmea = nmea
                        # незалежний GNSS-еталон у сирий лог інерції (той самий годинник)
                        self._nav.log_nmea(nmea)
                else:
                    time.sleep(0.01)
            except Exception as e:
                logger.error(f"Beitian worker помилка: {e}")
                time.sleep(1.0)

    # -----------------------------------------------------------------------
    # MAVLink proxy worker
    # -----------------------------------------------------------------------

    def mavlink_proxy_worker(self):
        logger.info("MAVLink proxy worker запущен (high-rate)")

        last_gps_send_ts = 0.0
        gps_send_interval = 0.5  # Частота відправки — 2 Гц

        while self.running:
            try:
                if not self.bridge:
                    time.sleep(0.01)
                    continue

                processed = 0

                for _ in range(100):
                    msg_gcs = self.bridge.recv_from_gcs()
                    if not msg_gcs:
                        break
                    processed += 1
                    m_type = msg_gcs.get_type()
                    if m_type not in self._seen_gcs_types:
                        self._seen_gcs_types.add(m_type)
                        logger.info(f"GCS MAVLink msg detected: {m_type}")

                    drop = m_type in ("SET_GPS_GLOBAL_ORIGIN", "LED_CONTROL")
                    if m_type == "COMMAND_LONG":
                        if int(getattr(msg_gcs, "command", -1)) == 179:
                            drop = True
                    elif m_type == "COMMAND_INT":
                        if int(getattr(msg_gcs, "command", -1)) == 179:
                            drop = True
                    if not drop:
                        self.bridge.send_to_fc(msg_gcs)

                for _ in range(200):
                    msg_fc = self.bridge.recv_from_fc()
                    if not msg_fc:
                        break
                    processed += 1
                    # HEARTBEAT самого автопілота (компонент 1, не GCS і не
                    # компаньйон — через роутер сюди приходять і їхні).
                    is_autopilot_hb = (
                        msg_fc.get_type() == "HEARTBEAT"
                        and msg_fc.get_srcComponent() == 1
                        and getattr(msg_fc, "type", None) != 6          # MAV_TYPE_GCS
                        and getattr(msg_fc, "autopilot", None) != 8     # MAV_AUTOPILOT_INVALID
                    )
                    if is_autopilot_hb:
                        # стан ARM -> інерція тримає швидкість 0 на землі
                        self._nav.on_heartbeat(msg_fc.base_mode, msg_fc.custom_mode, msg_fc.system_status)
                    if (not self._fc_heartbeat_seen) and is_autopilot_hb:
                        self._fc_heartbeat_seen = True
                        self._fc_target = (msg_fc.get_srcSystem(), msg_fc.get_srcComponent())
                        logger.info("FC HEARTBEAT detected and forwarded to GCS")
                    if self.telemetry_snapshot:
                        self.telemetry_snapshot.update_from_msg(msg_fc)
                    fc_msg_type = msg_fc.get_type()
                    if fc_msg_type == "ATTITUDE":
                        self._nav.on_attitude(msg_fc.time_boot_ms, msg_fc.roll, msg_fc.pitch, msg_fc.yaw,
                                                 msg_fc.rollspeed, msg_fc.pitchspeed, msg_fc.yawspeed)
                    elif fc_msg_type == "RAW_IMU":
                        self._nav.on_raw_imu(msg_fc.time_usec, msg_fc.xacc, msg_fc.yacc, msg_fc.zacc,
                                             xgyro=msg_fc.xgyro, ygyro=msg_fc.ygyro, zgyro=msg_fc.zgyro,
                                             xmag=msg_fc.xmag, ymag=msg_fc.ymag, zmag=msg_fc.zmag)
                    elif fc_msg_type in ("SCALED_IMU2", "SCALED_IMU3"):
                        self._nav.log_raw("IMU2" if fc_msg_type == "SCALED_IMU2" else "IMU3",
                                          msg_fc.time_boot_ms / 1000.0,
                                          msg_fc.xacc, msg_fc.yacc, msg_fc.zacc,
                                          msg_fc.xgyro, msg_fc.ygyro, msg_fc.zgyro,
                                          msg_fc.xmag, msg_fc.ymag, msg_fc.zmag)
                    elif fc_msg_type == "SERVO_OUTPUT_RAW":
                        self._nav.log_raw("SRV", msg_fc.time_usec / 1e6,
                                          *[getattr(msg_fc, f"servo{i}_raw") for i in range(1, 9)])
                    elif fc_msg_type == "ESC_TELEMETRY_1_TO_4":
                        self._nav.log_raw("ESC", None, *list(msg_fc.rpm), *list(msg_fc.current),
                                          *list(msg_fc.voltage))
                    elif fc_msg_type == "VIBRATION":
                        self._nav.log_raw("VIB", msg_fc.time_usec / 1e6,
                                          msg_fc.vibration_x, msg_fc.vibration_y, msg_fc.vibration_z,
                                          msg_fc.clipping_0, msg_fc.clipping_1, msg_fc.clipping_2)
                    elif fc_msg_type == "SCALED_PRESSURE":
                        self._nav.on_pressure(msg_fc.time_boot_ms, msg_fc.press_abs)
                    elif fc_msg_type == "GLOBAL_POSITION_INT":
                        # relative_alt — лише запасний вертикальний канал (вихід
                        # EKF3), якщо SCALED_PRESSURE не надходить; lat/lon —
                        # лише в сирий лог як еталон для офлайн-звірки.
                        self._nav.on_relative_alt(msg_fc.relative_alt / 1000.0)
                        self._nav.log_fc_position(msg_fc.lat / 1e7, msg_fc.lon / 1e7,
                                                     msg_fc.vx / 100.0, msg_fc.vy / 100.0)
                    self.bridge.send_to_gcs(msg_fc)

                    # === ТОЧКОВА ІН'ЄКЦІЯ STARLINK ===
                    now = time.time()
                    if now - last_gps_send_ts >= gps_send_interval:
                        last_gps_send_ts = now
                        if self._stargps and self._stargps._data:
                            last_rec = self._stargps._data[-1]
                            age = (datetime.datetime.now(datetime.UTC) - last_rec.timestamp).total_seconds()
                            if age <= STARLINK_STALE_SEC:
                                self.send_starlink_to_server(last_rec)
                                logger.info(f"Starlink telemetry sent to server: {last_rec.lat}, {last_rec.lon}")
                                self._starlink_stale_logged = False
                            elif not self._starlink_stale_logged:
                                logger.warning(
                                    f"Starlink дані застаріли ({age:.1f}s, поріг {STARLINK_STALE_SEC}s) — "
                                    "припиняю слати в адмінку той самий запис"
                                )
                                self._starlink_stale_logged = True

                self._request_imu_streams()
                now_s = time.time()
                if now_s - self._last_nav_status_log > 30.0:
                    est = self._nav.estimate(now_s)
                    if est is not None:
                        self._last_nav_status_log = now_s
                        pe = est["pure"]
                        logger.info(f"Інерція (чиста): N={pe['north_m']:.0f}м E={pe['east_m']:.0f}м "
                                    f"v=({pe['vn']:.1f},{pe['ve']:.1f})м/с imu_alive={est['imu_alive']}")
                if processed == 0:
                    self._nav.flush()
                    time.sleep(0.002)

            except Exception as e:
                logger.warning(f"MAVLink proxy worker помилка: {e}")
                time.sleep(0.01)
    def send_starlink_to_server(self, last_rec):
        """Пряма відправка стандартною командою pymavlink.

        2026-09-28: раніше йшло через self.bridge.mav_gcs (udpin:0.0.0.0:14550) —
        порт, на який у mav-router.conf НЕМАЄ жодного endpoint'у, тож
        mav_gcs ніколи не отримував жодного пакета і не знав, куди слати
        (pymavlink на udpin шле лише туди, звідки вже щось отримав).
        Результат: send() виконувався без помилок, але дані нікуди не
        долітали — жодного NAMED_VALUE_FLOAT ніколи не було в БД адмінки.
        mav_fc (udpin:0.0.0.0:14553) — той самий сокет, яким уже успішно
        користується send_gps_input(): mavlink-router сам активно штовхає
        туди пакети (endpoint "navigation_service"), тож зворотна адреса
        відома, і відправлене звідси mavlink-router розносить по всій
        mesh-мережі (включно з telemetry_publisher/14562, звідки це вже
        підхоплює telemetry-sender)."""
        if self.bridge and self.bridge.mav_fc:
            ts = int(time.time() * 1000) & 0xffffffff

            self.bridge.mav_fc.mav.named_value_float_send(
                ts,
                b'STRLNK_LAT',  # Передаємо явно як байти
                float(last_rec.lat)
            )
            self.bridge.mav_fc.mav.named_value_float_send(
                ts,
                b'STRLNK_LON',
                float(last_rec.lon)
            )
    # -----------------------------------------------------------------------
    # Main loop
    # -----------------------------------------------------------------------

    def main_loop(self):
        """
        Головний цикл: вибір GPS, формування та відправка NMEA.

        TODO: REFACTOR:
          This is the legacy NMEA GPS hub flow. Keep for fallback/testing, but
          migrate Starlink GPS injection to MAVLink GPS_INPUT so navigation
          does not need a separate NMEA UART output to FC.

        Розширений пріоритет джерел:
          Manual(60s) > Starlink(stable) > Forecast(5s) > Beitian
        """
        logger.info("Головний цикл запущен (5 Hz)")

        loop_count    = 0
        last_adsb_time = time.time()
        interval = config.MAIN_LOOP_INTERVAL_SEC
        print_every = config.PRINT_EVERY

        while self.running:
            try:
                loop_start = time.time()

                # Копіюємо поточний стан потокозахисно
                with self.lock:
                    manual_coords   = self.last_manual_coords
                    manual_time     = self.last_manual_time
                    starlink_avail  = self.starlink_available
                    starlink_data   = self.last_starlink_data
                    beitian_nmea    = self.last_beitian_nmea
                    manual_marker   = self.last_manual_marker
                    source_mode     = self.source_mode
                    starlink_stable = self.starlink_stable
                    forecast_list   = list(self._forecast_list)
                    forecast_time   = self._forecast_time

                source_was_forecast = False

                # -------------------------------------------------------
                # [NEW] Визначаємо ефективне Starlink джерело:
                #
                #   BEITIAN mode → starlink вимкнений (як раніше)
                #   AUTO/STARLINK + starlink_stable  → використовуємо filtered data
                #   AUTO/STARLINK + !starlink_stable → пробуємо forecast (5 сек)
                #   forecast прострочений            → fallback на Beitian
                # -------------------------------------------------------
                if source_mode == "BEITIAN":
                    effective_starlink_avail = False

                elif starlink_avail and not starlink_stable:
                    # Starlink є, але якість не пройшла статистичний фільтр
                    forecast_age = time.time() - forecast_time
                    if (forecast_list
                            and forecast_time > 0
                            and forecast_age < FORECAST_SECONDS):
                        # Вибираємо точку прогнозу відповідно до часу
                        idx = min(int(forecast_age * FORECAST_HZ), len(forecast_list) - 1)
                        fp  = forecast_list[idx]
                        # Підміняємо starlink_data синтетичними координатами
                        starlink_data = {
                            "latitude":  fp.lat,
                            "longitude": fp.lon,
                            "altitude":  float(starlink_data.get("altitude", 0.0))
                                         if starlink_data else 0.0,
                            "gps_sats":  12,
                        }
                        effective_starlink_avail = True
                        source_was_forecast      = True
                    else:
                        # Forecast прострочений — переходимо на Beitian
                        effective_starlink_avail = False

                else:
                    effective_starlink_avail = starlink_avail

                # Вибираємо GPS джерело через оригінальний GPSPriority
                gps_data, source, sats = self.priority.select_source(
                    manual_coords=manual_coords,
                    manual_time=manual_time,
                    starlink_available=effective_starlink_avail,
                    starlink_data=starlink_data,
                    beitian_nmea=beitian_nmea
                )

                if gps_data:
                    self.last_output_alt = float(gps_data.get("altitude", 0.0))

                    if self.is_moving:
                        print("Рухаємось — можна брати координати")
                    else:
                        print("Стоїмо — координати ненадійні")

                    if self.bridge and gps_data:
                        self.bridge.send_gps_input(lat=gps_data["latitude"], lon=gps_data["longitude"], alt=gps_data.get("altitude", 0.0), sats=sats )

                    # Логування — показуємо FORECAST замість STARLINK якщо forecast
                    loop_count += 1
                    if loop_count % print_every == 0:
                        src_label = "FORECAST" if (source_was_forecast and source == "STARLINK") else source
                        logger.info(
                            f"NMEA [{src_label:8s}] "
                            f"Mode={source_mode:8s} "
                            f"Lat={gps_data['latitude']:8.5f} "
                            f"Lon={gps_data['longitude']:8.5f} "
                            f"Alt={gps_data.get('altitude', 0):6.1f}m "
                            f"Sats={sats:2d} "
                            f"stable={starlink_stable} "
                            f"UART_TX(ok={self.uart_tx_ok}, fail={self.uart_tx_fail})"
                        )

                # Відправляємо ADS-B оновлення (1 Hz)
                now = time.time()
                if now - last_adsb_time >= 1.0 and self.bridge:
                    last_adsb_time = now
                    try:
                        if starlink_data and starlink_data.get("latitude") is not None:
                            self.bridge.send_adsb_vehicle(
                                icao=111,
                                lat=starlink_data["latitude"],
                                lon=starlink_data["longitude"],
                                alt_m=float(starlink_data.get("altitude", 0.0)),
                                name="STARLINK"
                            )
                        beitian_parsed, _ = self.priority._parse_beitian_nmea(beitian_nmea or "")
                        if beitian_parsed:
                            self.bridge.send_adsb_vehicle(
                                icao=222,
                                lat=beitian_parsed["latitude"],
                                lon=beitian_parsed["longitude"],
                                alt_m=float(beitian_parsed.get("altitude", 0.0)),
                                name="GPS-RAW"
                            )
                        if manual_marker:
                            self.bridge.send_adsb_vehicle(
                                icao=333,
                                lat=manual_marker["latitude"],
                                lon=manual_marker["longitude"],
                                alt_m=float(manual_marker.get("altitude", 0.0)),
                                name="MANUAL"
                            )
                        if gps_data:
                            src_name = "FORECAST" if source_was_forecast else source
                            self.bridge.send_adsb_vehicle(
                                icao=444,
                                lat=gps_data["latitude"],
                                lon=gps_data["longitude"],
                                alt_m=float(gps_data.get("altitude", 0.0)),
                                name=f"GPS-{src_name}"
                            )
                    except Exception as e:
                        logger.debug(f"ADS-B error: {e}")

                elapsed    = time.time() - loop_start
                sleep_time = interval - elapsed
                if sleep_time > 0:
                    time.sleep(sleep_time)

            except KeyboardInterrupt:
                break
            except Exception as e:
                logger.error(f"Main loop помилка: {e}")
                time.sleep(0.1)

    # -----------------------------------------------------------------------
    # Run / Shutdown
    # -----------------------------------------------------------------------

    def start(self):
        logger.info("=" * 60)
        logger.info("MavLink GPS Hub — Enhanced (Sirena + satellite-gps-)")
        logger.info("=" * 60)

        if not self.connect_hardware():
            logger.error("Не вдалось підключитись до апаратури!")
            return False

        self.running = True

        self.starlink_thread = threading.Thread(
            target=self.starlink_worker, daemon=True, name="StarLinkWorker"
        )
        self.starlink_thread.start()

        self.beitian_thread = threading.Thread(
            target=self.beitian_worker, daemon=True, name="BeitianWorker"
        )
        self.beitian_thread.start()

        self.mavlink_thread = threading.Thread(
            target=self.mavlink_proxy_worker, daemon=True, name="MavlinkProxyWorker"
        )
        self.mavlink_thread.start()

        logger.info("Робочі потоки запущені")

        try:
            self.main_loop()
        except KeyboardInterrupt:
            logger.info("Прийнято Ctrl+C")
        finally:
            self.stop()

    def stop(self):
        logger.info("Завершення роботи...")
        self.running = False

        if self.starlink_thread:
            self.starlink_thread.join(timeout=2.0)
        if self.beitian_thread:
            self.beitian_thread.join(timeout=2.0)
        if self.mavlink_thread:
            self.mavlink_thread.join(timeout=2.0)

        if self.bridge:
            try:
                self.bridge.disconnect()
            except Exception as e:
                logger.error(f"Помилка закриття MAVLink: {e}")

        logger.info("Система зупинена.")

def main() -> None:
    core = MavLinkGPSHub(starlink)

    def shutdown(signum, frame):
        del frame
        logger.info(f"Отримано сигнал {signum}, завершую...")
        core.stop()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        core.start()
    finally:
        core.stop()


if __name__ == "__main__":
    main()
