#!/usr/bin/env python3
"""
Інструмент для роботи з Starlink gRPC API.

Надає функції для отримання координат, статусу, перезавантаження,
та керування GPS на Starlink dish.

Обробляє помилки зв'язку та використовує кешування при недоступності.
"""

import sys
import logging
from pathlib import Path
from typing import Any, Dict, Optional

# Налаштування логування
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [starlink]: %(message)s"
)
logger = logging.getLogger(__name__)

# Константи
DEFAULT_STARLINK_IP = "192.168.100.1"
DEFAULT_STARLINK_PORT = 9200


class StarlinkClient:
    """
    Клієнт для роботи з Starlink gRPC API.
    
    Надає методи для отримання координат, статусу, керування dish.
    Включає кешування при помилках зв'язку.
    """
    
    def __init__(self, starlink_grpc_module: Any = None):
        """
        Ініціалізувати клієнт Starlink.
        
        Args:
            starlink_grpc_module: Модуль starlink_grpc з starlink-grpc-tools.
                                 Якщо None, буде автоматично завантажен.
        """
        self.grpc = starlink_grpc_module or self._import_starlink_grpc()
        # Кеш для зберігання останніх успішних даних
        self._location_cache = None
        self._status_cache = None
    
    @staticmethod
    def _import_starlink_grpc() -> Any:
        """Завантажити модуль starlink_grpc з starlink-grpc-tools."""
        base = Path(__file__).resolve().parent
        tools_dir = base / "starlink-grpc-tools"
        
        if not (tools_dir / "starlink_grpc.py").exists():
            raise FileNotFoundError(f"Не знайдено {tools_dir / 'starlink_grpc.py'}")
        
        sys.path.insert(0, str(tools_dir))
        try:
            import starlink_grpc  # type: ignore
            logger.info("Модуль starlink_grpc успішно завантажено")
            return starlink_grpc
        except ImportError as e:
            logger.error(f"Не вдалось завантажити starlink_grpc: {e}")
            raise
    
    def get_location(self, target: str = f"{DEFAULT_STARLINK_IP}:{DEFAULT_STARLINK_PORT}") -> Dict[str, Any]:
        """
        Отримати координати та статус GPS з Starlink dish.
        
        Args:
            target: Адреса у форматі "IP:PORT" (default: 192.168.100.1:9200)
        
        Returns:
            Dict з ключами:
            - latitude: Широта (float або None)
            - longitude: Довгота (float або None)
            - altitude: Висота в метрах (float або None)
            - gps_enabled: Чи увімкнена GPS на dish (bool)
            - gps_ready: Чи готова GPS для позиціювання (bool)
            - gps_sats: Кількість використаних супутників (int)
            
            При помилці повертає кешовані дані (якщо є) або None-значення.
        """
        try:
            ctx = self.grpc.ChannelContext(target=target)
            status, _, _ = self.grpc.status_data(context=ctx)
            location = self.grpc.location_data(context=ctx) or {}
            
            data = {
                "latitude": location.get("latitude"),
                "longitude": location.get("longitude"),
                "altitude": location.get("altitude"),
                "gps_enabled": status.get("gps_enabled"),
                "gps_ready": status.get("gps_ready"),
                "gps_sats": status.get("gps_sats") or 0,
                "available": True,
            }
            
            # Кешуємо успішні дані
            self._location_cache = data
            logger.debug(f"GPS останні дані: lat={data['latitude']}, lon={data['longitude']}, sats={data['gps_sats']}")
            
            return data
            
        except Exception as e:
            logger.warning(f"Не вдалось отримати координати від Starlink: {e}")
            
            # Повертаємо кеш при помилці
            if self._location_cache:
                logger.info("Використовуємо кешовані координати")
                return self._location_cache
            
            # Якщо кеш відсутній, повертаємо структуру з None-значеннями
            return {
                "latitude": None,
                "longitude": None,
                "altitude": None,
                "gps_enabled": None,
                "gps_ready": None,
                "gps_sats": 0,
                "available": False,
            }
    
    def get_status(self, target: str = f"{DEFAULT_STARLINK_IP}:{DEFAULT_STARLINK_PORT}") -> Dict[str, Any]:
        """
        Отримати повний статус Starlink dish.
        
        Args:
            target: Адреса у форматі "IP:PORT"
        
        Returns:
            Dict з повною інформацією про dish (див. starlink_grpc.py документацію).
            При помилці повертає кешовані дані або пусту структуру.
        """
        try:
            ctx = self.grpc.ChannelContext(target=target)
            status, _, _ = self.grpc.status_data(context=ctx)
            
            data = {
                "available": True,
                "id": status.get("id"),
                "hardware_version": status.get("hardware_version"),
                "software_version": status.get("software_version"),
                "state": status.get("state"),
                "uptime": status.get("uptime"),
                "snr": status.get("snr"),
                "pop_ping_drop_rate": status.get("pop_ping_drop_rate"),
                "downlink_throughput_bps": status.get("downlink_throughput_bps"),
                "uplink_throughput_bps": status.get("uplink_throughput_bps"),
                "pop_ping_latency_ms": status.get("pop_ping_latency_ms"),
                "fraction_obstructed": status.get("fraction_obstructed"),
                "gps_enabled": status.get("gps_enabled"),
                "gps_ready": status.get("gps_ready"),
                "gps_sats": status.get("gps_sats"),
            }
            
            self._status_cache = data
            logger.debug(f"Статус Starlink: state={data['state']}, uptime={data['uptime']}s")
            
            return data
            
        except Exception as e:
            logger.warning(f"Не вдалось отримати статус Starlink: {e}")
            
            if self._status_cache:
                logger.info("Використовуємо кешований статус")
                return self._status_cache
            
            return {"available": False}
    
    def reboot(self, target: str = f"{DEFAULT_STARLINK_IP}:{DEFAULT_STARLINK_PORT}") -> bool:
        """
        Перезавантажити Starlink dish.
        
        Args:
            target: Адреса у форматі "IP:PORT"
        
        Returns:
            True при успіху, False при помилці.
        """
        try:
            from yagrc import reflector as yagrc_reflector
            import grpc
            
            reflector = yagrc_reflector.GrpcReflectionClient()
            
            with grpc.insecure_channel(target) as channel:
                reflector.load_protocols(channel, symbols=["SpaceX.API.Device.Device"])
                stub = reflector.service_stub_class("SpaceX.API.Device.Device")(channel)
                request_class = reflector.message_class("SpaceX.API.Device.Request")
                
                request = request_class(reboot={})
                response = stub.Handle(request, timeout=10)
                
                logger.info("Dish успішно перезавантажено")
                return True
                
        except Exception as e:
            logger.error(f"Не вдалось перезавантажити Starlink: {e}")
            return False
    
    def set_gps(self, target: str = f"{DEFAULT_STARLINK_IP}:{DEFAULT_STARLINK_PORT}", 
                enable: bool = True) -> bool:
        """
        Включити або вимкнути GPS на Starlink dish.
        
        Args:
            target: Адреса у форматі "IP:PORT"
            enable: True для включення, False для вимкнення
        
        Returns:
            True при успіху, False при помилці.
        
        Note:
            Це налаштування не впливає на доступність location_data через gRPC.
            Впливає лише на interno використання GPS dish.
        """
        try:
            from yagrc import reflector as yagrc_reflector
            import grpc
            
            reflector = yagrc_reflector.GrpcReflectionClient()
            
            with grpc.insecure_channel(target) as channel:
                reflector.load_protocols(channel, symbols=["SpaceX.API.Device.Device"])
                stub = reflector.service_stub_class("SpaceX.API.Device.Device")(channel)
                request_class = reflector.message_class("SpaceX.API.Device.Request")
                
                request = request_class(dish_inhibit_gps={"inhibit_gps": not enable})
                response = stub.Handle(request, timeout=10)
                
                status_str = "увімкнена" if enable else "вимкнена"
                logger.info(f"GPS на dish {status_str}")
                return True
                
        except Exception as e:
            logger.error(f"Не вдалось встановити GPS: {e}")
            return False


