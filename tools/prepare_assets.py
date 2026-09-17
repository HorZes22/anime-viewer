# -*- coding: utf-8 -*-
"""
Подготовка присланных картинок для приложения.

Что делает:
  * складывает оригиналы в assets/source/ (ничего не теряется);
  * обрезает лишние поля и приводит размеры к нужным для интерфейса;
  * у «светлого» логотипа убирает белый фон, чтобы он ложился на панель темы;
  * сохраняет готовые файлы в assets/ с понятными именами.

Запускать один раз (или после замены картинок в assets/source/):
    .\\runtime\\python.exe tools\\prepare_assets.py
"""
import io
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter

app = QGuiApplication([])

ASSETS = ROOT / "assets"
SOURCE = ASSETS / "source"
ASSETS.mkdir(exist_ok=True)
SOURCE.mkdir(exist_ok=True)

# что откуда берём и как называем результат
PLAN = {
    "bg_app_16x9.png": "app_bg.jpg",
    "bg_player.png": "player_bg.jpg",
    "bg_manga_strip.png": "reader_bg.jpg",
    "placeholder_poster.png": "poster_placeholder.png",
    "logo_main.png": "logo_dark.png",
    "logo_alt.png": "logo_light.png",
    "avatar_default.png": "empty_state.png",
    "icon_tag.png": "tag_icon.png",
}


def content_rect(image: QImage, threshold: int = 8, white_key: bool = False) -> QRect:
    """
    Границы «полезного» содержимого: для картинок с прозрачностью — по альфе,
    для белого фона (white_key) — по пикселям, отличным от белого.
    """
    left, top = image.width(), image.height()
    right = bottom = -1
    for y in range(image.height()):
        for x in range(image.width()):
            color = image.pixelColor(x, y)
            if white_key:
                visible = 255 - min(color.red(), color.green(), color.blue()) > threshold
            else:
                visible = color.alpha() > threshold
            if visible:
                left = min(left, x)
                right = max(right, x)
                top = min(top, y)
                bottom = max(bottom, y)
    if right < 0:
        return QRect(0, 0, image.width(), image.height())
    margin_x = max(2, (right - left) // 50)
    margin_y = max(2, (bottom - top) // 50)
    rect = QRect(
        max(0, left - margin_x), max(0, top - margin_y),
        min(image.width(), right + margin_x) - max(0, left - margin_x) + 1,
        min(image.height(), bottom + margin_y) - max(0, top - margin_y) + 1,
    )
    return rect


def white_to_alpha(image: QImage) -> QImage:
    """
    Белый фон -> прозрачность (мягкий ключ по яркости).
    Нужно светлому логотипу: он нарисован на белом, а панель приложения светлая,
    но не чисто белая — жёсткий прямоугольник выглядел бы как наклейка.
    """
    result = image.convertToFormat(QImage.Format.Format_ARGB32)
    for y in range(result.height()):
        for x in range(result.width()):
            color = result.pixelColor(x, y)
            luma = 0.2126 * color.red() + 0.7152 * color.green() + 0.0722 * color.blue()
            if luma >= 240:
                alpha = 0
            elif luma <= 200:
                alpha = 255
            else:
                alpha = int(round((240 - luma) / 40 * 255))
            color.setAlpha(min(color.alpha(), alpha))
            result.setPixelColor(x, y, color)
    return result


def save(image: QImage, name: str) -> tuple[str, int, int]:
    path = ASSETS / name
    image.save(str(path), "PNG")
    print(f"   → {name}: {image.width()}x{image.height()}, "
          f"{path.stat().st_size // 1024} КБ")
    return name, image.width(), image.height()


print("Подготовка ассетов\n")
for source_name, target_name in PLAN.items():
    source_path = ROOT / source_name
    if not source_path.exists():
        source_path = SOURCE / source_name
    if not source_path.exists():
        print(f"  {source_name}: нет файла — пропускаю")
        continue
    image = QImage(str(source_path))
    if image.isNull():
        print(f"  {source_name}: не читается")
        continue
    print(f"  {source_name} ({image.width()}x{image.height()})")

    if target_name in ("app_bg.jpg", "player_bg.jpg", "reader_bg.jpg"):
        # обои: в приложении они затемняются под тему, поэтому JPEG (качество 92)
        # неотличим от PNG, а весит в разы меньше
        path = ASSETS / target_name
        image.convertToFormat(QImage.Format.Format_RGB32).save(str(path), "JPG", 92)
        print(f"   → {target_name}: {image.width()}x{image.height()}, "
              f"{path.stat().st_size // 1024} КБ")

    elif target_name == "poster_placeholder.png":
        # заглушка постера: делегат обрезает её по пропорции, 512 px достаточно
        save(image.scaled(512, 512, Qt.AspectRatioMode.KeepAspectRatio,
                          Qt.TransformationMode.SmoothTransformation),
             target_name)

    elif target_name == "logo_dark.png":
        crop = content_rect(image)
        print(f"      содержимое: {crop.width()}x{crop.height()} в {image.width()}x{image.height()}")
        cropped = image.copy(crop).convertToFormat(QImage.Format.Format_ARGB32)
        save(cropped.scaled(128, 128, Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.SmoothTransformation), target_name)

    elif target_name == "logo_light.png":
        cropped = image.copy(content_rect(image, threshold=6, white_key=True))
        transparent = white_to_alpha(cropped.convertToFormat(QImage.Format.Format_ARGB32))
        print(f"      содержимое: {cropped.width()}x{cropped.height()}, белый фон убран")
        save(transparent.scaled(128, 128, Qt.AspectRatioMode.KeepAspectRatio,
                                Qt.TransformationMode.SmoothTransformation), target_name)

    elif target_name == "empty_state.png":
        cropped = image.copy(content_rect(image, threshold=16))
        print(f"      содержимое: {cropped.width()}x{cropped.height()}")
        save(cropped.scaled(256, 256, Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.SmoothTransformation), target_name)

    elif target_name == "tag_icon.png":
        cropped = image.copy(content_rect(image, threshold=16))
        print(f"      содержимое: {cropped.width()}x{cropped.height()}")
        # Иконка нарисована тонкими линиями на холсте 1024 px. При обычном
        # уменьшении линии «растворяются» (альфа усредняется), поэтому уменьшаем
        # и затем усиляем альфу: слабые пиксели отбрасываем, остальные делаем плотными.
        small = cropped.convertToFormat(QImage.Format.Format_ARGB32).scaled(
            64, 64, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        visible = 0
        for y in range(small.height()):
            for x in range(small.width()):
                color = small.pixelColor(x, y)
                alpha = color.alpha()
                alpha = 0 if alpha < 24 else min(255, int(alpha * 1.8))
                color.setAlpha(alpha)
                if alpha:
                    visible += 1
                small.setPixelColor(x, y, color)
        share = visible / max(1, small.width() * small.height()) * 100
        print(f"      видимых точек после уменьшения: {share:.0f}%")
        save(small, target_name)

    # оригинал переносим в assets/source
    if source_path.parent == ROOT:
        shutil.move(str(source_path), str(SOURCE / source_name))
        print(f"      оригинал перенесён в assets/source/{source_name}")

print("\nГотово. Содержимое assets/:")
for path in sorted(ASSETS.glob("*.png")):
    print(f"   {path.name}: {path.stat().st_size // 1024} КБ")
