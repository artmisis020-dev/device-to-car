#!/usr/bin/env python3
"""
GPS Priority — вибір активного GPS джерела та генерація NMEA
=============================================================

Цей модуль відповідає за одне рішення: яке GPS джерело зараз подавати в FC.

Пріоритет джерел (від вищого до нижчого):
  1. MANUAL  — оператор клікнув "Set GPS Here" в Mission Planner
               Діє 60 сек, потім автоматично знімається
  2. STARLINK — Starlink через gRPC API (192.168.100.1:9200)
               Доступний тільки якщо тарілка підключена і має фікс
  3. BEITIAN  — Beitian GPS приймач через UART (/dev/ttyAMA2, 38400 baud)
               Завжди є як fallback
  4. NONE    — нема жодного джерела (FC залишається без GPS)

Після вибору джерела → make_nmea() формує GGA+RMC рядки для FC.
"""

import time
import logging
import datetime
from typing import Dict, Any, Optional, Tuple
from pynmea2 import parse as parse_nmea

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [priority]: %(message)s"
)
logger = logging.getLogger(__name__)


class GPSPriority:
    """
    Вибирає GPS джерело і формує NMEA для FC.

    Екземпляр живе весь час роботи системи.
    Викликається з MavLinkGPSHub.main_loop() кожні 200 мс (5 Hz).
    """

    def __init__(self, manual_timeout_sec: float = 60.0, talker: str = "GP"):
        """
        manual_timeout_sec: скільки сек тримати MANUAL режим після установки.
                            45-60 сек — достатньо щоб FC прийняв позицію.
        talker: NMEA talker ID.
                "GP" — GPS (стандарт, ArduPilot розуміє)
                "GN" — GNSS (multi-constellation)
                "BD" — BeiDou (китайська система, не використовуємо)
        """
        self.manual_timeout_sec = manual_timeout_sec
        self.talker = talker
        self.last_source = None   # для логування змін джерела

    def select_source(
        self,
        manual_coords: Optional[Dict[str, float]] = None,
        manual_time: float = 0.0,
        starlink_available: bool = False,
        starlink_data: Optional[Dict[str, Any]] = None,
        beitian_nmea: Optional[str] = None
    ) -> Tuple[Optional[Dict[str, Any]], str, int]:
        """
        Головна функція вибору джерела. Викликається 5 разів/сек з main_loop.

        Аргументи беруться зі стану MavLinkGPSHub під локом.

        Повертає:
          selected_data — dict {latitude, longitude, altitude}
          source_type   — "MANUAL" / "STARLINK" / "BEITIAN" / "NONE"
          sats          — кількість супутників (для NMEA якості)
        """
        now = time.time()
        source_data = None
        source_type = "NONE"
        sats = 0

        # --- Пріоритет 1: MANUAL (ручна GPS точка від оператора) ---
        # Оператор в Mission Planner відправляє SET_GPS_GLOBAL_ORIGIN або
        # команду #179 — це зберігається в last_manual_coords.
        # Використовуємо 60 сек, потім автоматично переходимо на Starlink/Beitian.
        if (manual_coords and
                manual_time > 0 and
                (now - manual_time) < self.manual_timeout_sec):
            source_data = {
                "latitude":  manual_coords.get("latitude"),
                "longitude": manual_coords.get("longitude"),
                "altitude":  manual_coords.get("altitude", 0),
            }
            source_type = "MANUAL"
            sats = int(manual_coords.get("sats", 12))
            logger.debug(f"GPS: MANUAL (залишилось {self.manual_timeout_sec - (now - manual_time):.1f}s)")

        # --- Пріоритет 2: STARLINK ---
        # starlink_available встановлюється в starlink_worker після успішного gRPC запиту.
        # starlink_data вже відфільтрований через moving average (або forecast з main.py).
        elif (starlink_available and starlink_data and
              starlink_data.get("latitude") is not None):
            source_data = {
                "latitude":  starlink_data.get("latitude"),
                "longitude": starlink_data.get("longitude"),
                "altitude":  starlink_data.get("altitude", 0),
            }
            source_type = "STARLINK"
            sats = int(starlink_data.get("gps_sats", 0))
            logger.debug(f"GPS: STARLINK (sats={sats})")

        # --- Пріоритет 3: BEITIAN (фізичний GPS приймач) ---
        # beitian_nmea — останній GGA або RMC рядок від beitian_worker.
        # Парсимо NMEA і отримуємо координати.
        elif beitian_nmea:
            source_data, sats = self._parse_beitian_nmea(beitian_nmea)
            if source_data:
                source_type = "BEITIAN"
                logger.debug(f"GPS: BEITIAN (sats={sats})")

        # --- Пріоритет 4: NONE ---
        else:
            logger.debug("GPS: NONE (нема доступних джерел)")

        # Логуємо зміну джерела (не кожен цикл — тільки при переключенні)
        if source_type != self.last_source:
            logger.info(f"*** GPS джерело змінилось: {self.last_source} → {source_type} ***")
            self.last_source = source_type

        return source_data, source_type, sats

    @staticmethod
    def _parse_beitian_nmea(nmea_line: str) -> Tuple[Optional[Dict[str, float]], int]:
        """
        Парсить один NMEA рядок від Beitian і повертає координати.

        Підтримуємо тільки GGA і RMC — інші типи (GSA, GSV, VTG, GLL)
        не містять потрібних даних або дублюють їх.

        GGA — основний: містить lat/lon/alt + якість фіксу + кількість супутників
        RMC — запасний: містить lat/lon + швидкість/курс, але без висоти

        NMEA координати в форматі DDDMM.MMMM, конвертуємо в десяткові градуси:
          degrees = int(DDDMM.MMMM / 100)
          minutes = DDDMM.MMMM - degrees*100
          decimal = degrees + minutes/60
        """
        try:
            line = nmea_line.strip()
            if not line.startswith('$'):
                return None, 0

            # Видаляємо контрольну суму (*XX) перед парсингом
            if '*' in line:
                line = line[:line.rfind('*')]
            line = line[1:]  # видаляємо $

            parts = line.split(',')
            if len(parts) < 2:
                return None, 0

            sentence_type = parts[0]  # наприклад "GPGGA", "GNGGA", "BDGGA"

            # --- GGA парсинг ---
            if 'GGA' in sentence_type and len(parts) >= 10:
                try:
                    fix_quality = int(parts[6]) if parts[6] else 0
                    if fix_quality == 0:
                        return None, 0  # no fix — не використовуємо

                    # Широта: DDMM.MMMM + N/S
                    lat_str, lat_hem = parts[2], parts[3]
                    if not lat_str or not lat_hem:
                        return None, 0
                    lat_deg = int(lat_str[:2])
                    lat_min = float(lat_str[2:])
                    latitude = lat_deg + lat_min / 60.0
                    if lat_hem == 'S':
                        latitude = -latitude

                    # Довгота: DDDMM.MMMM + E/W
                    lon_str, lon_hem = parts[4], parts[5]
                    if not lon_str or not lon_hem:
                        return None, 0
                    lon_deg = int(lon_str[:3])
                    lon_min = float(lon_str[3:])
                    longitude = lon_deg + lon_min / 60.0
                    if lon_hem == 'W':
                        longitude = -longitude

                    altitude = float(parts[9]) if parts[9] else 0.0
                    num_sats = int(parts[7]) if parts[7] else 0

                    return {"latitude": latitude, "longitude": longitude, "altitude": altitude}, num_sats

                except (ValueError, IndexError) as e:
                    logger.debug(f"Помилка парсингу GGA: {e}")
                    return None, 0

            # --- RMC парсинг (запасний, немає висоти) ---
            elif 'RMC' in sentence_type and len(parts) >= 6:
                try:
                    if parts[2] != 'A':
                        return None, 0  # статус 'V' = invalid fix

                    lat_str, lat_hem = parts[3], parts[4]
                    lon_str, lon_hem = parts[5], parts[6]
                    if not all([lat_str, lat_hem, lon_str, lon_hem]):
                        return None, 0

                    lat_deg = int(lat_str[:2])
                    lat_min = float(lat_str[2:])
                    latitude = lat_deg + lat_min / 60.0
                    if lat_hem == 'S':
                        latitude = -latitude

                    lon_deg = int(lon_str[:3])
                    lon_min = float(lon_str[3:])
                    longitude = lon_deg + lon_min / 60.0
                    if lon_hem == 'W':
                        longitude = -longitude

                    return {"latitude": latitude, "longitude": longitude, "altitude": 0.0}, 0

                except (ValueError, IndexError):
                    return None, 0

            return None, 0

        except Exception as e:
            logger.debug(f"Помилка парсингу NMEA '{nmea_line}': {e}")
            return None, 0

    def make_nmea(
        self,
        latitude: float,
        longitude: float,
        altitude: float = 0.0,
        sats: int = 0,
        fix_quality: int = 1,
        hdop: float = 0.9
    ) -> Tuple[str, str]:
        """
        Формує GGA + RMC рядки для відправки в FC через UART.

        FC (ArduPilot GPS_TYPE=5 NMEA) читає ці рядки і оновлює свою
        GPS позицію — саме звідси береться координата в Mission Planner
        і використовується для навігації.

        fix_quality=1 — стандартний GPS фікс (FC приймає)
        hdop=0.9 — хороша точність (FC не ігнорує)

        УВАГА: talker ("GP"/"GN") впливає на те як ArduPilot класифікує
        GPS — рекомендується "GP" для сумісності.
        """
        now_utc = datetime.datetime.utcnow()
        t = now_utc.strftime("%H%M%S") + f".{int(now_utc.microsecond/10000):02d}"
        d = now_utc.strftime("%d%m%y")

        lat_deg, lat_min, lat_h = self._deg_to_nmea(latitude,  is_lat=True)
        lon_deg, lon_min, lon_h = self._deg_to_nmea(longitude, is_lat=False)

        # GGA: час, позиція, якість фіксу, к-сть супутників, hdop, висота
        gga = (
            f"${self.talker}GGA,{t},{lat_deg:02d}{lat_min:07.4f},{lat_h},"
            f"{lon_deg:03d}{lon_min:07.4f},{lon_h},"
            f"{fix_quality},{sats:02d},{hdop:.1f},{altitude:.1f},M,0.0,M,,*00"
        )
        gga_body = gga[1:gga.rfind("*")]
        gga = gga.replace("*00", f"*{self._calc_checksum(gga_body)}")

        # RMC: час, статус A=active, позиція, швидкість=0, курс=0, дата
        # Швидкість і курс = 0 — ArduPilot використовує власну IMU для швидкості
        rmc = (
            f"${self.talker}RMC,{t},A,{lat_deg:02d}{lat_min:07.4f},{lat_h},"
            f"{lon_deg:03d}{lon_min:07.4f},{lon_h},"
            f"0.0,0.0,{d},,,A*00"
        )
        rmc_body = rmc[1:rmc.rfind("*")]
        rmc = rmc.replace("*00", f"*{self._calc_checksum(rmc_body)}")

        return gga, rmc

    @staticmethod
    def _deg_to_nmea(coord: float, is_lat: bool = True) -> Tuple[int, float, str]:
        """
        Конвертує десяткові градуси у NMEA формат DDDMM.MMMM.
        Повертає (degrees_int, minutes_float, hemisphere_char).
        """
        hemi = ("N" if coord >= 0 else "S") if is_lat else ("E" if coord >= 0 else "W")
        coord = abs(coord)
        degrees = int(coord)
        minutes = (coord - degrees) * 60.0
        return degrees, minutes, hemi

    @staticmethod
    def _calc_checksum(body: str) -> str:
        """
        XOR контрольна сума NMEA (все між $ і *).
        ArduPilot перевіряє контрольну суму — неправильна → рядок ігнорується.
        """
        checksum = 0
        for ch in body:
            checksum ^= ord(ch)
        return f"{checksum:02X}"


# Глобальний singleton для використання без класу
_global_priority: Optional[GPSPriority] = None


def get_priority(talker: str = "GP") -> GPSPriority:
    """Повертає або створює глобальний екземпляр GPSPriority."""
    global _global_priority
    if _global_priority is None:
        _global_priority = GPSPriority(talker=talker)
    return _global_priority


def select_source(
    manual_coords=None, manual_time=0.0,
    starlink_available=False, starlink_data=None, beitian_nmea=None
):
    """Функція-обгортка для зворотної сумісності."""
    return get_priority().select_source(
        manual_coords, manual_time, starlink_available, starlink_data, beitian_nmea
    )
