"""Варіант 1: сирі точки Starlink, без жодної обробки — те, що зараз реально
йде далі (бо фільтр у navigation_module/main.py вимкнений: filtered = location)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from data_loader import RawPoint


def compute(raw_points: list[RawPoint]) -> list[tuple[float, float, float]]:
    """Повертає [(t, lat, lon), ...] — 1:1 копія сирих точок."""
    return [(p.t, p.lat, p.lon) for p in raw_points]
