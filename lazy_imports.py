# -*- coding: utf-8 -*-
"""
Менеджер ленивых импортов Anime Viewer — модуль рядом с main.py.

Зачем: старт приложения задерживают не сами модули приложения, а тяжёлые внешние
библиотеки. Здесь их импорт переносится на момент первого использования:

    from lazy_imports import lazy

    mpv = lazy("mpv")                       # импорта ещё нет — только «обещание»
    player = mpv.MPV(...)                   # вот здесь модуль реально грузится

Кого это касается в этом приложении:

  * `mpv` — python-mpv тянет за собой libmpv и разбирает .py плагина;
  * `PySide6.QtNetworkAuth` — часть PySide6-Addons, нужна только для OAuth;
  * `PIL` / `bs4` (BeautifulSoup) — их может подключить плагин или будущий
    источник; в самой программе они не нужны, поэтому в портативной сборке их
    нет и импорт не должен ронять приложение;
  * парсеры ранобэ (ranobe.py) — модуль целиком грузится при первом обращении
    к вкладке «Ранобэ».

Дополнительно:

  * `optional(name)` — «мягкий» импорт: возвращает None, если модуля нет
    (так подключаются необязательные части: accent, mica, shikimori_api…);
  * `timings()` — сколько миллисекунд занял каждый ленивый импорт (видно в логе);
  * `preload("mpv")` — прогрев в фоновом потоке, если лень недопустима
    (например, плеер понадобится сразу после выбора серии).

Важно: LazyModule ведёт себя как обычный модуль (атрибуты, вызовы, `in`,
`repr`), поэтому ленивый импорт не требует правок в местах использования.
"""

from __future__ import annotations

import importlib
import threading
import time
from typing import Any, Optional

# Сколько занимал каждый ленивый импорт: имя -> секунды (для отчёта в логе)
_TIMINGS: dict[str, float] = {}
_LOCK = threading.RLock()
_MODULES: dict[str, Any] = {}          # уже загруженные LazyModule по имени


class LazyModule:
    """
    Замена `import X`: настоящий модуль подгружается при первом обращении.

    Поддерживает всё, что нужно на практике: `lazy_mod.attr`, `lazy_mod.func(...)`,
    `hasattr`, `repr`, и `bool` (True, если модуль вообще доступен).
    """

    __slots__ = ("_name", "_module", "_error", "_package")

    def __init__(self, name: str, package: Optional[str] = None) -> None:
        self._name = str(name)
        self._package = package
        self._module: Any = None
        self._error: Optional[BaseException] = None

    # ---------- загрузка ----------
    @property
    def name(self) -> str:
        return self._name

    def loaded(self) -> bool:
        """Загружен ли модуль прямо сейчас (без попытки импорта)."""
        return self._module is not None

    def _load(self) -> Any:
        if self._module is not None:
            return self._module
        with _LOCK:
            if self._module is not None:
                return self._module
            started = time.perf_counter()
            try:
                self._module = importlib.import_module(self._name, self._package)
                _TIMINGS[self._name] = time.perf_counter() - started
            except BaseException as exc:      # noqa: BLE001 — модуль может падать любым способом
                self._error = exc
                _TIMINGS[self._name] = time.perf_counter() - started
                raise ImportError(f"ленивый импорт «{self._name}» не удался: {exc}") from exc
            return self._module

    # ---------- поведение модуля ----------
    def __getattr__(self, item: str) -> Any:
        # имена с подчёркиванием — служебные (__slots__, __path__ и т.п.)
        if item.startswith("__") and item.endswith("__"):
            raise AttributeError(item)
        return getattr(self._load(), item)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._load()(*args, **kwargs)

    def __contains__(self, item: Any) -> bool:
        try:
            return item in self._load()
        except ImportError:
            return False

    def __bool__(self) -> bool:
        """True, если модуль доступен (при этом он НЕ загружается)."""
        if self._module is not None:
            return True
        if self._error is not None:
            return False
        try:
            importlib.util.find_spec(self._name)
            return True
        except (ImportError, ValueError, ModuleNotFoundError):
            return False

    def __repr__(self) -> str:
        state = "загружен" if self._module is not None else (
            f"ошибка: {self._error}" if self._error is not None else "ленивый"
        )
        return f"<LazyModule {self._name} ({state})>"


def lazy(name: str, package: Optional[str] = None) -> LazyModule:
    """
    Ленивый импорт модуля. Повторный вызов для того же имени отдаёт тот же объект,
    поэтому `lazy("mpv")` в разных местах — это один модуль, а не две загрузки.
    """
    # пакет учитываем в ключе: lazy("x", "a.b") и lazy("x") — разные цели
    key = f"{package + '.' if package else ''}{name}"
    with _LOCK:
        existing = _MODULES.get(key)
        if isinstance(existing, LazyModule):
            return existing
        module = LazyModule(name, package)
        _MODULES[key] = module
        return module


def optional(name: str, default: Any = None, package: Optional[str] = None) -> Any:
    """
    «Мягкий» импорт: если модуля нет или он падает — вернётся default (обычно None).

    Так подключаются необязательные части приложения: одно отсутствие accent.py
    или PySide6.QtNetworkAuth не должно мешать запуску.
    """
    try:
        return importlib.import_module(name, package)
    except BaseException as exc:      # noqa: BLE001 — чужой модуль может падать как угодно
        print(f"[ленивый импорт] {name}: не подключён ({type(exc).__name__}: {exc})")
        return default


def preload(*names: str) -> None:
    """
    Прогрев: загружает указанные модули В ТЕКУЩЕМ потоке.

    Вызывать из фонового потока (см. MainWindow._preload_heavy_modules), чтобы
    первый клик пользователя не ждал разбора библиотеки.
    """
    for name in names:
        module = lazy(name)
        try:
            module._load()
            print(f"[ленивый импорт] прогрет: {name} ({_TIMINGS.get(name, 0) * 1000:.0f} мс)")
        except ImportError as exc:
            print(f"[ленивый импорт] прогрев не удался: {exc}")


def preload_async(*names: str) -> threading.Thread:
    """Прогрев в отдельном потоке: приложение стартует, не дожидаясь библиотек."""
    thread = threading.Thread(
        target=preload, args=names, name="lazy-preload", daemon=True
    )
    thread.start()
    return thread


def timings() -> dict[str, float]:
    """Сколько секунд занял каждый ленивый импорт (для лога при выходе)."""
    with _LOCK:
        return dict(_TIMINGS)


def report() -> str:
    """Строка для логов: что загружалось лениво и сколько это заняло."""
    data = timings()
    if not data:
        return "ленивые импорты: ещё ничего не загружалось"
    slow = sorted(data.items(), key=lambda kv: kv[1], reverse=True)[:5]
    return "ленивые импорты: " + ", ".join(f"{name} — {sec * 1000:.0f} мс" for name, sec in slow)


# --------------------------------------------------------------------------------------
# Готовые ленивые ссылки для приложения: их можно импортировать и использовать
# как обычные модули, но плата за импорт берётся только при обращении.
# --------------------------------------------------------------------------------------
mpv = lazy("mpv")                       # встроенный плеер (libmpv)
PIL_Image = lazy("PIL.Image")           # Pillow: нужен плагинам-источникам картинок
bs4 = lazy("bs4")                       # BeautifulSoup: нужен парсерам сайтов
QtNetworkAuth = lazy("PySide6.QtNetworkAuth")   # OAuth-флоу (PySide6-Addons)
ranobe_module = lazy("ranobe")          # источники текста: сайты + свои файлы
