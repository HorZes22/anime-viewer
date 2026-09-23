# -*- coding: utf-8 -*-
"""
Кэш с TTL для Anime Viewer — модуль рядом с main.py.

Зачем отдельный модуль: в приложении несколько мест, где один и тот же ответ
сети запрашивается повторно (поиск по названию, список серий, список озвучек
Kodik/AniLibria, страницы плеера). Раньше каждый модуль держал свой словарь без
срока годности — и данные либо жили вечно (пользователь видел устаревшее), либо
перезапрашивались на каждый клик (медленно).

Что здесь есть:

  * TTLCache — словарь с временем жизни записей и потолком по числу записей;
  * memoize_ttl — декоратор: результат функции кэшируется по её аргументам;
  * CACHE — общий кэш приложения (по умолчанию TTL 10 минут, см. DEFAULT_TTL);
  * дисковый кэш (cache/ttl/<ключ>.json) — нужен там, где ответ стоит дорого
    (каталог, списки серий) и полезен даже после перезапуска приложения.

Потокобезопасность: поиск и загрузка постеров идут из нескольких потоков, поэтому
все операции с кэшем защищены RLock, а отдача наружу — копией объектов.

Важно: кэш никогда не «ломает» вызывающий код. Любая ошибка (нет места на диске,
битый JSON, недоступная папка) означает «кэш пуст», а не исключение.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

# Сколько живёт запись по умолчанию: 10 минут — компромисс между свежестью
# (источники обновляют серии и переводы) и скоростью (повторный клик мгновенный).
DEFAULT_TTL = 10 * 60
# Сколько записей держим в памяти одного кэша. Больше не нужно: списки поиска
# и серий короткие, а каталог листается страницами.
DEFAULT_MAX_ITEMS = 512


class _Entry:
    """Одна запись кэша: значение и момент, когда оно перестанет быть годным."""

    __slots__ = ("value", "expires_at", "stored_at")

    def __init__(self, value: Any, ttl: float) -> None:
        self.value = value
        self.stored_at = time.time()
        self.expires_at = self.stored_at + max(0.0, float(ttl))

    def alive(self, now: Optional[float] = None) -> bool:
        return (time.time() if now is None else now) < self.expires_at


class TTLCache:
    """
    Небольшой кэш «ключ -> значение» с временем жизни.

    Пример:
        cache = TTLCache(ttl=600)
        cache.set(("kodik", 11757), links)
        links = cache.get(("kodik", 11757))        # None, если запись устарела
        links = cache.get_or_set(("kodik", 11757), lambda: fetch())
    """

    def __init__(self, ttl: float = DEFAULT_TTL, max_items: int = DEFAULT_MAX_ITEMS,
                 name: str = "cache") -> None:
        self.ttl = float(ttl)
        self.max_items = max(8, int(max_items))
        self.name = name
        self._items: dict[Any, _Entry] = {}
        self._lock = threading.RLock()
        self.hits = 0          # статистика: нужна в логах и для отладки
        self.misses = 0
        self.expired = 0

    # ---------- ключи ----------
    @staticmethod
    def make_key(*parts: Any) -> tuple:
        """
        Ключ из любых частей. Словари и списки приводим к стабильной строке:
        иначе одинаковые аргументы давали бы разные ключи (dict не хэшируется).
        """
        normalized: list[Any] = []
        for part in parts:
            if isinstance(part, (dict, list, tuple, set)):
                try:
                    normalized.append(json.dumps(part, sort_keys=True, ensure_ascii=False, default=str))
                except (TypeError, ValueError):
                    normalized.append(repr(part))
            else:
                normalized.append(part)
        return tuple(normalized)

    # ---------- чтение/запись ----------
    def get(self, key: Any, default: Any = None) -> Any:
        with self._lock:
            entry = self._items.get(key)
            if entry is None:
                self.misses += 1
                return default
            if not entry.alive():
                # просроченную запись убираем сразу: иначе она занимает место
                self._items.pop(key, None)
                self.expired += 1
                self.misses += 1
                return default
            self.hits += 1
            return entry.value

    def set(self, key: Any, value: Any, ttl: Optional[float] = None) -> Any:
        with self._lock:
            self._items[key] = _Entry(value, self.ttl if ttl is None else ttl)
            if len(self._items) > self.max_items:
                self._trim()
        return value

    def get_or_set(self, key: Any, factory: Callable[[], Any],
                   ttl: Optional[float] = None) -> Any:
        """
        Значение из кэша или результат factory().

        factory вызывается ВНЕ блокировки: иначе сетевой запрос одного потока
        остановил бы чтение кэша всеми остальными.
        """
        found = self.get(key, _MISSING)
        if found is not _MISSING:
            return found
        value = factory()
        # пустой результат не кэшируем: «не нашлось» обычно значит «сеть мигнула»,
        # и повторный запрос должен пройти честно
        if value:
            self.set(key, value, ttl)
        return value

    def invalidate(self, key: Any) -> None:
        with self._lock:
            self._items.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def prune(self) -> int:
        """Убирает просроченные записи. Возвращает число удалённых."""
        now = time.time()
        with self._lock:
            dead = [key for key, entry in self._items.items() if not entry.alive(now)]
            for key in dead:
                self._items.pop(key, None)
            return len(dead)

    def _trim(self) -> None:
        """Сначала убираем просроченное, потом — самые старые по времени записи."""
        self.prune()
        with self._lock:
            while len(self._items) > self.max_items:
                oldest = min(self._items.items(), key=lambda kv: kv[1].stored_at)[0]
                self._items.pop(oldest, None)

    def stats(self) -> dict:
        with self._lock:
            return {
                "name": self.name,
                "items": len(self._items),
                "hits": self.hits,
                "misses": self.misses,
                "expired": self.expired,
                "ttl": self.ttl,
            }

    def report(self) -> str:
        """Короткая строка для логов и статусной строки."""
        data = self.stats()
        return (f"{data['name']}: записей {data['items']}, попаданий {data['hits']}, "
                f"промахов {data['misses']}, устарело {data['expired']} (TTL {int(data['ttl'])} с)")


class _Missing:
    """Маркер «в кэше ничего нет»: None — тоже валидное значение."""


_MISSING = _Missing()


class DiskCache:
    """
    Кэш на диске (cache/ttl/<имя>-<хэш>.json) — переживает перезапуск приложения.

    Нужен для дорогих ответов: каталог, список серий, список озвучек тайтла.
    Значение обязано быть JSON-совместимым; если нет — запись просто пропускается.
    """

    def __init__(self, folder: Path, ttl: float = DEFAULT_TTL, name: str = "disk") -> None:
        self.folder = Path(folder)
        self.ttl = float(ttl)
        self.name = name
        self.enabled = True

    def _file(self, key: Any) -> Path:
        raw = json.dumps(TTLCache.make_key(key), ensure_ascii=False, default=str)
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()
        return self.folder / f"{self.name}-{digest}.json"

    def get(self, key: Any) -> Any:
        if not self.enabled:
            return None
        path = self._file(key)
        try:
            if not path.exists():
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return None
            if time.time() - float(data.get("saved_at") or 0) > self.ttl:
                return None
            return data.get("value")
        except (OSError, ValueError, TypeError):
            return None

    def set(self, key: Any, value: Any) -> None:
        if not self.enabled:
            return
        path = self._file(key)
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(
                {"saved_at": time.time(), "value": value}, ensure_ascii=False
            )
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", delete=False, dir=str(self.folder), suffix=".tmp"
            ) as tmp:
                tmp.write(payload)
                tmp_path = Path(tmp.name)
            tmp_path.replace(path)          # атомарная запись: обрыв не портит кэш
        except (OSError, TypeError, ValueError):
            pass

    def clear(self) -> int:
        """Удаляет файлы этого кэша. Возвращает число удалённых."""
        removed = 0
        try:
            for path in self.folder.glob(f"{self.name}-*.json"):
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    continue
        except OSError:
            pass
        return removed

    def prune(self) -> int:
        """Убирает просроченные файлы кэша."""
        removed = 0
        try:
            for path in self.folder.glob(f"{self.name}-*.json"):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    saved = float(data.get("saved_at") or 0)
                except (OSError, ValueError, TypeError):
                    saved = 0.0
                if time.time() - saved > self.ttl:
                    try:
                        path.unlink()
                        removed += 1
                    except OSError:
                        continue
        except OSError:
            pass
        return removed


# --------------------------------------------------------------------------------------
# Общие кэши приложения: поиск/серии/озвучки/каталог. Модули берут их отсюда, поэтому
# TTL задаётся в одном месте (настройка «Кэш результатов», см. _core.py).
# --------------------------------------------------------------------------------------
CACHE = TTLCache(ttl=DEFAULT_TTL, max_items=256, name="общий")
SEARCH_CACHE = TTLCache(ttl=DEFAULT_TTL, max_items=128, name="поиск")
EPISODES_CACHE = TTLCache(ttl=DEFAULT_TTL, max_items=128, name="серии")
QUALITY_CACHE = TTLCache(ttl=DEFAULT_TTL, max_items=128, name="качества")
CATALOG_CACHE = TTLCache(ttl=DEFAULT_TTL, max_items=64, name="каталог")

ALL_CACHES = (CACHE, SEARCH_CACHE, EPISODES_CACHE, QUALITY_CACHE, CATALOG_CACHE)


def memoize_ttl(cache: TTLCache, key_builder: Optional[Callable[..., Any]] = None):
    """
    Декоратор: кэширует результат функции по её аргументам.

        @memoize_ttl(EPISODES_CACHE)
        def episodes(self, anime_id): ...

    Пустой результат (None, [], {}) не кэшируется — см. TTLCache.get_or_set.
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if key_builder is not None:
                key = key_builder(*args, **kwargs)
            else:
                # self отбрасываем: кэш общий на приложение, а не на экземпляр
                key = TTLCache.make_key(fn.__qualname__, args[1:] or args, kwargs)
            return cache.get_or_set(key, lambda: fn(*args, **kwargs))

        wrapper.__name__ = getattr(fn, "__name__", "wrapper")
        wrapper.__doc__ = fn.__doc__
        wrapper.__wrapped__ = fn          # type: ignore[attr-defined]
        return wrapper

    return decorator


