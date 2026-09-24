"""Конфігурація pixel_tracking — той самий стиль, що sirena_manager/config.py
(модульні константи з env, без dataclass).

Після переходу на GStreamer `tee` всередині video_module/srt_relay_capture.py
(див. video_module/capture_relay/track_tap.py) цей процес більше НЕ обробляє
кадри сам — він лише вмикає/вимикає trap-прапорець через .env і передає
ціль/статус через файли, які читає/пише srt_relay_capture.py."""

import os

MANAGER_HOST = os.environ.get("SIRENA_TRACK_HOST", "0.0.0.0")
MANAGER_PORT = int(os.environ.get("SIRENA_TRACK_PORT", "9075"))

# Той самий .env, що вже читає/пише sirena_manager і video_module — жодного
# нового джерела правди. TRACK_TARGET_FILE/TRACK_STATUS_FILE — ті самі
# дефолти, що й у video_module/capture_relay/config.py, щоб обидва боки
# дивились в один і той самий файл без явного узгодження шляхів.
ROOT_ENV_PATH = os.environ.get("SIRENA_ROOT_ENV_PATH", "/opt/sirena/.env")
TRACK_TARGET_FILE = os.environ.get("SIRENA_TRACK_TARGET_FILE", "/opt/sirena/track_target.json")
TRACK_STATUS_FILE = os.environ.get("SIRENA_TRACK_STATUS_FILE", "/opt/sirena/track_status.json")

SYSTEMCTL = os.environ.get("SIRENA_SYSTEMCTL", "sudo systemctl")
SRT_RELAY_CAPTURE_UNIT = "srt-relay-capture.service"
RESTART_TIMEOUT_S = int(os.environ.get("SIRENA_TRACK_RESTART_TIMEOUT_S", "20"))
