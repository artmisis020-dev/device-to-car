#!/usr/bin/env python3
from __future__ import annotations

"""
Автономне виявлення V4L2/USB камер на Linux (Raspberry Pi).

 сервіс знаходить усі підключені USB-камери, читає
власну назву кожної камери та формує чистий список. Якщо дві камери мають
однакову назву, додається унікальний айді (стабільний USB-порт),
щоб кожен запис у списку був унікальним.

Оброблені нюанси:
  - Однакові камери можуть мати спільний USB serial -> by-id конфліктує та
    втрачає одну з них. Стабільний ключ тут — шлях USB-порту, він унікальний
    для кожної камери.
  - Кожна UVC-камера має вузол захоплення і metadata-вузол -> metadata відкидається.
  - Внутрішні ISP/codec вузли Raspberry Pi (video14/19/20...) не є USB -> відкидаються.
  - Нумерація /dev/videoN змінюється між перезапусками -> вузол щоразу шукається
    заново за стабільним id, без кешування.
"""


import logging
import re
from pathlib import Path
import subprocess
from collections import Counter
from dataclasses import dataclass, asdict

try:
    import pyudev
except ImportError:
    pyudev = None

logger = logging.getLogger(__name__)


@dataclass
class Camera:
    id: str  # стабільний ключ, переживає reboot/replug (наприклад "usb-1.3")
    label: str  # назва для інтерфейсу; уточнення додається тільки при збігу назв
    name: str  # сира назва, яку повідомляє камера
    path: str  # ПОТОЧНИЙ /dev/videoN -- може змінюватися між перезапусками
    port: str  # шлях USB-порту -- унікальний/стабільний ідентифікатор
    serial: str
    vendor_id: str
    model_id: str

    def as_dict(self) -> dict:
        return asdict(self)


def _clean(s: str | None) -> str:
    """udev кодує пробіли як підкреслення; приводимо назву до нормального вигляду."""
    return (s or "").replace("_", " ").strip()


def _usb_port(id_path: str) -> str:
    """'platform-...-usb-0:1.3:1.0' -> '1.3'"""
    m = re.search(r"usb(?:v\d+)?-\d+:([\d.]+):", id_path or "")
    return m.group(1) if m else ""


def _get_camera_name(dev) -> str:
    """Власна назва камери, зібрана з USB manufacturer + product.

    Багато generic-камер задають V4L2 card name як неінформативний рядок
    "Product: Product", тому назву збираємо з USB descriptor strings
    (те, що показує `lsusb`) і лише якщо вони порожні, використовуємо назву card.
    """
    vendor = _clean(dev.get("ID_VENDOR"))
    model = _clean(dev.get("ID_MODEL"))

    parts = []
    if vendor and vendor.lower() not in model.lower():
        parts.append(vendor)  # уникаємо "Logitech Logitech Webcam"
    if model:
        parts.append(model)
    name = " ".join(parts).strip()

    if not name:  # останній резервний варіант: V4L2 card name
        prod = _clean(dev.get("ID_V4L_PRODUCT"))
        bits = [b.strip() for b in prod.split(":")]
        if len(bits) == 2 and bits[0].lower() == bits[1].lower():
            prod = bits[0]  # згортаємо "Camera: Camera" -> "Camera"
        name = prod

    return name or "Camera"


