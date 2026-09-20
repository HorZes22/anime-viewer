# -*- coding: utf-8 -*-
"""
Мини-режим плеера — модуль рядом с main.py.

Что делает: превращает главное окно в компактный «поверх всех» плеер по Ctrl+M
(видео на весь размер окна, минимум кнопок) и возвращает всё как было при
повторном нажатии.

Почему не отдельное окно с отдельным mpv: видео рисует один экземпляр mpv внутри
своего QWidget, и второй экземпляр пришлось бы синхронизировать (позиция, звук,
дорожки). Поэтому мини-режим — это то же окно с другой раскладкой: ничего не
перезагружается, воспроизведение не сбивается.
"""

from __future__ import annotations

from typing import Any, Optional


class MiniPlayer:
    """
    Переключатель компактного режима.

    Скрывает всё лишнее (левая панель, поиск, часть кнопок), включает
    «поверх всех окон» и уменьшает размер; при выходе возвращает размер,
    положение и видимость всех элементов.
    """

    MINI_SIZE = (720, 420)
    MARGIN = 24

    def __init__(self, window: Any) -> None:
        self.window = window
        self.active = False
        self._hidden: list[Any] = []
        self._saved_geometry: Optional[Any] = None
        self._saved_flags: Optional[Any] = None
        self._saved_maximized = False

    # ------------------------------------------------------------------ служебное
    def _targets(self) -> list[Any]:
        """
        Что прятать в мини-режиме.

        Список собирается по факту наличия атрибутов: окно может быть построено
        чуть иначе, и мини-режим не должен из-за этого ломаться.
        """
        names = (
            "side_panel",            # левая панель целиком (каталог/поиск/манга)
            "search_edit", "search_button",
            "shikimori_button", "theme_button", "plugins_button",
            "continue_button", "logo_label", "app_subtitle",
            "dubbing_hint", "rate_row", "rate_info", "rate_save_button",
            "rate_status_combo", "rate_score_combo",
            "next_button", "add_link_button", "open_file_button", "open_url_button",
        )
        targets: list[Any] = []
        for name in names:
            widget = getattr(self.window, name, None)
            if widget is None:
                continue
            targets.append(widget)
        return targets

    # ------------------------------------------------------------------ переключение
    def toggle(self) -> bool:
        """Переключает режим. Возвращает True, если мини-режим теперь включён."""
        if self.active:
            self.exit()
        else:
            self.enter()
        return self.active

    def enter(self) -> None:
        from PySide6.QtCore import Qt

        window = self.window
        self._saved_geometry = window.saveGeometry()
        self._saved_flags = window.windowFlags()
        flags = self._saved_flags
        try:
            self._saved_maximized = window.isMaximized()
        except Exception:  # noqa: BLE001
            self._saved_maximized = False

        self._hide_extras()
        try:
            window.setWindowFlags(flags | Qt.WindowType.WindowStaysOnTopHint)
            window.show()          # смена флагов в Qt требует повторного показа
            width, height = self.MINI_SIZE
            window.resize(width, height)
            screen = window.screen()
            if screen is not None:
                area = screen.availableGeometry()
                window.move(
                    area.right() - width - self.MARGIN,
                    area.bottom() - height - self.MARGIN,
                )
        except Exception as exc:  # noqa: BLE001
            print(f"[мини-режим] {type(exc).__name__}: {exc}")
        self.active = True

    def exit(self) -> None:
        from PySide6.QtCore import Qt

        window = self.window
        for widget in self._hidden:
            try:
                widget.setVisible(True)
            except Exception:  # noqa: BLE001
                continue
        self._hidden = []
        try:
            flags = self._saved_flags
            if flags is not None:
                window.setWindowFlags(flags & ~Qt.WindowType.WindowStaysOnTopHint)
            if self._saved_geometry is not None:
                window.restoreGeometry(self._saved_geometry)
            if self._saved_maximized:
                window.showMaximized()
            else:
                window.show()
        except Exception as exc:  # noqa: BLE001
            print(f"[мини-режим] {type(exc).__name__}: {exc}")
        self.active = False

    # ------------------------------------------------------------------ скрытие
    def _hide_extras(self) -> None:
        """Прячет всё, кроме плеера и его управления."""
        self._hidden = []
        for widget in self._targets():
            try:
                # isHidden() — именно флаг «скрыт вручную», а не «не видно сейчас»:
                # у невидимого родителя isVisible() тоже False, и по нему мини-режим
                # ничего бы не спрятал (так же ведёт себя и настоящее окно)
                if not widget.isHidden():
                    self._hidden.append(widget)
                    widget.setVisible(False)
            except Exception:  # noqa: BLE001
                continue
