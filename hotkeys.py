# -*- coding: utf-8 -*-
"""
Глобальные горячие клавиши для Windows — модуль рядом с main.py.

Зачем отдельный модуль: чтобы пауза работала, даже когда окно приложения не в
фокусе (смотрите видео и работаете в другом окне).

Как сделано: WinAPI RegisterHotKey + собственный поток, который ждёт сообщения
WM_HOTKEY (GetMessage). Никаких новых зависимостей (keyboard/pynput не нужны) —
и, в отличие от перехвата всей клавиатуры, система сама сообщает только те
сочетания, которые зарегистрировало приложение.

Ограничения, о которых нужно знать:
  * только Windows (на других системах register() вернёт False, приложение просто
    продолжит работать с обычными горячими клавишами);
  * сочетание может быть занято другой программой — тогда регистрация не удастся,
    и это видно по возвращаемому значению (приложение пишет об этом в статус).
"""

from __future__ import annotations

import threading
from typing import Callable, Optional

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312

# Имена клавиш -> виртуальные коды (то, что чаще всего нужно для плеера)
VK_CODES = {
    "space": 0x20, "enter": 0x0D, "esc": 0x1B, "tab": 0x09, "backspace": 0x08,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "media_next": 0xB0, "media_prev": 0xB1, "media_stop": 0xB2,
    "media_play": 0xB3, "media_play_pause": 0xB3,
    "volume_up": 0xAF, "volume_down": 0xAE, "volume_mute": 0xAD,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75,
    "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
}


def parse_combo(combo: str) -> tuple[int, int]:
    """
    «ctrl+alt+space» -> (модификаторы, виртуальный код).

    Поддерживаются ctrl/control, alt, shift, win, а также обычные буквы и цифры.
    """
    mods = 0
    key_code = 0
    for part in str(combo or "").lower().replace(" ", "").split("+"):
        if not part:
            continue
        if part in ("ctrl", "control"):
            mods |= MOD_CONTROL
        elif part == "alt":
            mods |= MOD_ALT
        elif part == "shift":
            mods |= MOD_SHIFT
        elif part in ("win", "super", "meta"):
            mods |= MOD_WIN
        elif part in VK_CODES:
            key_code = VK_CODES[part]
        elif len(part) == 1 and part.isalnum():
            key_code = ord(part.upper())
        elif part.startswith("f") and part[1:].isdigit() and 1 <= int(part[1:]) <= 24:
            key_code = 0x6F + int(part[1:])          # F1 = 0x70
    return mods, key_code


class GlobalHotkeys:
    """
    Регистрация глобальных сочетаний и их обработка в отдельном потоке.

    callbacks вызываются из потока горячих клавиш, поэтому в них нельзя трогать
    виджеты: приложение передаёт сюда функцию, которая только испускает
    Qt-сигнал (сигналы безопасны между потоками).
    """

    def __init__(self) -> None:
        self._hotkeys: dict[int, Callable[[], None]] = {}
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._thread_id: int = 0
        self._next_id = 1
        self.errors: list[str] = []

    @property
    def available(self) -> bool:
        try:
            import sys

            return sys.platform.startswith("win")
        except Exception:  # noqa: BLE001
            return False

    @property
    def registered(self) -> list[str]:
        return [str(cb) for cb in self._hotkeys.values()]

    # ------------------------------------------------------------------ регистрация
    def register(self, combo: str, callback: Callable[[], None]) -> bool:
        """Регистрирует сочетание. False — занято другой программой или не Windows."""
        if not self.available:
            return False
        mods, key_code = parse_combo(combo)
        if not key_code:
            self.errors.append(f"не разобрано сочетание «{combo}»")
            return False
        try:
            import ctypes

            user32 = ctypes.windll.user32          # type: ignore[attr-defined]
            hotkey_id = self._next_id
            ok = user32.RegisterHotKey(None, hotkey_id, mods | MOD_NOREPEAT, key_code)
        except Exception as exc:  # noqa: BLE001
            self.errors.append(f"{combo}: {type(exc).__name__}: {exc}")
            return False
        if not ok:
            self.errors.append(
                f"«{combo}» не удалось занять (сочетание уже использует другая программа)"
            )
            return False
        self._next_id += 1
        self._hotkeys[hotkey_id] = callback
        self._ensure_thread()
        return True

    def register_defaults(self, actions: dict[str, Callable[[], None]]) -> dict[str, bool]:
        """Регистрирует набор сочетаний: {«ctrl+alt+space»: функция} -> что удалось."""
        return {combo: self.register(combo, callback) for combo, callback in actions.items()}

    def unregister_all(self) -> None:
        try:
            import ctypes

            user32 = ctypes.windll.user32          # type: ignore[attr-defined]
            for hotkey_id in list(self._hotkeys):
                user32.UnregisterHotKey(None, hotkey_id)
        except Exception:  # noqa: BLE001
            pass
        self._hotkeys.clear()

    def stop(self) -> None:
        """Останавливает поток (вызывается при выходе из приложения)."""
        self.unregister_all()
        self._stop.set()
        if self._thread_id:
            try:
                import ctypes

                ctypes.windll.user32.PostThreadMessageW(   # type: ignore[attr-defined]
                    self._thread_id, 0x0012, 0, 0          # WM_QUIT
                )
            except Exception:  # noqa: BLE001
                pass
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.5)
        self._thread = None

    # ------------------------------------------------------------------ поток
    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="global-hotkeys", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        """Ждёт WM_HOTKEY и вызывает обработчики."""
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32          # type: ignore[attr-defined]
            kernel32 = ctypes.windll.kernel32      # type: ignore[attr-defined]
            msg = wintypes.MSG()
            self._thread_id = int(kernel32.GetCurrentThreadId())
            while not self._stop.is_set():
                result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if result in (0, -1):              # WM_QUIT или ошибка
                    break
                if msg.message == WM_HOTKEY:
                    callback = self._hotkeys.get(int(msg.wParam))
                    if callback is not None:
                        try:
                            callback()
                        except Exception as exc:  # noqa: BLE001
                            print(f"[горячие клавиши] {type(exc).__name__}: {exc}")
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as exc:  # noqa: BLE001 — на не-Windows просто выходим
            print(f"[горячие клавиши] поток остановлен: {type(exc).__name__}: {exc}")