def _is_capture_node(dev) -> bool:
    """Кожна UVC-камера (звичайна USB-камера) реєструє в системі два вузли capture і metadata. Перший використовується для захоплення відео, другий — для читання властивостей камери. Вони мають однакову назву, але різні ID_V4L_CAPABILITIES. Потрібно відкидати metadata-вузли, щоб не було дублювання камер у списку."""
    """True для реальних вузлів захоплення; False для сусіднього metadata-вузла."""
    caps = dev.get("ID_V4L_CAPABILITIES")
    if caps is not None:
        return ":capture:" in caps
    try:  # резервний варіант: питаємо kernel напряму
        out = subprocess.run(
            ["v4l2-ctl", "-d", dev.device_node, "--all"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except Exception:
        return False
    return "Video Capture" in out.split("Device Caps", 1)[-1]


def list_cameras(verbose: bool = True) -> list[Camera]:
    """Усі підключені USB-камери у детермінованому порядку, з унікальними назвами."""
    if pyudev is None:
        logger.warning("pyudev недоступний — пошук камер вимкнено")
        return []
    ctx = pyudev.Context()
    seen_ports: set[str] = set()
    cameras: list[Camera] = []

    for dev in ctx.list_devices(subsystem="video4linux"):
        if dev.get("ID_BUS") != "usb":  # пропускаємо внутрішні ISP/codec вузли
            continue
        if not _is_capture_node(dev):  # пропускаємо metadata-вузли
            continue

        port = _usb_port(dev.get("ID_PATH", "")) or dev.get("ID_SERIAL_SHORT", "")
        if not port or port in seen_ports:  # одна логічна камера на порт
            continue
        seen_ports.add(port)

        cameras.append(
            Camera(
                id=f"usb-{port}",
                label="",  # заповнюється нижче, коли вже відомо які назви дублюються
                name=_get_camera_name(dev),
                path=dev.device_node,
                port=port,
                serial=dev.get("ID_SERIAL_SHORT", ""),
                vendor_id=dev.get("ID_VENDOR_ID", ""),
                model_id=dev.get("ID_MODEL_ID", ""),
            )
        )

    cameras.sort(key=lambda c: c.port)
    if verbose:
        print(f"Знайдені камери: {[c.as_dict() for c in cameras]}")
    # Уточнюємо назву: додаємо порт ТІЛЬКИ для назв, які зустрічаються більше одного разу.
    counts = Counter(c.name for c in cameras)
    for c in cameras:
        c.label = f"{c.name} ({c.port})" if counts[c.name] > 1 else c.name

    return cameras


def get_default_camera() -> Camera | None:
    cams = list_cameras(verbose=False)
    return cams[0] if cams else None


def resolve_video_device(
    configured_device: str | None, default_device: str = "/dev/video0"
) -> str:
    """Повертає існуючий VIDEO_DEVICE або першу знайдену USB capture камеру."""
    configured_device = (configured_device or default_device).strip()
    if Path(configured_device).exists():
        return configured_device

    default_camera = get_default_camera()
    if default_camera:
        return default_camera.path

    return configured_device


def video_nodes() -> list[str]:
    """Повертає всі поточні /dev/video* вузли для діагностики."""
    return sorted(str(path) for path in Path("/dev").glob("video*"))

def resolve_device(cam_path: str) -> str | None:
    """Стабільний id -> поточний /dev/videoN. Викликати щоразу при start/switch."""
    for cam in list_cameras(verbose=False):
        if cam.path == cam_path:
            return cam.path
    return None

def node_for_id(cam_id: str) -> str | None:
    """Стабільний id (напр. 'usb-1.3') -> поточний /dev/videoN.

    Викликати щоразу при start/switch, бо нумерація /dev/videoN змінюється
    між перезапусками. На вхід очікує СТАБІЛЬНИЙ id (Camera.id), а не шлях.
    """
    for cam in list_cameras(verbose=False):
        if cam.id == cam_id:
            return cam.path
    return None


def list_formats(device: str) -> str:
    """`v4l2-ctl --list-formats-ext` для вузла -- зручно для побудови caps."""
    try:
        return subprocess.run(
            ["v4l2-ctl", "-d", device, "--list-formats-ext"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except Exception as e:
        return f"(could not query {device}: {e})"


_FOURCC_ALIASES = {"YUY2": "YUYV", "YUYV": "YUYV", "MJPG": "MJPG", "JPEG": "MJPG"}


def _parse_formats_ext(device: str) -> list[tuple[str, int, int, int]]:
    """Розбирає вивід `list_formats()` у список (fourcc, width, height, fps)."""
    modes: list[tuple[str, int, int, int]] = []
    fourcc = None
    size = None
    for line in list_formats(device).splitlines():
        line = line.strip()
        m = re.match(r"^\[\d+\]:\s*'(\w+)'", line)
        if m:
            fourcc = m.group(1)
            continue
        m = re.match(r"^Size:\s*\S*\s*(\d+)x(\d+)", line)
        if m:
            size = (int(m.group(1)), int(m.group(2)))
            continue
        m = re.search(r"\(([\d.]+)\s*fps\)", line)
        if m and fourcc and size:
            modes.append((fourcc, size[0], size[1], round(float(m.group(1)))))
    return modes


def supports_mode(device: str, input_format: str, width: int, height: int, fps: int) -> bool:
    """Перевіряє, чи камера дійсно вміє задану комбінацію формат/роздільність/fps
    -- щоб зловити невідповідність до старту GStreamer-пайплайна чіткою
    помилкою, а не незрозумілим збоєм негоціації caps.

    Фейлиться "відкрито" (повертає True), якщо не вдалось опитати камеру --
    краще спробувати старт і побачити реальну помилку, ніж заблокувати
    легітимний запуск через збій самого опитування."""
    wanted = _FOURCC_ALIASES.get(input_format.upper())
    if wanted is None:
        return True
    try:
        modes = _parse_formats_ext(device)
    except Exception:
        return True
    if not modes:
        return True
    return any(
        fc == wanted and w == width and h == height and abs(f - fps) <= 1
        for fc, w, h, f in modes
    )


"""Для тестування цього модуля можна запустити його напряму: python3 cameras_services.py (але треба бути в енві)"""
if __name__ == "__main__":
    cams = list_cameras()
    if not cams:
        print("No USB cameras found.")
    for i, c in enumerate(cams):
        tag = "  <- default" if i == 0 else ""
        print(f"[{c.id}]  {c.label}  ->  {c.path}{tag}")
