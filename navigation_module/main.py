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

# Локальні модулі (оригінальний Sirena)
import starlink
import mavlink_bridge
import gps_priority
import config
try:
    from telemetry_snapshot import TelemetrySnapshotPublisher
except ImportError:
    TelemetrySnapshotPublisher = None

# StarGPSHandler з satellite-gps-
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
# from stargps_handler import StarGPSHandler, GPSRecord, Arc4Hysteresis, run_cmad
from stargps_handler import StarGPSHandler, GPSRecord
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
        self._forecast_list: List[GPSRecord] = []
        self._forecast_time: float = 0.0

        self.starlink_algo = config.STARLINK_ALGO
        self.starlink_max_speed_mps = config.STARLINK_MAX_SPEED_MPS
        # self.arc4_hyst = Arc4Hysteresis()
        self._starlink_samples = deque(maxlen=self.starlink_filter_window)

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
        
        # Spoof-детекція: великий стрибок між двома сусідніми точками
        if h.last_dist_diff > GPS_SPOOF_METERS_THRE and len(h) > 10:
            self.starlink_spoofed = not self.starlink_spoofed
            logger.warning(
                f"[SPOOF] Starlink jump {h.last_dist_diff:.0f}m > {GPS_SPOOF_METERS_THRE:.0f}m, "
                f"spoofed={self.starlink_spoofed}"
            )
        
        if self.starlink_spoofed:
            return False

        algo = getattr(self, "starlink_algo", "ARC4")
        if algo == "NO_FILTER":
            return True

        # Для CMAD та ARC4 використовуємо оптимальне вікно (останні 7 азимутів)
        # для забезпечення наднизької затримки (low-latency) позиціонування
        window_size = 7
        if len(h) < window_size:
            return False

        last = h.latest()
        if last is None:
            return False

        # Збираємо останні азимути для розрахунків
        az_window = [h[i].az for i in range(len(h) - window_size, len(h))]
        speed = last.speed

        # bypass для статики на землі
        is_static = speed < 0.5

        if algo == "ARC4":
            pass
            # if is_static:
            #     return True
            # metric, is_stable = self.arc4_hyst.update(az_window, speed)
            # return is_stable

        elif algo == "CMAD":
            pass
            # if is_static:
            #     return True
            # metric, is_stable = run_cmad(az_window, thr=8.0)
            # return is_stable

        else:
            # LINEAR_STD (оригінальний алгоритм)
            if len(h) < GPS_QUEUE_SIZE:
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

        while self.running:
            try:
                now = time.time()
                current_interval = self.starlink_poll_sec if self.starlink_available else self.starlink_retry_sec

                if now - self.last_starlink_check >= current_interval:
                    self.last_starlink_check = now

                    try:
                        location = starlink.get_location()

                        if location and location.get("available"):
                            # 1. Оригінальний фільтр (moving average + outlier rejection)
                            # filtered = self._filter_starlink_location(location)
                            filtered = location
                            with self.lock:
                                self.starlink_available = True
                                self.last_starlink_data = location

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
                                    timestamp=datetime.datetime.utcnow(),
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
                            with self.lock:
                                self.starlink_available = False
                            logger.warning("Starlink недоступен")

                    except Exception as e:
                        with self.lock:
                            self.starlink_available = False
                        logger.error(f"Помилка Starlink: {e}")

                time.sleep(1.0)

            except Exception as e:
                logger.error(f"Starlink worker помилка: {e}")
                time.sleep(5.0)

    def _approx_distance_m(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        dlat_m = (lat2 - lat1) * 111320.0
        dlon_m = (lon2 - lon1) * 111320.0
        return (dlat_m * dlat_m + dlon_m * dlon_m) ** 0.5

    def _filter_starlink_location(self, location: Dict[str, Any]) -> Dict[str, Any]:
        """
        Згладжує координати Starlink ковзним середнім та відсікає викиди за відстанню та швидкістю.
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

            if len(self._starlink_samples) > 0:
                prev   = self._starlink_samples[-1]
                dist_m = self._approx_distance_m(prev["latitude"], prev["longitude"], lat, lon)
                d_alt  = abs(alt - float(prev.get("altitude", 0.0)))
                
                # Розрахунок реального dt та швидкості
                prev_t = prev.get("time", now_t - 0.08) # за замовчуванням 0.08с (~12 Гц)
                dt = now_t - prev_t
                speed_mps = dist_m / dt if dt > 0.0001 else 0.0
                
                # Якщо точки затримались або відсутні більше 2.0 секунд, скидаємо фільтр відстані
                # для уникнення зависання (Filter Lock) під час швидкого польоту
                if dt > 2.0:
                    logger.warning(
                        f"Starlink filter timeout reset: dt={dt:.2f}s. Clearing queue to prevent Filter Lock."
                    )
                    self._starlink_samples.clear()
                    self._starlink_samples.append({"latitude": lat, "longitude": lon,
                                                    "altitude": alt, "gps_sats": sats, "time": now_t})
                # Фільтрація
                elif (dist_m > self.starlink_pos_jump_max_m 
                        or d_alt > self.starlink_alt_jump_max_m 
                        or speed_mps > self.starlink_max_speed_mps):
                    logger.warning(
                        f"Starlink outlier rejected: dist={dist_m:.1f}m, speed={speed_mps:.1f}m/s (dt={dt:.3f}s), dAlt={d_alt:.1f}m"
                    )
                else:
                    self._starlink_samples.append({"latitude": lat, "longitude": lon,
                                                    "altitude": alt, "gps_sats": sats, "time": now_t})
            else:
                self._starlink_samples.append({"latitude": lat, "longitude": lon,
                                                "altitude": alt, "gps_sats": sats, "time": now_t})

            if len(self._starlink_samples) == 0:
                return self.last_starlink_data or location

            n       = len(self._starlink_samples)
            lat_avg = sum(p["latitude"]  for p in self._starlink_samples) / n
            lon_avg = sum(p["longitude"] for p in self._starlink_samples) / n
            alt_avg = sum(float(p.get("altitude", 0.0)) for p in self._starlink_samples) / n
            sats_avg = int(round(sum(int(p.get("gps_sats", 0)) for p in self._starlink_samples) / n))

            return {"latitude": lat_avg, "longitude": lon_avg,
                    "altitude": alt_avg, "gps_sats": sats_avg, "available": True}

        except Exception as e:
            logger.warning(f"Starlink filter fallback: {e}")
            return self.last_starlink_data or location

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
                    if (not self._fc_heartbeat_seen) and msg_fc.get_type() == "HEARTBEAT":
                        self._fc_heartbeat_seen = True
                        logger.info("FC HEARTBEAT detected and forwarded to GCS")
                    if self.telemetry_snapshot:
                        self.telemetry_snapshot.update_from_msg(msg_fc)
                    self.bridge.send_to_gcs(msg_fc)

                if processed == 0:
                    time.sleep(0.002)

            except Exception as e:
                logger.warning(f"MAVLink proxy worker помилка: {e}")
                time.sleep(0.01)

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
                            "gps_sats":  0,
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

                    gga, rmc = self.priority.make_nmea(
                        latitude=gps_data["latitude"],
                        longitude=gps_data["longitude"],
                        altitude=gps_data.get("altitude", 0.0),
                        sats=sats
                    )

                    # Відправляємо в FC через UART
                    if self.bridge:
                        try:
                            if self.bridge.write_uart_nmea(gga, rmc):
                                self.uart_tx_ok += 1
                            else:
                                self.uart_tx_fail += 1
                        except Exception as e:
                            self.uart_tx_fail += 1
                            logger.debug(f"UART write error: {e}")

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
