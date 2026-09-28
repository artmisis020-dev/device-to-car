"""Спільні геометричні хелпери для порівняння варіантів згладжування Starlink GPS.

Усі 4 варіанти рахуються в локальних метрах (проста рівнокутна проєкція
навколо одного спільного origin), а не в градусах lat/lon — так простіше й
дешевше рахувати відстані/швидкості й інтегрувати інерційну швидкість
(vx/vy вже в м/с), а похибка апроксимації на масштабі одного польоту
(сотні метрів) нехтовно мала.
"""

from __future__ import annotations

import datetime
import math

EARTH_R_M = 6_371_000.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_R_M * math.asin(math.sqrt(a))


def make_projector(lat0: float, lon0: float):
    """(lat, lon) -> (x_схід_м, y_північ_м) відносно (lat0, lon0)."""
    coslat0 = math.cos(math.radians(lat0))

    def project(lat: float, lon: float) -> tuple[float, float]:
        x = (lon - lon0) * 111_320.0 * coslat0
        y = (lat - lat0) * 110_540.0
        return x, y

    return project


def parse_iso_to_epoch(ts_iso: str) -> float:
    """'2026-09-26T14:43:00+03:00' -> unix epoch (UTC)."""
    return datetime.datetime.fromisoformat(ts_iso).timestamp()


def ne_delta_m(lat1: float, lon1: float, lat2: float, lon2: float) -> tuple[float, float]:
    """(north_m, east_m) зміщення від (lat1,lon1) до (lat2,lon2)."""
    project = make_projector(lat1, lon1)
    east_m, north_m = project(lat2, lon2)
    return north_m, east_m


def add_ne_offset_m(lat: float, lon: float, north_m: float, east_m: float) -> tuple[float, float]:
    """Зсуває (lat, lon) на (north_m, east_m) метрів — обернена операція до
    проєкції в make_projector(). Використовується для інерційного
    dead-reckoning: нова точка = стара точка + (vx*dt, vy*dt)."""
    new_lat = lat + north_m / 110_540.0
    new_lon = lon + east_m / (111_320.0 * math.cos(math.radians(lat)))
    return new_lat, new_lon