# Глобальний клієнт для одноразового використання
_global_client: Optional[StarlinkClient] = None


def get_client() -> StarlinkClient:
    """Отримати або створити глобальний клієнт Starlink."""
    global _global_client
    if _global_client is None:
        _global_client = StarlinkClient()
    return _global_client


def get_location(target: str = f"{DEFAULT_STARLINK_IP}:{DEFAULT_STARLINK_PORT}") -> Dict[str, Any]:
    """Отримати координати з Starlink (функція-обгортка)."""
    return get_client().get_location(target)


def get_status(target: str = f"{DEFAULT_STARLINK_IP}:{DEFAULT_STARLINK_PORT}") -> Dict[str, Any]:
    """Отримати статус Starlink (функція-обгортка)."""
    return get_client().get_status(target)


def reboot(target: str = f"{DEFAULT_STARLINK_IP}:{DEFAULT_STARLINK_PORT}") -> bool:
    """Перезавантажити Starlink (функція-обгортка)."""
    return get_client().reboot(target)


def set_gps(target: str = f"{DEFAULT_STARLINK_IP}:{DEFAULT_STARLINK_PORT}", 
            enable: bool = True) -> bool:
    """Встановити GPS на Starlink (функція-обгортка)."""
    return get_client().set_gps(target, enable)


if __name__ == "__main__":
    """Тестування модуля."""
    logger.info("Тест modulu starlink.py")
    
    try:
        client = get_client()
        
        # Тест get_location
        print("\n=== Тест get_location ===")
        loc = client.get_location()
        print(f"Available: {loc.get('available')}")
        print(f"Latitude: {loc.get('latitude')}")
        print(f"Longitude: {loc.get('longitude')}")
        print(f"Altitude: {loc.get('altitude')}")
        print(f"GPS Sats: {loc.get('gps_sats')}")
        
        # Тест get_status
        print("\n=== Тест get_status ===")
        status = client.get_status()
        print(f"Available: {status.get('available')}")
        print(f"State: {status.get('state')}")
        print(f"Uptime: {status.get('uptime')}")
        
    except Exception as e:
        logger.error(f"Тест завершено з помилкою: {e}")