def set_ttl(seconds: float) -> None:
    """
    Меняет TTL всех кэшей приложения (настройка в «Оформлении» → «Приложение»).

    Значение 0 отключает кэширование: кэш очищается и записи больше не живут.
    """
    ttl = max(0.0, float(seconds))
    for cache in ALL_CACHES:
        cache.ttl = ttl
        if ttl <= 0:
            cache.clear()


def clear_all() -> int:
    """Полная очистка кэшей (кнопка «Сбросить кэш»). Возвращает число записей."""
    total = 0
    for cache in ALL_CACHES:
        total += len(cache._items)
        cache.clear()
    return total


def report() -> str:
    """Сводка по кэшам — показывается в настройках и логах."""
    parts = [cache.report() for cache in ALL_CACHES if cache.stats()["items"] or cache.stats()["hits"]]
    if not parts:
        return "кэш пуст (попаданий пока не было)"
    if os.environ.get("ANIME_VIEWER_CACHE_VERBOSE") == "1":
        return "\n".join(parts)
    hits = sum(cache.hits for cache in ALL_CACHES)
    items = sum(len(cache._items) for cache in ALL_CACHES)
    return f"{len(parts)} кэшей, записей {items}, попаданий {hits} (TTL {int(CACHE.ttl)} с)"
