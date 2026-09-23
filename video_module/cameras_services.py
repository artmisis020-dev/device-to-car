#!/usr/bin/env python3
from __future__ import annotations

"""
Автономне виявлення V4L2-камер захоплення на Linux (Raspberry Pi) — USB
(веб-камери, тепловізори тощо) і будь-які інші підключені камери (напр. CSI).

Сервіс знаходить усі підключені камери, читає власну назву кожної та формує
чистий список. Якщо дві камери мають однакову назву, додається унікальний
айді (стабільний порт), щоб кожен запис у списку був унікальним.

Оброблені нюанси:
  - Однакові камери можуть мати спільний USB serial -> by-id конфліктує та
    втрачає одну з них. Стабільний ключ тут — шлях USB-порту (для не-USB
    камер — сирий ID_PATH), він унікальний для кожної камери.
  - Кожна UVC-камера має вузол захоплення і metadata-вузол -> metadata відкидається.
  - Внутрішні ISP/codec вузли Raspberry Pi (video14/19/20...) — не камери, а
    m2m (мають і :capture:, і :output: одночасно) -> відкидаються за цією
    ознакою, а не за типом шини (раніше фільтр "не USB" випадково відкидав і
    справжні не-USB камери, напр. CSI).
  - CSI-приймач (rp1-cfe на RPi5, unicam на старіших моделях) навмисно НЕ
    показується в списку — сирий Bayer з CSI цей пайплайн (v4l2src без
    ISP/дебаєризації) не вміє перетворити на картинку (перевірено: реальна
    спроба стрімити падає з VIDIOC_STREAMON EINVAL), тож показ його як
    "камери 3" тільки провокує биту кнопку перемикання. Camera.modes
    (нижче) лишається як загальна інфраструктура для валідації режиму
    будь-якої камери, яка ВСЕ Ж показується (перемикання враховує повний
    режим формат+ширина+висота+fps, не тільки роздільність).
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
    modes: list = None  # [[fourcc, ширина, висота, fps], ...] YUYV/MJPG, найбільші перші; [] = нема сумісного режиму

    def as_dict(self) -> dict:
        return asdict(self)


def _clean(s: str | None) -> str:
    """udev кодує пробіли як підкреслення; приводимо назву до нормального вигляду."""
    return (s or "").replace("_", " ").strip()


def _usb_port(id_path: str) -> str:
    """'platform-xhci-hcd.0-usb-0:1.3:1.0' -> 'xhci-hcd.0:1.3'.

    Контролер (xhci-hcd.N) обов'язково включаємо в результат: на RPi5 —
    кілька незалежних USB-контролерів (окремі шини, видно в lsusb як різні
    Bus), і сам лише port-chain після "usb-N:" тут неоднозначний — дві
    фізично РІЗНІ камери на "порту 2" РІЗНИХ контролерів (xhci-hcd.0 і
    xhci-hcd.1) дають однаковий "2", а _stable_port() використовує це як
    ключ дедуплікації "одна камера на порт" — без контролера в ключі друга
    камера мовчки відкидається як "дублікат" першої (реальний баг, знайдено
    на sirena-P-4: MacroSilicon-грабер і тепловізор обидва на "порту 2",
    різні контролери — показувалась лише одна з двох підключених камер)."""
    m = re.search(r"(xhci-hcd\.\d+)-usb(?:v\d+)?-\d+:([\d.]+):", id_path or "")
    if m:
        return f"{m.group(1)}:{m.group(2)}"
    # Фолбек на старий патерн — плати з одним USB-контролером, де ID_PATH
    # не містить "xhci-hcd.N" (напр. інший SoC/драйвер).
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


# Вузли, які треба відкинути з двох різних причин — і тут навмисно в одному
# списку, бо ефект однаковий (не показувати в списку камер):
#   1. Справді не камери: внутрішній кодек/ISP RPi (bcm2835-codec/isp,
#      pispbe, rpivid) — m2m-вузли, вже відсіюються _is_output_capable(),
#      тут лише подвійна страховка за назвою на випадок дивної прошивки.
#   2. CSI-приймач (rp1-cfe на RPi5, unicam на старіших моделях) — технічно
#      сенсор може бути підключений (перевірено: ov5647), але сирий Bayer
#      цей пайплайн (v4l2src без ISP/дебаєризації) не перетравить —
#      реальна спроба стрімити падає з VIDIOC_STREAMON EINVAL. Показ його
#      як окремої "камери" в адмінці лише провокує биту кнопку
#      перемикання, тож ховаємо повністю, поки нема libcamera-інтеграції.
_NON_CAMERA_NAME_HINTS = (
    "bcm2835-codec", "bcm2835-isp", "pispbe", "rpivid", "hevc-dec",
    "rp1-cfe", "unicam",
)


def _is_output_capable(dev) -> bool:
    """m2m-вузли (кодек/ISP) вміють і :capture:, і :output: одночасно —
    справжній сенсор камери завжди тільки :capture:."""
    caps = dev.get("ID_V4L_CAPABILITIES") or ""
    return ":output:" in caps


def _looks_like_non_camera(dev) -> bool:
    haystack = " ".join(
        filter(None, [dev.get("ID_V4L_PRODUCT"), dev.get("ID_MODEL"), dev.sys_name])
    ).lower()
    return any(hint in haystack for hint in _NON_CAMERA_NAME_HINTS)


def _stable_port(dev) -> str:
    """Стабільний (переживає reboot/replug) ідентифікатор фізичного порту —
    для USB це номер usb-порту, для вбудованих камер (CSI і т.п.) стабільного
    "порту" в тому ж сенсі нема, тож використовуємо сам ID_PATH (він теж
    прив'язаний до фізичного роз'єму на платі, а не до /dev/videoN)."""
    id_path = dev.get("ID_PATH", "")
    return _usb_port(id_path) or dev.get("ID_SERIAL_SHORT", "") or id_path


def _has_real_formats(device_node: str) -> bool:
    """Чи вузол взагалі оголошує хоч якийсь формат захоплення (v4l2-ctl).
    Це лише заслін від вузлів, де щось пішло не так на рівні probe/kernel —
    НЕ спроба визначити "чи є фізичний сенсор". Для CSI (rp1-cfe/unicam)
    драйвер декларує один і той самий загальний список форматів незалежно
    від того, чи підключений сенсор, тож на цьому рівні реальну придатність
    камери відрізнити неможливо — цим займається _usable_modes()
    нижче (реальна відсутність узгодженого Discrete+fps режиму)."""
    try:
        output = list_formats(device_node)
    except Exception:
        return True  # фейлимось "відкрито" — краще зайва камера, ніж загублена
    return bool(re.search(r"\[\d+\]:\s*'\w+'", output))


def _usable_modes(device: str) -> list:
    """Реально негойційовані (Discrete size + fps) режими для форматів, які
    вміє споживати наш GStreamer-пайплайн (YUYV/MJPG) — [fourcc, width,
    height, fps], fourcc уже нормалізований через _FOURCC_ALIASES (щоб
    збігався з тим, що читає supports_mode()/INPUT_FORMAT). Найбільша площа
    й fps першими. Порожній список — чіткий сигнал "камера є, але жодного
    сумісного з пайплайном режиму нема" (типово для сирого Bayer з CSI);
    саме на ці дані (а не лише на ширину/висоту — той самий пристрій часто
    підтримує різний максимальний fps на різних роздільностях/форматах)
    орієнтується supervisor.set_camera() на боці sirena_manager, щоб не
    перемикати на камеру чи роздільність, яка одразу зверне пайплайн."""
    out = []
    try:
        modes = _parse_formats_ext(device)
    except Exception:
        return []
    for fourcc, w, h, fps in modes:
        norm = _FOURCC_ALIASES.get(fourcc)
        if norm not in ("YUYV", "MJPG"):
            continue
        out.append([norm, w, h, fps])
    out.sort(key=lambda m: (m[1] * m[2], m[3]), reverse=True)
    return out


def list_cameras(verbose: bool = True) -> list[Camera]:
    """Усі підключені камери захоплення у детермінованому порядку, з унікальними
    назвами — USB-камери (веб-камери, тепловізори тощо) і будь-які інші
    (напр. CSI), окрім внутрішніх ISP/кодек-вузлів RPi і вузлів без реального
    сенсора."""
    if pyudev is None:
        logger.warning("pyudev недоступний — пошук камер вимкнено")
        return []
    ctx = pyudev.Context()
    seen_ports: set[str] = set()
    cameras: list[Camera] = []

    for dev in ctx.list_devices(subsystem="video4linux"):
        if not _is_capture_node(dev):  # пропускаємо metadata-вузли
            continue
        if _is_output_capable(dev):  # пропускаємо m2m (кодек/ISP) вузли
            continue
        if _looks_like_non_camera(dev):  # додатковий запобіжник за назвою/драйвером
            continue

        port = _stable_port(dev)
        if not port or port in seen_ports:  # одна логічна камера на порт
            continue
        if not _has_real_formats(dev.device_node):  # вузол взагалі нічого не оголошує
            continue
        seen_ports.add(port)

        bus = dev.get("ID_BUS") or "cam"
        cameras.append(
            Camera(
                id=f"{bus}-{port}",
                label="",  # заповнюється нижче, коли вже відомо які назви дублюються
                name=_get_camera_name(dev),
                path=dev.device_node,
                port=port,
                serial=dev.get("ID_SERIAL_SHORT", ""),
                vendor_id=dev.get("ID_VENDOR_ID", ""),
                model_id=dev.get("ID_MODEL_ID", ""),
                modes=_usable_modes(dev.device_node),
            )
        )

    cameras.sort(key=lambda c: c.port)
    if verbose:
        print(f"Знайдені камери ({len(cameras)}): {[c.as_dict() for c in cameras]}")
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
        print("No cameras found.")
    for i, c in enumerate(cams):
        tag = "  <- default" if i == 0 else ""
        print(f"[{c.id}]  {c.label}  ->  {c.path}{tag}")
