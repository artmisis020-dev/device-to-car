#!/usr/bin/env python3
"""
Інструмент для роботи з Starlink gRPC API.

Надає функції для отримання координат, статусу, перезавантаження,
та керування GPS на Starlink dish.

Обробляє помилки зв'язку та використовує кешування при недоступності.
"""

import logging
from typing import Any, Dict

import config
import starlink_grpc

# Налаштування логування
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s [starlink]: %(message)s"
)
logger = logging.getLogger(__name__)

# Backwards-compatible alias for existing callers.
DEFAULT_TARGET = config.DEFAULT_TARGET


def _get_nested(obj: Any, *path: str, default: Any = None) -> Any:
    current = obj
    for name in path:
        try:
            if isinstance(current, dict):
                current = current[name]
            else:
                current = getattr(current, name)
        except (AttributeError, KeyError, TypeError, ValueError):
            return default
    return current


class StarlinkClient:
    """
    Клієнт для роботи з Starlink gRPC API.

    Надає методи для отримання координат, статусу, керування dish.
    Включає кешування при помилках зв'язку.
    """

    def __init__(self, starlink_grpc_module: Any = None, target: str = ""):
        """
        Ініціалізувати клієнт Starlink.

        Args:
            starlink_grpc_module: Optional fake/test module. Production uses
                                  the installed starlink_grpc package.
            target: Starlink dish Address
        """
        self.grpc = starlink_grpc_module
        self.target = target
        self.context = self.grpc.ChannelContext(target=self.target)

    def get_location(self) -> Dict[str, Any] | None:
        """
        Отримати координати та статус GPS з Starlink dish.

        Returns:
            Dict з ключами:
            - latitude: Широта (float або None)
            - longitude: Довгота (float або None)
            - altitude: Висота в метрах (float або None)
        """
        try:
            location = self.grpc.location_data(context=self.context) or {}
            data = {
                "latitude": location.get("latitude"),
                "longitude": location.get("longitude"),
                "altitude": location.get("altitude"),
                "gps_sats": location.get("gps_sats", 0),
                "available": True,
            }

            logger.debug(
                f"GPS останні дані: lat={data['latitude']}, lon={data['longitude']}, sats={data['gps_sats']}"
            )

            return data

        except Exception as e:
            logger.warning(f"Не вдалось отримати координати від Starlink: {e}")
            return None

    def get_status(self) -> Dict[str, Any]:
        """
        Отримати повний статус Starlink dish.

        Returns:
            Dict з повною інформацією про dish (див. starlink_grpc.py документацію).
            При помилці повертає кешовані дані або пусту структуру.
        """
        try:
            data = self.grpc.get_status()
            return data

        except Exception as e:
            logger.warning(f"Не вдалось отримати статус Starlink: {e}")
            return {"available": False}

    # TODO: REVIEW THIS!
    def get_software_update_status(
        self, target: str = DEFAULT_TARGET
    ) -> Dict[str, Any]:
        """
        Перевірити, чи очікує Starlink dish встановлення software update.

        Логіка базується на dish_check_update.py з starlink-grpc-tools:
        перевіряємо кілька дубльованих прапорців, бо частина з них може
        зникати/змінюватись між версіями dish firmware.
        """
        try:
            ctx = self.grpc.ChannelContext(target=target)
            try:
                status = self.grpc.get_status(context=ctx)
            except TypeError:
                status = self.grpc.get_status(ctx)

            alert_flag = _get_nested(status, "alerts", "install_pending")
            state = _get_nested(status, "software_update_state")
            stats_state = _get_nested(
                status, "software_update_stats", "software_update_state"
            )
            ready_flag = _get_nested(status, "swupdate_reboot_ready")
            sw_version = _get_nested(
                status, "device_info", "software_version", default="UNKNOWN"
            )

            state_flag = (
                None if state is None else state == config.SOFTWARE_UPDATE_REBOOT_REQUIRED
            )
            stats_flag = (
                None
                if stats_state is None
                else stats_state == config.SOFTWARE_UPDATE_REBOOT_REQUIRED
            )
            state_disabled = (
                None if state is None else state == config.SOFTWARE_UPDATE_DISABLED
            )
            stats_disabled = (
                None if stats_state is None else stats_state == config.SOFTWARE_UPDATE_DISABLED
            )

            if alert_flag is None and state_flag is None and stats_flag is None:
                install_pending = bool(ready_flag)
            else:
                install_pending = bool(alert_flag or state_flag or stats_flag)

            return {
                "available": True,
                "software_version": sw_version,
                "install_pending": install_pending,
                "updates_disabled": bool(state_disabled or stats_disabled),
                "reboot_required": install_pending,
                "flags": {
                    "alerts_install_pending": alert_flag,
                    "software_update_state_reboot_required": state_flag,
                    "software_update_stats_reboot_required": stats_flag,
                    "swupdate_reboot_ready": ready_flag,
                    "software_update_state_disabled": state_disabled,
                    "software_update_stats_disabled": stats_disabled,
                },
                "raw": {
                    "software_update_state": state,
                    "software_update_stats_state": stats_state,
                },
            }
        except Exception as e:
            logger.warning(f"Не вдалось перевірити Starlink software update: {e}")
            return {"available": False, "error": str(e)}

    def reboot(self) -> None:
        """
        Перезавантажити Starlink dish.

        Returns:
            None
        """
        status, _, _ = self.grpc.reboot(context=self.context)

        return status

    def set_gps(self, enable: bool = True) -> bool:
        """
        Включити або вимкнути GPS на Starlink dish.

        Args:
            enable: True для включення, False для вимкнення

        Returns:
            True при успіху, False при помилці.

        Note:
            Це налаштування не впливає на доступність location_data через gRPC.
            Впливає лише на interno використання GPS dish.
        """
        try:
            self.grpc.set_gps_config(enable, context=self.context)
            status_str = "увімкнена" if enable else "вимкнена"
            logger.info(f"GPS на dish {status_str}")
            return True
        except Exception as e:
            logger.error(f"Не вдалось встановити GPS: {e}")
            return False

    def close(self):
        self.context.close()


# Глобальний клієнт для одноразового використання
starlink_client = StarlinkClient(starlink_grpc, DEFAULT_TARGET)


def get_client() -> StarlinkClient:
    return starlink_client


# if __name__ == "__main__":
#     """Тестування модуля."""
#     logger.info("Тест modulu starlink.py")

#     try:
#         client = get_client()

#         # Тест get_location
#         print("\n=== Тест get_location ===")
#         loc = client.get_location()
#         print(f"Available: {loc.get('available')}")
#         print(f"Latitude: {loc.get('latitude')}")
#         print(f"Longitude: {loc.get('longitude')}")
#         print(f"Altitude: {loc.get('altitude')}")
#         print(f"GPS Sats: {loc.get('gps_sats')}")

#         # Тест get_status
#         print("\n=== Тест get_status ===")
#         status = client.get_status()
#         print(f"Available: {status.get('available')}")
#         print(f"State: {status.get('state')}")
#         print(f"Uptime: {status.get('uptime')}")

#     except Exception as e:
#         logger.error(f"Тест завершено з помилкою: {e}")
