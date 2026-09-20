# -*- coding: utf-8 -*-
"""
Свои шрифты из assets/fonts — модуль рядом с main.py.

Зачем: в портативной сборке нет гарантии, что на чужом компьютере есть нужный
шрифт. Положите .ttf/.otf в assets/fonts — приложение подключит их при запуске
( QFontDatabase.addApplicationFont ), и тему можно будет описать как
    {"font_family": "Inter"}
— шрифт появится и на чужой машине.

Проверка: list_families() возвращает то, что реально загрузилось; если файлов нет,
приложение работает со стандартным набором (Segoe UI и т.п.).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

SUPPORTED_SUFFIXES = (".ttf", ".otf", ".ttc", ".woff2")
FONTS_DIRNAME = "fonts"


def fonts_dir(assets_dir: Path) -> Path:
    return Path(assets_dir) / FONTS_DIRNAME


def load_app_fonts(assets_dir: Path) -> list[str]:
    """
    Подключает все шрифты из assets/fonts. Возвращает список семейств.

    Вызывать один раз при старте (QFontDatabase требует QApplication). Ошибки
    отдельных файлов не мешают остальным.
    """
    from PySide6.QtGui import QFontDatabase

    directory = fonts_dir(assets_dir)
    families: list[str] = []
    try:
        if not directory.is_dir():
            return families
        files = sorted(
            path for path in directory.iterdir()
            if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES
        )
    except OSError as exc:
        print(f"[шрифты] не удалось прочитать {directory}: {exc}")
        return families

    for path in files:
        try:
            font_id = QFontDatabase.addApplicationFont(str(path))
            if font_id < 0:
                print(f"[шрифты] {path.name}: формат не поддерживается Qt")
                continue
            loaded = QFontDatabase.applicationFontFamilies(font_id)
            for family in loaded:
                if family and family not in families:
                    families.append(family)
            print(f"[шрифты] подключён {path.name}: {', '.join(loaded) or 'семейство не определено'}")
        except Exception as exc:  # noqa: BLE001 — битый файл не должен мешать запуску
            print(f"[шрифты] {path.name}: {type(exc).__name__}: {exc}")
    return families


def resolve_family(theme: dict, fallback: str = "") -> str:
    """
    Шрифт из темы: «font_family» (строка или список — берём первый доступный).

    Возвращает имя семейства или пустую строку, если в теме ничего нет.
    """
    from PySide6.QtGui import QFontDatabase

    value = (theme or {}).get("font_family")
    if not value:
        return ""
    candidates = [value] if isinstance(value, str) else list(value or [])
    available = set(QFontDatabase.families())
    for candidate in candidates:
        name = str(candidate).strip()
        if not name:
            continue
        if not available or name in available:
            return name
    # шрифт не найден — берём первый предложенный, Qt подставит замену
    return str(candidates[0]).strip() if candidates else ""


def font_report(assets_dir: Path) -> str:
    """Короткая справка для логов/диалога: что лежит в папке шрифтов."""
    directory = fonts_dir(assets_dir)
    try:
        files = [p.name for p in sorted(directory.iterdir()) if p.is_file()]
    except OSError:
        files = []
    if not files:
        return f"свои шрифты не подключены (папка {directory} пуста)"
    return f"шрифтов в assets/{FONTS_DIRNAME}: {len(files)} — {', '.join(files[:6])}"


def ensure_fonts_dir(assets_dir: Path) -> Optional[Path]:
    """Создаёт папку шрифтов (вызывается из настроек, чтобы её было куда положить)."""
    directory = fonts_dir(assets_dir)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        return directory
    except OSError:
        return None
