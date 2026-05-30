#!/usr/bin/env python3
"""
External Entrypoint for Sirena Video Service Manager
"""
import sys
from pathlib import Path

# Поточна папка, де лежить цей скрипт
current_dir = Path(__file__).resolve().parent

# Папка, де лежать самі модулі менеджера
modules_dir = current_dir / "service_manager"

# Додаємо обидва шляхи в sys.path
if str(current_dir) not in sys.path:
    sys.path.insert(0, str(current_dir))

if str(modules_dir) not in sys.path:
    sys.path.insert(0, str(modules_dir))  # Це дозволить файлу main.py спокійно робити `import config`

try:
    # Імпортуємо модуль main з папки service_manager
    from service_manager import main
except ImportError as e:
    print(f"Помилка імпорту модулів менеджера: {e}")
    print("Перевірте, чи папка service_manager та файли всередині неї на місці.")
    sys.exit(1)

if __name__ == "__main__":
    main.main()