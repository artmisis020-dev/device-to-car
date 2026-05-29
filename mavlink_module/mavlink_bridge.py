#!/usr/bin/env python3
"""
MAVLink Bridge — міст між FC, GCS та GPS UART
==============================================

Фізична топологія на RPi:
  /dev/ttyAMA2  ← Beitian GPS (NMEA вхід, тільки читання)
  /dev/ttyAMA3  → FC Pixhawk UART (NMEA вихід, тільки запис)

Мережева топологія (UDP):
  FC Pixhawk ──UART──► mavlink_router ──UDP 14551──► MAVLinkBridge.mav_fc (recv)
  GCS Mission Planner ────────────────UDP 14550────► MAVLinkBridge.mav_gcs (recv/send)

Потоки даних:
  GCS → FC:  recv_from_gcs() → [фільтр команд] → send_to_fc()
  FC → GCS:  recv_from_fc()  → send_to_gcs()
  GPS → FC:  read_uart_nmea() → [main.py вибирає джерело] → write_uart_nmea()

Важливо: mavlink_router має бути запущений окремо (systemd сервіс).
  Він приймає MAVLink від FC через фізичний UART і роздає по UDP 14551.
"""

import socket
import serial
import threading
import logging
import time
from typing import Optional, Dict, Any, Callable
from pymavlink import mavutil

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [mavlink]: %(message)s"
)
logger = logging.getLogger(__name__)

# --- UDP порти MAVLink ---
GCS_LISTEN_PORT = 14550      # слухаємо команди від GCS (Mission Planner підключається сюди)
FC_RECV_ADDR    = "127.0.0.1" # mavlink_router шле FC телеметрію на localhost
FC_RECV_PORT    = 14551       # порт від mavlink_router (FC → RPi)
GCS_SEND_ADDR   = "127.0.0.1" # GCS теж на localhost (Mission Planner через WireGuard або тунель)
GCS_SEND_PORT   = 14550

UART_TIMEOUT = 0.1           # timeout читання UART (сек) — не блокує потік надовго


