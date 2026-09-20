# -*- coding: utf-8 -*-
"""
Иконка в системном трее — модуль рядом с main.py.

Что делает: создаёт QSystemTrayIcon с меню (Показать/Скрыть, Пауза/Продолжить,
Следующая серия, Выход) и решает, что делать при нажатии на крестик окна —
закрывать приложение или сворачивать его в трей (настройка «закрывать или
сворачивать»).

Почему отдельный модуль: трей — независимая часть, у неё свои тонкости
(на некоторых системах трея нет вовсе; окно нужно прятать и показывать заново),
и приложение не должно из-за неё усложняться.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

# Что делать при нажатии на крестик
CLOSE_ASK = "ask"          # спрашивать (закрыть или свернуть)
CLOSE_QUIT = "quit"        # закрывать приложение
CLOSE_TRAY = "tray"        # сворачивать в трей
CLOSE_CHOICES = (
    (CLOSE_QUIT, "Закрывать приложение"),
    (CLOSE_TRAY, "Сворачивать в трей"),
    (CLOSE_ASK, "Спрашивать каждый раз"),
)


class AppTray:
    """
    Иконка в трее приложения.

    actions — словарь функций: show/hide, toggle_pause, next_episode, quit.
    Любое действие вызывается в GUI-потоке (трей Qt живёт в нём же), поэтому
    внутрь можно передавать обычные методы окна.
    """

    def __init__(self, window: Any, icon: Any, title: str = "Anime Viewer") -> None:
        from PySide6.QtWidgets import QSystemTrayIcon

        self.window = window
        self.available = QSystemTrayIcon.isSystemTrayAvailable()
        self.tray: Optional[QSystemTrayIcon] = None
        self._icon = icon
        self._title = title
        if not self.available:
            print("[трей] системный трей недоступен — сворачивание в трей отключено")

    # ------------------------------------------------------------------ создание
    def create(self, actions: dict[str, Callable[[], None]],
               playback_hint: str = "") -> Optional[Any]:
        """Создаёт иконку и меню. Возвращает иконку или None, если трея нет."""
        if not self.available:
            return None
        from PySide6.QtWidgets import QMenu, QSystemTrayIcon

        tray = QSystemTrayIcon(self._icon, self.window)
        tray.setToolTip(self._title + (f"\n{playback_hint}" if playback_hint else ""))

        menu = QMenu(self.window)
        show_action = menu.addAction("Показать / скрыть окно")
        show_action.triggered.connect(actions.get("toggle_window", lambda: None))
        menu.addSeparator()
        pause_action = menu.addAction("Пауза / продолжить")
        pause_action.triggered.connect(actions.get("toggle_pause", lambda: None))
        next_action = menu.addAction("Следующая серия")
        next_action.triggered.connect(actions.get("next_episode", lambda: None))
        menu.addSeparator()
        quit_action = menu.addAction("Выход")
        quit_action.triggered.connect(actions.get("quit", lambda: None))

        tray.setContextMenu(menu)
        tray.activated.connect(
            lambda reason: self._on_activated(reason, actions.get("toggle_window"))
        )
        tray.show()
        self.tray = tray
        return tray

    def _on_activated(self, reason: Any, toggle: Optional[Callable[[], None]]) -> None:
        """Клик по иконке: левый — показать/скрыть, средний — пауза."""
        from PySide6.QtWidgets import QSystemTrayIcon

        if reason == QSystemTrayIcon.ActivationReason.Trigger and toggle is not None:
            toggle()

    # ------------------------------------------------------------------ сообщения
    def notify(self, title: str, text: str) -> None:
        """Показывает системное уведомление (например, «серия скачана»)."""
        if self.tray is None:
            return
        try:
            from PySide6.QtWidgets import QSystemTrayIcon

            self.tray.showMessage(title, text, QSystemTrayIcon.MessageIcon.Information, 4000)
        except Exception as exc:  # noqa: BLE001 — уведомление не критично
            print(f"[трей] уведомление не показано: {exc}")

    def set_tooltip(self, text: str) -> None:
        if self.tray is not None:
            try:
                self.tray.setToolTip(text)
            except Exception:  # noqa: BLE001
                pass

    def set_visible(self, visible: bool) -> None:
        if self.tray is not None:
            try:
                self.tray.setVisible(visible)
            except Exception:  # noqa: BLE001
                pass

    def hide(self) -> None:
        self.set_visible(False)


def hide_to_tray(window: Any) -> None:
    """Прячет окно в трей (окно сворачивается, приложение продолжает работать)."""
    window.hide()


def show_from_tray(window: Any) -> None:
    """Возвращает окно из трея и поднимает его на передний план."""
    window.show()
    window.setWindowState(window.windowState() & ~window.windowState().WindowMinimized)
    window.raise_()
    window.activateWindow()
