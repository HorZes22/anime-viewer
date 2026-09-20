# -*- coding: utf-8 -*-
"""
Акцентный цвет из постера — отдельный модуль рядом с main.py.

Зачем: у каждой темы есть свой акцент, но пользователю часто хочется, чтобы
интерфейс «подстроился» под тайтл. Функция extract_accent_color() берёт постер,
находит в нём доминирующий насыщенный цвет и возвращает его в hex — приложение
подставляет результат в тему (настройка «Акцент из постера»).

Важно про зависимости: Pillow в портативную сборку не входит, зато уже есть Qt,
поэтому картинка разбирается через QImage (квантование по оттенку + усреднение) —
без новых зависимостей и без внешних процессов.

Кэш: cache/accent/<ключ>.json. Ключ — id тайтла (или имя файла), внутри лежат
цвет и время записи, поэтому повторный выбор тайтла не пересчитывает картинку.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional, Union

# Сколько цветов держим в гистограмме оттенка (24 = шаг 15°).
HUE_BUCKETS = 24
# Минимальная насыщенность и яркость пикселя, чтобы он считался «цветом»:
# тёмно-серые кадры и белые блики не должны задавать акцент.
MIN_SATURATION = 0.22
MIN_VALUE = 0.12
# Кэш живёт долго: постеры тайтлов не меняются.
CACHE_TTL = 60 * 60 * 24 * 30


def _hex(color: Any) -> str:
    """QColor -> #rrggbb."""
    return str(color.name())


def dominant_color(image: Any) -> Optional[Any]:
    """
    Доминирующий насыщенный цвет изображения (QColor) или None.

    Алгоритм: картинка уменьшается до 96×96 (этого достаточно и это быстро),
    каждый пиксель относится к корзине по оттенку, вес корзины — насыщенность ×
    яркость. Затем цвет усредняется внутри победившей корзины: так акцент не
    «прыгает» на отдельном ярком пикселе.
    """
    from PySide6.QtGui import QColor

    if image is None or getattr(image, "isNull", lambda: True)():
        return None
    scaled = image.scaled(96, 96)
    buckets: dict[int, list[tuple[int, int, int, float]]] = {}
    for y in range(scaled.height()):
        for x in range(scaled.width()):
            color = QColor(scaled.pixel(x, y))
            if not color.isValid():
                continue
            hue, saturation, value, alpha = color.getHsvF()
            if alpha == 0 or hue < 0:
                continue
            if saturation < MIN_SATURATION or value < MIN_VALUE:
                continue
            weight = saturation * value
            bucket = int(hue * HUE_BUCKETS) % HUE_BUCKETS
            buckets.setdefault(bucket, []).append(
                (color.red(), color.green(), color.blue(), weight)
            )
    if not buckets:
        return None

    def bucket_weight(items: list[tuple[int, int, int, float]]) -> float:
        return sum(weight for *_rgb, weight in items)

    best = max(buckets.values(), key=bucket_weight)
    total = sum(weight for *_rgb, weight in best) or 1.0
    red = int(sum(rgb[0] * weight for rgb in [(i[0], i[1], i[2], i[3]) for i in best]) / total)
    green = int(sum(i[1] * i[3] for i in best) / total)
    blue = int(sum(i[2] * i[3] for i in best) / total)
    return QColor(red, green, blue)


def readable_accent(color: Any) -> str:
    """
    Делает цвет пригодным для интерфейса: поднимает насыщенность и яркость.

    Причина: средний цвет постера часто тёмный и «грязный», а акцент должен
    читаться на тёмном фоне и поверх него должен быть различим текст
    (цвет текста поверх акцента приложение считает по яркости само).
    """
    from PySide6.QtGui import QColor

    hue, saturation, value, alpha = color.getHsvF()
    if hue < 0:
        return _hex(color)
    # тёмный акцент в интерфейсе почти не виден — поднимаем яркость до 0.75–0.9
    value = max(0.75, min(0.92, value if value > 0.55 else 0.8))
    saturation = max(0.42, min(0.85, saturation if saturation > 0.3 else 0.6))
    return _hex(QColor.fromHsvF(hue, saturation, value, alpha))


def extract_accent_color(
    source: Union[str, Path, bytes, Any],
    cache_dir: Optional[Path] = None,
    cache_key: str = "",
) -> str:
    """
    Акцентный цвет (#rrggbb) по постеру или пустая строка, если цвет не нашёлся.

    source — путь к файлу, байты картинки или готовый QImage.
    cache_dir + cache_key — куда писать результат (обычно cache/accent и id тайтла).
    """
    from PySide6.QtGui import QImage

    if cache_key and cache_dir is not None:
        cached = _read_cache(Path(cache_dir), cache_key)
        if cached:
            return cached

    image = _as_image(source, QImage)
    if image is None:
        return ""
    color = dominant_color(image)
    if color is None:
        return ""
    accent = readable_accent(color)
    if cache_key and cache_dir is not None:
        _write_cache(Path(cache_dir), cache_key, accent)
    return accent


def _as_image(source: Union[str, Path, bytes, Any], qimage_cls: Any) -> Optional[Any]:
    """Приводит вход к QImage (путь, байты или уже готовая картинка)."""
    if source is None:
        return None
    if isinstance(source, qimage_cls):
        return None if source.isNull() else source
    if isinstance(source, (bytes, bytearray)):
        image = qimage_cls()
        return image if image.loadFromData(bytes(source)) else None
    try:
        path = Path(source)
    except TypeError:
        return None
    if not path.is_file():
        return None
    image = qimage_cls(str(path))
    return None if image.isNull() else image


# ------------------------------------------------------------------ кэш
def _cache_file(cache_dir: Path, key: str) -> Path:
    safe = "".join(ch if (ch.isalnum() or ch in "-_") else "_" for ch in str(key))[:80]
    return cache_dir / f"{safe or 'accent'}.json"


def _read_cache(cache_dir: Path, key: str) -> str:
    path = _cache_file(cache_dir, key)
    try:
        if not path.exists():
            return ""
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return ""
        if time.time() - float(data.get("saved_at") or 0) > CACHE_TTL:
            return ""
        return str(data.get("accent") or "")
    except (OSError, ValueError, TypeError):
        return ""


def _write_cache(cache_dir: Path, key: str, accent: str) -> None:
    path = _cache_file(cache_dir, key)
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"accent": accent, "saved_at": time.time()}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass


def clear_cache(cache_dir: Path) -> int:
    """Удаляет кэш акцентов (кнопка «сбросить» в настройках). Возвращает число файлов."""
    removed = 0
    try:
        for path in Path(cache_dir).glob("*.json"):
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue
    except OSError:
        pass
    return removed
