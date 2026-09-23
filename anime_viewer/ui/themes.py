# -*- coding: utf-8 -*-
"""
Темы, палитры, картинки оформления, иконки и таблица стилей.

Шаг 1 миграции: код пока лежит в anime_viewer/_core.py, а этот модуль объявляет
свои имена — чтобы импорты (`from anime_viewer.sources.base import VideoSource`)
уже работали и не менялись, когда код физически переедет сюда.
"""

from __future__ import annotations

from .._core import *          # noqa: F401,F403

__all__ = ['ASSET_FILES', 'DEFAULT_THEME', 'THEME_MODES', 'WallpaperWidget', 'asset_path', 'build_qss', 'ensure_icons', 'load_app_icon', 'load_asset_image', 'theme_colors', 'tinted_asset_icon', 'wallpaper_pixmap']
