# -*- coding: utf-8 -*-
"""
Заставка при запуске Anime Viewer — модуль рядом с main.py.

Зачем: между запуском python и появлением окна проходит время: разбор PySide6,
libmpv, чтение настроек, сборка интерфейса. На слабой машине это заметно, и
выглядит как «приложение не запустилось». Заставка показывает, что процесс идёт.

Два способа, по порядку:

  1. **tkinter** (стандартная библиотека) — окно появляется до импорта PySide6,
     поэтому работает на самом раннем этапе. Важно: в портативном Python этой
     сборки tkinter нет (runtime\\ не содержит _tkinter.pyd и tcl/tk) — тогда
     способ просто отключается, и остаётся вариант 2.
  2. **QtSplash** — маленькое окно на PySide6: показывается сразу после создания
     QApplication и до первого показа главного окна. Именно этот вариант и
     работает в готовой сборке.

Отключается переменной окружения ANIME_VIEWER_NO_SPLASH=1
(полезно для тестов и для диагностики «тормозит ли заставка»).
"""

from __future__ import annotations

import os
import sys
import threading
import time
from typing import Any, Optional

APP_TITLE = "Anime Viewer"
HINT = "Загружаю интерфейс…"
# Через сколько миллисекунд заставка закрывается сама, если приложение почему-то
# не сообщило о готовности: лучше исчезнуть, чем висеть поверх окна навсегда.
AUTO_HIDE_MS = 12000
# То же для tkinter-заставки: она живёт только до момента, когда PySide6 показал
# свою заставку (обычно это 1–2 секунды), поэтому таймер короче.
TK_AUTO_HIDE_MS = 8000

_tk_state: dict[str, Any] = {"root": None, "thread": None, "started": 0.0, "closed": False}


def enabled() -> bool:
    """Включена ли заставка (её можно выключить переменной окружения)."""
    return os.environ.get("ANIME_VIEWER_NO_SPLASH") != "1"


def tkinter_available() -> bool:
    """Есть ли tkinter в этом Python (в портативной сборке его обычно нет)."""
    try:
        import importlib.util

        return importlib.util.find_spec("tkinter") is not None
    except (ImportError, ValueError):
        return False


def show_tkinter(text: str = HINT) -> bool:
    """
    Показывает заставку средствами tkinter в отдельном потоке.

    Возвращает True, если окно удалось создать. Tk требует, чтобы все вызовы шли
    из ОДНОГО потока, поэтому окно живёт в своём потоке и там же крутит mainloop,
    а закрытие делается через очередь команд (root.after + destroy).
    """
    if not enabled() or not tkinter_available():
        return False
    if _tk_state["root"] is not None:
        return True

    ready = threading.Event()

    def run() -> None:
        try:
            import tkinter as tk
        except Exception as exc:  # noqa: BLE001 — tkinter может быть без tcl/tk
            print(f"[заставка] tkinter недоступен: {type(exc).__name__}: {exc}")
            ready.set()
            return
        try:
            root = tk.Tk()
            root.overrideredirect(True)          # без рамки и кнопок окна
            root.attributes("-topmost", True)
            width, height = 380, 120
            screen_w = root.winfo_screenwidth()
            screen_h = root.winfo_screenheight()
            root.geometry(f"{width}x{height}+{(screen_w - width) // 2}+{(screen_h - height) // 2}")
            root.configure(bg="#0d0d0e")
            tk.Label(
                root, text=APP_TITLE, fg="#e8e8e9", bg="#0d0d0e",
                font=("Segoe UI", 16, "bold"),
            ).pack(pady=(26, 4))
            tk.Label(root, text=text, fg="#96969b", bg="#0d0d0e",
                     font=("Segoe UI", 10)).pack()
            _tk_state["root"] = root
            _tk_state["started"] = time.time()
            # Страховка: если приложение почему-то не закроет заставку, окно уйдёт
            # само (иначе оно осталось бы висеть поверх всех окон).
            root.after(TK_AUTO_HIDE_MS, root.destroy)
            ready.set()
            root.mainloop()
        except Exception as exc:  # noqa: BLE001 — заставка не должна мешать запуску
            print(f"[заставка] не показана: {type(exc).__name__}: {exc}")
            ready.set()

    thread = threading.Thread(target=run, name="splash-tk", daemon=True)
    _tk_state["thread"] = thread
    thread.start()
    ready.wait(0.5)      # ждём не больше полсекунды: заставка не должна задерживать старт
    return _tk_state["root"] is not None


