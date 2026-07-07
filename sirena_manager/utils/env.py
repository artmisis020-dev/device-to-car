from typing import Dict
from sirena_manager.config import ROOT_ENV_PATH
from pathlib import Path

ROOT_ENV_FILE = Path(ROOT_ENV_PATH)


def read_env(env_file: Path = ROOT_ENV_FILE) -> Dict[str, str]:
    values: Dict[str, str] = {}
    try:
        for raw_line in env_file.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"')
    except FileNotFoundError:
        pass
    return values


def write_env( values: Dict[str, str], env_file: Path = ROOT_ENV_FILE) -> None:
    existing_lines = []
    if env_file.exists():
        existing_lines = env_file.read_text(encoding="utf-8").splitlines()

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

    env_file.write_text("\n".join(updated_lines) + "\n", encoding="utf-8")
