# -*- coding: utf-8 -*-
"""
Логирование приложения: logs/app.log с ротацией 5×5 МБ.

Готовый модуль — applog.py рядом с main.py; здесь он реэкспортируется,
чтобы импорт из пакета тоже работал: `from anime_viewer.utils.logging import setup_logging`.
"""

from __future__ import annotations

import sys
from pathlib import Path

# applog.py лежит в папке приложения (рядом с main.py), а не в пакете
_APP_DIR = Path(__file__).resolve().parent.parent.parent
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

from applog import log_path, log_report, get_logger, setup_logging  # noqa: E402

__all__ = ["setup_logging", "get_logger", "log_path", "log_report"]