def close_tkinter() -> None:
    """Закрывает tkinter-заставку (если она была показана)."""
    root = _tk_state.get("root")
    if root is None or _tk_state.get("closed"):
        return
    _tk_state["closed"] = True
    try:
        # destroy из чужого потока небезопасен — просим поток окна закрыться сам
        root.after(0, root.destroy)
    except Exception:  # noqa: BLE001
        pass
    _tk_state["root"] = None


class QtSplash:
    """
    Заставка на PySide6: показывается до сборки главного окна.

    Использование (см. anime_viewer/_core.py → main):
        splash = QtSplash(icon_path)
        splash.show()
        ... создать MainWindow ...
        splash.close()
    """

    def __init__(self, icon: Optional[str] = None, title: str = APP_TITLE,
                 text: str = HINT) -> None:
        self.title = title
        self.text = text
        self.icon = icon
        self.widget: Any = None
        self._shown_at = 0.0

    def show(self) -> bool:
        """Создаёт и показывает окно. False — заставка недоступна/выключена."""
        if not enabled() or self.widget is not None:
            return False
        try:
            from PySide6.QtCore import Qt, QTimer
            from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
            from PySide6.QtWidgets import QSplashScreen
        except Exception as exc:  # noqa: BLE001
            print(f"[заставка] Qt недоступен: {type(exc).__name__}: {exc}")
            return False

        try:
            pixmap = QPixmap(380, 130)
            pixmap.fill(QColor("#0d0d0e"))
            painter = QPainter(pixmap)
            painter.setPen(QColor("#5e5ce6"))
            painter.drawRect(0, 0, 379, 129)
            painter.setPen(QColor("#e8e8e9"))
            title_font = QFont()
            title_font.setPointSize(15)
            title_font.setBold(True)
            painter.setFont(title_font)
            painter.drawText(24, 58, self.title)
            painter.setPen(QColor("#96969b"))
            hint_font = QFont()
            hint_font.setPointSize(9)
            painter.setFont(hint_font)
            painter.drawText(24, 84, self.text)
            painter.end()

            splash = QSplashScreen(pixmap)
            splash.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
            if self.icon:
                try:
                    splash.setWindowIcon(QIcon(self.icon))
                except Exception:  # noqa: BLE001
                    pass
            splash.show()
            # Страховка: если приложение не сообщит о готовности, заставка уйдёт сама
            QTimer.singleShot(AUTO_HIDE_MS, self.close)
            self.widget = splash
            self._shown_at = time.time()
            return True
        except Exception as exc:  # noqa: BLE001 — заставка не важнее запуска
            print(f"[заставка] не показана: {type(exc).__name__}: {exc}")
            return False

    def set_text(self, text: str) -> None:
        """Меняет подпись (например, «Читаю настройки…»)."""
        splash = self.widget
        if splash is None:
            return
        try:
            splash.showMessage(text)
        except Exception:  # noqa: BLE001
            pass

    def close(self) -> None:
        """Скрывает заставку. Повторный вызов безопасен."""
        splash = self.widget
        self.widget = None
        if splash is None:
            return
        try:
            splash.close()
        except Exception:  # noqa: BLE001
            pass
        if self._shown_at:
            print(f"[заставка] показана {int((time.time() - self._shown_at) * 1000)} мс")


def show(icon: Optional[str] = None, text: str = HINT) -> QtSplash:
    """Показывает Qt-заставку и возвращает объект для последующего закрытия."""
    splash = QtSplash(icon, text=text)
    splash.show()
    return splash
