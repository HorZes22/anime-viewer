# -*- coding: utf-8 -*-
"""
Точка входа приложения: QApplication, шрифты из assets, главное окно.

Запуск приложения — через корневой main.py (он вызывает app.main()).
"""

from __future__ import annotations

import sys

from ._core import main as _run


def main() -> int:
    """Запускает приложение и возвращает код выхода Qt."""
    return _run()


__all__ = ["main"]
