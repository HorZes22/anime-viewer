# -*- coding: utf-8 -*-
"""
Источник AniLibria: клиент официального API и источник.

Шаг 1 миграции: код пока лежит в anime_viewer/_core.py, а этот модуль объявляет
свои имена — чтобы импорты (`from anime_viewer.sources.base import VideoSource`)
уже работали и не менялись, когда код физически переедет сюда.
"""

from __future__ import annotations

from .._core import *          # noqa: F401,F403

__all__ = ['AniLibriaClient', 'AniLibriaVideoSource', 'PlaceholderVideoSource']