class MAVLinkBridge:
    """
    Центральний міст для всіх MAVLink та UART з'єднань.

    Екземпляр створюється в MavLinkGPSHub.__init__() і живе весь час роботи системи.
    Всі методи thread-safe через GIL Python (кожен виклик атомарний на рівні сокета).
    """

    def __init__(self):
        # --- MAVLink UDP з'єднання ---
        self.mav_fc  = None   # pymavlink connection: приймаємо телеметрію від FC
        self.mav_gcs = None   # pymavlink connection: приймаємо команди від GCS + відправляємо назад

        # --- UART порти ---
        self.uart_gps_in  = None  # serial: читаємо NMEA від Beitian GPS (/dev/ttyAMA2)
        self.uart_fc_out  = None  # serial: пишемо NMEA в FC (/dev/ttyAMA3)

        self._running = False
        # Callback для команд від GCS — встановлюється з main.py через set_gcs_callback()
        # Викликається синхронно в recv_from_gcs() при отриманні кожного повідомлення
        self._recv_gcs_callback: Optional[Callable] = None
        self._recv_fc_callback:  Optional[Callable] = None

    def connect_mavlink(self) -> bool:
        """
        Відкриває два UDP сокети для MAVLink.

        FC сторона (mav_fc):
          udpin:127.0.0.1:14551 — слухаємо пакети від mavlink_router.
          source_system=1 — ідентифікуємо себе як GCS (system ID 1).

        GCS сторона (mav_gcs):
          udpin:0.0.0.0:14550 — слухаємо команди від будь-якого GCS у мережі.
          Mission Planner типово відправляє heartbeat і команди сюди.

        Повертає False якщо не вдалось — система продовжить в обмеженому режимі
        (тільки GPS, без MAVLink проксі).
        """
        try:
            logger.info(f"Підключення до FC на {FC_RECV_ADDR}:{FC_RECV_PORT} (udpin)")
            self.mav_fc = mavutil.mavlink_connection(
                f'udpin:{FC_RECV_ADDR}:{FC_RECV_PORT}',
                source_system=1
            )

            logger.info(f"Запускаємо GCS proxy на 0.0.0.0:{GCS_LISTEN_PORT} (udpin)")
            self.mav_gcs = mavutil.mavlink_connection(
                f'udpin:0.0.0.0:{GCS_LISTEN_PORT}',
                source_system=1
            )

            logger.info("✅ MAVLink з'єднання успішно встановлено")
            return True

        except Exception as e:
            logger.error(f"Помилка при підключенні до MAVLink: {e}")
            return False

    def connect_uart_gps(self, port: str = "/dev/ttyAMA2", baud: int = 38400) -> bool:
        """
        Відкриває UART для читання NMEA від Beitian GPS.

        Beitian підключений до /dev/ttyAMA2 (UART2 на RPi).
        Швидкість 38400 baud — фіксована в конфігурації Beitian.
        Якщо порт недоступний — система продовжить без Beitian (тільки Starlink).
        """
        try:
            logger.info(f"Підключення до UART IN (Beitian) на {port} {baud} baud")
            self.uart_gps_in = serial.Serial(port, baud, timeout=UART_TIMEOUT)
            logger.info("UART IN (Beitian) успішно відкрито")
            return True
        except Exception as e:
            logger.warning(f"Не вдалось підключитися до UART IN (Beitian): {e}")
            self.uart_gps_in = None
            return False

    def connect_uart_fc_output(self, port: str = "/dev/ttyAMA3", baud: int = 38400) -> bool:
        """
        Відкриває UART для запису NMEA в FC.

        FC Pixhawk підключений до /dev/ttyAMA3 (UART3 на RPi).
        FC налаштований на GPS_TYPE=5 (NMEA), 38400 baud.
        КРИТИЧНО: якщо цей UART недоступний — FC не отримає GPS взагалі.
        """
        try:
            logger.info(f"Підключення до UART OUT (FC) на {port} {baud} baud")
            self.uart_fc_out = serial.Serial(port, baud, timeout=UART_TIMEOUT)
            logger.info("UART OUT (FC) успішно відкрито")
            return True
        except Exception as e:
            logger.warning(f"Не вдалось підключитися до UART OUT (FC): {e}")
            self.uart_fc_out = None
            return False

    def recv_from_gcs(self) -> Optional[Any]:
        """
        Неблокуюче читання одного MAVLink повідомлення від GCS.

        Якщо встановлений _recv_gcs_callback — викликає його синхронно.
        У main.py callback = MavLinkGPSHub._handle_gcs_command() — обробляє
        SET_GPS_GLOBAL_ORIGIN (ручна точка) та LED_CONTROL (переключення GPS).

        Повертає None якщо черга порожня (non-blocking).
        """
        try:
            if self.mav_gcs is None:
                return None
            msg = self.mav_gcs.recv_match(blocking=False)
            if msg and self._recv_gcs_callback:
                self._recv_gcs_callback(msg)
            return msg
        except Exception as e:
            logger.warning(f"Помилка при читанні з GCS: {e}")
            return None

    def recv_from_fc(self) -> Optional[Any]:
        """
        Неблокуюче читання одного MAVLink повідомлення від FC.

        Повідомлення (ATTITUDE, GPS_RAW_INT, BATTERY_STATUS, etc.) потім
        mavlink_proxy_worker пересилає до GCS і в TelemetrySender.
        """
        try:
            if self.mav_fc is None:
                return None
            msg = self.mav_fc.recv_match(blocking=False)
            if msg and self._recv_fc_callback:
                self._recv_fc_callback(msg)
            return msg
        except Exception as e:
            logger.warning(f"Помилка при читанні з FC: {e}")
            return None

    def send_to_gcs(self, msg: Any) -> bool:
        """
        Пересилає MAVLink повідомлення від FC до GCS.

        Викликається в mavlink_proxy_worker для кожного FC повідомлення.
        Використовує mav_gcs.write() — відповідь іде на останній відомий
        GCS адресу (pymavlink запам'ятовує звідки прийшов останній пакет).
        """
        try:
            if self.mav_gcs is None:
                return False
            self.mav_gcs.write(msg.get_msgbuf())
            return True
        except Exception as e:
            logger.warning(f"Помилка при відправці до GCS: {e}")
            return False

    def send_to_fc(self, msg: Any) -> bool:
        """
        Пересилає MAVLink команду від GCS до FC.

        ВАЖЛИВО: деякі команди від GCS перехоплюються і НЕ пересилаються FC:
          - SET_GPS_GLOBAL_ORIGIN → обробляємо самі (ручна GPS точка)
          - LED_CONTROL           → обробляємо самі (переключення джерела GPS)
          - COMMAND_LONG/INT #179 → обробляємо самі (Set Position)
        Решта команд (ARM, MODE, WAYPOINT, etc.) — пересилаємо без змін.
        """
        try:
            if self.mav_fc is None:
                return False
            self.mav_fc.write(msg.get_msgbuf())
            return True
        except Exception as e:
            logger.warning(f"Помилка при відправці до FC: {e}")
            return False

    def read_uart_nmea(self) -> Optional[str]:
        """
        Читає один рядок NMEA з UART (Beitian GPS).

        Повертає рядок типу "$GPGGA,..." або "$GPRMC,...".
        Beitian_worker викликає це в циклі і зберігає тільки GGA/RMC рядки.
        GSV, GSA, VTG — ігноруються (вони не потрібні для позиціонування).
        """
        try:
            if self.uart_gps_in is None:
                return None
            if self.uart_gps_in.in_waiting > 0:
                line = self.uart_gps_in.readline().decode('ascii', errors='ignore').strip()
                if line:
                    return line
            return None
        except Exception as e:
            logger.warning(f"Помилка при читанні UART: {e}")
            return None

    def write_uart_nmea(self, gga: str, rmc: str) -> bool:
        """
        Записує GGA + RMC в UART FC (5 Hz з main_loop).

        FC (ArduPilot) читає цей UART як GPS1 (GPS_TYPE=5 NMEA, 38400 baud).
        Обидва рядки GGA і RMC потрібні — GGA дає позицію+висоту,
        RMC дає швидкість і курс.
        CR+LF обов'язкові — NMEA стандарт.
        """
        try:
            if self.uart_fc_out is None:
                logger.warning("UART OUT (FC) не підключено")
                return False
            if not gga.endswith('\r\n'):
                gga += '\r\n'
            if not rmc.endswith('\r\n'):
                rmc += '\r\n'
            self.uart_fc_out.write(gga.encode('ascii', errors='ignore'))
            self.uart_fc_out.write(rmc.encode('ascii', errors='ignore'))
            logger.debug("NMEA записано на UART")
            return True
        except Exception as e:
            logger.error(f"Помилка при записі NMEA на UART: {e}")
            return False

    def send_adsb_vehicle(self, icao: int, lat: float, lon: float,
                          alt_m: float, name: str = "") -> bool:
        """
        Відправляє псевдо-ADS-B повідомлення до GCS для візуалізації на карті.

        Використовується щоб показати оператору в Mission Planner усі GPS джерела
        одночасно у вигляді "літаків" на карті:
          ICAO 111 → STARLINK  (відфільтрована позиція)
          ICAO 222 → GPS-RAW   (сирий Beitian)
          ICAO 333 → MANUAL    (ручна точка оператора)
          ICAO 444 → GPS-активне джерело (те що йде у FC)

        Це суто для відображення — в FC нічого не відправляється.
        """
        try:
            if self.mav_gcs is None:
                return False
            self.mav_gcs.mav.adsb_vehicle_send(
                icao,
                int(lat * 1e7),          # широта × 10^7 (MAVLink формат)
                int(lon * 1e7),          # довгота × 10^7
                0,                       # altitude type (0 = pressure)
                int(alt_m * 1000),       # висота в мм
                0,                       # heading (0 = невідомо)
                0,                       # horizontal velocity
                0,                       # vertical velocity
                name.encode('ascii', errors='ignore')[:8],  # callsign макс 8 символів
                7,                       # emitter type 7 = "No info"
                1,                       # tslc = 1 сек (свіжі дані)
                31,                      # flags
                0                        # squawk
            )
            logger.debug(f"ADS-B {name} (ICAO {icao}) відправлено")
            return True
        except Exception as e:
            logger.warning(f"Помилка при відправці ADS-B: {e}")
            return False

    def send_statustext(self, text: str, severity: int = 6) -> bool:
        """
        Відправляє текстове повідомлення до GCS (відображається в Mission Planner HUD).

        severity: 0=EMERGENCY, 3=ERROR, 4=WARNING, 5=NOTICE, 6=INFO
        Використовується щоб повідомити оператора про зміну режиму GPS.
        """
        try:
            if self.mav_gcs is None:
                return False
            text = text[:50]  # MAVLink STATUSTEXT обмежений 50 символами
            self.mav_gcs.mav.statustext_send(
                severity,
                text.encode('ascii', errors='ignore')
            )
            logger.info(f"STATUSTEXT: {text}")
            return True
        except Exception as e:
            logger.warning(f"Помилка при відправці STATUSTEXT: {e}")
            return False

    def handle_set_gps_global_origin(self, msg: Any) -> Dict[str, Any]:
        """
        Обробляє SET_GPS_GLOBAL_ORIGIN від GCS.

        Ця команда відправляється з Mission Planner коли оператор клікає
        "Set Home Here" або "Set Origin". В Sirena перевикористана для
        задання ручної GPS точки (режим MANUAL).

        Координати в MAVLink: lat/lon × 10^7 (int32), alt × 1000 мм (int32).
        """
        try:
            coords = {
                "latitude":  msg.latitude  / 1e7,
                "longitude": msg.longitude / 1e7,
                "altitude":  msg.altitude  / 1000.0,  # мм → метри
            }
            logger.info(f"SET_GPS_GLOBAL_ORIGIN: lat={coords['latitude']}, lon={coords['longitude']}")
            return coords
        except Exception as e:
            logger.warning(f"Помилка при обробці SET_GPS_GLOBAL_ORIGIN: {e}")
            return {}

    def handle_led_control(self, msg: Any) -> Dict[str, Any]:
        """
        Обробляє LED_CONTROL від GCS — перемикання джерела GPS.

        LED_CONTROL — нестандартне використання MAVLink команди:
        Mission Planner відправляє її з кастомними RGB значеннями,
        а Sirena інтерпретує як команду переключення GPS:
          RGB(255,255,255) → STARLINK
          RGB(0,0,0)       → BEITIAN

        Чому LED_CONTROL? Бо це проста команда без ACK і Mission Planner
        легко її надсилає через скрипти або кнопки на панелі.
        """
        try:
            r = msg.red   if hasattr(msg, 'red')   else None
            g = msg.green if hasattr(msg, 'green') else None
            b = msg.blue  if hasattr(msg, 'blue')  else None

            # Mission Planner іноді передає RGB через custom_bytes замість полів
            if (r is None or g is None or b is None) and hasattr(msg, 'custom_bytes'):
                raw = bytes(getattr(msg, 'custom_bytes', b''))
                if len(raw) >= 3:
                    r, g, b = raw[0], raw[1], raw[2]

            r = r or 0
            g = g or 0
            b = b or 0

            # Декодуємо RGB → джерело GPS
            source = "beitian"
            if r == 255 and g == 255 and b == 255:
                source = "starlink"
            elif r == 0 and g == 0 and b == 0:
                source = "beitian"

            logger.info(f"LED_CONTROL RGB({r},{g},{b}) → source={source}")
            return {"r": r, "g": g, "b": b, "source": source}
        except Exception as e:
            logger.warning(f"Помилка при обробці LED_CONTROL: {e}")
            return {}

    def set_gcs_callback(self, callback: Callable):
        """
        Встановлює callback для команд від GCS.
        Callback викликається синхронно в recv_from_gcs() — має бути швидким.
        """
        self._recv_gcs_callback = callback

    def set_fc_callback(self, callback: Callable):
        """Встановлює callback для повідомлень від FC (не використовується зараз)."""
        self._recv_fc_callback = callback

    def disconnect(self):
        """Закриває всі UART і MAVLink з'єднання при shutdown."""
        try:
            if self.uart_gps_in:
                self.uart_gps_in.close()
                logger.info("UART IN (Beitian) закрито")
            if self.uart_fc_out:
                self.uart_fc_out.close()
                logger.info("UART OUT (FC) закрито")
        except Exception as e:
            logger.warning(f"Помилка при закритті UART: {e}")

    # Alias для зворотної сумісності
    close = disconnect
