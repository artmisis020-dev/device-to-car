"""Читання/запис /opt/sirena/.env — той самий формат, що
sirena_manager/utils/env.py. Навмисно окрема, самодостатня копія (не
крос-імпорт): кожен top-level модуль цього репо розгортається окремо,
власним venv/деплоєм — additional_modules не залежить від sirena_manager
на рівні Python-імпортів, лише читає/пише той самий файл на диску."""

from pathlib import Path
from typing import Dict


def read_env(path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    try:
        for raw_line in Path(path).read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"')
    except FileNotFoundError:
        pass
    return values


def write_env(values: Dict[str, str], path) -> None:
    p = Path(path)
    existing_lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []

    updated_lines = []
    written = set()
    for raw_line in existing_lines:
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#") or "=" not in raw_line:
            updated_lines.append(raw_line)
            continue
        key = raw_line.split("=", 1)[0].strip()
        if key in values:
            updated_lines.append(f"{key}={values[key]}")
            written.add(key)
        else:
            updated_lines.append(raw_line)

    for key, value in values.items():
        if key not in written:
            updated_lines.append(f"{key}={value}")

    p.write_text("\n".join(updated_lines) + "\n", encoding="utf-8")
