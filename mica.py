# -*- coding: utf-8 -*-
"""
Прозрачный фон окна (Mica / Acrylic) для Windows 11 — модуль рядом с main.py.

Как это работает: DWM умеет рисовать под окном системный «материал» (Mica), но
только если окно сообщает ему об этом через DwmSetWindowAttribute. Функция
enable_mica() делает это через ctypes — без сторонних библиотек.

Особенности, из-за которых код такой осторожный:
  * материал реально работает только на Windows 11 (сборка 22000+). На Windows 10
    и на «сборках-самозванцах» вызов просто вернёт False — приложение работает как
    обычно, ничего не ломается;
  * окну нужно разрешить полупрозрачность: Qt продолжит рисовать свои панели, но
    фон окна станет «стеклянным» (см. apply_to_window);
  * у старых сборок Windows 11 атрибут называется 1029 (DWMWA_MICA_EFFECT), у новых
    (22H2+) — 38 (DWMWA_SYSTEMBACKDROP_TYPE). Пробуем оба и запоминаем рабочий.
"""

from __future__ import annotations

import sys
from typing import Any, Optional

# DwmSetWindowAttribute: номера атрибутов
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_SYSTEMBACKDROP_TYPE = 38          # Windows 11 22H2+
DWMWA_MICA_EFFECT = 1029                # Windows 11 21H2

# Значения DWM_SYSTEMBACKDROP_TYPE
DWMSBT_AUTO = 0
DWMSBT_NONE = 1
DWMSBT_MAINWINDOW = 2       # Mica
DWMSBT_TRANSIENTWINDOW = 3  # Acrylic
DWMSBT_TABBEDWINDOW = 4     # Mica Alt

# Скругление углов (DWMWCP)
DWMWCP_DEFAULT = 0
DWMWCP_DONOTROUND = 1
DWMWCP_ROUND = 2

MIN_WINDOWS_11_BUILD = 22000


def is_windows() -> bool:
    return sys.platform.startswith("win")


def is_windows_11() -> bool:
    """Windows 11 (сборка 22000+) — там Mica поддерживается системой."""
    if not is_windows():
        return False
    try:
        version = sys.getwindowsversion()          # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return False
    return int(getattr(version, "build", 0)) >= MIN_WINDOWS_11_BUILD


def _dwm_set(hwnd: int, attribute: int, value: int) -> bool:
    """Один вызов DwmSetWindowAttribute (True — DWM принял атрибут)."""
    try:
        import ctypes

        dwmapi = ctypes.windll.dwmapi            # type: ignore[attr-defined]
        data = ctypes.c_int(value)
        result = dwmapi.DwmSetWindowAttribute(
            ctypes.c_void_p(hwnd), ctypes.c_uint(attribute),
            ctypes.byref(data), ctypes.sizeof(data),
        )
        return result == 0
    except Exception:  # noqa: BLE001 — на другой ОС и старых сборках просто не выйдет
        return False


def enable_mica(window: Any, backdrop: int = DWMSBT_MAINWINDOW,
                dark: bool = True) -> bool:
    """
    Включает Mica/Acrylic для окна Qt.

    window — QWidget/QMainWindow (нужен нативный идентификатор winId()).
    backdrop — DWMSBT_MAINWINDOW (Mica), DWMSBT_TRANSIENTWINDOW (Acrylic) или
    DWMSBT_TABBEDWINDOW.
    Возвращает True, если материал применился. На Windows 10 вернёт False.
    """
    if not is_windows_11() or window is None:
        return False
    try:
        hwnd = int(window.winId())
    except Exception:  # noqa: BLE001
        return False
    if not hwnd:
        return False

    # тёмная тема окна: заголовок и системный фон подстраиваются под приложение
    _dwm_set(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, 1 if dark else 0)
    _dwm_set(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND)
    applied = _dwm_set(hwnd, DWMWA_SYSTEMBACKDROP_TYPE, int(backdrop))
    if not applied:
        # Windows 11 21H2: до появления SYSTEMBACKDROP_TYPE материал включался так
        applied = _dwm_set(hwnd, DWMWA_MICA_EFFECT, 1)
    return bool(applied)


def disable_mica(window: Any) -> bool:
    """Выключает материал (ровный фон темы)."""
    if not is_windows_11() or window is None:
        return False
    try:
        hwnd = int(window.winId())
    except Exception:  # noqa: BLE001
        return False
    if not hwnd:
        return False
    done = _dwm_set(hwnd, DWMWA_SYSTEMBACKDROP_TYPE, DWMSBT_NONE)
    _dwm_set(hwnd, DWMWA_MICA_EFFECT, 0)
    return bool(done)


def apply_to_window(window: Any, enabled: bool, dark: bool = True) -> bool:
    """
    Включает/выключает прозрачный фон и настраивает окно Qt под него.

    Прозрачность нужна, чтобы под окном был виден материал DWM: окно получает
    атрибут WA_TranslucentBackground, а панели приложения продолжают рисоваться
    сами (см. WallpaperWidget в main.py) — поэтому интерфейс не «просвечивает».
    Возвращает True, если материал включён.
    """
    from PySide6.QtCore import Qt

    if window is None:
        return False
    if not enabled:
        try:
            window.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        except Exception:  # noqa: BLE001
            pass
        return disable_mica(window)

    try:
        window.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        # нативный HWND нужен до обращения к DWM
        window.winId()
    except Exception:  # noqa: BLE001
        pass
    return enable_mica(window, dark=dark)
