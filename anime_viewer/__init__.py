# -*- coding: utf-8 -*-
"""
Anime Viewer — пакет приложения.

Здесь лежит разделённый main.py: рабочая точка входа — корневой main.py (он
реэкспортирует всё отсюда, поэтому `from main import Plugin` в плагинах работает
как раньше), а весь код приложения — в модуле _core.py этого пакета.

Структура (модули объявляют свои имена и реэкспортируют их из _core.py):

    anime_viewer/
        app.py            — точка входа (QApplication, шрифты, MainWindow)
        ui/               — main_window, cards, themes, dialogs
        widgets/          — player (MpvWidget)
        manga/            — reader, cache
        core/             — history, settings, plugins, config
        sources/          — base, anilibria, mangadex, manual
        utils/            — network, certs, logging

Код переносится блоками: сначала модуль появляется как точка импорта, затем в него
физически переезжает код (список имён — в каждом модуле, порядок переноса — в
tools/split_main.py). Так перенос не ломает ни приложение, ни плагины.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Портативный Python из этой сборки настраивает sys.path через файл runtime\*. _pth
# и каталог приложения в него НЕ попадает. Поэтому корень приложения и .libs
# добавляем сами: иначе модули рядом с main.py (kodik.py, accent.py, themes_ext.py…)
# молча не подключатся, а пакет просто не найдётся.
_APP_DIR = Path(__file__).resolve().parent.parent
for _path in (str(_APP_DIR), str(_APP_DIR / ".libs")):
    if _path not in sys.path:
        sys.path.append(_path)

__version__ = "1.6"
