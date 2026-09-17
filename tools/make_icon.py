# -*- coding: utf-8 -*-
"""
Сборка иконки приложения (assets/app_icon.ico) из логотипа.

Иконка многоразмерная: Windows берёт нужный размер для панели задач, проводника
и ярлыка, поэтому она выглядит чётко и мелкой, и крупной. Внутри — PNG-записи
(их понимает Windows Vista и новее, а файл весит в разы меньше, чем у обычного
несжатого .ico).

Запускать после замены логотипа:
    .\\runtime\\python.exe tools\\make_icon.py
"""
import io
import os
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QBuffer, QByteArray, QSize, Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter

app = QGuiApplication([])

SOURCE = ROOT / "assets" / "logo_dark.png"
TARGET = ROOT / "assets" / "app_icon.ico"
SIZES = (16, 24, 32, 48, 64, 128, 256)


def png_bytes(image: QImage) -> bytes:
    """Картинка в PNG-байтах (для записи внутрь .ico)."""
    buffer = QByteArray()
    device = QBuffer(buffer)
    device.open(QBuffer.OpenModeFlag.WriteOnly)
    image.save(device, "PNG")
    return bytes(buffer)


def square(image: QImage, size: int) -> QImage:
    """Логотип на прозрачном квадратном холсте — так иконка не «прыгает» по краям."""
    canvas = QImage(QSize(size, size), QImage.Format.Format_ARGB32)
    canvas.fill(Qt.GlobalColor.transparent)
    scaled = image.scaled(
        QSize(size, size), Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    painter = QPainter(canvas)
    painter.drawImage((size - scaled.width()) // 2, (size - scaled.height()) // 2, scaled)
    painter.end()
    return canvas


def build_ico(images: list[QImage], path: Path) -> int:
    """
    Записывает .ico с PNG-записями.

    Формат: заголовок (6 байт) + по 16 байт на каждый размер + сами картинки.
    Размер 256 в записи кодируется нулём — так требует формат.
    """
    payloads = [(image, png_bytes(image)) for image in images]
    offset = 6 + 16 * len(payloads)

    out = bytearray()
    out += struct.pack("<HHH", 0, 1, len(payloads))     # reserved, type=icon, количество
    for image, data in payloads:
        out += struct.pack(
            "<BBBBHHII",
            0 if image.width() >= 256 else image.width(),
            0 if image.height() >= 256 else image.height(),
            0, 0, 1, 32, len(data), offset,
        )
        offset += len(data)
    for _image, data in payloads:
        out += data
    path.write_bytes(bytes(out))
    return len(out)


if not SOURCE.is_file():
    print(f"нет файла {SOURCE} — сначала положите логотип в assets/")
    raise SystemExit(1)

logo = QImage(str(SOURCE))
if logo.isNull():
    print(f"{SOURCE} не читается")
    raise SystemExit(1)

images = [square(logo, size) for size in SIZES]
size_bytes = build_ico(images, TARGET)
print(f"иконка собрана: {TARGET.name}")
print(f"   размеры: {', '.join(str(s) for s in SIZES)} px")
print(f"   вес: {size_bytes / 1024:.1f} КБ")

# проверка: файл должен читаться обратно и содержать все размеры
check = QImage(str(TARGET))
print(f"   проверка чтения: {'OK' if not check.isNull() else 'ПРОВАЛ'} "
      f"({check.width()}x{check.height()})")
data = TARGET.read_bytes()
count = struct.unpack_from("<H", data, 4)[0]
print(f"   записей внутри: {count} (ожидалось {len(SIZES)})")
