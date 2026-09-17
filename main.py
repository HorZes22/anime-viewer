# -*- coding: utf-8 -*-
"""
Anime Viewer — десктопное приложение для просмотра аниме и чтения манги.

Возможности:
  1. Поиск аниме по названию через API Shikimori (https://shikimori.one/api/animes?search=...).
  2. Список найденных аниме (название, год, постер).
  3. При выборе аниме — список эпизодов.
  4. Для каждого эпизода — выпадающий список озвучек (студий).
  5. Воспроизведение эпизода во ВСТРОЕННОМ плеере (python-mpv, встраивается в QWidget через winId()).
  6. Сохранение прогресса просмотра в JSON-файл рядом с программой.
  7. Манга: поиск и чтение глав через официальный API MangaDex (api.mangadex.org) —
     встроенная читалка (лента/постранично), прогресс чтения в manga_progress.json.
     Для запросов по-русски название сначала уточняется через Shikimori, чтобы найти тайтл
     в MangaDex (там названия на латинице).

Стек: Python 3.10+, PySide6, python-mpv, requests.

ВАЖНО ПРО ИСТОЧНИК ВИДЕО:
    Приложение НЕ содержит парсеров видео. Точка расширения — класс VideoSource
    (см. PlaceholderVideoSource.get_stream_url). Именно туда нужно вставить
    реальный парсер Kodik/AniLibria/Sibnet.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import queue
import shutil
import sys
import tempfile
import threading
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse


def _has_module(name: str) -> bool:
    """Есть ли Python-модуль (без его импорта)."""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


def _dependency_report(error: ImportError) -> str:
    """
    Понятная диагностика вместо трейсбека, если не хватает зависимостей.
    Печатается в консоль при запуске без PySide6/requests.
    """
    missing = error.name or str(error)
    lines = [
        "",
        "=" * 72,
        "Не хватает зависимостей для запуска Anime Viewer.",
        f"Не найден модуль: {missing}",
        "",
        "Установите зависимости:",
        "    pip install -r requirements.txt",
        "",
        "Текущее состояние окружения:",
        f"    python:          {sys.version.split()[0]} ({sys.executable})",
        f"    PySide6:         {'найден' if _has_module('PySide6') else 'НЕТ (pip install PySide6)'}",
        f"    requests:        {'найден' if _has_module('requests') else 'НЕТ (pip install requests)'}",
        f"    python-mpv:      {'найден' if _has_module('mpv') else 'НЕТ (pip install python-mpv)'}",
        f"    mpv.exe в PATH:  {shutil.which('mpv') or 'НЕТ (нужен сам плеер mpv, не только pip-пакет)'}",
        "",
        "Напоминание: python-mpv — это только обёртка. Нужен сам mpv:",
        "    winget install mpv     (или распакуйте сборку mpv и добавьте её в PATH)",
        "=" * 72,
        "",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------------------
# Импорт GUI-стека. Если чего-то нет — выходим с понятной инструкцией (см. выше).
# --------------------------------------------------------------------------------------
try:
    import requests

    from PySide6.QtCore import QRect, QSize, Qt, QThread, QTimer, Signal
    from PySide6.QtGui import QIcon, QPixmap
    from PySide6.QtWidgets import (
    QFrame,
    QStyle,
        QApplication,
        QCheckBox,
        QColorDialog,
        QComboBox,
        QDialog,
        QDialogButtonBox,
        QFileDialog,
        QHBoxLayout,
        QInputDialog,
        QLabel,
        QLineEdit,
        QListWidget,
        QListWidgetItem,
        QMainWindow,
        QMessageBox,
        QPlainTextEdit,
        QPushButton,
        QSlider,
        QSplitter,
        QStatusBar,
        QStyledItemDelegate,
        QTabWidget,
        QVBoxLayout,
        QWidget,
    )
except ImportError as _import_error:  # pragma: no cover - зависит от окружения
    print(_dependency_report(_import_error))
    raise SystemExit(1)

# --------------------------------------------------------------------------------------
# Пытаемся импортировать python-mpv. Если его нет — приложение всё равно запустится
# и покажет подсказку в области плеера (чтобы можно было посмотреть интерфейс).
# --------------------------------------------------------------------------------------
try:
    import mpv  # type: ignore
except Exception:  # pragma: no cover - зависит от окружения
    mpv = None  # type: ignore


# --------------------------------------------------------------------------------------
# Константы / пути
# --------------------------------------------------------------------------------------
# Вывод в консоль: русский текст и символы вроде «＋» не должны ронять приложение
# при перенаправленном выводе (консоль Windows может быть в cp866/cp1251).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except Exception:
    pass

BASE_DIR = Path(__file__).resolve().parent          # папка, рядом с которой лежит main.py
PROGRESS_FILE = BASE_DIR / "progress.json"          # файл прогресса просмотра
CACHE_DIR = BASE_DIR / "cache"
POSTER_DIR = CACHE_DIR / "posters"

# Версия приложения и версия API плагинов. Плагин может проверить их в on_started(),
# чтобы честно сказать «я не подхожу этой версии», а не ломаться непонятно как.
APP_VERSION = "1.5"
PLUGIN_API_VERSION = 1

# Зеркала Shikimori. Первое — официальный домен; остальные используются автоматически,
# если основной недоступен (например, shikimori.one блокируется провайдером в РФ).
SHIKIMORI_HOSTS = [
    "https://shikimori.one",
    "https://shikimori.net",
    "https://shikimori.org",
]
# Shikimori требует осмысленный User-Agent, иначе отдаёт 403.
USER_AGENT = "AnimeViewer/1.0 (local desktop app; python-requests)"

# Озвучки по умолчанию. Список можно менять без правки кода — в файле settings.json
# (ключ "dubbings"). Реально играть могут только те, что поддержаны источником
# (сейчас это AniLibria) или для которых вы добавили ссылку вручную (manual_streams.json).
DUBBINGS = [
    "AniLibria",
    "AniLibria (субтитры)",
    "JAM CLUB",
    "AniDUB",
    "Studio Band",
    "KANSAI",
    "SHIZA Project",
    "AnimeVost",
    "AnimeJoy",
    "AniStar",
    "AnimeLabs",
    "SovetRomantica",
    "Onibaku",
    "FoxTail",
    "NewComers",
    "Azazel",
    "Dream Cast",
    "Totoro",
    "Inari",
    "Kirigaya",
    "Amikoshi",
    "Fumo",
    "Kuroko",
    "XDUB",
    "AniMedia",
    "AnimeGXP",
    "VoiceProject",
    "Crunchyroll",
    "Netflix",
    "Тайтл-озвучка не нужна (оригинал + субтитры)",
]

# Официальный API AniLibria (API v1, старый /v3 отдаёт 410 Gone).
# Даёт реальные HLS-ссылки официального CDN и привязку к Shikimori ID.
# Важно: у AniLibria ОДНА звуковая дорожка (их собственная озвучка) — мультиозвучки
# в API нет, поэтому остальные студии появляются в списке, но играют только если
# для них добавлена своя ссылка (кнопка «＋ ссылка» / manual_streams.json).
ANILIBRIA_API = "https://anilibria.top/api/v1"
ANILIBRIA_SITE = "https://anilibria.top"
# релизы из каталога AniLibria, у которых нет shikimori.id, получают свой числовой id
# с этим смещением (чтобы не пересекаться с настоящими ID Shikimori)
ANILIBRIA_ID_OFFSET = 10_000_000
CATALOG_PAGE_SIZE = 40           # сколько релизов запрашиваем за раз
CATALOG_SORTINGS = {
    "Новинки": "FRESH_AT_DESC",
    "По рейтингу": "RATING_DESC",
    "По году": "YEAR_DESC",
    "По названию": "NAME_ASC",
}

REQUEST_TIMEOUT = 15       # секунд на один HTTP-запрос
POSTER_TIMEOUT = 20
SEARCH_LIMIT = 20          # сколько аниме запрашиваем у Shikimori (меньше — быстрее ответ)
POSTER_WORKERS = 12        # сколько постеров качаем одновременно
STATE_FILE = CACHE_DIR / "state.json"   # запоминаем рабочее зеркало между запусками
SETTINGS_FILE = BASE_DIR / "settings.json"          # список озвучек и прочие настройки
MANUAL_FILE = BASE_DIR / "manual_streams.json"      # ссылки, добавленные пользователем вручную
HISTORY_FILE = BASE_DIR / "history.json"            # история просмотров
PLUGINS_DIR = BASE_DIR / "plugins"                  # плагины приложения (см. plugins/README.txt)
SOURCES_DIR = BASE_DIR / "sources_local"            # только источники видео (старый формат)
PLUGIN_SETTINGS_FILE = BASE_DIR / "plugin_settings.json"   # данные, которые плагины хранят сами
SEARCH_DEBOUNCE_MS = 550   # пауза после набора текста перед автопоиском
MIN_AUTOSEARCH_LEN = 3     # с какой длины запроса искать автоматически
DUBBING_UNAVAILABLE = "· нет в источнике"   # пометка недоступных озвучек в списке

# --------------------------------------------------------------------------------------
# Манга: официальный API MangaDex (https://api.mangadex.org/docs/).
# Ключ не нужен; страницы отдаёт сеть MangaDex@Home (/at-home/server/{chapter}).
# --------------------------------------------------------------------------------------
MANGADEX_API = "https://api.mangadex.org"
MANGADEX_UPLOADS = "https://uploads.mangadex.org"
MANGA_CACHE_DIR = CACHE_DIR / "manga"          # кэш обложек и страниц глав
MANGA_PROGRESS_FILE = BASE_DIR / "manga_progress.json"
MANGA_SEARCH_LIMIT = 24        # сколько тайтлов берём у MangaDex за раз
MANGA_PAGE_WORKERS = 4         # сколько страниц качаем одновременно
MANGA_PREFETCH = 4             # сколько страниц держим загруженными впереди текущей
MANGA_CACHE_LIMIT_MB = 300     # после этого удаляем самые старые страницы из кэша
MANGA_PAGE_RETRIES = 2         # повторы загрузки страницы (у MangaDex бывают медленные ноды)
MANGA_NODE_TIMEOUT = 8         # сколько ждём ноду MangaDex@Home, прежде чем считать её мёртвой
# Язык перевода: код MangaDex -> подпись в интерфейсе. Первый — приоритетный.
MANGA_LANGS = [
    ("ru", "Русский"),
    ("en", "Английский"),
    ("uk", "Украинский"),
    ("ja", "Японский (оригинал)"),
]
MANGA_STATUS = {
    "ongoing": "выходит", "completed": "завершена", "hiatus": "заморожена",
    "cancelled": "отменена",
}
# Сортировки каталога манги: код -> (подпись, поле сортировки MangaDex).
# Каталог листается страницами: MangaDex не отдаёт весь список сразу, но сообщает
# общее число тайтлов, поэтому в интерфейсе видно «сколько всего».
MANGA_CATALOG_SORTS = {
    "popular": ("Популярные", "followedCount"),
    "rating": ("По оценке", "rating"),
    "fresh": ("Новинки глав", "latestUploadedChapter"),
    "title": ("По названию", "title"),
    "year": ("По году", "year"),
}
MANGA_CATALOG_PAGE = 40        # сколько тайтлов каталога подгружаем за раз
# Режимы читалки: как показывать страницы и как их масштабировать.
MANGA_MODES = [("strip", "Лента (непрерывно)"), ("single", "Постранично")]
MANGA_FITS = [("width", "По ширине"), ("height", "По высоте"), ("none", "100%")]
# Короткие подписи для кнопки в панели чтения: полные («По ширине») в узкой панели
# обрезаются, поэтому на кнопке — краткий вариант, а полный текст в подсказке.
MANGA_FIT_SHORT = {"width": "Ширина", "height": "Высота", "none": "100%"}
DEFAULT_MANGA = {
    "lang": "ru",          # приоритетный язык перевода
    "fallback_en": True,    # доливать главы, которых нет на русском
    "mode": "strip",
    "fit": "width",
    "gap": 10,             # отступ между страницами в ленте, px
    "disk_cache": True,    # сохранять страницы на диск (повторное чтение — мгновенное)
    "source": "mangadex",  # выбранный источник манги (код из MangaSource.name)
    "catalog_sort": "popular",   # порядок тайтлов в каталоге манги
    "catalog_only_ru": False,    # показывать только тайтлы с русским переводом
}


def fmt_time(seconds: float) -> str:
    """Секунды -> строка вида 01:23:45 или 12:34."""
    seconds = max(0, int(seconds or 0))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def find_ca_bundle() -> Optional[str]:
    """
    Ищет файл с корневыми сертификатами для mpv.

    Важно: в Windows-сборке mpv своего CA-бандла нет, поэтому HTTPS-потоки
    (Kodik/AniLibria отдают https) падают с «TLS certificate verification failed».
    Берём бандл из certifi (он ставится вместе с requests).
    Можно переопределить переменной окружения ANIME_VIEWER_CA_FILE.
    """
    env_path = os.environ.get("ANIME_VIEWER_CA_FILE")
    if env_path and Path(env_path).is_file():
        return env_path
    try:
        import certifi  # ставится как зависимость requests

        path = certifi.where()
        if Path(path).is_file():
            return path
    except Exception:
        pass
    return None


# ======================================================================================
# Хранилище прогресса (JSON рядом с программой)
# ======================================================================================
_thread_local = threading.local()


def _thread_session() -> "requests.Session":
    """
    Отдельная сессия requests на каждый поток: постеры качаются параллельно,
    а requests.Session не потокобезопасна. Так каждая нить получает свой keep-alive.
    """
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update({"User-Agent": USER_AGENT})
        _thread_local.session = session
    return session


def fetch_image(
    url: str,
    folder: Path,
    attempts: int = 1,
    timeout: int = POSTER_TIMEOUT,
    use_disk: bool = True,
) -> Optional[bytes]:
    """
    Скачивает картинку в дисковый кэш (папка folder, имя файла — sha1 от ссылки).

    Общая функция для постеров и страниц манги: она вызывается сразу из нескольких
    потоков, поэтому берёт сессию requests на поток (см. _thread_session), а не
    общую. use_disk=False — не читать и не писать кэш (настройка «хранить страницы
    на диске»). Возвращает байты картинки или None, если скачать не удалось.
    """
    if not url:
        return None
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    cache_file = folder / (hashlib.sha1(url.encode("utf-8")).hexdigest() + ".img")
    try:
        if use_disk and cache_file.exists():
            data = cache_file.read_bytes()
            if data:
                return data
    except OSError:
        pass

    last_error: Any = None
    for attempt in range(max(1, attempts)):
        try:
            resp = _thread_session().get(url, timeout=timeout)
            resp.raise_for_status()
            data = resp.content
            if data:
                if use_disk:
                    try:
                        cache_file.write_bytes(data)
                    except OSError:
                        pass
                return data
        except Exception as exc:  # noqa: BLE001 — хост мог не ответить
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(0.5 * (attempt + 1))
    print(f"[картинка] не загрузилась {url[:80]}: {last_error}")
    return None


class ProgressStore:
    """
    Хранит позиции просмотра в JSON-файле.

    Формат файла:
        {
          "version": 1,
          "items": {
              "1234|3|AniLibria": {"pos": 512.4, "updated": 1712345678.9}
          }
        }
    Ключ: "<anime_id>|<эпизод>|<озвучка>".
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._items: dict[str, dict[str, Any]] = {}
        self.load()

    # ---------- ключ ----------
    @staticmethod
    def key(anime_id: Any, episode: Any, dubbing: Any) -> str:
        return f"{anime_id}|{episode}|{dubbing}"

    # ---------- чтение/запись файла ----------
    def load(self) -> None:
        try:
            if self.path.exists():
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                items = data.get("items") if isinstance(data, dict) else None
                if isinstance(items, dict):
                    self._items = items
        except (OSError, ValueError):
            # Битый или недоступный файл — просто начинаем с пустого прогресса.
            self._items = {}

    def save(self) -> None:
        """Атомарная запись: сначала во временный файл, потом replace."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"version": 1, "items": self._items}
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", delete=False, dir=str(self.path.parent), suffix=".tmp"
            ) as tmp:
                json.dump(payload, tmp, ensure_ascii=False, indent=2)
                tmp_path = Path(tmp.name)
            tmp_path.replace(self.path)
        except OSError:
            # Не смогли сохранить — не роняем приложение из-за прогресса.
            pass

    # ---------- API ----------
    def get(self, anime_id: Any, episode: Any, dubbing: Any) -> float:
        rec = self._items.get(self.key(anime_id, episode, dubbing))
        try:
            return float(rec.get("pos", 0.0)) if isinstance(rec, dict) else 0.0
        except (TypeError, ValueError):
            return 0.0

    def set(self, anime_id: Any, episode: Any, dubbing: Any, pos: float) -> None:
        import time as _time

        self._items[self.key(anime_id, episode, dubbing)] = {
            "pos": round(float(pos or 0.0), 1),
            "updated": _time.time(),
        }

    def forget(self, anime_id: Any, episode: Any, dubbing: Any) -> None:
        """Забывает позицию для серии (используется кнопкой «Забыть»)."""
        self._items.pop(self.key(anime_id, episode, dubbing), None)
        self.save()


# ======================================================================================
# HTTP-клиент Shikimori
# ======================================================================================
class ShikimoriClient:
    """Тонкая обёртка над REST API Shikimori. Все запросы обёрнуты в try/except."""

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            }
        )
        self._hosts: list[str] = list(SHIKIMORI_HOSTS)
        self._base: str = self._hosts[0]   # последнее рабочее зеркало
        self._base_verified: bool = False  # подтверждено ли оно реальным ответом
        # кэши в памяти — повторный поиск и повторные заходы в аниме мгновенные
        self._search_cache: dict[str, list[dict]] = {}
        self._episodes_supported: Optional[bool] = None   # False, если зеркало не умеет /episodes
        self._load_state()

    # ---------- низкоуровневый GET с перебором зеркал ----------
    @property
    def active_host(self) -> str:
        """Домен, через который реально идут запросы (показывается в статусной строке)."""
        return self._base

    # ---------- запоминание рабочего зеркала между запусками ----------
    def _load_state(self) -> None:
        """Читает сохранённое зеркало: тогда первый поиск не тратит время на перебор."""
        try:
            if STATE_FILE.exists():
                data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                host = data.get("shikimori_host")
                if host in self._hosts:
                    self._base = host
                    self._base_verified = True   # доверяем, но при ошибке уйдём в перебор
        except (OSError, ValueError):
            pass

    def _save_state(self) -> None:
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(
                json.dumps({"shikimori_host": self._base}, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            pass

    def _candidates(self) -> list[str]:
        """Хосты в порядке перебора: сначала последнее рабочее зеркало."""
        return [self._base] + [h for h in self._hosts if h != self._base]

    def _single_request(
        self,
        base: str,
        path: str,
        params: Optional[dict],
        timeout: Any,
    ) -> "requests.Response":
        """Один запрос к конкретному зеркалу. 404/429 поднимаются как HTTPError."""
        resp = self.session.get(base + path, params=params, timeout=timeout)
        if resp.status_code == 404:
            raise requests.HTTPError(f"404 Not Found: {base}{path}", response=resp)
        if resp.status_code == 429:
            raise requests.HTTPError("429 Too Many Requests (Shikimori rate limit)", response=resp)
        resp.raise_for_status()
        return resp

    def _request(
        self,
        path: str,
        params: Optional[dict] = None,
        timeout: int = REQUEST_TIMEOUT,
    ) -> "requests.Response":
        """
        GET по относительному пути (/api/...) с быстрым перебором зеркал.

        Схема:
          1) если рабочее зеркало уже подтверждено — сразу короткий запрос к нему;
          2) иначе опрашиваем ВСЕ зеркала параллельно и берём того, кто ответил первым.
             Так блокировка провайдером (например, shikimori.one не отдаёт TLS)
             не добавляет 15 секунд ожидания к первому поиску.
        Ошибка 404 означает «эндпоинта тут нет» — другие зеркала не помогут.
        """
        fast_timeout = (4, min(timeout, 8))   # (время на соединение, время на чтение)
        errors: list[Any] = []

        if self._base_verified:
            try:
                return self._single_request(self._base, path, params, fast_timeout)
            except requests.HTTPError as exc:
                if getattr(getattr(exc, "response", None), "status_code", None) == 404:
                    raise
                errors.append(exc)
            except requests.RequestException as exc:
                errors.append(exc)
            self._base_verified = False   # зеркало перестало отвечать — перепроверим

        from concurrent.futures import ThreadPoolExecutor, as_completed

        hosts = list(self._hosts)
        executor = ThreadPoolExecutor(max_workers=len(hosts))
        try:
            futures = {
                executor.submit(self._single_request, host, path, params, fast_timeout): host
                for host in hosts
            }
            for future in as_completed(futures):
                host = futures[future]
                try:
                    resp = future.result()
                except requests.HTTPError as exc:
                    if getattr(getattr(exc, "response", None), "status_code", None) == 404:
                        raise
                    errors.append(exc)
                    continue
                except Exception as exc:  # noqa: BLE001 — любое зеркало может упасть
                    errors.append(exc)
                    continue
                self._base = host          # запоминаем победителя
                self._base_verified = True
                self._save_state()
                print(f"[Shikimori] рабочее зеркало: {host}")
                return resp
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        raise RuntimeError(
            f"Не удалось получить {path} ни с одного зеркала Shikimori "
            f"({', '.join(self._hosts)}). Последняя ошибка: {errors[-1] if errors else 'нет'}"
        )

    def _get_json(self, path: str, params: Optional[dict] = None, timeout: int = REQUEST_TIMEOUT) -> Any:
        resp = self._request(path, params=params, timeout=timeout)
        try:
            return resp.json()
        except ValueError as exc:
            raise RuntimeError(f"Некорректный JSON от {resp.url}: {exc}")

    def warm_up(self) -> str:
        """
        Заранее выбирает рабочее зеркало (вызывается в фоне при старте приложения).
        Тогда первый поиск пользователя идёт по короткому пути, без перебора зеркал.
        """
        try:
            self._get_json("/api/animes", params={"search": "a", "limit": 1})
        except Exception as exc:
            print(f"[Shikimori] прогрев зеркала не удался: {exc}")
        return self._base

    # ---------- поиск аниме ----------
    def search_animes(self, query: str, use_cache: bool = True) -> list[dict]:
        """
        Возвращает метаданные найденных аниме — БЕЗ скачивания постеров,
        чтобы список появлялся максимально быстро (постеры докачиваются
        параллельно отдельным потоком, см. PosterLoader).

        Словари: {id, title, russian, year, kind, score, episodes, episodes_aired,
                  poster_url, poster_bytes(None)}
        """
        key = query.strip().lower()
        if use_cache and key in self._search_cache:
            return self._search_cache[key]

        data = self._get_json(
            "/api/animes",
            params={
                "search": query,
                "limit": SEARCH_LIMIT,
                "order": "popularity",  # сортировка по популярности
            },
        )
        if not isinstance(data, list):
            return []

        result: list[dict] = []
        for raw in data:
            if not isinstance(raw, dict):
                continue
            # Постеры у зеркал лежат по разным путям (/system/... и /uploads/...),
            # поэтому берём адрес с того же домена, который ответил на запрос.
            poster_url = self._extract_poster_url(raw, self._base)
            result.append(
                {
                    "id": raw.get("id"),
                    "title": raw.get("name") or "",
                    "russian": raw.get("russian") or raw.get("name") or "",
                    "year": self._extract_year(raw),
                    "kind": raw.get("kind") or "",
                    "score": raw.get("score") or 0.0,
                    "episodes": raw.get("episodes") or 0,
                    "episodes_aired": raw.get("episodes_aired") or 0,
                    "poster_url": poster_url,
                    "poster_bytes": None,   # догружается отдельным потоком
                }
            )
        self._search_cache[key] = result
        return result

    # ---------- список эпизодов ----------
    def get_episodes(self, anime_id: int, fallback_count: int) -> list[tuple[int, str]]:
        """
        Пытается получить реальные эпизоды через /api/animes/{id}/episodes.
        Если эндпоинт недоступен (у зеркал его часто нет — 404) — генерирует
        список 1..fallback_count по полю episodes из поиска. Отсутствие эндпоинта
        запоминается, чтобы не терять время на 404 при каждом аниме.
        """
        if self._episodes_supported is not False:
            try:
                data = self._get_json(f"/api/animes/{anime_id}/episodes")
                episodes: list[tuple[int, str]] = []
                if isinstance(data, list):
                    for ep in data:
                        if not isinstance(ep, dict):
                            continue
                        num = ep.get("episode")
                        if num is None:
                            continue
                        name = (ep.get("name") or "").strip()
                        episodes.append((int(num), name))
                if episodes:
                    episodes.sort(key=lambda x: x[0])
                    self._episodes_supported = True
                    return episodes
            except Exception:
                # Эндпоинт может не существовать/быть недоступным — это не критично.
                pass
            self._episodes_supported = False

        count = max(int(fallback_count or 0), 1)
        return [(i, "") for i in range(1, count + 1)]

    # ---------- поиск манги (нужен, чтобы искать русские названия в MangaDex) ----------
    def search_mangas(self, query: str, limit: int = 8) -> list[dict]:
        """
        Ищет мангу в Shikimori. Нужно потому, что в MangaDex названия на латинице:
        по русскому запросу («Мастера меча онлайн») сначала находим тайтл здесь,
        а затем ищем его романдзи-название в MangaDex (см. MainWindow._manga_search).

        Возвращает: {id, russian, name, english, japanese, year, kind, poster_url}.
        Ошибки сети не поднимаются наружу — просто пустой список.
        """
        try:
            data = self._get_json(
                "/api/mangas",
                params={"search": query, "limit": max(1, int(limit))},
            )
        except Exception as exc:
            print(f"[Shikimori] поиск манги не удался: {exc}")
            return []
        result: list[dict] = []
        for raw in data or []:
            if not isinstance(raw, dict):
                continue
            result.append(
                {
                    "id": raw.get("id"),
                    "russian": (raw.get("russian") or "").strip(),
                    "name": (raw.get("name") or "").strip(),
                    "english": (raw.get("english") or "").strip(),
                    "japanese": (raw.get("japanese") or "").strip(),
                    "year": self._extract_year(raw),
                    "kind": raw.get("kind") or "",
                    "poster_url": self._extract_poster_url(raw, self._base),
                }
            )
        return result

    # ---------- вспомогательное ----------
    @staticmethod
    def _extract_year(raw: dict) -> Optional[int]:
        for key in ("aired_on", "released_on"):
            value = raw.get(key)
            if isinstance(value, str) and len(value) >= 4 and value[:4].isdigit():
                return int(value[:4])
        return None

    @staticmethod
    def _extract_poster_url(raw: dict, base: str) -> str:
        image = raw.get("image") or {}
        if not isinstance(image, dict):
            return ""
        url = image.get("original") or image.get("preview") or ""
        if not url:
            return ""
        if url.startswith("http"):
            return url
        if url.startswith("/"):
            return base + url
        return ""

    def _download_poster(self, url: str) -> Optional[bytes]:
        """
        Скачивает постер с дисковым кэшем (cache/posters/<sha1>.img).
        Вызывается из нескольких потоков сразу, поэтому использует отдельную
        сессию requests на поток (keep-alive без гонок).
        Если картинка не отдалась рабочим зеркалом — пробуем тот же путь на других.
        """
        if not url:
            return None
        candidates = [url]
        path = urlparse(url).path
        for host in self._hosts:
            alt = host + path
            if alt not in candidates:
                candidates.append(alt)

        for candidate in candidates:
            try:
                POSTER_DIR.mkdir(parents=True, exist_ok=True)
                name = hashlib.sha1(candidate.encode("utf-8")).hexdigest() + ".img"
                cache_file = POSTER_DIR / name
                if cache_file.exists():
                    data = cache_file.read_bytes()
                    if data:
                        return data
                resp = _thread_session().get(candidate, timeout=POSTER_TIMEOUT)
                resp.raise_for_status()
                data = resp.content
                if data:
                    try:
                        cache_file.write_bytes(data)
                    except OSError:
                        pass
                    return data
            except Exception:
                # Пробуем следующее зеркало; постер — не критичная часть.
                continue
        return None


# ======================================================================================
# Клиент официального API AniLibria (API v1) — реальные потоки официального CDN
# ======================================================================================
def _normalize_title(text: str) -> str:
    """Название -> сравнимый вид: нижний регистр, ё→е, только буквы/цифры/пробелы."""
    text = (text or "").lower().replace("ё", "е")
    cleaned = "".join(ch if (ch.isalnum() or ch.isspace()) else " " for ch in text)
    return " ".join(cleaned.split())


def _title_similarity(left: str, right: str) -> float:
    """
    Похожесть двух названий от 0 до 1.
    Точное вложение («Мастера меча онлайн» внутри «Мастера меча онлайн II») даёт 0.95,
    иначе — обычное отношение совпадения последовательностей.
    """
    from difflib import SequenceMatcher

    a, b = _normalize_title(left), _normalize_title(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.95
    return SequenceMatcher(None, a, b).ratio()


class AniLibriaClient:
    """
    Обёртка над anilibria.top/api/v1.

    Что даёт:
      * поиск релизов по названию (/anime/catalog/releases?f[search]=...);
      * привязку к Shikimori: у релиза есть shikimori.id — совпадает с ID из поиска;
      * список серий с названиями и прямыми HLS-ссылками (hls_480/720/1080)
        официального CDN libria.fun — это и есть источник воспроизведения.

    Старый api.anilibria.tv/v3 отвечает 410 Gone, поэтому используется v1.

    Сопоставление тайтлов строгое: сначала ищем точное совпадение shikimori.id,
    затем — по названию с порогом похожести. Если тайтла в AniLibria нет, релиз
    не подставляется (иначе на запрос несуществующего аниме включался бы чужой тайтл).
    """

    MIN_TITLE_SIMILARITY = 0.72      # обычный порог совпадения по названию
    MIN_WEAK_SIMILARITY = 0.60       # мягкий порог: нужен ещё и совпадающий год

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        self._release_by_anime: dict[int, Optional[dict]] = {}   # shikimori_id -> релиз
        self._episodes_by_anime: dict[int, list[dict]] = {}      # shikimori_id -> серии
        self.last_status: str = ""                               # человекочитаемый итог поиска
        self._rejected_hint: str = ""                            # ближайшее по названию, если не нашли

    # ---------- низкоуровневый GET ----------
    def _get_json(self, path: str, params: Optional[dict] = None, timeout: int = REQUEST_TIMEOUT) -> Any:
        try:
            resp = self.session.get(ANILIBRIA_API + path, params=params, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as exc:
            raise RuntimeError(f"AniLibria API {path}: {exc}") from exc

    # ---------- каталог релизов (то, что озвучила AniLibria) ----------
    def catalog(self, page: int = 1, limit: int = CATALOG_PAGE_SIZE,
                sorting: str = "FRESH_AT_DESC") -> tuple[list[dict], int]:
        """
        Возвращает (список релизов, всего_релизов) — весь каталог AniLibria
        с сортировкой. Каждый элемент приведён к виду, удобному интерфейсу.
        """
        data = self._get_json(
            "/anime/catalog/releases",
            params={"limit": limit, "page": page, "f[sorting]": sorting},
        )
        raw = data.get("data") if isinstance(data, dict) else data
        total = 0
        if isinstance(data, dict):
            total = int(((data.get("meta") or {}).get("pagination") or {}).get("total") or 0)

        items: list[dict] = []
        for release in raw or []:
            if not isinstance(release, dict):
                continue
            poster = release.get("poster") or {}
            poster_url = (
                poster.get("preview") or poster.get("src")
                or (poster.get("optimized") or {}).get("preview") or ""
            )
            if poster_url.startswith("/"):
                poster_url = ANILIBRIA_SITE + poster_url
            name = release.get("name") or {}
            genres = [
                g.get("name") if isinstance(g, dict) else str(g)
                for g in (release.get("genres") or [])
            ]
            items.append({
                "anilibria_id": release.get("id"),
                "title": name.get("main") or "",
                "title_en": name.get("english") or "",
                "year": release.get("year"),
                "episodes": release.get("episodes_total"),
                "shikimori_id": (release.get("shikimori") or {}).get("id"),
                "rating": (release.get("shikimori") or {}).get("rating"),
                "poster_url": poster_url,
                "genres": [g for g in genres if g][:3],
                "alias": release.get("alias") or "",
            })
        return items, total

    def as_anime(self, item: dict) -> dict:
        """
        Превращает элемент каталога в «аниме» для интерфейса: дальше с ним работает
        обычный поток (источник, серии, озвучки, прогресс, история).
        """
        anime_id = item.get("shikimori_id")
        if not anime_id:
            anime_id = ANILIBRIA_ID_OFFSET + int(item.get("anilibria_id") or 0)
        return {
            "id": anime_id,
            "russian": item.get("title") or "",
            "title": item.get("title_en") or item.get("title") or "",
            "year": item.get("year"),
            "kind": "tv",
            "score": item.get("rating") or 0,
            "episodes": item.get("episodes") or 0,
            "episodes_aired": 0,
            "poster_url": item.get("poster_url") or "",
            "poster_bytes": None,
            "genres": item.get("genres") or [],
            "from_catalog": True,
            "anilibria_id": item.get("anilibria_id"),
        }

    def _search(self, query: str, limit: int = 20) -> list[dict]:
        """
        Поиск релизов. Сначала пробуем /app/search/releases (охват выше — это поиск
        самого сайта), при неудаче — фильтр каталога f[search].
        """
        items: list[Any] = []
        try:
            data = self._get_json("/app/search/releases", params={"query": query, "limit": limit})
            items = data.get("data", data) if isinstance(data, dict) else data
        except Exception as exc:
            print(f"[AniLibria] /app/search не сработал ({exc}), пробую каталог")
        if not items:
            data = self._get_json("/anime/catalog/releases", params={"f[search]": query, "limit": limit})
            items = data.get("data") if isinstance(data, dict) else data
        return [it for it in (items or []) if isinstance(it, dict)]

    @staticmethod
    def _base_title(text: str) -> str:
        """Название без подзаголовка: до «:», « — » или « - » (помогает поиску)."""
        for sep in (":", " — ", " - "):
            if sep in (text or ""):
                return text.split(sep, 1)[0].strip()
        return (text or "").strip()

    @staticmethod
    def _release_names(release: dict) -> list[str]:
        name = release.get("name") or {}
        return [n for n in (name.get("main"), name.get("english"), name.get("alternative")) if n]

    # ---------- поиск релиза по аниме Shikimori ----------
    def find_release(
        self,
        anime_id: int,
        title: str,
        title_alt: str = "",
        year: Optional[int] = None,
    ) -> Optional[dict]:
        """
        Находит релиз AniLibria для аниме Shikimori.

        Порядок:
          1) поиск по русскому названию, по его базовой части, по романдзи и его базовой части;
          2) точное совпадение shikimori.id — самый надёжный вариант;
          3) иначе лучшее совпадение по названию с похожестью >= 0.72;
          4) либо мягкое совпадение >= 0.60, но только если у релиза нет shikimori.id
             и совпадает год (у многих релизов AniLibria поле shikimori пустое);
          5) иначе None — тайтла в AniLibria нет, подставлять чужой нельзя.
        """
        if anime_id in self._release_by_anime:
            return self._release_by_anime[anime_id]

        self._rejected_hint = ""   # чтобы подсказка не осталась от предыдущего аниме

        queries: list[str] = []
        for text in (title, self._base_title(title), title_alt, self._base_title(title_alt)):
            text = (text or "").strip()
            if text and text not in queries:
                queries.append(text)

        candidates: list[dict] = []
        seen: set[Any] = set()
        for query in queries:
            try:
                for release in self._search(query):
                    key = release.get("id")
                    if key not in seen:
                        seen.add(key)
                        candidates.append(release)
            except Exception as exc:
                print(f"[AniLibria] поиск «{query}» не удался: {exc}")

        release: Optional[dict] = None

        # 2) точное совпадение по Shikimori ID
        for candidate in candidates:
            if (candidate.get("shikimori") or {}).get("id") == anime_id:
                release = candidate
                self.last_status = "точное совпадение по shikimori.id"
                break

        # 3-4) сравнение названий (учитываем и полное название, и базовую часть)
        if release is None and candidates:
            best: Optional[dict] = None
            best_score = 0.0
            for candidate in candidates:
                for name in self._release_names(candidate):
                    score = max(
                        max(_title_similarity(q, name) for q in queries),
                        0.0,
                    )
                    if score > best_score:
                        best_score, best = score, candidate
            if best is not None:
                name = (best.get("name") or {}).get("main")
                same_year = (
                    year is not None
                    and best.get("year") is not None
                    and abs(int(best.get("year")) - int(year)) <= 1
                )
                if best_score >= self.MIN_TITLE_SIMILARITY:
                    release = best
                    self.last_status = f"«{name}», совпадение по названию (похожесть {best_score:.2f})"
                elif best_score >= self.MIN_WEAK_SIMILARITY and same_year and not best.get("shikimori"):
                    # у релиза нет привязки к Shikimori, но совпали и название, и год
                    release = best
                    self.last_status = (
                        f"«{name}», совпадение по названию и году "
                        f"(похожесть {best_score:.2f}, без привязки к Shikimori — проверьте)"
                    )
                else:
                    hint = f", похожее: «{name}»" if name else ""
                    print(f"[AniLibria] для «{title}» ({anime_id}) нет подходящего релиза "
                          f"(лучшая похожесть {best_score:.2f}{hint})")
                    self._rejected_hint = f"«{name}»" if name else ""

        if release is None:
            self.last_status = "тайтла нет в AniLibria"
            if self._rejected_hint:
                self.last_status += f", ближайшее по названию: {self._rejected_hint}"

        self._release_by_anime[anime_id] = release
        return release

    # ---------- серии ----------
    def get_episodes(
        self,
        anime_id: int,
        title: str,
        title_alt: str = "",
        year: Optional[int] = None,
        release_id: Optional[int] = None,
    ) -> list[dict]:
        """Возвращает список серий релиза (каждая — dict с ordinal, name, hls_*)."""
        if anime_id in self._episodes_by_anime:
            return self._episodes_by_anime[anime_id]

        # если тайтл выбран из каталога — берём релиз напрямую, без поиска по названию
        release = None
        if release_id:
            try:
                release = self._get_json(f"/anime/releases/{int(release_id)}")
                self.last_status = "релиз из каталога AniLibria"
            except Exception as exc:
                print(f"[AniLibria] релиз {release_id} не открылся: {exc}")
        if release is None:
            release = self.find_release(anime_id, title, title_alt, year)
        episodes: list[dict] = []
        if release:
            try:
                detail = self._get_json(f"/anime/releases/{release.get('id')}")
                raw_eps = detail.get("episodes") or []
                for ep in raw_eps:
                    if isinstance(ep, dict) and ep.get("ordinal") is not None:
                        episodes.append(ep)
                episodes.sort(key=lambda e: int(e.get("ordinal") or 0))
            except Exception as exc:
                print(f"[AniLibria] не удалось получить серии: {exc}")

        self._episodes_by_anime[anime_id] = episodes
        return episodes

    # ---------- ссылка на поток ----------
    def get_stream_url(
        self,
        anime_id: int,
        episode: int,
        title: str,
        title_alt: str = "",
        year: Optional[int] = None,
        release_id: Optional[int] = None,
    ) -> Optional[str]:
        """HLS-ссылка нужной серии (1080p → 720p → 480p)."""
        for ep in self.get_episodes(anime_id, title, title_alt, year, release_id):
            if int(ep.get("ordinal") or 0) == int(episode):
                for key in ("hls_1080", "hls_720", "hls_480"):
                    url = ep.get(key)
                    if url:
                        return url
        return None


# ======================================================================================
# Источник видео (АБСТРАКТНЫЙ)
# ======================================================================================
class VideoSource(ABC):
    """
    Абстрактный источник видеопотока.

    Чтобы подключить свой парсер — отнаследуйтесь от этого класса и реализуйте
    get_stream_url(). Возвращайте прямую ссылку на поток (m3u8/mp4), которую понимает mpv.

    Необязательные хуки (переопределяются при необходимости):
      * get_episodes()       — реальный список серий источника;
      * available_dubbings() — какие озвучки источник умеет отдавать.
    """

    name: str = "abstract"

    # UI кладёт сюда название текущего аниме — некоторым источникам нужно
    # искать по названию, а не по ID (сигнатуру get_stream_url не меняем).
    anime_title: str = ""
    anime_title_alt: str = ""     # альтернативное (обычно английское/романдзи) название
    anime_year: Optional[int] = None   # год выпуска (помогает сопоставлению)
    anime_catalog_id: Optional[int] = None   # id релиза в каталоге источника, если известен
    last_status: str = ""         # человекочитаемый итог последнего поиска в источнике

    @abstractmethod
    def get_stream_url(self, anime_id: int, episode: int, dubbing: str) -> Optional[str]:
        """
        :param anime_id: ID аниме на Shikimori (или свой ID, если парсер так умеет)
        :param episode:  номер эпизода (1-based)
        :param dubbing:  название озвучки, например "AniLibria"
        :return: прямая ссылка на поток (str) либо None, если поток не найден
        """
        raise NotImplementedError

    # ---------- необязательные возможности источника ----------
    def get_episodes(self, anime_id: int) -> Optional[list[tuple[int, str]]]:
        """
        Реальный список серий источника: [(номер, название), ...].
        None = «источник не знает», тогда UI берёт список из данных Shikimori.
        """
        return None

    def available_dubbings(self) -> list[str]:
        """Какие озвучки источник умеет отдавать (остальные UI помечает как недоступные)."""
        return list(DUBBINGS)


class AniLibriaVideoSource(VideoSource):
    """
    РЕАЛЬНЫЙ источник видео: официальный API AniLibria (anilibria.top/api/v1).

    Работает без парсеров-«серых» агрегаторов: берёт HLS-ссылки официального
    CDN libria.fun, которые AniLibria сама отдаёт в своём API. Релиз ищется по
    shikimori.id, поэтому выбор аниме в нашем интерфейсе сразу попадает в нужный тайтл.

    Ограничение: AniLibria отдаёт только собственную озвучку (AniLibria),
    поэтому остальные студии в списке помечаются как недоступные.
    """

    name = "anilibria"
    label = "AniLibria"          # как источник называется в интерфейсе

    def __init__(self) -> None:
        self.client = AniLibriaClient()

    def available_dubbings(self) -> list[str]:
        return ["AniLibria"]

    def get_episodes(self, anime_id: int) -> Optional[list[tuple[int, str]]]:
        try:
            episodes = self.client.get_episodes(
                int(anime_id), self.anime_title, self.anime_title_alt, self.anime_year,
                self.anime_catalog_id,
            )
        except Exception as exc:
            print(f"[AniLibria] список серий недоступен: {exc}")
            self.last_status = f"ошибка: {exc}"
            return None
        self.last_status = self.client.last_status
        if not episodes:
            return None
        return [(int(ep.get("ordinal") or 0), (ep.get("name") or "").strip()) for ep in episodes]

    def get_stream_url(self, anime_id: int, episode: int, dubbing: str) -> Optional[str]:
        if dubbing not in self.available_dubbings():
            # У AniLibria только своя озвучка — для JAM CLUB/AniDUB/... нужен другой источник.
            print(f"[AniLibria] озвучки «{dubbing}» в источнике нет")
            self.last_status = f"озвучки «{dubbing}» в источнике нет"
            return None
        url = self.client.get_stream_url(
            int(anime_id), int(episode), self.anime_title, self.anime_title_alt,
            self.anime_year, self.anime_catalog_id,
        )
        self.last_status = self.client.last_status
        return url


# ======================================================================================
# Клиент официального API MangaDex — поиск манги, главы, страницы
# ======================================================================================
class MangaDexClient:
    """
    Обёртка над https://api.mangadex.org (ключ не нужен).

    Три шага чтения:
      1. search(query)      -> список тайтлов (с обложками);
      2. chapters(manga_id) -> главы с языком перевода и командой перевода;
      3. pages(chapter_id)  -> прямые ссылки на картинки страниц
                               (их отдаёт сеть MangaDex@Home: /at-home/server/{id}).

    Важная деталь: страницы отдаются с разных нод (baseUrl меняется от запроса к запросу,
    и конкретная нода иногда отвечает медленно). Поэтому загрузка идёт с повторами,
    а ссылки на главу можно запросить заново (повторный вызов pages()).
    """

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        self._search_cache: dict[tuple, list[dict]] = {}
        self._chapter_cache: dict[tuple, list[dict]] = {}
        # Каким хостом реально качаются страницы: "node" (.mangadex.network) или
        # "uploads". У части провайдеров ноды сети MangaDex@Home не отдают данные
        # (виснут до таймаута), тогда как uploads.mangadex.org работает — поэтому
        # после первого успеха запоминаем победителя и больше не ждём впустую.
        self._page_host_pref: Optional[str] = None
        self._page_host_lock = threading.Lock()   # «гонку хостов» делаем один раз
        self.last_note: str = ""   # пояснение для статусной строки (что и как качается)
        self.cache_pages_to_disk: bool = True   # выключается в настройках читалки

    # ---------- низкоуровневый GET с повторами ----------
    def _get_json(
        self,
        path: str,
        params: Optional[dict] = None,
        timeout: int = REQUEST_TIMEOUT,
        attempts: int = 3,
    ) -> Any:
        """
        GET с повторами: у MangaDex бывают 429 (лимит запросов) и разовые таймауты.
        Повторяем с нарастающей паузой, наружу отдаём понятную ошибку.
        """
        last_error: Any = None
        for attempt in range(max(1, attempts)):
            try:
                resp = self.session.get(MANGADEX_API + path, params=params, timeout=timeout)
                if resp.status_code == 429:
                    last_error = RuntimeError("429 Too Many Requests (лимит MangaDex)")
                    time.sleep(1.0 + attempt)
                    continue
                if resp.status_code in (500, 502, 503, 504):
                    last_error = RuntimeError(f"{resp.status_code} от MangaDex")
                    time.sleep(0.5 * (attempt + 1))
                    continue
                resp.raise_for_status()
                return resp.json()
            except Exception as exc:  # noqa: BLE001 — сеть бывает любой
                last_error = exc
                if attempt + 1 < attempts:
                    time.sleep(0.4 * (attempt + 1))
        raise RuntimeError(f"MangaDex недоступен: {last_error}")

    # ---------- поиск ----------
    def search(
        self,
        query: str,
        limit: int = MANGA_SEARCH_LIMIT,
        langs: Optional[list[str]] = None,
    ) -> list[dict]:
        """
        Поиск тайтлов по названию. langs — фильтр «есть перевод на этом языке»:
        если с фильтром пусто, запрос повторяется без него (лучше показать тайтл
        без русского перевода, чем ничего).
        """
        query = (query or "").strip()
        if not query:
            return []
        key = (query.lower(), int(limit), tuple(langs or ()))
        if key in self._search_cache:
            return self._search_cache[key]

        params: dict[str, Any] = {
            "title": query,
            "limit": max(1, int(limit)),
            "includes[]": ["cover_art"],
            "order[relevance]": "desc",
            "contentRating[]": ["safe", "suggestive", "erotica"],
        }
        if langs:
            params["availableTranslatedLanguage[]"] = list(langs)

        data = self._get_json("/manga", params)
        items = [self.as_manga(raw) for raw in (data.get("data") or [])]

        if not items and langs:
            params.pop("availableTranslatedLanguage[]", None)
            data = self._get_json("/manga", params)
            items = [self.as_manga(raw) for raw in (data.get("data") or [])]

        items = [item for item in items if item.get("id")]
        self._search_cache[key] = items
        return items

    @staticmethod
    def as_manga(raw: dict) -> dict:
        """Ответ MangaDex -> словарь, понятный UI (как у карточек аниме)."""
        attributes = raw.get("attributes") or {}
        titles: dict = attributes.get("title") or {}
        alt_titles = attributes.get("altTitles") or []
        primary = titles.get("ru") or titles.get("en") or next(iter(titles.values()), "")
        english = titles.get("en") or ""
        romaji = titles.get("ja-ro") or titles.get("ja") or ""

        # русского названия может не быть в title — поищем среди альтернативных
        if not titles.get("ru"):
            for entry in alt_titles:
                if isinstance(entry, dict) and entry.get("ru"):
                    primary = entry["ru"]
                    break

        cover_url = ""
        for relation in raw.get("relationships") or []:
            if relation.get("type") != "cover_art":
                continue
            file_name = (relation.get("attributes") or {}).get("fileName")
            if file_name:
                cover_url = f"{MANGADEX_UPLOADS}/covers/{raw.get('id')}/{file_name}.256.jpg"

        description = attributes.get("description") or {}
        status = attributes.get("status") or ""
        return {
            "id": raw.get("id"),
            "title": primary or english or romaji or "Без названия",
            "title_alt": english or romaji,
            "russian": titles.get("ru") or "",
            "year": attributes.get("year"),
            "status": MANGA_STATUS.get(status, status),
            "last_chapter": (attributes.get("lastChapter") or "").strip(),
            "poster_url": cover_url,
            "langs": sorted(attributes.get("availableTranslatedLanguages") or []),
            "description": (description.get("ru") or description.get("en") or "").strip(),
            "demographic": attributes.get("publicationDemographic") or "",
            "shikimori": None,   # заполняется, если тайтл подобран через Shikimori
        }

    # ---------- каталог (все доступные тайтлы) ----------
    def catalog(
        self,
        offset: int = 0,
        limit: int = MANGA_CATALOG_PAGE,
        sort: str = "popular",
        only_russian: bool = False,
    ) -> tuple[list[dict], int]:
        """
        Страница каталога MangaDex: все тайтлы, у которых есть главы.

        Возвращает (тайтлы, всего_в_каталоге). MangaDex не отдаёт «весь список»
        одним запросом, поэтому каталог листается по offset — тем же способом,
        что и страницы поиска. Фильтр «только с русским» оставляет тайтлы,
        у которых есть перевод на русский.
        """
        label, order_key = MANGA_CATALOG_SORTS.get(sort, MANGA_CATALOG_SORTS["popular"])
        params: dict[str, Any] = {
            "limit": max(1, min(100, int(limit))),
            "offset": max(0, int(offset)),
            f"order[{order_key}]": "asc" if order_key == "title" else "desc",
            "includes[]": ["cover_art"],
            "contentRating[]": ["safe", "suggestive"],
            "hasAvailableChapters": "true",     # без глав тайтл читать нечем
        }
        if only_russian:
            params["availableTranslatedLanguage[]"] = ["ru"]

        data = self._get_json("/manga", params)
        items = [self.as_manga(raw) for raw in (data.get("data") or [])]
        items = [item for item in items if item.get("id")]
        total = int(data.get("total") or 0)
        return items, total

    # ---------- главы ----------
    def chapters(self, manga_id: str, langs: tuple[str, ...] = ("ru", "en")) -> list[dict]:
        """
        Список глав. У одной главы бывает несколько переводов (язык + команда),
        поэтому на каждый номер главы оставляем один вариант по приоритету langs.

        Возвращает: {id, chapter, volume, title, lang, lang_label, group, pages, published}.
        """
        manga_id = str(manga_id or "")
        if not manga_id:
            return []
        key = (manga_id, tuple(langs))
        if key in self._chapter_cache:
            return self._chapter_cache[key]

        params: dict[str, Any] = {
            "limit": 500,
            "order[volume]": "asc",
            "order[chapter]": "asc",
            "includes[]": ["scanlation_group"],
            "contentRating[]": ["safe", "suggestive", "erotica"],
        }
        if langs:
            params["translatedLanguage[]"] = list(langs)

        payload = self._get_json(f"/manga/{manga_id}/feed", params)
        priority = {code: index for index, code in enumerate(langs)}
        best: dict[str, dict] = {}
        for raw in payload.get("data") or []:
            attributes = raw.get("attributes") or {}
            number = str(attributes.get("chapter") or "").strip()
            if not number:
                continue      # главы без номера (фрагменты/оншоты) в списке не нужны
            lang = attributes.get("translatedLanguage") or ""
            group = ""
            for relation in raw.get("relationships") or []:
                if relation.get("type") == "scanlation_group":
                    group = ((relation.get("attributes") or {}).get("name") or "").strip()
            # порядок выбора: сначала язык по приоритету, затем наличие команды перевода
            rank = (priority.get(lang, len(priority) + 1), 0 if group else 1)
            candidate = {
                "id": raw.get("id"),
                "chapter": number,
                "volume": str(attributes.get("volume") or "").strip(),
                "title": (attributes.get("title") or "").strip(),
                "lang": lang,
                "lang_label": dict(MANGA_LANGS).get(lang, (lang or "").upper()),
                "group": group,
                "pages": int(attributes.get("pages") or 0),
                "published": attributes.get("publishAt") or "",
                "_rank": rank,
            }
            current = best.get(number)
            if current is None or rank < current["_rank"]:
                best[number] = candidate

        chapters = sorted(best.values(), key=lambda item: self._chapter_sort_key(item["chapter"]))
        for item in chapters:
            item.pop("_rank", None)
        self._chapter_cache[key] = chapters
        return chapters

    @staticmethod
    def _chapter_sort_key(number: str) -> tuple[float, str]:
        """Сортировка глав: числовые по значению, прочие («экстра») — в конец."""
        try:
            return (float(number), "")
        except (TypeError, ValueError):
            return (float("inf"), str(number))

    # ---------- страницы ----------
    def pages(self, chapter_id: str, data_saver: bool = False) -> list[str]:
        """
        Прямые ссылки на страницы главы (в правильном порядке).
        data_saver=True — уменьшенные копии: быстрее и экономнее по трафику.
        """
        payload = self._get_json(f"/at-home/server/{chapter_id}")
        base = (payload.get("baseUrl") or "").rstrip("/")
        chapter = payload.get("chapter") or {}
        file_hash = chapter.get("hash") or ""
        folder = "data-saver" if data_saver else "data"
        files = chapter.get("dataSaver" if data_saver else "data") or []
        if not base or not file_hash:
            return []
        return [f"{base}/{folder}/{file_hash}/{name}" for name in files]

    # ---------- картинки (обложки и страницы) с дисковым кэшем ----------
    def _fetch_image(
        self,
        url: str,
        folder: Path,
        attempts: int = 1,
        timeout: int = POSTER_TIMEOUT,
        use_disk: bool = True,
    ) -> Optional[bytes]:
        """Картинка MangaDex с дисковым кэшем (общая функция fetch_image)."""
        return fetch_image(url, folder, attempts=attempts, timeout=timeout, use_disk=use_disk)

    def _download_poster(self, url: str) -> Optional[bytes]:
        """
        Обложка тайтла. Метод с таким именем нужен PosterLoader — он ждёт объект
        с _download_poster(url), поэтому для списка манги передаём этот клиент.
        """
        return self._fetch_image(url, MANGA_CACHE_DIR / "covers")

    @staticmethod
    def alternative_page_url(url: str) -> str:
        """
        Та же страница, но через uploads.mangadex.org. Нужно потому, что ноды сети
        MangaDex@Home у части провайдеров недоступны (в РФ соединение виснет или
        сбрасывается), а этот хост отдаёт страницы по старому пути /data/<hash>/<файл>.
        Возвращает "" если ссылку преобразовать нельзя.
        """
        path = urlparse(url).path
        if not path.startswith(("/data/", "/data-saver/")):
            return ""
        return MANGADEX_UPLOADS + path

    def _page_candidates(self, url: str) -> list[str]:
        """Порядок попыток для страницы: сначала хост, который уже работал."""
        alternative = self.alternative_page_url(url)
        node = not url.startswith(MANGADEX_UPLOADS)
        if node and not alternative:
            return [url]
        if alternative and not node:
            return [url]
        if self._page_host_pref == "uploads":
            return [alternative, url]
        return [url, alternative]

    def download_page(self, url: str) -> Optional[bytes]:
        """
        Страница главы. Пробует ноды MangaDex@Home, а если они не отвечают —
        тот же файл через uploads.mangadex.org, и запоминает рабочий вариант,
        чтобы остальные страницы качались без ожидания таймаутов.
        """
        candidates = self._page_candidates(url)
        # Первая страница за сеанс: не ждём по очереди (мёртвая нода — это десятки
        # секунд), а опрашиваем хосты одновременно и берём тот, кто ответил первым.
        # Замок нужен потому, что страницы качаются в несколько потоков сразу:
        # без него «гонка» запускалась бы для каждой из них.
        if self._page_host_pref is None and len(candidates) > 1:
            with self._page_host_lock:
                if self._page_host_pref is None:
                    data = self._download_page_race(candidates)
                    if data:
                        return data
                else:
                    candidates = self._page_candidates(url)

        for index, candidate in enumerate(candidates):
            timeout = MANGA_NODE_TIMEOUT if index == 0 else max(8, POSTER_TIMEOUT // 2)
            data = self._fetch_image(
                candidate,
                MANGA_CACHE_DIR / "pages",
                attempts=MANGA_PAGE_RETRIES + 1,
                timeout=timeout,
                use_disk=self.cache_pages_to_disk,
            )
            if data:
                self._page_host_pref = (
                    "uploads" if candidate.startswith(MANGADEX_UPLOADS) else "node"
                )
                if index > 0:
                    self.last_note = self._FALLBACK_NOTE
                return data
        return None

    _FALLBACK_NOTE = "ноды MangaDex недоступны, страницы идут через uploads.mangadex.org"

    def _download_page_race(self, candidates: list[str]) -> Optional[bytes]:
        """
        Одновременная попытка скачать страницу с нескольких хостов.
        Нужна только один раз за сеанс: дальше все страницы идут с победителя.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        pool = ThreadPoolExecutor(max_workers=len(candidates))
        try:
            futures = {
                pool.submit(
                    self._fetch_image, candidate, MANGA_CACHE_DIR / "pages", 1,
                    MANGA_NODE_TIMEOUT, self.cache_pages_to_disk,
                ): candidate
                for candidate in candidates
            }
            for future in as_completed(futures):
                candidate = futures[future]
                try:
                    data = future.result()
                except Exception:  # noqa: BLE001
                    data = None
                if not data:
                    continue
                self._page_host_pref = (
                    "uploads" if candidate.startswith(MANGADEX_UPLOADS) else "node"
                )
                if candidate != candidates[0]:
                    self.last_note = self._FALLBACK_NOTE
                print(f"[MangaDex] страницы качаем с {urlparse(candidate).netloc}")
                return data
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        return None

    # ---------- обслуживание кэша ----------
    @staticmethod
    def prune_cache(limit_mb: int = MANGA_CACHE_LIMIT_MB) -> int:
        """
        Удаляет самые старые страницы, если кэш разросся. Возвращает число удалённых
        файлов: кэш страниц растёт быстро, а место на диске не бесконечное.
        """
        try:
            files = [p for p in (MANGA_CACHE_DIR / "pages").glob("*.img") if p.is_file()]
            total = sum(p.stat().st_size for p in files)
            limit = max(50, int(limit_mb)) * 1024 * 1024
            if total <= limit:
                return 0
            files.sort(key=lambda p: p.stat().st_mtime)
            removed = 0
            for path in files:
                if total <= limit:
                    break
                try:
                    total -= path.stat().st_size
                    path.unlink()
                    removed += 1
                except OSError:
                    continue
            if removed:
                print(f"[MangaDex] кэш страниц очищен: удалено файлов — {removed}")
            return removed
        except OSError:
            return 0


# ======================================================================================
# Источник манги (АБСТРАКТНЫЙ) — точка расширения для плагинов
# ======================================================================================
class MangaSource(ABC):
    """
    Источник манги: откуда брать поиск, главы и страницы.

    Встроенный источник — MangaDex (см. MangaDexSource), но плагин может подключить
    свой сайт: достаточно унаследоваться от этого класса и реализовать search(),
    chapters() и pages(). Загрузка картинок и кэш уже готовы (download_page),
    переопределять их нужно только если у сайта особые требования (куки, referer).

    Что должен вернуть источник:

      search(query, limit, langs) -> [{"id", "title", "title_alt", "russian", "year",
          "status", "last_chapter", "poster_url", "langs", "description"}, ...]
          id — строковый идентификатор тайтла ВНУТРИ источника (у MangaDex это uuid);
          langs — список кодов языков перевода, доступных у тайтла (может быть пустым).

      chapters(manga_id, langs) -> [{"id", "chapter", "volume", "title", "lang",
          "lang_label", "group", "pages"}, ...]
          id — идентификатор главы (по нему потом запрашиваются страницы);
          chapter — номер главы строкой («12», «12.5»), по нему идёт сортировка.

      pages(chapter_id) -> ["https://.../01.jpg", ...]  — ссылки по порядку чтения.

    Приложение само не ходит в сеть за страницами: оно передаёт ссылки загрузчику
    (MangaPageLoader), который вызывает download_page(url).
    """

    name = "manga"          # короткий технический код источника (в ключах прогресса)
    label = "Манга"         # как источник называется в интерфейсе
    multi_language = True   # поддерживает ли выбор языка перевода (иначе список скрыт)
    last_note = ""          # пояснение последнего обращения (показывается в статусе)
    cache_pages_to_disk = True   # выключается настройкой «хранить страницы на диске»
    supports_catalog = False     # умеет ли источник показать каталог всех тайтлов
    catalog_sorts: dict[str, str] = {}   # {код сортировки: подпись} для каталога

    # ---------- обязательное ----------
    @abstractmethod
    def search(
        self,
        query: str,
        limit: int = MANGA_SEARCH_LIMIT,
        langs: Optional[list[str]] = None,
    ) -> list[dict]:
        """Поиск тайтлов по названию."""

    # ---------- необязательное ----------
    def catalog(
        self,
        offset: int = 0,
        limit: int = MANGA_CATALOG_PAGE,
        sort: str = "popular",
        only_russian: bool = False,
    ) -> tuple[list[dict], int]:
        """
        Страница каталога всех доступных тайтлов: (тайтлы, всего).

        По умолчанию источник каталога не даёт (supports_catalog = False), и
        приложение предложит поиск. Свой каталог добавить просто: верните
        supports_catalog = True, заполните catalog_sorts и реализуйте метод.
        """
        return [], 0

    @abstractmethod
    def chapters(self, manga_id: str, langs: tuple[str, ...] = ("ru", "en")) -> list[dict]:
        """Список глав тайтла (с языками перевода, если источник их различает)."""

    @abstractmethod
    def pages(self, chapter_id: str) -> list[str]:
        """Ссылки на страницы главы по порядку."""

    # ---------- готовое (переопределять обычно не нужно) ----------
    def download_page(self, url: str) -> Optional[bytes]:
        """Скачивает страницу главы в дисковый кэш cache/manga/pages."""
        return fetch_image(
            url,
            MANGA_CACHE_DIR / "pages",
            attempts=MANGA_PAGE_RETRIES + 1,
            use_disk=self.cache_pages_to_disk,
        )

    def _download_poster(self, url: str) -> Optional[bytes]:
        """
        Обложка тайтла. Метод с таким именем ждёт PosterLoader, поэтому источник
        можно передавать ему напрямую (как это делает приложение).
        """
        return fetch_image(url, MANGA_CACHE_DIR / "covers")

    def chapter_web_url(self, chapter_id: str) -> str:
        """Ссылка на главу в браузере — для кнопки «Браузер». Пусто = кнопка не нужна."""
        return ""

    def fit_chapters(self, chapters: list[dict]) -> list[dict]:
        """
        Приводит список глав к нужному виду: убирает пустые номера и сортирует.
        Готовый помощник для плагинов — можно просто вызвать в конце chapters().
        """
        cleaned = [c for c in (chapters or []) if str(c.get("chapter") or "").strip()]
        return sorted(cleaned, key=lambda c: MangaDexClient._chapter_sort_key(str(c["chapter"])))


class MangaDexSource(MangaSource):
    """
    Встроенный источник манги — официальный API MangaDex.
    Здесь только адаптер: вся работа с API лежит в MangaDexClient.
    """

    name = "mangadex"
    label = "MangaDex"
    multi_language = True
    supports_catalog = True
    catalog_sorts = {code: title for code, (title, _field) in MANGA_CATALOG_SORTS.items()}

    def __init__(self, client: Optional["MangaDexClient"] = None) -> None:
        self.client = client or MangaDexClient()

    # проксируем пояснения клиента (например, «страницы идут через uploads.mangadex.org»)
    @property
    def last_note(self) -> str:  # type: ignore[override]
        return self.client.last_note

    @last_note.setter
    def last_note(self, value: str) -> None:
        self.client.last_note = value

    @property
    def cache_pages_to_disk(self) -> bool:
        return self.client.cache_pages_to_disk

    @cache_pages_to_disk.setter
    def cache_pages_to_disk(self, value: bool) -> None:
        self.client.cache_pages_to_disk = bool(value)

    def search(
        self,
        query: str,
        limit: int = MANGA_SEARCH_LIMIT,
        langs: Optional[list[str]] = None,
    ) -> list[dict]:
        return self.client.search(query, limit=limit, langs=langs)

    def catalog(
        self,
        offset: int = 0,
        limit: int = MANGA_CATALOG_PAGE,
        sort: str = "popular",
        only_russian: bool = False,
    ) -> tuple[list[dict], int]:
        return self.client.catalog(offset=offset, limit=limit, sort=sort,
                                   only_russian=only_russian)

    def chapters(self, manga_id: str, langs: tuple[str, ...] = ("ru", "en")) -> list[dict]:
        return self.client.chapters(manga_id, langs)

    def pages(self, chapter_id: str) -> list[str]:
        return self.client.pages(chapter_id)

    def download_page(self, url: str) -> Optional[bytes]:
        return self.client.download_page(url)

    def _download_poster(self, url: str) -> Optional[bytes]:
        return self.client._download_poster(url)

    def chapter_web_url(self, chapter_id: str) -> str:
        return f"https://mangadex.org/chapter/{chapter_id}"

    def prune_cache(self, limit_mb: int = MANGA_CACHE_LIMIT_MB) -> int:
        return self.client.prune_cache(limit_mb)


class MangaProgressStore:
    """
    Прогресс чтения манги (manga_progress.json рядом с программой).

    Формат:
        {"version": 1, "items": {
            "<manga_id>": {
                "manga": {...снимок тайтла...}, "chapter_id": "…", "chapter": "12",
                "page": 4, "offset": 0.35, "lang": "ru", "updated": 1712345678.9
            }
        }}
    Запись одна на тайтл: при следующем открытии продолжаем с той же главы и страницы.
    """

    LIMIT = 200

    def __init__(self, path: Path = MANGA_PROGRESS_FILE) -> None:
        self.path = Path(path)
        self.items: dict[str, dict] = {}
        self.load()

    # ---------- чтение/запись ----------
    def load(self) -> None:
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding="utf-8"))
                items = data.get("items") if isinstance(data, dict) else None
                if isinstance(items, dict):
                    self.items = {str(k): v for k, v in items.items() if isinstance(v, dict)}
        except (OSError, ValueError):
            self.items = {}

    def save(self) -> None:
        """Атомарная запись: сначала во временный файл, потом replace."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"version": 1, "items": self.items}
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", delete=False, dir=str(self.path.parent), suffix=".tmp"
            ) as tmp:
                json.dump(payload, tmp, ensure_ascii=False, indent=2)
                tmp_path = Path(tmp.name)
            tmp_path.replace(self.path)
        except OSError:
            pass

    # ---------- операции ----------
    @staticmethod
    def _key(manga_id: Any, source: Any = "") -> str:
        """
        Ключ записи = источник + идентификатор тайтла. Так прогресс не перепутается,
        если два источника (например, MangaDex и плагин) вернут одинаковые id.
        """
        return f"{source or ''}|{manga_id}"

    def get(self, manga_id: Any, source: Any = "") -> Optional[dict]:
        return self.items.get(self._key(manga_id, source))

    def touch(
        self,
        manga: dict,
        chapter_id: str,
        chapter: str,
        page: int = 0,
        offset: float = 0.0,
        lang: str = "",
    ) -> None:
        """Записывает «где я остановился» и обновляет время записи."""
        manga_id = self._key(manga.get("id"), manga.get("source"))
        entry = self.items.get(manga_id)
        if entry is None:
            entry = {}
            self.items[manga_id] = entry
        snapshot = entry.setdefault("manga", {})
        for field in ("id", "title", "russian", "year", "status", "poster_url",
                      "last_chapter", "source"):
            if manga.get(field) and not snapshot.get(field):
                snapshot[field] = manga.get(field)

        entry["chapter_id"] = str(chapter_id or "")
        entry["chapter"] = str(chapter or "")
        entry["page"] = int(page or 0)
        entry["offset"] = round(float(offset or 0.0), 4)
        entry["lang"] = lang or entry.get("lang") or ""
        entry["updated"] = time.time()
        self._trim()
        self.save()

    def _trim(self) -> None:
        """Держим только самые свежие записи (LIMIT)."""
        if len(self.items) <= self.LIMIT:
            return
        ordered = sorted(
            self.items.items(), key=lambda kv: float(kv[1].get("updated") or 0), reverse=True
        )
        self.items = dict(ordered[: self.LIMIT])

    def remove(self, manga_id: Any, source: Any = "") -> None:
        if self.items.pop(self._key(manga_id, source), None) is not None:
            self.save()

    def listed(self) -> list[dict]:
        """Записи от свежих к старым — для списка «Читаю»."""
        return [
            entry
            for _key, entry in sorted(
                self.items.items(), key=lambda kv: float(kv[1].get("updated") or 0), reverse=True
            )
        ]

    def clear(self) -> None:
        self.items = {}
        self.save()


def load_settings() -> dict:
    """
    Настройки приложения (settings.json рядом с программой).
    Сейчас это список озвучек; файл создаётся автоматически при первом запуске,
    так что список студий можно менять без правки кода.
    """
    defaults: dict[str, Any] = {
        "dubbings": list(DUBBINGS),
        "theme": dict(DEFAULT_THEME),
        "player": {"volume": 80, "auto_next": True, "seek_step": 10},
        "library": {"path": "", "enabled": True},
        "manga": dict(DEFAULT_MANGA),
        "window": {"width": 1290, "height": 840, "last_query": ""},
    }
    try:
        if SETTINGS_FILE.exists():
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                merged = dict(defaults)
                merged.update(data)
                # вложенные словари сливаем отдельно, чтобы новые ключи не терялись
                for section in ("theme", "player", "window", "library", "manga"):
                    if isinstance(merged.get(section), dict):
                        base = dict(defaults[section])
                        base.update(merged[section])
                        merged[section] = base
                if not isinstance(merged.get("dubbings"), list) or not merged["dubbings"]:
                    merged["dubbings"] = defaults["dubbings"]
                return merged
        SETTINGS_FILE.write_text(
            json.dumps(defaults, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except (OSError, ValueError):
        pass
    return defaults


class PlaceholderVideoSource(VideoSource):
    """
    Шаблон-заготовка для своего парсера (Kodik/Sibnet/…). По умолчанию НЕ используется:
    приложение работает через AniLibriaVideoSource. Оставлен как точка расширения.
    """

    name = "placeholder"

    def get_stream_url(self, anime_id: int, episode: int, dubbing: str) -> Optional[str]:
        # // здесь подключи парсер Kodik/AniLibria/Sibnet
        #
        # Пример реальной реализации:
        #   1. Найти на Kodik/Sibnet страницу аниме по anime_id/названию.
        #   2. Найти серию (episode) и перевод (dubbing).
        #   3. Достать прямую ссылку на .m3u8 или .mp4 (обычно через API-эндпоинт плеера).
        #   4. Вернуть эту ссылку строкой.
        #
        # Пока парсер не подключён — возвращаем None, интерфейс покажет подсказку.
        print(
            f"[VideoSource:placeholder] нет парсера: anime_id={anime_id}, "
            f"episode={episode}, dubbing={dubbing}"
        )
        return None


class ManualLinkSource(VideoSource):
    """
    Ссылки, добавленные пользователем вручную (manual_streams.json).

    Формат файла:
        {
          "12876|1|AniDUB": "https://example.com/ep1.m3u8",
          "12876|2|JAM CLUB": "https://example.com/ep2.mp4"
        }
    Ключ — «<shikimori_id>|<серия>|<озвучка>», значение — любая ссылка, которую понимает mpv.

    Именно этот источник позволяет «включить» любую озвучку и любой тайтл, которого
    нет в AniLibria: достаточно добавить ссылку (кнопка «＋ ссылка» в интерфейсе).
    """

    name = "manual"

    def __init__(self, path: Path = MANUAL_FILE) -> None:
        self.path = Path(path)
        self._links: dict[str, str] = {}
        self.load()

    # ---------- файл ----------
    @staticmethod
    def key(anime_id: Any, episode: Any, dubbing: Any) -> str:
        return f"{anime_id}|{episode}|{dubbing}"

    def load(self) -> None:
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._links = {str(k): str(v) for k, v in data.items() if v}
        except (OSError, ValueError):
            self._links = {}

    def save(self) -> None:
        try:
            self.path.write_text(
                json.dumps(self._links, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass

    # ---------- API ----------
    def add_link(self, anime_id: Any, episode: Any, dubbing: str, url: str) -> None:
        self._links[self.key(anime_id, episode, dubbing)] = url.strip()
        self.save()

    def remove_link(self, anime_id: Any, episode: Any, dubbing: str) -> None:
        self._links.pop(self.key(anime_id, episode, dubbing), None)
        self.save()

    def link_for(self, anime_id: Any, episode: Any, dubbing: str) -> str:
        """
        Ссылка для конкретной серии. Если её нет — ищем шаблон вида
        ``<anime_id>|*|<озвучка>`` и подставляем номер серии вместо {episode}.
        Так одной строкой можно включить сразу всю озвучку для всех серий.
        """
        exact = self._links.get(self.key(anime_id, episode, dubbing), "")
        if exact:
            return exact
        template = self._links.get(f"{anime_id}|*|{dubbing}", "")
        if template:
            return template.replace("{episode}", str(episode)).replace("{ep}", str(episode))
        return ""

    def add_template(self, anime_id: Any, dubbing: str, template: str) -> None:
        """Шаблон ссылки на все серии: {episode} внутри URL заменяется на номер серии."""
        self._links[f"{anime_id}|*|{dubbing}"] = template.strip()
        self.save()

    def dubbings_for(self, anime_id: Any) -> list[str]:
        """Какие озвучки уже имеют хотя бы одну ссылку (или шаблон) для этого аниме."""
        prefix = f"{anime_id}|"
        found: list[str] = []
        for key in self._links:
            if key.startswith(prefix):
                parts = key.split("|", 2)
                if len(parts) == 3 and parts[2] not in found:
                    found.append(parts[2])
        return found

    # ---------- VideoSource ----------
    def get_stream_url(self, anime_id: int, episode: int, dubbing: str) -> Optional[str]:
        url = self.link_for(anime_id, episode, dubbing)
        if url:
            self.last_status = f"ссылка добавлена вручную ({dubbing})"
        return url or None

    def available_dubbings(self) -> list[str]:
        """
        Сам по себе источник не даёт ни одной озвучки: играет только то, для чего
        пользователь добавил ссылку. Поэтому список пуст — конкретные озвучки
        добавляет интерфейс через dubbings_for(anime_id). Иначе все 30 студий
        выглядели бы «доступными», хотя ссылок нет.
        """
        return []


class LocalLibrarySource(VideoSource):
    """
    Локальная библиотека: видеофайлы на вашем диске.

    Озвучка определяется по имени файла, поэтому «Dream Cast», «JAM CLUB» и любые
    другие студии появляются в списке сами — если у вас есть такие файлы::

        Мастера меча онлайн - 01 [Dream Cast].mkv
        Sword Art Online 01 (JAM CLUB).mp4
        [AniDUB] SAO - 02.mkv
        SAO_S01E02_AniDUB.mkv

    Тайтл сопоставляется с выбранным аниме по названию (та же логика похожести,
    что и для AniLibria), поэтому файлы достаточно назвать узнаваемо.
    """

    name = "library"
    label = "ваши файлы"

    EXTENSIONS = (".mp4", ".mkv", ".webm", ".avi", ".m4v", ".mov", ".ts", ".mpg", ".mpeg")
    # слова, которые не являются названием озвучки
    NOISE = (
        "1080p", "720p", "480p", "2160p", "4k", "hd", "bd", "bdrip", "webrip", "web-dl",
        "webdl", "hdtv", "x264", "x265", "hevc", "h264", "aac", "ac3", "dub", "sub",
        "rus", "jpn", "eng", "rip", "avc", "10bit", "8bit", "hevc", "hdr",
    )

    def __init__(self, path: str = "", known_dubbings: Optional[list[str]] = None) -> None:
        self.root = Path(path) if path else None
        self._files: list[dict] = []          # распарсенные файлы
        self._scanned = False
        # названия студий из настроек — по ним озвучка узнаётся даже без скобок
        self.known_dubbings: list[str] = [d for d in (known_dubbings or []) if d]

    # ---------- настройка ----------
    def set_root(self, path: str) -> None:
        self.root = Path(path) if path else None
        self._scanned = False
        self._files = []

    @property
    def ready(self) -> bool:
        return bool(self.root and self.root.is_dir())

    # ---------- разбор имён ----------
    @classmethod
    def _is_noise(cls, text: str) -> bool:
        """«1080p», «x264», «BDRip» — это не название озвучки."""
        import re

        words = [w for w in re.split(r"[\s._\-]+", text.lower()) if w]
        return bool(words) and all(w in cls.NOISE for w in words)

    @classmethod
    def _parse_name(cls, filename: str, known_dubbings: Optional[list[str]] = None) -> dict:
        """
        «Мастера меча онлайн - 01 [Dream Cast].mkv» -> title/episode/dubbing.

        Озвучка ищется сначала в скобках ([Dream Cast], (JAM CLUB)), затем —
        по названиям студий из настроек, даже если они записаны без скобок
        («..._AniDUB_1080p.mkv»).
        """
        import re

        stem = Path(filename).stem
        tags = re.findall(r"[\[(]([^\])]+)[\])]", stem)

        dubbing = ""
        for tag in tags:
            clean = tag.strip()
            if not clean or clean.isdigit() or cls._is_noise(clean):
                continue
            if sum(ch.isalpha() for ch in clean) < 3:
                continue
            dubbing = clean
            break

        if not dubbing and known_dubbings:
            joined = _normalize_title(stem).replace(" ", "")
            for name in known_dubbings:
                norm = _normalize_title(name).replace(" ", "")
                if len(norm) >= 4 and norm in joined:
                    dubbing = name
                    break

        base = re.sub(r"[\[(][^\])]*[\])]", " ", stem)   # убираем теги
        base = base.replace("_", " ").replace(".", " ").replace("-", " ")

        episode = None
        match = re.search(r"[Ss](\d{1,2})\s?[Ee](\d{1,3})", base)   # S01E02 / S01 E02
        if match:
            episode = int(match.group(2))
            base = base[: match.start()]
        else:
            match = re.search(
                r"(?:серия|seriya|seria|episode|ep|выпуск)[\s._-]*(\d{1,3})", base, re.IGNORECASE
            )
            if match:
                episode = int(match.group(1))
                base = base[: match.start()]
            else:
                # последнее «отдельное» число в имени — обычно номер серии.
                # Отдельное = не внутри слова (чтобы не разрезать «Online 02»).
                numbers = [
                    m for m in re.finditer(r"(?<![0-9A-Za-zА-Яа-я])(\d{1,3})(?![0-9A-Za-zА-Яа-я])", base)
                    if int(m.group(1)) < 1000
                ]
                if numbers:
                    last = numbers[-1]
                    episode = int(last.group(1))
                    base = base[: last.start()]

        title = " ".join(base.split())
        title = re.sub(r"[-–—]+\s*$", "", title).strip()
        return {"title": title, "episode": episode, "dubbing": dubbing, "path": ""}

    # ---------- сканирование ----------
    def scan(self, force: bool = False) -> list[dict]:
        if self._scanned and not force:
            return self._files
        files: list[dict] = []
        if self.ready:
            try:
                for path in sorted(self.root.rglob("*")):
                    if not path.is_file() or path.suffix.lower() not in self.EXTENSIONS:
                        continue
                    parsed = self._parse_name(path.name, self.known_dubbings)
                    parsed["path"] = str(path)
                    parsed["norm"] = _normalize_title(parsed["title"])
                    if parsed["norm"]:
                        files.append(parsed)
            except OSError as exc:
                print(f"[library] сканирование не удалось: {exc}")
        self._files = files
        self._scanned = True
        print(f"[library] найдено файлов: {len(files)} в {self.root}")
        return files

    # ---------- поиск по выбранному аниме ----------
    @staticmethod
    def _initials(text: str) -> str:
        """«Sword Art Online» -> «sao»: узнаём файлы с аббревиатурой в имени."""
        words = [w for w in _normalize_title(text).split() if w]
        return "".join(w[0] for w in words)

    def files_for(self, anime_id: int, title: str, title_alt: str = "") -> list[dict]:
        """Файлы, относящиеся к этому аниме (по похожести названия)."""
        initials = {self._initials(title), self._initials(title_alt)}
        found: list[dict] = []
        for item in self.scan():
            score = max(
                _title_similarity(title, item["title"]),
                _title_similarity(title_alt, item["title"]),
            )
            # сокращение: файл «SAO - 02 [JAM CLUB].mkv» относится к «Sword Art Online»
            short = item["norm"].replace(" ", "")
            if score < 0.6 and 2 <= len(short) <= 6 and short in initials:
                score = 0.75
            if score >= 0.6:
                entry = dict(item)
                entry["score"] = score
                found.append(entry)
        found.sort(key=lambda f: (-f["score"], f.get("episode") or 0))
        return found

    def dubbings_for(self, anime_id: int, title: str, title_alt: str = "") -> list[str]:
        """Озвучки, найденные в ваших файлах для этого аниме."""
        result: list[str] = []
        for item in self.files_for(anime_id, title, title_alt):
            name = item.get("dubbing") or "файл"
            if name not in result:
                result.append(name)
        return result

    def library_episodes(
        self, anime_id: int, title: str = "", title_alt: str = ""
    ) -> Optional[list[tuple[int, str]]]:
        """Серии, которые есть локально (для списка серий)."""
        title = title or self.anime_title
        alt = title_alt or self.anime_title_alt
        items = self.files_for(anime_id, title, alt)
        episodes: dict[int, list[str]] = {}
        for item in items:
            number = int(item.get("episode") or 0)
            if number <= 0:
                continue
            episodes.setdefault(number, [])
            dub = item.get("dubbing")
            if dub and dub not in episodes[number]:
                episodes[number].append(dub)
        if not episodes:
            return None
        return [
            (number, ", ".join(episodes[number]))
            for number in sorted(episodes)
        ]

    # ---------- VideoSource ----------
    def get_episodes(self, anime_id: int) -> Optional[list[tuple[int, str]]]:
        if not self.ready:
            return None
        return self.library_episodes(anime_id, self.anime_title, self.anime_title_alt)

    def available_dubbings(self) -> list[str]:
        return []   # конкретные озвучки зависят от аниме: см. dubbings_for()

    def get_stream_url(self, anime_id: int, episode: int, dubbing: str) -> Optional[str]:
        if not self.ready:
            return None
        candidates = self.files_for(anime_id, self.anime_title, self.anime_title_alt)
        exact = [
            item for item in candidates
            if int(item.get("episode") or 0) == int(episode) and item.get("dubbing") == dubbing
        ]
        # если озвучка не совпала — берём любой файл этой серии (обычно он один)
        if not exact:
            exact = [item for item in candidates if int(item.get("episode") or 0) == int(episode)]
        if not exact:
            return None
        self.last_status = f"файл: {Path(exact[0]['path']).name}"
        return exact[0]["path"]


SUBTITLE_EXTENSIONS = (".srt", ".ass", ".ssa", ".vtt", ".sub")


def find_subtitles(video_path: str) -> list[str]:
    """
    Ищет файлы субтитров рядом с видео, которые подходят к нему по имени:
    «sao-02.mkv» -> «sao-02.srt», «sao-02.ru.srt», «sao-02.forced.ass» и т.п.
    Русские версии возвращаются первыми.
    """
    video = Path(video_path)
    if not video.is_file():
        return []
    stem = video.stem.lower()
    found: list[tuple[int, str]] = []
    try:
        for candidate in video.parent.iterdir():
            if not candidate.is_file() or candidate.suffix.lower() not in SUBTITLE_EXTENSIONS:
                continue
            name = candidate.stem.lower()
            if not name.startswith(stem):
                continue
            # приоритет: .ru, затем .rus, затем forced, затем остальные
            priority = 3
            if ".ru" in name or "rus" in name:
                priority = 0
            elif ".en" in name or "eng" in name:
                priority = 1
            elif "forced" in name or "форс" in name:
                priority = 2
            found.append((priority, str(candidate)))
    except OSError:
        return []
    found.sort()
    return [path for _priority, path in found]


def parse_playlist(path: Path) -> list[tuple[str, str]]:
    """
    Читает плейлист и возвращает [(название, ссылка), ...].

    Поддерживаются:
      * .m3u / .m3u8 — обычный формат: строки #EXTINF:<длительность>,<название>
        перед ссылкой;
      * .json — список объектов [{"title": "...", "url": "..."}] либо словарь
        {"название": "ссылка"}.
    """
    entries: list[tuple[str, str]] = []
    try:
        raw = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"[playlist] не прочитать {path}: {exc}")
        return entries

    if Path(path).suffix.lower() == ".json":
        try:
            data = json.loads(raw)
        except ValueError as exc:
            print(f"[playlist] некорректный JSON: {exc}")
            return entries
        if isinstance(data, dict):
            for title, url in data.items():
                if isinstance(url, str) and url.startswith(("http://", "https://")):
                    entries.append((str(title), url))
        elif isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                url = item.get("url") or item.get("link") or item.get("src")
                title = item.get("title") or item.get("name") or item.get("dubbing") or ""
                if isinstance(url, str) and url.startswith(("http://", "https://")):
                    entries.append((str(title), url))
        return entries

    # .m3u / .m3u8
    title = ""
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            parts = line.split(",", 1)
            title = parts[1].strip() if len(parts) > 1 else ""
        elif line.startswith("#"):
            continue
        elif line.startswith(("http://", "https://")):
            entries.append((title, line))
            title = ""
    return entries


def _expose_main_module() -> None:
    """
    Делает текущий модуль доступным плагинам как `main`.

    Зачем: приложение обычно запускается как `python main.py`, то есть сам файл
    называется `__main__`. Если плагин напишет `from main import VideoSource`,
    Python загрузит main.py ВТОРОЙ раз под именем `main` — и в плагине окажется
    копия класса, а не тот же самый. Тогда плагин не считается наследником и
    молча не подключается, хотя выглядит правильным.

    Решение: заранее положить уже загруженный модуль в sys.modules["main"].
    Вызывается перед загрузкой любого плагина.
    """
    current = sys.modules.get("__main__")
    if current is not None and hasattr(current, "VideoSource"):
        sys.modules.setdefault("main", current)


def load_plugin_sources() -> list[VideoSource]:
    """
    Подключает пользовательские источники видео из папки sources_local.

    Каждый файл *.py в этой папке может определить класс-наследник VideoSource
    (метод get_stream_url обязателен). Приложение подхватит его автоматически,
    без правки main.py. Ошибки в плагине не ломают программу — они пишутся в консоль.

    Требования к классу: наследоваться от VideoSource и реализовать
    get_stream_url(anime_id, episode, dubbing) -> Optional[str].
    Необязательно: get_episodes(anime_id), available_dubbings().

    Новый, более широкий формат плагинов — папка plugins/ (см. PluginManager):
    там можно добавлять не только источники видео, но и источники манги, темы,
    кнопки и обработчики событий. Этот формат (sources_local) поддерживается
    и дальше, чтобы старые источники продолжали работать.
    """
    sources: list[VideoSource] = []
    if not SOURCES_DIR.is_dir():
        return sources
    _expose_main_module()      # чтобы `from main import VideoSource` дал тот же класс

    for path in sorted(SOURCES_DIR.glob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location(f"animeviewer_src_{path.stem}", path)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception as exc:
            print(f"[plugins] {path.name}: не удалось загрузить ({type(exc).__name__}: {exc})")
            continue

        for name, obj in vars(module).items():
            if not isinstance(obj, type) or obj is VideoSource:
                continue
            if not issubclass(obj, VideoSource):
                continue
            try:
                instance = obj()
            except Exception as exc:
                print(f"[plugins] {path.name}: класс {name} не создался ({exc})")
                continue
            sources.append(instance)
            print(f"[plugins] подключён источник «{getattr(instance, 'label', name)}» из {path.name}")
            if obj.available_dubbings is VideoSource.available_dubbings:
                print(
                    f"[plugins] предупреждение: {name} не переопределяет available_dubbings() — "
                    "все озвучки будут показаны как доступные. Переопределите его, если это не так."
                )
    return sources


# ======================================================================================
# Плагины: точки расширения приложения
# ======================================================================================
# Плагин — обычный файл .py в папке plugins/ с классом-наследником Plugin.
# Плагин может добавить источники видео, источники манги, темы оформления, кнопки
# и подписаться на события приложения. Всё это описано в plugins/README.txt.
# Главное правило: ошибка в плагине не должна ломать приложение — поэтому каждый
# вызов плагина обёрнут в try/except, а сведения об ошибке видно в «Плагины».
class PluginAction:
    """
    Кнопка, которую плагин добавляет в меню «Плагины».

    title    — надпись на кнопке;
    callback — функция(ctx), вызывается по нажатию (исключения ловит приложение);
    tooltip  — подсказка при наведении;
    needs    — что должно быть выбрано: "anime", "manga" или "" (ничего не нужно).
    """

    def __init__(
        self,
        title: str,
        callback: Callable[..., Any],
        tooltip: str = "",
        needs: str = "",
    ) -> None:
        self.title = str(title)
        self.callback = callback
        self.tooltip = str(tooltip)
        self.needs = str(needs)


class PluginStore:
    """
    Небольшое хранилище для плагинов (plugin_settings.json рядом с программой).

    У каждого плагина свой раздел — по имени плагина, поэтому плагины не мешают
    друг другу, а файл настроек приложения остаётся только про приложение.
    """

    def __init__(self, path: Path = PLUGIN_SETTINGS_FILE) -> None:
        self.path = Path(path)
        self.data: dict[str, dict] = {}
        self.load()

    def load(self) -> None:
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self.data = {str(k): v for k, v in data.items() if isinstance(v, dict)}
        except (OSError, ValueError):
            self.data = {}

    def save(self) -> None:
        # пустой файл без плагинов не создаём: незачем засорять папку приложения
        if not self.data and not self.path.exists():
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(self.data, ensure_ascii=False, indent=2)
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", delete=False, dir=str(self.path.parent), suffix=".tmp"
            ) as tmp:
                tmp.write(payload)
                tmp_path = Path(tmp.name)
            tmp_path.replace(self.path)
        except OSError:
            pass

    def section(self, name: str) -> dict:
        """Раздел плагина (создаётся при первом обращении)."""
        return self.data.setdefault(str(name or "plugin"), {})


class PluginContext:
    """
    То, что плагин получает от приложения.

    Внутри — только безопасные вещи: версии, пути, текущий выбор, сообщение в
    статусной строке и своё хранилище. Внутренние объекты окна плагину не выдаются,
    поэтому даже ошибка в плагине не может «уронить» интерфейс.
    """

    def __init__(self, app: Any, plugin: "Plugin") -> None:
        self._app = app
        self.plugin = plugin
        self.data: dict = {}                      # раздел плагина в plugin_settings.json
        self.save_data: Callable[[], None] = lambda: None

    # ---------- версии и пути ----------
    @property
    def version(self) -> str:
        return APP_VERSION

    @property
    def api(self) -> int:
        return PLUGIN_API_VERSION

    @property
    def base_dir(self) -> Path:
        return BASE_DIR

    @property
    def cache_dir(self) -> Path:
        return CACHE_DIR

    @property
    def plugins_dir(self) -> Path:
        return PLUGINS_DIR

    # ---------- настройки приложения (только чтение) ----------
    def settings(self) -> dict:
        return dict(getattr(self._app, "settings", {}) or {})

    def theme(self) -> dict:
        return dict(getattr(self._app, "theme", {}) or {})

    def manga_settings(self) -> dict:
        return dict(getattr(self._app, "manga_settings", {}) or {})

    # ---------- текущий выбор ----------
    def current_anime(self) -> Optional[dict]:
        anime = getattr(self._app, "current_anime", None)
        return dict(anime) if isinstance(anime, dict) else None

    def current_manga(self) -> Optional[dict]:
        manga = getattr(self._app, "current_manga", None)
        return dict(manga) if isinstance(manga, dict) else None

    def current_chapter(self) -> Optional[dict]:
        chapter = getattr(self._app, "current_chapter", None)
        return dict(chapter) if isinstance(chapter, dict) else None

    # ---------- действия ----------
    def status(self, text: str) -> None:
        """Показать сообщение в статусной строке приложения."""
        app = getattr(self._app, "status", None)
        if callable(app):
            app(str(text))

    def open_url(self, url: str) -> None:
        """Открыть ссылку в браузере по умолчанию."""
        try:
            from PySide6.QtCore import QUrl
            from PySide6.QtGui import QDesktopServices

            QDesktopServices.openUrl(QUrl(str(url)))
        except Exception as exc:  # noqa: BLE001
            print(f"[плагины] не удалось открыть ссылку: {exc}")

    def open_folder(self, path: Optional[Any] = None) -> None:
        """Открыть папку в проводнике (по умолчанию — папку плагинов)."""
        target = Path(path) if path else PLUGINS_DIR
        try:
            from PySide6.QtCore import QUrl
            from PySide6.QtGui import QDesktopServices

            QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))
        except Exception as exc:  # noqa: BLE001
            print(f"[плагины] не удалось открыть папку: {exc}")


class Plugin:
    """
    Базовый класс плагина приложения.

    Достаточно унаследоваться, описать себя в атрибутах и переопределить нужные
    методы — остальные работают по умолчанию (ничего не делают).

    Пример (см. plugins/_example_plugin.py):

        class MyPlugin(Plugin):
            name = "my-plugin"          # технический код (уникальный)
            title = "Мой плагин"        # название в списке плагинов
            version = "1.0"
            author = "Вася"
            description = "Что делает плагин"

            def actions(self):
                return [PluginAction("Поздороваться", self.hello)]

            def hello(self, ctx):
                ctx.status(f"Привет из {ctx.plugin.title}!")

    Точки расширения:
        video_sources() -> список добавленных источников видео (VideoSource);
        manga_sources() -> список источников манги (MangaSource);
        themes()        -> {код: палитра} — темы в «Оформлении»;
        actions()       -> список PluginAction — кнопки в меню «Плагины».

    События (все необязательные):
        on_started(ctx)                              — приложение запустилось;
        on_anime_selected(anime)                     — выбран тайтл аниме;
        on_playback_started(anime, episode, dubbing, url) — началось воспроизведение;
        on_chapter_opened(manga, chapter)            — открыта глава манги;
        on_closing()                                 — приложение закрывается.
    """

    name = "plugin"          # уникальный код плагина
    title = ""               # название для интерфейса
    version = "1.0"
    author = ""
    description = ""
    api = PLUGIN_API_VERSION

    # ---------- точки расширения ----------
    def video_sources(self) -> list["VideoSource"]:
        return []

    def manga_sources(self) -> list["MangaSource"]:
        return []

    def themes(self) -> dict[str, dict]:
        return {}

    def actions(self) -> list[PluginAction]:
        return []

    # ---------- события ----------
    def on_started(self, ctx: PluginContext) -> None:
        """Вызывается один раз после загрузки плагина."""

    def on_anime_selected(self, anime: dict) -> None:
        """Выбран тайтл в списке или каталоге."""

    def on_playback_started(self, anime: dict, episode: int, dubbing: str, url: str) -> None:
        """Началось воспроизведение серии."""

    def on_chapter_opened(self, manga: dict, chapter: dict) -> None:
        """Открыта глава манги."""

    def on_closing(self) -> None:
        """Приложение закрывается (можно сохранить свои данные)."""


class _SourcesOnlyPlugin(Plugin):
    """
    Обёртка для файла, в котором нет класса Plugin, но есть источники видео.
    Нужна для совместимости: такие файлы можно положить и в plugins/, и в sources_local.
    """

    def __init__(self, path: Path, sources: list["VideoSource"]) -> None:
        self.name = f"file:{path.stem}"
        self.title = f"Источники из {path.name}"
        self.description = "Файл добавляет источники видео без класса Plugin"
        self._sources = list(sources)

    def video_sources(self) -> list["VideoSource"]:
        return list(self._sources)


class LoadedPlugin:
    """Загруженный плагин: сам объект, файл, контекст и сведения об ошибках."""

    def __init__(
        self,
        path: Path,
        plugin: Optional[Plugin] = None,
        error: str = "",
        context: Optional[PluginContext] = None,
    ) -> None:
        self.path = Path(path)
        self.plugin = plugin
        self.error = error
        self.context = context
        self.errors: list[str] = []
        self.broken_hooks: set[str] = set()

    @property
    def name(self) -> str:
        if self.plugin is not None:
            return self.plugin.name or self.path.stem
        return self.path.stem

    @property
    def title(self) -> str:
        if self.plugin is not None:
            return self.plugin.title or self.plugin.name or self.path.stem
        return self.path.stem


class PluginManager:
    """
    Находит плагины в папке plugins/ и отдаёт приложению всё, что они добавили.

    Как это работает:
      * каждый *.py в plugins/ (кроме файлов, начинающихся с «_») загружается отдельно;
      * плагином считается класс-наследник Plugin — экземпляр создаётся автоматически,
        в одном файле их может быть несколько;
      * если в файле нет Plugin, но есть наследники VideoSource, они подключаются
        как источники видео (совместимость со старым форматом sources_local);
      * любая ошибка загрузки или вызова запоминается и видна в «Плагины → Список
        плагинов», но приложение из-за неё не падает.

    Ограничение на будущее: если плагин ошибается в одном и том же событии три раза,
    это событие для него перестаёт вызываться — чтобы ошибка не повторялась в цикле.
    """

    MAX_HOOK_FAILURES = 3

    def __init__(self, directory: Path = PLUGINS_DIR) -> None:
        self.directory = Path(directory)
        self.plugins: list[LoadedPlugin] = []
        self.errors: list[tuple[str, str]] = []      # (файл, текст) — плагин не загрузился
        self.warnings: list[tuple[str, str]] = []    # (файл, текст) — загрузился с замечанием
        self._app: Any = None
        self._store: Optional[PluginStore] = None

    # ---------- загрузка ----------
    def load(self) -> None:
        self.plugins = []
        self.errors = []
        self.warnings = []
        if not self.directory.is_dir():
            print(f"[плагины] папки {self.directory} нет — плагины не подключены")
            return
        # плагины пишут `from main import Plugin, ...` — отдаём им тот же модуль
        _expose_main_module()
        for path in sorted(self.directory.glob("*.py")):
            if path.name.startswith("_"):
                continue      # _example_*.py — примеры, они специально не подключаются
            self._load_file(path)
        loaded = [p for p in self.plugins if p.plugin is not None]
        if loaded:
            print(f"[плагины] подключено плагинов: {len(loaded)} — "
                  f"{', '.join(p.title for p in loaded)}")
        for path_name, message in self.errors:
            print(f"[плагины] {path_name}: {message}")

    def _load_file(self, path: Path) -> None:
        module_name = f"animeviewer_plugin_{path.stem}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                raise RuntimeError("не удалось прочитать файл плагина")
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module     # чтобы относительные импорты работали
            spec.loader.exec_module(module)
        except Exception as exc:  # noqa: BLE001 — плагин может упасть как угодно
            self.errors.append((path.name, f"не загрузился ({type(exc).__name__}: {exc})"))
            self.plugins.append(LoadedPlugin(path, None, f"{type(exc).__name__}: {exc}"))
            return

        found = 0
        orphan_sources: list[VideoSource] = []
        for object_name, obj in sorted(vars(module).items()):
            if not isinstance(obj, type):
                continue
            if issubclass(obj, Plugin) and obj is not Plugin and obj is not _SourcesOnlyPlugin:
                instance = self._instantiate(obj, object_name, path)
                if instance is not None:
                    found += 1
            elif issubclass(obj, VideoSource) and obj is not VideoSource:
                try:
                    orphan_sources.append(obj())
                except Exception as exc:  # noqa: BLE001
                    self.errors.append(
                        (path.name, f"источник {object_name} не создался ({exc})")
                    )
        if not found and orphan_sources:
            # файл без класса Plugin — считаем его набором источников видео
            self.plugins.append(LoadedPlugin(path, _SourcesOnlyPlugin(path, orphan_sources)))
            self.warnings.append(
                (path.name, "нет класса Plugin — источники видео подключены как есть")
            )

    def _instantiate(self, cls: type, object_name: str, path: Path) -> Optional[Plugin]:
        try:
            plugin = cls()
        except Exception as exc:  # noqa: BLE001
            self.errors.append((path.name, f"плагин {object_name} не создался ({exc})"))
            return None
        if not isinstance(plugin, Plugin):
            return None
        if any(p.name == plugin.name for p in self.plugins if p.plugin is not None):
            self.warnings.append(
                (path.name, f"плагин с кодом «{plugin.name}» уже подключён — этот пропущен")
            )
            return None
        try:
            plugin_api = int(getattr(plugin, "api", PLUGIN_API_VERSION))
        except (TypeError, ValueError):
            plugin_api = PLUGIN_API_VERSION
        if plugin_api > PLUGIN_API_VERSION:
            self.warnings.append(
                (path.name, f"плагин ждёт API {plugin_api}, приложение даёт {PLUGIN_API_VERSION}")
            )
        self.plugins.append(LoadedPlugin(path, plugin))
        return plugin

    # ---------- контексты и событие «запуск» ----------
    def bind(self, app: Any, store: Optional[PluginStore] = None) -> None:
        """
        Передаёт плагинам доступ к приложению: создаёт контексты и хранилище,
        после чего вызывает on_started() у каждого плагина.
        """
        self._app = app
        self._store = store or PluginStore()
        for loaded in self.plugins:
            if loaded.plugin is None:
                continue
            context = PluginContext(app, loaded.plugin)
            context.data = self._store.section(loaded.plugin.name)
            context.save_data = self._store.save
            loaded.context = context
            self.call_one(loaded, "on_started", context)

    # ---------- что добавили плагины ----------
    def _collect(self, method: str, expected: type, what: str) -> list[Any]:
        result: list[Any] = []
        for loaded in self.plugins:
            if loaded.plugin is None:
                continue
            handler = getattr(loaded.plugin, method, None)
            if not callable(handler):
                continue
            try:
                items = handler() or []
            except Exception as exc:  # noqa: BLE001
                self.note_hook_error(loaded, method, exc)
                continue
            for item in items:
                if isinstance(item, expected):
                    result.append(item)
                else:
                    self.warnings.append(
                        (loaded.path.name, f"{what}: пропущен объект {type(item).__name__}")
                    )
        return result

    def video_sources(self) -> list[VideoSource]:
        return self._collect("video_sources", VideoSource, "источник видео")

    def manga_sources(self) -> list[MangaSource]:
        return self._collect("manga_sources", MangaSource, "источник манги")

    def themes(self) -> dict[str, dict]:
        """Темы плагинов. Встроенные темы плагин перекрыть не может."""
        result: dict[str, dict] = {}
        for loaded in self.plugins:
            if loaded.plugin is None:
                continue
            try:
                themes = loaded.plugin.themes() or {}
            except Exception as exc:  # noqa: BLE001
                self.note_hook_error(loaded, "themes", exc)
                continue
            if not isinstance(themes, dict):
                self.warnings.append((loaded.path.name, "themes() вернул не словарь"))
                continue
            for key, palette in themes.items():
                if not isinstance(palette, dict) or not key:
                    self.warnings.append((loaded.path.name, f"тема «{key}» пропущена"))
                    continue
                if key in THEME_MODES:
                    self.warnings.append(
                        (loaded.path.name, f"тема «{key}» уже есть в приложении — пропущена")
                    )
                    continue
                palette = dict(palette)
                palette.setdefault("title", f"{loaded.title}: {key}")
                result[str(key)] = palette
        return result

    def actions(self) -> list[tuple[LoadedPlugin, PluginAction]]:
        """Кнопки плагинов: [(плагин, действие), ...]."""
        result: list[tuple[LoadedPlugin, PluginAction]] = []
        for loaded in self.plugins:
            if loaded.plugin is None:
                continue
            try:
                items = loaded.plugin.actions() or []
            except Exception as exc:  # noqa: BLE001
                self.note_hook_error(loaded, "actions", exc)
                continue
            for item in items:
                if isinstance(item, PluginAction):
                    result.append((loaded, item))
                else:
                    self.warnings.append(
                        (loaded.path.name, f"действие пропущено: {type(item).__name__}")
                    )
        return result

    # ---------- события ----------
    def call(self, hook: str, *args: Any) -> None:
        """Вызывает событие у всех плагинов (ошибки не выходят наружу)."""
        for loaded in list(self.plugins):
            self.call_one(loaded, hook, *args)

    def call_one(self, loaded: LoadedPlugin, hook: str, *args: Any) -> None:
        if loaded.plugin is None or hook in loaded.broken_hooks:
            return
        handler = getattr(loaded.plugin, hook, None)
        if not callable(handler):
            return
        try:
            handler(*args)
        except Exception as exc:  # noqa: BLE001 — плагин не должен ронять приложение
            self.note_hook_error(loaded, hook, exc)

    def note_hook_error(self, loaded: LoadedPlugin, hook: str, exc: Exception) -> None:
        """Записывает ошибку плагина (используется и при вызове кнопок плагинов)."""
        text = f"{hook}: {type(exc).__name__}: {exc}"
        loaded.errors.append(text)
        print(f"[плагины] {loaded.path.name}: {text}")
        failures = sum(1 for item in loaded.errors if item.startswith(f"{hook}:"))
        if failures >= self.MAX_HOOK_FAILURES:
            loaded.broken_hooks.add(hook)
            print(f"[плагины] {loaded.path.name}: {hook} отключён после {failures} ошибок")

    # ---------- сведения о состоянии ----------
    def summary(self) -> str:
        """Короткая строка для статусной строки/шапки."""
        count = len([p for p in self.plugins if p.plugin is not None])
        if not count:
            return "плагинов нет"
        note = f", с ошибками: {len(self.errors)}" if self.errors else ""
        return f"плагинов: {count}{note}"

    def is_empty(self) -> bool:
        return not any(p.plugin is not None for p in self.plugins)


class MultiVideoSource(VideoSource):
    """
    Объединяет несколько источников: запрос идёт по очереди, берётся первый,
    который дал результат. Так официальный AniLibria и ручные ссылки работают вместе.
    """

    name = "anilibria+manual"
    label = "AniLibria + ваши ссылки"

    def __init__(self, sources: list[VideoSource]) -> None:
        self.sources = sources

    # ---------- описание ----------
    def available_dubbings(self) -> list[str]:
        result: list[str] = []
        for source in self.sources:
            for dubbing in source.available_dubbings():
                if dubbing not in result:
                    result.append(dubbing)
        return result

    def set_anime(self, anime_id: Any, title: str, title_alt: str, year: Optional[int],
                  catalog_id: Optional[int] = None) -> None:
        """Передаёт сведения о выбранном аниме всем источникам."""
        for source in self.sources:
            source.anime_title = title
            source.anime_title_alt = title_alt
            source.anime_year = year
            source.anime_catalog_id = catalog_id

    # ---------- выбор источника ----------
    @staticmethod
    def _note(source: VideoSource) -> str:
        """Пояснение последнего обращения к источнику (у библиотеки его может не быть)."""
        return str(getattr(source, "last_status", "") or "")

    def get_episodes(self, anime_id: int) -> Optional[list[tuple[int, str]]]:
        notes: list[str] = []       # пояснения источников, которые ничего не нашли
        for source in self.sources:
            try:
                episodes = source.get_episodes(anime_id)
            except Exception as exc:
                print(f"[{source.name}] get_episodes: {exc}")
                continue
            if episodes:
                self.last_status = self._note(source)
                return episodes
            note = self._note(source)
            if note:
                notes.append(note)
        # важно вернуть объяснение содержательного источника (например, AniLibria),
        # а не пустой статус библиотеки, которая идёт первой в списке
        self.last_status = notes[-1] if notes else ""
        return None

    def get_stream_url(self, anime_id: int, episode: int, dubbing: str) -> Optional[str]:
        notes: list[str] = []
        for source in self.sources:
            try:
                url = source.get_stream_url(anime_id, episode, dubbing)
            except Exception as exc:
                print(f"[{source.name}] get_stream_url: {exc}")
                continue
            if url:
                self.last_status = self._note(source)
                return url
            note = self._note(source)
            if note:
                notes.append(note)
        self.last_status = notes[-1] if notes else ""
        return None

    def manual(self) -> Optional[ManualLinkSource]:
        """Доступ к источнику ручных ссылок (для кнопки «＋ ссылка»)."""
        return next((s for s in self.sources if isinstance(s, ManualLinkSource)), None)

    def catalog_client(self) -> Optional["AniLibriaClient"]:
        """Клиент AniLibria — нужен интерфейсу для загрузки каталога релизов."""
        source = next((s for s in self.sources if isinstance(s, AniLibriaVideoSource)), None)
        return source.client if source is not None else None

    def library(self) -> Optional[LocalLibrarySource]:
        """Доступ к источнику локальной библиотеки (в интерфейсе сейчас не используется)."""
        return next((s for s in self.sources if isinstance(s, LocalLibrarySource)), None)


# ======================================================================================
# Фоновый поток для сетевых операций (чтобы UI не подвисал)
# ======================================================================================
class Worker(QThread):
    """Запускает произвольную функцию в отдельном потоке и возвращает результат сигналом."""

    result_ready = Signal(object)
    error = Signal(str)

    def __init__(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        super().__init__()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs

    def run(self) -> None:  # выполняется в отдельном потоке
        try:
            result = self._fn(*self._args, **self._kwargs)
        except Exception as exc:  # любая ошибка сети/парсинга — в UI
            self.error.emit(str(exc))
            return
        self.result_ready.emit(result)


class PosterLoader(QThread):
    """
    Параллельно докачивает постеры для уже показанного списка аниме.

    Зачем: список появляется сразу после ответа API (метаданные), а картинки
    прилетают по мере готовности — интерфейс не ждёт 30 последовательных загрузок
    (именно это и давало основную задержку поиска).

    poster_ready(row, bytes) — постер для строки списка готова.
    finished_all()           — все постеры разобраны (успешные и неудачные).
    """

    poster_ready = Signal(int, object)
    finished_all = Signal()

    def __init__(self, client: "ShikimoriClient", items: list[tuple[int, str]], parent=None) -> None:
        super().__init__(parent)
        self._client = client
        self._items = items          # [(номер строки в списке, url постера), ...]
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        from concurrent.futures import ThreadPoolExecutor

        def fetch(task: tuple[int, str]) -> tuple[int, Optional[bytes]]:
            row, url = task
            if self._cancelled:
                return row, None
            return row, self._client._download_poster(url)

        # Вышли из приложения до старта потока — задачи не подписываем вообще:
        # иначе пул попытается принять работу уже во время завершения процесса.
        if self._cancelled:
            self.finished_all.emit()
            return
        try:
            with ThreadPoolExecutor(max_workers=POSTER_WORKERS) as pool:
                try:
                    results = pool.map(fetch, self._items)
                except RuntimeError:
                    results = []      # пул закрывается вместе с приложением
                for row, data in results:
                    if self._cancelled:
                        break
                    if data:
                        self.poster_ready.emit(row, data)
        except Exception as exc:
            import traceback

            print(f"[PosterLoader] {exc}\n{traceback.format_exc()}")
        finally:
            self.finished_all.emit()


# ======================================================================================
# Виджет плеера mpv (встраивание через winId())
# ======================================================================================
class MpvWidget(QWidget):
    """
    QWidget, в который python-mpv рендерит видео.

    Ключевой момент встраивания: self.winId() — это HWND нативного окна Qt,
    его нужно передать в mpv.MPV(wid=...). Для этого у виджета принудительно
    включается атрибут WA_NativeWindow (иначе Qt может не создать HWND).

    Колбэки mpv вызываются в ЕГО собственном потоке, поэтому наружу мы передаём
    данные только через Qt-сигналы (они автоматически ставятся в очередь GUI-потока).
    """

    # сигналы наружу (безопасны между потоками)
    position_changed = Signal(float, float)   # текущая позиция, длительность
    pause_changed = Signal(bool)              # пауза вкл/выкл
    eof_reached = Signal()                    # файл досмотрен до конца
    player_error = Signal(str)                # текст ошибки
    resume_needed = Signal(float)             # нужно выполнить seek (после старта файла)
    file_started = Signal()                   # файл начал грузиться

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)

        # --- обязательно для встраивания mpv ---
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors, True)

        # минимум как у обычного видео: 16:9 и не больше, иначе при маленьком окне
        # область вылезала за рамку плеера
        self.setMinimumSize(320, 180)
        self.setStyleSheet("background-color: #000000;")

        # Заглушка-подсказка (видна только если mpv не запустился)
        self._placeholder = QLabel(
            "Плеер mpv недоступен.\n"
            "Установите mpv и python-mpv:\n"
            "  pip install python-mpv\n"
            "  (mpv.exe должен быть в PATH, напр. C:\\Program Files\\mpv\\mpv.exe)",
            self,
        )
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._placeholder.setStyleSheet("color: #dddddd; background: #101010;")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._placeholder)

        # состояние
        self._player: Any = None
        self._duration: float = 0.0
        self._time_pos: float = 0.0
        self._pending_seek: Optional[float] = None
        self._ca_bundle: Optional[str] = None

        # внутренние соединения: сигнал из потока mpv -> слот в GUI-потоке
        self.resume_needed.connect(self.seek)

        self._init_player()

    # ------------------------------------------------------------------ инициализация
    def _init_player(self) -> None:
        if mpv is None:
            return  # останется заглушка с текстом

        try:
            # winId() создаёт нативное окно и возвращает его хэндл (HWND на Windows)
            wid = str(int(self.winId()))
            options: dict[str, Any] = dict(
                wid=wid,                       # <-- встраивание в этот QWidget
                vo="gpu",                      # аппаратный видеовыход
                hwdec="auto-safe",             # аппаратное декодирование, если возможно
                osc="no",                      # свой OSC не нужен: есть свои контролы
                input_default_bindings=True,
                input_vo_keyboard=True,
                keep_open="yes",               # после конца файла держать последний кадр
                idle="yes",                    # не выходить, когда файл не загружен
                volume=80,                     # начальная громкость
                force_window="no",
            )
            # HTTPS-потоки: mpv нужен CA-бандл, иначе «TLS certificate verification failed»
            ca_bundle = find_ca_bundle()
            if ca_bundle:
                options["tls_ca_file"] = ca_bundle
            if os.environ.get("ANIME_VIEWER_TLS_NO_VERIFY") == "1":
                # крайний случай: сеть с MITM-прокси. Включать только осознанно.
                options["tls_verify"] = "no"
            self._player = mpv.MPV(**options)
            self._ca_bundle = ca_bundle
        except Exception as exc:
            self._player = None
            self._placeholder.setText(f"Не удалось запустить mpv:\n{exc}")
            return

        self._placeholder.hide()  # mpv рисует сам, подсказка больше не нужна

        # наблюдаем за свойствами плеера
        try:
            self._player.observe_property("time-pos", self._on_time_pos)
            self._player.observe_property("duration", self._on_duration)
            self._player.observe_property("pause", self._on_pause)
            self._player.observe_property("eof-reached", self._on_eof)
        except Exception as exc:
            self.player_error.emit(f"Не удалось подписаться на события mpv: {exc}")

    # ------------------------------------------------------------------ колбэки mpv
    # ВНИМАНИЕ: всё это выполняется в потоке mpv -> только сигналы, никаких виджетов!
    def _on_time_pos(self, _name: str, value: Any) -> None:
        self._time_pos = float(value or 0.0)
        self.position_changed.emit(self._time_pos, self._duration)

    def _on_duration(self, _name: str, value: Any) -> None:
        self._duration = float(value or 0.0)
        # Как только mpv узнал длительность — можно прыгнуть на сохранённую позицию.
        if self._pending_seek is not None and self._duration > 0:
            target = self._pending_seek
            self._pending_seek = None
            self.resume_needed.emit(target)
        self.position_changed.emit(self._time_pos, self._duration)

    def _on_pause(self, _name: str, value: Any) -> None:
        self.pause_changed.emit(bool(value))

    def _on_eof(self, _name: str, value: Any) -> None:
        if value:
            self.eof_reached.emit()

    # ------------------------------------------------------------------ мышь
    # Клики приходят прямо в этот QWidget (mpv рисует в его же окно и мышь не
    # перехватывает), поэтому пауза по клику делается штатным обработчиком Qt.
    def mousePressEvent(self, event) -> None:  # noqa: N802 — имя задано Qt
        """Левый клик по картинке видео — пауза / продолжить."""
        if event.button() == Qt.MouseButton.LeftButton and self._player is not None:
            self.toggle_pause()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 — имя задано Qt
        """Двойной клик не должен выключать паузу, поставленную первым нажатием."""
        if event.button() == Qt.MouseButton.LeftButton:
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    # ------------------------------------------------------------------ публичное API
    def is_available(self) -> bool:
        return self._player is not None

    def play_url(self, url: str, start_at: float = 0.0) -> None:
        """Загружает ссылку и начинает воспроизведение с позиции start_at."""
        if self._player is None:
            self.player_error.emit("mpv не инициализирован — воспроизведение невозможно.")
            return
        self._pending_seek = float(start_at) if start_at and start_at > 1.0 else None
        self._duration = 0.0
        self._time_pos = 0.0
        try:
            self._player.play(url)   # python-mpv: установить файл и снять паузу
            self.file_started.emit()
        except Exception as exc:
            self.player_error.emit(f"Ошибка воспроизведения: {exc}")

    def toggle_pause(self) -> None:
        if self._player is None:
            return
        try:
            self._player.pause = not bool(self._player.pause)
        except Exception as exc:
            self.player_error.emit(f"Ошибка паузы: {exc}")

    def set_pause(self, paused: bool) -> None:
        if self._player is None:
            return
        try:
            self._player.pause = bool(paused)
        except Exception as exc:
            self.player_error.emit(f"Ошибка паузы: {exc}")

    def is_paused(self) -> bool:
        if self._player is None:
            return True
        try:
            return bool(self._player.pause)
        except Exception:
            return True

    def seek(self, seconds: float) -> None:
        if self._player is None:
            return
        try:
            self._player.command("seek", float(max(0.0, seconds)), "absolute")
        except Exception as exc:
            self.player_error.emit(f"Ошибка перемотки: {exc}")

    def set_volume(self, volume: int) -> None:
        if self._player is None:
            return
        try:
            self._player.volume = float(volume)
        except Exception as exc:
            self.player_error.emit(f"Ошибка громкости: {exc}")

    def stop(self) -> None:
        if self._player is None:
            return
        try:
            self._player.command("stop")
        except Exception:
            pass

    def current_position(self) -> float:
        return float(self._time_pos or 0.0)

    def current_duration(self) -> float:
        return float(self._duration or 0.0)

    def raw_player(self) -> Any:
        """Доступ к объекту mpv (нужен, например, для списка звуковых дорожек)."""
        return self._player

    def add_subtitle(self, path: str) -> bool:
        """Подключает файл субтитров к текущему видео (mpv sub-add)."""
        if self._player is None:
            return False
        try:
            self._player.command("sub-add", str(path), "select")
            return True
        except Exception as exc:
            self.player_error.emit(f"Не удалось подключить субтитры: {exc}")
            return False

    def subtitle_tracks(self) -> list[dict]:
        """Список подключённых дорожек субтитров."""
        player = self._player
        if player is None:
            return []
        try:
            return [t for t in (player.track_list or []) if t.get("type") == "sub"]
        except Exception:
            return []

    def shutdown(self) -> None:
        if self._player is None:
            return
        try:
            self._player.terminate()
        except Exception:
            pass
        self._player = None


# ======================================================================================
# Читалка манги: загрузчик страниц, полотно и виджет чтения
# ======================================================================================
class MangaPageLoader(QThread):
    """
    Качает страницы главы в фоне, в несколько потоков.

    Очередь пополняет читалка: она просит только видимые страницы и небольшой запас
    вперёд (см. MangaReader._ensure_pages), поэтому трафик не тратится на всю главу.

    page_ready(индекс, байты) — страница готова;
    page_failed(индекс, причина) — не получилось (можно повторить через forget()).
    """

    page_ready = Signal(int, object)
    page_failed = Signal(int, str)
    finished_all = Signal()

    def __init__(self, client: "MangaDexClient", workers: int = MANGA_PAGE_WORKERS, parent=None) -> None:
        super().__init__(parent)
        self._client = client
        self._workers = max(1, int(workers))
        self._queue: "queue.Queue[Optional[tuple[int, str]]]" = queue.Queue()
        self._cancelled = False
        self._pending: set[int] = set()
        self._lock = threading.Lock()

    # ---------- очередь ----------
    def submit(self, index: int, url: str) -> bool:
        """Ставит страницу в очередь. False — эта страница уже качается/скачана."""
        with self._lock:
            if index in self._pending:
                return False
            self._pending.add(index)
        self._queue.put((index, url))
        return True

    def forget(self, index: int) -> None:
        """Снимает пометку «уже в работе» — страницу можно запросить заново."""
        with self._lock:
            self._pending.discard(index)

    def cancel(self) -> None:
        """Просит поток завершиться: будим его «пустыми» задачами."""
        self._cancelled = True
        for _ in range(self._workers + 1):
            self._queue.put(None)

    # ---------- работа в потоке ----------
    def run(self) -> None:
        from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

        try:
            with ThreadPoolExecutor(max_workers=self._workers) as pool:
                futures: dict[Any, int] = {}
                while not self._cancelled:
                    # добираем задачи, пока есть свободные потоки
                    while len(futures) < self._workers and not self._cancelled:
                        try:
                            item = self._queue.get_nowait()
                        except queue.Empty:
                            break
                        if item is None:
                            self._cancelled = True
                            break
                        index, url = item
                        try:
                            futures[pool.submit(self._client.download_page, url)] = index
                        except RuntimeError as exc:
                            # пул уже закрыт (поток останавливают) — выходим тихо,
                            # это не ошибка загрузки
                            print(f"[MangaPageLoader] остановлен: {exc}")
                            self._cancelled = True
                            break
                    if not futures:
                        time.sleep(0.05)
                        continue
                    done, _pending = wait(
                        list(futures), timeout=0.4, return_when=FIRST_COMPLETED
                    )
                    for future in done:
                        index = futures.pop(future)
                        with self._lock:
                            self._pending.discard(index)
                        try:
                            data = future.result()
                        except Exception as exc:  # noqa: BLE001
                            data, reason = None, str(exc)[:120]
                        else:
                            reason = "страница не загрузилась"
                        if self._cancelled:
                            break
                        if data:
                            self.page_ready.emit(index, data)
                        else:
                            self.page_failed.emit(index, reason)
        except Exception as exc:  # noqa: BLE001 — поток не должен падать молча
            import traceback

            print(f"[MangaPageLoader] {exc}\n{traceback.format_exc()}")
        finally:
            self.finished_all.emit()


class MangaStrip(QWidget):
    """
    Полотно читалки: страницы рисуются на одном виджете.

    Так сделано специально: если положить каждую страницу отдельным QLabel,
    глава из 60 страниц занимает сотни мегабайт и прокрутка начинает дёргаться.
    Здесь картинки держатся по индексу, а в paintEvent рисуются только те,
    что попали в видимую область.

    Режимы масштаба (mode): "width" — по ширине окна, "height" — по высоте окна,
    "none" — исходный размер с учётом zoom. В режиме single=True показывается
    одна страница (current), вписанная в окно целиком.
    """

    PLACEHOLDER_RATIO = 1.45   # предполагаемое соотношение сторон, пока размер неизвестен

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.pages: dict[int, QPixmap] = {}
        self.sizes: list[QSize] = []
        self.tops: list[int] = []
        self.gap = int(DEFAULT_MANGA["gap"])
        self.mode = str(DEFAULT_MANGA["fit"])
        self.zoom = 1.0
        self.single = False
        self.current = 0
        self.theme: dict = dict(DEFAULT_THEME)
        self.viewport_size = QSize(800, 600)
        self._total = 0
        self.wallpaper_enabled = bool(DEFAULT_THEME.get("wallpaper", True))
        self._scroll_y = 0      # прокрутка: подложка не должна уезжать со страницами
        # Живой размер окна чтения: полотно спрашивает его в момент пересчёта,
        # иначе после изменения режима или размера окна оно работало бы по старым
        # данным (и страница «по высоте» получалась бы неправильной).
        self.viewport_getter: Optional[Callable[[], QSize]] = None

    def set_scroll_offset(self, value: int) -> None:
        """Запоминает прокрутку области чтения (нужно для фоновой картинки)."""
        if value != self._scroll_y:
            self._scroll_y = int(value)
            self.update()

    def _current_viewport(self) -> QSize:
        if self.viewport_getter is not None:
            try:
                size = self.viewport_getter()
                if isinstance(size, QSize) and size.height() > 0 and size.width() > 0:
                    self.viewport_size = size
            except Exception:  # noqa: BLE001 — читалка могла уже закрываться
                pass
        return self.viewport_size

    # ---------- данные ----------
    def count(self) -> int:
        return len(self.sizes)

    def reset(self, count: int) -> None:
        """Новая глава: count страниц, размеры пока неизвестны (плейсхолдеры)."""
        self.pages.clear()
        self.sizes = [QSize(0, 0) for _ in range(max(0, count))]
        self.current = 0
        self.zoom = 1.0
        self.relayout()

    def set_pixmap(self, index: int, pixmap: QPixmap) -> None:
        self.pages[index] = pixmap
        self.relayout()

    def apply_theme(self, theme: dict) -> None:
        self.theme = dict(theme or DEFAULT_THEME)
        self.wallpaper_enabled = bool(self.theme.get("wallpaper", True))
        self.update()

    def set_gap(self, gap: int) -> None:
        self.gap = max(0, int(gap))
        self.relayout()

    def set_mode(self, mode: str) -> None:
        self.mode = mode if mode in ("width", "height", "none") else "width"
        self.relayout()

    def set_single(self, single: bool) -> None:
        self.single = bool(single)
        self.relayout()

    # ---------- геометрия ----------
    def _natural(self, index: int) -> Optional[QSize]:
        pixmap = self.pages.get(index)
        if pixmap is None or pixmap.isNull():
            return None
        return pixmap.size()

    def _page_size(self, index: int) -> QSize:
        """
        Размер страницы на полотне.

        Лента: "width" — по ширине окна, "height" — по высоте окна, "none" — исходный
        размер с учётом zoom.
        Постранично: тот же смысл, но страница ещё и центрируется; если она больше окна,
        у окна чтения появляются полосы прокрутки (см. relayout).

        Ширина окна берётся именно у окна чтения (viewport), а не у полотна: полотно
        может быть шире окна из-за зума, и тогда в режиме «по ширине» страница
        растянулась бы на всю эту ширину и обратно уже не сузилась.
        """
        viewport = self._current_viewport()
        natural = self._natural(index)
        if natural and natural.width() > 0 and natural.height() > 0:
            natural_w, natural_h = natural.width(), natural.height()
        else:
            # размер страницы ещё неизвестен — рисуем заглушку с типичной пропорцией
            natural_w = 1000
            natural_h = int(round(1000 * self.PLACEHOLDER_RATIO))

        if self.single:
            avail_w = max(160, viewport.width() - 2 * self.gap)
            avail_h = max(160, viewport.height() - 2 * self.gap)
        else:
            avail_w = max(160, viewport.width())
            avail_h = max(160, viewport.height())

        if self.mode == "height":
            height = avail_h
            width = int(round(natural_w * height / natural_h))
        elif self.mode == "none":
            width = int(round(natural_w * self.zoom))
            height = int(round(natural_h * self.zoom))
        else:   # "width" — режим по умолчанию
            width = avail_w
            height = int(round(natural_h * width / natural_w))
        return QSize(max(1, width), max(1, height))

    def relayout(self) -> None:
        """
        Пересчитывает размеры страниц и размеры полотна.

        Полотно всегда не меньше окна чтения (иначе фон «рвался» бы), а если страницы
        шире окна (режим 100% или зум) — расширяется под самую широкую из них, чтобы
        у окна появлялась горизонтальная прокрутка, а не обрезался край страницы.
        """
        viewport = self._current_viewport()
        self.sizes = [self._page_size(index) for index in range(len(self.sizes))]
        tops: list[int] = []
        y = self.gap
        for size in self.sizes:
            tops.append(y)
            y += size.height() + self.gap
        self.tops = tops

        content_w = max((size.width() for size in self.sizes), default=0)
        if self.single:
            # постранично: полотно равно окну, а если страница крупнее — растёт под неё
            content_h = max((size.height() for size in self.sizes), default=0) + 2 * self.gap
            total = max(viewport.height(), content_h, 1)
            content_w += 2 * self.gap
        else:
            total = max(y, viewport.height())

        self._total = total
        if self.height() != total:
            self.setFixedHeight(total)
        minimum_w = content_w if content_w > viewport.width() else 0
        if self.minimumWidth() != minimum_w:
            self.setMinimumWidth(minimum_w)
            # снимаем возможный прежний максимум, иначе ширина не вернётся к окну
            self.setMaximumWidth(16777215)
        self.update()

    def page_top(self, index: int) -> int:
        if not self.tops or index < 0:
            return 0
        if index >= len(self.tops):
            return max(0, self._total - self.viewport_size.height())
        return self.tops[index]

    def page_height(self, index: int) -> int:
        if 0 <= index < len(self.sizes):
            return self.sizes[index].height()
        return max(1, self.viewport_size.height())

    def page_at(self, y: int) -> int:
        """Индекс страницы, которая находится в точке y (по её верхней границе)."""
        if not self.tops:
            return 0
        low, high = 0, len(self.tops) - 1
        while low < high:
            mid = (low + high + 1) // 2
            if self.tops[mid] <= y:
                low = mid
            else:
                high = mid - 1
        return low

    # ---------- отрисовка ----------
    def paintEvent(self, event) -> None:  # noqa: N802 (Qt-стиль)
        from PySide6.QtGui import QColor, QPainter, QPen

        colors = theme_colors(self.theme)
        visible = event.rect()
        painter = QPainter(self)
        painter.fillRect(visible, QColor(colors["player"]))

        # фоновая картинка области чтения: рисуется на месте окна чтения, поэтому
        # при прокрутке остаётся на месте, а не уезжает вместе со страницами
        if self.wallpaper_enabled:
            wallpaper = wallpaper_pixmap(
                "reader_bg",
                QSize(self.width(), max(2, self.viewport_size.height())),
                self.theme,
                "player",
                0.55,
            )
            if wallpaper is not None:
                painter.save()
                painter.setClipRect(visible)
                painter.drawPixmap(0, self._scroll_y, wallpaper)
                painter.restore()

        if not self.sizes:
            self._paint_empty_state(painter, colors)
            painter.end()
            return

        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        if self.single:
            self._paint_single(painter, colors)
        else:
            self._paint_strip(painter, colors, visible)
        painter.end()

    def _paint_empty_state(self, painter, colors: dict) -> None:
        """Пустая читалка: иллюстрация (если есть в assets) и подсказка по центру."""
        from PySide6.QtGui import QColor, QPen

        illustration = load_asset_image("empty_state") if self.wallpaper_enabled else None
        text = (
            "Здесь появится манга.\n"
            "Выберите тайтл слева, затем главу — и страницы начнут загружаться."
        )
        painter.setPen(QPen(QColor(colors["muted"])))

        if illustration is None:
            painter.drawText(self.rect(), int(Qt.AlignmentFlag.AlignCenter), text)
            return

        picture = illustration.scaled(
            QSize(min(140, self.width() // 3), min(190, self.height() // 3)),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        metrics = painter.fontMetrics()
        text_height = metrics.height() * 2 + 10
        block_height = picture.height() + 16 + text_height
        top = max(8, (self.height() - block_height) // 2)
        painter.drawImage((self.width() - picture.width()) // 2, top, picture)
        painter.drawText(
            QRect(12, top + picture.height() + 16, self.width() - 24, text_height),
            int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop),
            text,
        )

    def _paint_single(self, painter, colors: dict) -> None:
        """
        Постраничный режим: одна страница по центру окна чтения.
        Размер берётся из sizes — тот же, что использовался при расчёте полотна,
        поэтому страница никогда не «прыгает» между перерисовками.
        """
        from PySide6.QtGui import QColor, QPen

        index = max(0, min(self.current, len(self.sizes) - 1))
        size = self.sizes[index]
        x = max(0, (self.width() - size.width()) // 2)
        y = max(0, (self.height() - size.height()) // 2)
        pixmap = self.pages.get(index)
        if pixmap is not None and not pixmap.isNull():
            painter.drawPixmap(x, y, size.width(), size.height(), pixmap)
            return
        rect = QRect(x, y, size.width(), size.height())
        painter.setPen(Qt.PenStyle.NoPen)
        painter.fillRect(rect, QColor(colors["card"]))
        painter.setPen(QPen(QColor(colors["muted"])))
        painter.drawText(
            rect, int(Qt.AlignmentFlag.AlignCenter), f"Загрузка страницы {index + 1}…"
        )

    def _paint_strip(self, painter, colors: dict, visible: QRect) -> None:
        """
        Лента: рисуем только страницы, попавшие в видимую область (событие paint
        приходит с прямоугольником именно видимой части — на нём и экономим).
        """
        from PySide6.QtGui import QColor, QPen

        first = self.page_at(max(0, visible.top()))
        bottom = visible.bottom()
        for index in range(first, len(self.sizes)):
            y = self.tops[index]
            if y > bottom:
                break
            size = self.sizes[index]
            x = max(0, (self.width() - size.width()) // 2)
            pixmap = self.pages.get(index)
            if pixmap is not None and not pixmap.isNull():
                painter.drawPixmap(x, y, size.width(), size.height(), pixmap)
            else:
                rect = QRect(x, y, size.width(), size.height())
                painter.setPen(Qt.PenStyle.NoPen)
                painter.fillRect(rect, QColor(colors["card"]))
                painter.setPen(QPen(QColor(colors["muted"])))
                painter.drawText(
                    rect, int(Qt.AlignmentFlag.AlignCenter), f"Загрузка страницы {index + 1}…"
                )
                painter.setPen(QPen(QColor(colors["border"])))

    def sizeHint(self) -> QSize:  # noqa: N802 (Qt-стиль)
        return QSize(self.width(), self._total)


class MangaReader(QWidget):
    """
    Виджет чтения манги: QScrollArea + полотно (MangaStrip).

    Разделение обязанностей: сама читалка ничего не качает и про сеть не знает —
    она сообщает наружу сигналом need_page(индекс, ссылка), а картинки ей отдают
    через page_ready(индекс, байты). Так же устроены постеры в списках аниме
    (см. PosterLoader), поэтому логика загрузки в приложении одинаковая.

    Режимы: лента (непрерывная прокрутка) и постранично (одна страница).
    """

    need_page = Signal(int, str)      # индекс страницы, ссылка — нужно скачать
    page_changed = Signal(int, int)   # текущая страница (с нуля), всего страниц
    next_chapter = Signal()
    prev_chapter = Signal()
    note = Signal(str)                # короткое сообщение в статусную строку

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        from PySide6.QtWidgets import QScrollArea

        self.settings: dict = dict(DEFAULT_MANGA)
        self.urls: list[str] = []
        self._ready: set[int] = set()
        self._requested: set[int] = set()
        self._failed: dict[int, int] = {}
        self._last_index = -1

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        # по горизонтали прокрутка нужна: в режиме 100% и при зуме страница шире окна
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.scroll.setStyleSheet("background: transparent;")
        # прокрутка по пикселям: шаг колеса небольшой, иначе лента «прыгает» страницами
        self.scroll.verticalScrollBar().setSingleStep(60)
        self.scroll.horizontalScrollBar().setSingleStep(60)

        self.strip = MangaStrip(self.scroll)
        self.strip.viewport_getter = lambda: self.scroll.viewport().size()
        self.strip.set_gap(int(self.settings.get("gap", DEFAULT_MANGA["gap"])))
        self.strip.set_mode(str(self.settings.get("fit", DEFAULT_MANGA["fit"])))
        self.strip.set_single(str(self.settings.get("mode", DEFAULT_MANGA["mode"])) == "single")
        self.scroll.setWidget(self.strip)
        layout.addWidget(self.scroll)

        self.scroll.verticalScrollBar().valueChanged.connect(self._on_scrolled)
        # колесо мыши в постраничном режиме перелистывает страницы
        self.scroll.viewport().installEventFilter(self)
        self.scroll.verticalScrollBar().installEventFilter(self)

    # ---------- настройки вида ----------
    def apply_reader_settings(self, settings: dict) -> None:
        """
        Применяет настройки чтения (режим, масштаб, отступ).
        Тема здесь не трогается — её задаёт отдельный apply_theme(theme):
        настройки читалки и палитра оформления — разные вещи.
        """
        self.settings = dict(settings or DEFAULT_MANGA)
        self.strip.set_gap(int(self.settings.get("gap", DEFAULT_MANGA["gap"])))
        self.strip.set_mode(str(self.settings.get("fit", DEFAULT_MANGA["fit"])))
        self.strip.set_single(str(self.settings.get("mode", DEFAULT_MANGA["mode"])) == "single")
        self._sync_current()

    def apply_theme(self, theme: dict) -> None:
        self.strip.apply_theme(theme)
        self.strip.update()

    def set_single(self, single: bool) -> None:
        self.settings["mode"] = "single" if single else "strip"
        self.strip.set_single(single)
        self._sync_current()
        self.goto_page(self.strip.current)

    def set_fit(self, mode: str) -> None:
        self.settings["fit"] = mode
        self.strip.set_mode(mode)
        self._sync_current()
        self.goto_page(self.strip.current)

    def toggle_fit(self) -> str:
        """Переключает «по ширине» -> «по высоте» -> 100% -> «по ширине»."""
        order = [code for code, _label in MANGA_FITS]
        try:
            index = order.index(str(self.settings.get("fit", "width")))
        except ValueError:
            index = 0
        mode = order[(index + 1) % len(order)]
        self.set_fit(mode)
        return dict(MANGA_FITS).get(mode, mode)

    def zoom_by(self, factor: float) -> float:
        """Масштаб в режиме «100%» (в остальных режимах страница вписана в окно)."""
        self.strip.zoom = max(0.25, min(5.0, self.strip.zoom * factor))
        if str(self.settings.get("fit")) != "none":
            self.set_fit("none")
        else:
            self.strip.relayout()
            self.update()
        return self.strip.zoom

    def reset_zoom(self) -> None:
        self.strip.zoom = 1.0
        self.strip.relayout()

    # ---------- данные главы ----------
    def set_chapter(self, urls: list[str]) -> None:
        """Новая глава: список ссылок на страницы, всё остальное сбрасывается."""
        self.urls = list(urls or [])
        self._ready.clear()
        self._requested.clear()
        self._failed.clear()
        self._last_index = -1
        self.strip.reset(len(self.urls))
        self.scroll.verticalScrollBar().setValue(0)
        self.scroll.horizontalScrollBar().setValue(0)
        self.strip.set_scroll_offset(0)
        self._ensure_pages()
        if not self.urls:
            self.note.emit("В этой главе нет доступных страниц.")
        self._last_index = 0
        self.page_changed.emit(0, len(self.urls))

    def clear(self) -> None:
        self.urls = []
        self._ready.clear()
        self._requested.clear()
        self._failed.clear()
        self.strip.reset(0)

    def has_chapter(self) -> bool:
        return bool(self.urls)

    # ---------- приём страниц от загрузчика ----------
    def page_ready(self, index: int, data: object) -> None:
        if not isinstance(data, (bytes, bytearray)) or not (0 <= index < len(self.urls)):
            return
        pixmap = QPixmap()
        if not pixmap.loadFromData(bytes(data)):
            self._on_page_failed(index, "файл не похож на картинку")
            return
        anchor = self.state()          # чтобы страница не «прыгала» под курсором
        self._ready.add(index)
        self._failed.pop(index, None)
        self.strip.set_pixmap(index, pixmap)
        self._restore_anchor(anchor)
        self._ensure_pages()
        self._sync_current()

    def page_failed(self, index: int, reason: str) -> None:
        self._requested.discard(index)
        attempts = self._failed.get(index, 0) + 1
        self._failed[index] = attempts
        if attempts == 1:
            self.note.emit(f"Страница {index + 1} не загрузилась ({reason}) — пробую ещё раз.")
            QTimer.singleShot(1500, lambda i=index: self._retry_page(i))
        else:
            self.note.emit(f"Страница {index + 1} не загрузилась: {reason}")
        self._sync_current()

    def _retry_page(self, index: int) -> None:
        if self._failed.get(index) == 1 and index not in self._ready:
            self._ensure_pages()

    def _on_page_failed(self, index: int, reason: str) -> None:
        self.page_failed(index, reason)

    def _ensure_pages(self) -> None:
        """Просит загрузить видимые страницы и небольшой запас впереди."""
        if not self.urls:
            return
        total = len(self.urls)
        current = max(0, min(self.strip.current, total - 1))
        window = range(max(0, current - 1), min(total, current + 1 + MANGA_PREFETCH))
        for index in window:
            if index in self._ready or index in self._requested:
                continue
            if self._failed.get(index, 0) >= 3:      # три раза не вышло — не долбим сеть
                continue
            self._requested.add(index)
            self.need_page.emit(index, self.urls[index])

    # ---------- навигация ----------
    def current_index(self) -> int:
        return max(0, min(self.strip.current, max(0, len(self.urls) - 1)))

    def state(self) -> tuple[int, float]:
        """Где мы: (номер страницы, доля прокрутки внутри страницы 0..1)."""
        index = self.current_index()
        if self.strip.single or not self.urls:
            return index, 0.0
        top = self.strip.page_top(index)
        height = max(1, self.strip.page_height(index))
        offset = (self.scroll.verticalScrollBar().value() - top) / height
        return index, max(0.0, min(1.0, offset))

    def goto_page(self, index: int, offset: float = 0.0) -> None:
        """Переход на страницу (offset — доля внутри страницы, для продолжения чтения)."""
        if not self.urls:
            return
        index = max(0, min(int(index), len(self.urls) - 1))
        self.strip.current = index
        if self.strip.single:
            self.strip.update()
        else:
            target = self.strip.page_top(index) + int(
                max(0.0, min(1.0, offset)) * self.strip.page_height(index)
            )
            self.scroll.verticalScrollBar().setValue(target)
            # если сдвиг не применился (размеры страницы ещё не пересчитаны) — повторим позже
            QTimer.singleShot(0, lambda t=target: self.scroll.verticalScrollBar().setValue(t))
        self.strip.update()
        self._ensure_pages()
        # прокрутка могла уже сообщить о смене страницы (см. _on_scrolled);
        # второй раз то же событие не отправляем
        if index != self._last_index:
            self._last_index = index
            self.page_changed.emit(index, len(self.urls))

    def goto_next_page(self) -> None:
        self._step_page(1)

    def goto_prev_page(self) -> None:
        self._step_page(-1)

    def _step_page(self, delta: int) -> None:
        if not self.urls:
            return
        index = self.current_index() + delta
        if index < 0:
            if self.current_index() == 0:
                self.prev_chapter.emit()
            return
        if index >= len(self.urls):
            self.next_chapter.emit()
            return
        self.goto_page(index)

    def next_screen(self) -> None:
        """Следующий «экран» в ленте (≈90% высоты окна) или следующая страница."""
        if not self.urls:
            return
        if self.strip.single:
            self._step_page(1)
            return
        bar = self.scroll.verticalScrollBar()
        step = max(120, int(self.strip.viewport_size.height() * 0.9))
        bar.setValue(min(bar.maximum(), bar.value() + step))

    def prev_screen(self) -> None:
        if not self.urls:
            return
        if self.strip.single:
            self._step_page(-1)
            return
        bar = self.scroll.verticalScrollBar()
        step = max(120, int(self.strip.viewport_size.height() * 0.9))
        bar.setValue(max(0, bar.value() - step))

    def scroll_by(self, delta: int) -> None:
        bar = self.scroll.verticalScrollBar()
        bar.setValue(max(0, min(bar.maximum(), bar.value() + delta)))

    def _on_scrolled(self, _value: int) -> None:
        self.strip.set_scroll_offset(self.scroll.verticalScrollBar().value())
        self._sync_current()
        self._ensure_pages()

    def _sync_current(self) -> None:
        """Обновляет «текущую страницу» по положению прокрутки и сообщает наружу."""
        if not self.urls:
            return
        if self.strip.single:
            index = self.current_index()
        else:
            index = self.strip.page_at(self.scroll.verticalScrollBar().value())
            self.strip.current = index
        if index != self._last_index:
            self._last_index = index
            self.strip.update()
            self.page_changed.emit(index, len(self.urls))

    # ---------- события ----------
    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt-стиль)
        """
        При изменении размера окна чтения страницы пересчитываются заново
        (в режиме «по ширине» они растягиваются), а позиция чтения сохраняется.
        """
        super().resizeEvent(event)
        if not self.strip.count():
            return
        anchor = self.state()
        self.strip.viewport_size = self.scroll.viewport().size()
        self.strip.relayout()
        self._restore_anchor(anchor)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 (Qt-стиль)
        """Колесо мыши в постраничном режиме листает страницы."""
        if self.strip.single and event.type() == event.Type.Wheel:
            delta = event.angleDelta().y()
            if delta:
                self._step_page(-1 if delta > 0 else 1)
            return True
        return super().eventFilter(obj, event)

    def _restore_anchor(self, anchor: tuple[int, float]) -> None:
        """Возвращает позицию чтения после пересчёта размеров полотна."""
        if not self.urls:
            return
        index, offset = anchor
        if self.strip.single:
            self.strip.current = min(index, len(self.urls) - 1)
            self.strip.update()
            return
        target = self.strip.page_top(index) + int(
            max(0.0, min(1.0, offset)) * self.strip.page_height(index)
        )
        bar = self.scroll.verticalScrollBar()
        if bar.value() != target:
            bar.setValue(target)


# ======================================================================================
# Оформление интерфейса (тёмная тема) и отрисовка карточек аниме
# ======================================================================================
# Палитра: почти чёрный фон, панели чуть светлее, акцент — фиолетово-синий.
# Готовые палитры: пользователь выбирает режим и акцентный цвет в «Оформлении».
THEME_MODES = {
    # Нейтральная тёмная тема: спокойные серые тона без лишней синевы
    "dark": {
        "title": "Тёмная премиум",
        "bg": "#0d0d0e",              # самый глубокий фон окна
        "panel": "#141415",           # панели (список, плеер)
        "card": "#1d1d1f",            # карточка/кнопка
        "card_hover": "#262629",      # карточка под курсором
        "player": "#050505",          # область видео и страниц
        "text": "#e8e8e9",            # основной текст
        "muted": "#96969b",           # подписи и приглушённый текст
        "border": "#2c2c2f",          # рамки и разделители
        "input": "#18181a",           # поля ввода
        "input_focus": "#1f1f22",     # поле ввода в фокусе
    },
    # Глубокая синяя тема
    "midnight": {
        "title": "Полночь (синяя)",
        "bg": "#080b10", "panel": "#0e1219", "card": "#161b26", "card_hover": "#1d2433",
        "player": "#030406", "text": "#e3e9f3", "muted": "#8a97a9", "border": "#232a38",
        "input": "#111621", "input_focus": "#181e2c",
    },
    # Светлая тема: мягкий серый фон, белые панели; плеер остаётся тёмным
    "light": {
        "title": "Светлая",
        "bg": "#f5f5f7", "panel": "#ffffff", "card": "#f5f5f7", "card_hover": "#ebebf0",
        "player": "#0a0a0b", "text": "#1d1d1f", "muted": "#6e6e73", "border": "#d2d2d7",
        "input": "#ffffff", "input_focus": "#f5f5f7",
    },
    # Контрастная тема — для слабого зрения и плохих экранов
    "contrast": {
        "title": "Контрастная",
        "bg": "#000000", "panel": "#0a0a0a", "card": "#141414", "card_hover": "#202020",
        "player": "#000000", "text": "#ffffff", "muted": "#afafaf", "border": "#3a3a3a",
        "input": "#000000", "input_focus": "#101010",
    },
}
DEFAULT_THEME = {
    "mode": "dark",
    "accent": "#5e5ce6",     # спокойный сине-фиолетовый, не «кислотный»
    "font_scale": 1.0,
    "card_height": 100,
    "show_posters": True,
    "wallpaper": True,       # фоновые картинки из assets (можно выключить)
}

ICONS_DIR = CACHE_DIR / "icons"

# --------------------------------------------------------------------------------------
# Картинки оформления (папка assets, готовятся tools/prepare_assets.py).
# Если файла нет — приложение работает как обычно: фон будет ровным цветом темы,
# вместо заглушки постера — прямоугольник, вместо логотипа — только текст.
# --------------------------------------------------------------------------------------
ASSETS_DIR = BASE_DIR / "assets"
ASSET_FILES = {
    "app_bg": "app_bg.jpg",                  # обои окна
    "player_bg": "player_bg.jpg",            # подложка панели плеера
    "reader_bg": "reader_bg.jpg",            # подложка области чтения
    "poster_placeholder": "poster_placeholder.png",   # заглушка постера
    "logo_dark": "logo_dark.png",            # логотип для тёмных тем
    "logo_light": "logo_light.png",          # логотип для светлой темы
    "empty_state": "empty_state.png",        # иллюстрация пустого состояния
    "tag_icon": "tag_icon.png",              # метка-иконка в карточках
}

_asset_cache: dict[tuple, Any] = {}     # кэш картинок и обоев (ключ — имя, размер, цвет)


def asset_path(name: str) -> Optional[Path]:
    """Путь к картинке оформления или None, если её нет (это не ошибка)."""
    filename = ASSET_FILES.get(name)
    if not filename:
        return None
    path = ASSETS_DIR / filename
    return path if path.is_file() else None


def load_asset_image(name: str) -> Optional[Any]:
    """
    Читает картинку оформления из assets (с кэшем в памяти).

    Кэш обязателен: карточки списка перерисовываются постоянно, и читать файл
    с диска на каждую перерисовку было бы слишком дорого.
    """
    from PySide6.QtGui import QImage

    path = asset_path(name)
    if path is None:
        return None
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return None
    key = ("image", name, stamp)
    if key in _asset_cache:
        return _asset_cache[key]
    image = QImage(str(path))
    if image.isNull():
        return None
    _asset_cache[key] = image
    return image


def load_app_icon() -> Any:
    """
    Иконка приложения: assets/app_icon.ico (её собирает tools/make_icon.py),
    а если её нет — логотип из assets. Без файлов вернётся пустая иконка,
    приложение при этом работает как обычно.
    """
    from PySide6.QtGui import QIcon

    for name in ("app_icon.ico", "logo_dark.png"):
        path = ASSETS_DIR / name
        if path.is_file():
            icon = QIcon(str(path))
            if not icon.isNull():
                return icon
    return QIcon()


def wallpaper_pixmap(
    name: str,
    size: QSize,
    theme: dict,
    base_key: str = "bg",
    opacity: float = 0.5,
) -> Optional[Any]:
    """
    Обои под размер области: картинка растягивается «по покрытию» и смешивается
    с цветом темы (base_key), чтобы интерфейс оставался читаемым.

    Результат кэшируется по (имя, размер, цвет, прозрачность): при перерисовке
    масштабирование не повторяется.
    """
    from PySide6.QtGui import QColor, QPainter, QPixmap

    if size.width() < 2 or size.height() < 2:
        return None
    image = load_asset_image(name)
    if image is None:
        return None

    colors = theme_colors(theme)
    base = colors.get(base_key, colors["bg"])
    key = ("wallpaper", name, size.width(), size.height(), base, round(opacity, 2))
    if key in _asset_cache:
        return _asset_cache[key]

    pixmap = QPixmap(size)
    pixmap.fill(QColor(base))
    scaled = QPixmap.fromImage(image).scaled(
        size,
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation,
    )
    painter = QPainter(pixmap)
    painter.setOpacity(max(0.0, min(1.0, opacity)))
    painter.drawPixmap(
        -(scaled.width() - size.width()) // 2,
        -(scaled.height() - size.height()) // 2,
        scaled,
    )
    painter.end()
    _asset_cache[key] = pixmap
    if len(_asset_cache) > 24:      # кэш небольшой: старые размеры окон не нужны
        for old_key in list(_asset_cache)[:-12]:
            _asset_cache.pop(old_key, None)
    return pixmap


def tinted_asset_icon(name: str, color: str, size: int = 14) -> Optional[Any]:
    """
    Иконка оформления в цвете темы.

    Файлы в assets нарисованы однотонно, поэтому используем их как маску:
    так одна и та же иконка подходит и тёмной, и светлой теме.
    """
    from PySide6.QtGui import QColor, QPainter, QPixmap

    image = load_asset_image(name)
    if image is None:
        return None
    key = ("tinted", name, color, size)
    if key in _asset_cache:
        return _asset_cache[key]

    scaled = image.scaled(
        QSize(size, size),
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    pixmap = QPixmap(scaled.size())
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.drawImage(0, 0, scaled)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
    painter.fillRect(pixmap.rect(), QColor(color))
    painter.end()
    _asset_cache[key] = pixmap
    return pixmap


class WallpaperWidget(QWidget):
    """
    Панель с фоновой картинкой оформления.

    Фон рисуется кодом, а не через QSS: QSS не умеет растягивать картинку под
    размер виджета (только повторять), а панели у нас меняют размер вместе с окном.
    Если картинок нет или фон выключен в «Оформлении» — панель просто заливается
    цветом темы, то есть выглядит как обычно.
    """

    def __init__(
        self,
        wallpaper: str,
        base_key: str = "panel",
        opacity: float = 0.45,
        rounded: bool = True,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.wallpaper = wallpaper
        self.base_key = base_key
        self.opacity = opacity
        self.rounded = rounded
        self.theme: dict = dict(DEFAULT_THEME)
        self.enabled = True

    def apply_theme(self, theme: dict) -> None:
        self.theme = dict(theme or DEFAULT_THEME)
        self.enabled = bool(self.theme.get("wallpaper", True))
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt-стиль)
        from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen

        colors = theme_colors(self.theme)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        path = QPainterPath()
        if self.rounded:
            path.addRoundedRect(0.5, 0.5, self.width() - 1, self.height() - 1, 16, 16)
        else:
            path.addRect(0, 0, self.width(), self.height())

        painter.fillPath(path, QColor(colors.get(self.base_key, colors["panel"])))
        if self.enabled:
            wallpaper = wallpaper_pixmap(
                self.wallpaper, self.size(), self.theme, self.base_key, self.opacity
            )
            if wallpaper is not None:
                painter.save()
                painter.setClipPath(path)
                painter.drawPixmap(0, 0, wallpaper)
                painter.restore()
        if self.rounded:
            painter.setPen(QPen(QColor(colors["border"]), 1.0))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)
        painter.end()


def _relative_luminance(color) -> float:
    """
    Относительная яркость цвета по WCAG (0 — чёрный, 1 — белый).
    Нужна, чтобы правильно выбрать цвет текста поверх акцента: по HSV это не работает
    (насыщенный синий имеет высокое V, но тёмный).
    """
    try:
        red, green, blue, _alpha = color.getRgbF()
    except Exception:  # noqa: BLE001
        return 0.0

    def channel(value: float) -> float:
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4

    return 0.2126 * channel(red) + 0.7152 * channel(green) + 0.0722 * channel(blue)


def _is_light_color(color) -> bool:
    """
    Светлый ли цвет. Порог 0.179 — точка, где контраст с чёрным и белым одинаков,
    поэтому текст поверх акцента получается максимально читаемым.
    """
    try:
        return _relative_luminance(color) > 0.179
    except Exception:  # noqa: BLE001
        return True


def theme_colors(theme: dict) -> dict:
    """
    Палитра темы: цвета палитры + вычисленные оттенки акцента и размеры шрифтов.

    Недостающие ключи добираются из тёмной темы: тему может задать плагин
    с частичной палитрой, и это не должно ломать отрисовку.
    """
    from PySide6.QtGui import QColor

    theme = theme or {}
    mode = str(theme.get("mode", "dark"))
    palette = dict(THEME_MODES["dark"])
    palette.update(THEME_MODES.get(mode) or {})
    palette["mode"] = mode

    accent_text = str(theme.get("accent") or DEFAULT_THEME["accent"])
    accent = QColor(accent_text)
    if not accent.isValid():
        accent = QColor(DEFAULT_THEME["accent"])
    hue, saturation, value, alpha = accent.getHsvF()

    # Оттенки акцента: у светлого акцента ховер темнее, у тёмного — светлее,
    # так кнопка заметно реагирует на курсор и не «выгорает».
    if value > 0.8:
        hover = QColor.fromHsvF(hue, saturation, max(0.0, value * 0.9), alpha)
        press = QColor.fromHsvF(hue, saturation, max(0.0, value * 0.8), alpha)
    else:
        hover = QColor.fromHsvF(hue, saturation, min(1.0, value * 1.15), alpha)
        press = QColor.fromHsvF(hue, saturation, max(0.0, value * 0.95), alpha)

    palette["accent"] = accent.name()
    palette["accent_hover"] = hover.name()
    palette["accent_press"] = press.name()
    # текст поверх акцента — по яркости, а не по HSV
    palette["on_accent"] = "#000000" if _is_light_color(accent) else "#ffffff"
    # очень прозрачный акцент: мягкая подсветка выбранных кнопок и полос
    palette["accent_soft"] = QColor(
        accent.red(), accent.green(), accent.blue(), 28
    ).name(QColor.NameFormat.HexArgb)

    # типографика: всё считается от масштаба шрифта из настроек
    try:
        scale = float(theme.get("font_scale", 1.0) or 1.0)
    except (TypeError, ValueError):
        scale = 1.0
    base = max(11, int(round(13 * scale)))
    palette["font_px"] = base
    palette["font_small"] = max(10, base - 1)
    palette["font_big"] = base + 2
    palette["font_mono"] = max(10, base - 1)
    palette["font_title"] = base + 8
    return palette


def ensure_icons(colors: dict) -> dict:
    """
    Рисует мелкие иконки кодом (стрелка списка, крестик очистки, галочка флажка)
    и возвращает словарь «имя → путь» для таблицы стилей.

    Так интерфейс не зависит ни от эмодзи, ни от внешних файлов: иконки рисуются
    в cache/icons цветами текущей темы. Имя файла содержит тему, иначе после
    переключения темы Qt мог бы показать иконку от прежней палитры.
    """
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath

    paths: dict[str, str] = {}
    try:
        ICONS_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return paths

    mode = str(colors.get("mode", "dark") or "dark")

    def save(name: str, draw, size: int = 16, scale: int = 3) -> str:
        """Рисует иконку в увеличенном виде и сохраняет — края остаются гладкими."""
        image = QImage(QSize(size * scale, size * scale), QImage.Format.Format_ARGB32)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.scale(scale, scale)
        draw(painter, size)
        painter.end()
        path = ICONS_DIR / f"{name}_{mode}.png"
        try:
            image.save(str(path), "PNG")
        except Exception:  # noqa: BLE001
            return ""
        return str(path).replace("\\", "/")

    def chevron(color: str):
        """Стрелка вниз для выпадающих списков."""
        def draw(painter, size: int) -> None:
            pen = painter.pen()
            pen.setColor(QColor(color))
            pen.setWidthF(1.7)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            path = QPainterPath()
            path.moveTo(size * 0.28, size * 0.4)
            path.lineTo(size * 0.5, size * 0.62)
            path.lineTo(size * 0.72, size * 0.4)
            painter.drawPath(path)

        return draw

    def cross(color: str):
        """Крестик для кнопки очистки поля поиска."""
        def draw(painter, size: int) -> None:
            pen = painter.pen()
            pen.setColor(QColor(color))
            pen.setWidthF(1.6)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            pad = size * 0.3
            painter.drawLine(QPointF(pad, pad), QPointF(size - pad, size - pad))
            painter.drawLine(QPointF(size - pad, pad), QPointF(pad, size - pad))

        return draw

    def check(color: str):
        """Галочка для флажков (в QSS нет своей)."""
        def draw(painter, size: int) -> None:
            pen = painter.pen()
            pen.setColor(QColor(color))
            pen.setWidthF(2.2)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            path = QPainterPath()
            path.moveTo(size * 0.22, size * 0.52)
            path.lineTo(size * 0.42, size * 0.72)
            path.lineTo(size * 0.78, size * 0.3)
            painter.drawPath(path)

        return draw

    paths["arrow"] = save("chevron", chevron(colors.get("muted", "#8a8a8a")))
    paths["arrow_hover"] = save("chevron_hover", chevron(colors.get("text", "#ffffff")))
    paths["clear"] = save("clear", cross(colors.get("muted", "#8a8a8a")))
    paths["clear_hover"] = save("clear_hover", cross(colors.get("text", "#ffffff")))
    paths["check"] = save("check", check(colors.get("on_accent", "#ffffff")))
    return paths


def build_qss(theme: dict) -> str:
    """
    Собирает таблицу стилей по выбранной теме: цвета и размеры подставляются
    из theme_colors(), иконки — сгенерированные PNG (см. ensure_icons), чтобы
    оформление не зависело от эмодзи, шрифтов и внешних файлов.
    """
    c = theme_colors(theme)
    icons = ensure_icons(c)
    icon_arrow = icons.get("arrow", "")
    icon_arrow_hover = icons.get("arrow_hover") or icon_arrow
    icon_check = icons.get("check", "")
    icon_clear = icons.get("clear", "")
    icon_clear_hover = icons.get("clear_hover") or icon_clear

    font_family = '"Segoe UI Variable Small", "Segoe UI", "Inter", "Noto Sans", sans-serif'
    font_mono = '"Consolas", "Cascadia Mono", monospace'

    return f"""
/* ---------- глобальные настройки ---------- */
* {{
    font-family: {font_family};
    font-size: {c['font_px']}px;
    color: {c['text']};
}}
QWidget#Root, QMainWindow {{ background-color: {c['bg']}; }}

/* ---------- панели ---------- */
/* фон панелей и окна рисует WallpaperWidget (картинку нельзя растянуть через QSS),
   поэтому здесь только прозрачность — чтобы стиль Qt не закрашивал фон поверх */
QWidget#Root, QWidget#SidePanel, QWidget#PlayerPanel {{ background: transparent; border: none; }}

/* ---------- типографика ---------- */
QLabel#AppTitle {{
    font-size: {c['font_title']}px; font-weight: 700; color: {c['text']}; padding-left: 2px;
}}
QLabel#AppSubtitle {{ font-size: {c['font_small']}px; color: {c['muted']}; }}
QLabel#SectionTitle {{
    font-size: {c['font_small']}px; font-weight: 600; color: {c['muted']};
    letter-spacing: 1.5px; padding-top: 8px; padding-bottom: 2px;
}}
QLabel#Hint {{ font-size: {c['font_small']}px; color: {c['muted']}; }}
QLabel#NowPlaying {{ font-size: {c['font_big']}px; font-weight: 500; color: {c['text']}; }}
QLabel#TimeLabel {{ color: {c['text']}; font-family: {font_mono}; font-size: {c['font_mono']}px; }}

/* ---------- вкладки ---------- */
QTabWidget::pane {{ border: none; background: transparent; }}
QTabBar::tab {{
    background: transparent; color: {c['muted']}; padding: 8px 16px;
    margin-right: 4px; margin-bottom: 4px;
    border: 1px solid transparent; border-radius: 8px; font-weight: 600;
}}
QTabBar::tab:hover {{ color: {c['text']}; background: {c['card']}; }}
QTabBar::tab:selected {{
    background: {c['accent']}; color: {c['on_accent']}; border-color: {c['accent_hover']};
}}
QTabBar::tab:selected:hover {{ background: {c['accent_hover']}; }}

/* ---------- поля ввода ---------- */
QLineEdit {{
    background: {c['input']}; border: 1px solid {c['border']}; border-radius: 10px;
    padding: 9px 12px; color: {c['text']};
    selection-background-color: {c['accent']}; selection-color: {c['on_accent']};
}}
QLineEdit:hover {{ border-color: {c['muted']}; }}
QLineEdit:focus {{
    border: 2px solid {c['accent']}; background: {c['input_focus']};
    padding: 8px 11px;   /* компенсируем более толстую рамку: текст не сдвигается */
}}
QLineEdit#SearchEdit {{ padding-right: 30px; }}
QLineEdit#SearchEdit QToolButton {{
    background: transparent; border: none; margin-right: 4px;
    image: url({icon_clear}); width: 16px; height: 16px;
}}
QLineEdit#SearchEdit QToolButton:hover {{ image: url({icon_clear_hover}); }}

/* многострочные поля (импорт ссылок, отчёт по плагинам) — иначе в тёмной теме
   Qt рисует их светлыми, и светлый текст на светлом фоне не читается */
QPlainTextEdit, QTextEdit {{
    background: {c['input']}; border: 1px solid {c['border']}; border-radius: 10px;
    padding: 8px 10px; color: {c['text']};
    selection-background-color: {c['accent']}; selection-color: {c['on_accent']};
}}
QPlainTextEdit:focus, QTextEdit:focus {{ border: 1px solid {c['accent']}; }}

/* ---------- выпадающие списки ---------- */
QComboBox {{
    background: {c['input']}; border: 1px solid {c['border']}; border-radius: 9px;
    padding: 7px 30px 7px 12px; color: {c['text']};
}}
QComboBox:hover {{ border-color: {c['muted']}; background: {c['card']}; }}
QComboBox:focus {{
    border: 2px solid {c['accent']}; padding: 6px 29px 6px 11px;
}}
QComboBox:disabled {{ color: {c['muted']}; background: {c['panel']}; }}
QComboBox::drop-down {{ subcontrol-origin: padding; subcontrol-position: top right;
    width: 24px; border: none; }}
QComboBox::down-arrow {{ image: url({icon_arrow}); width: 14px; height: 14px; }}
QComboBox::down-arrow:hover {{ image: url({icon_arrow_hover}); }}
QComboBox QAbstractItemView {{
    background: {c['panel']}; border: 1px solid {c['border']}; border-radius: 8px;
    color: {c['text']}; padding: 4px; outline: none;
    selection-background-color: {c['accent']}; selection-color: {c['on_accent']};
}}

/* ---------- кнопки ---------- */
QPushButton {{
    background: {c['card']}; color: {c['text']}; border: 1px solid {c['border']};
    border-radius: 9px; padding: 9px 16px; font-weight: 500;
}}
QPushButton:hover {{ background: {c['card_hover']}; border-color: {c['muted']}; }}
QPushButton:pressed {{ background: {c['panel']}; border-color: {c['border']}; }}
QPushButton:disabled {{ color: {c['muted']}; background: {c['bg']}; border-color: {c['border']}; }}
QPushButton:checked {{
    background: {c['accent_soft']}; border-color: {c['accent']};
    color: {c['accent']}; font-weight: 600;
}}
QPushButton#AccentButton, QPushButton#PlayButton {{
    background: {c['accent']}; border: 1px solid {c['accent_hover']};
    color: {c['on_accent']}; font-weight: 600;
}}
QPushButton#PlayButton {{ font-size: {c['font_big']}px; padding: 10px 24px; border-radius: 12px; }}
QPushButton#AccentButton:hover, QPushButton#PlayButton:hover {{
    background: {c['accent_hover']}; border-color: {c['accent']};
}}
QPushButton#AccentButton:pressed, QPushButton#PlayButton:pressed {{
    background: {c['accent_press']};
}}
QPushButton#GhostButton {{
    background: transparent; border: 1px solid transparent; color: {c['muted']};
    padding: 8px 14px;
}}
QPushButton#GhostButton:hover {{
    color: {c['text']}; background: {c['card']}; border-color: {c['border']};
}}
QPushButton#GhostButton:pressed {{ background: {c['card_hover']}; }}
QPushButton#GhostButton:checked {{ color: {c['accent']}; font-weight: 600; }}
QPushButton#IconButton {{
    padding: 6px; min-width: 36px; min-height: 36px;
    background: transparent; border: none; border-radius: 18px;
}}
QPushButton#IconButton:hover {{ background: {c['card']}; }}
QPushButton#IconButton:pressed {{ background: {c['card_hover']}; }}

/* ---------- выпадающее меню (кнопка «Плагины») ---------- */
QMenu {{
    background: {c['panel']}; border: 1px solid {c['border']};
    border-radius: 10px; padding: 6px;
}}
QMenu::item {{
    padding: 8px 18px 8px 14px; border-radius: 7px; color: {c['text']};
}}
QMenu::item:selected {{ background: {c['accent']}; color: {c['on_accent']}; }}
QMenu::item:disabled {{ color: {c['muted']}; }}
QMenu::separator {{ height: 1px; background: {c['border']}; margin: 6px 8px; }}

/* ---------- списки ---------- */
QListWidget#AnimeList, QListWidget#HistoryList {{
    background: transparent; border: none; outline: none; padding: 4px;
}}

/* ---------- ползунки ---------- */
QSlider::groove:horizontal {{
    height: 6px; background: {c['border']}; border-radius: 3px; margin: 2px 0;
}}
QSlider::sub-page:horizontal {{ background: {c['accent']}; border-radius: 3px; }}
QSlider::handle:horizontal {{
    background: {c['on_accent']}; border: 2px solid {c['accent']};
    width: 12px; height: 12px; margin: -6px 0; border-radius: 8px;
}}
QSlider::handle:horizontal:hover {{ border-color: {c['accent_hover']}; }}
QSlider::handle:horizontal:disabled {{ background: {c['bg']}; border-color: {c['muted']}; }}
/* у громкости ручка меньше: там ползунок короткий */
QSlider#VolumeSlider::handle:horizontal {{
    width: 10px; height: 10px; margin: -5px 0; border-radius: 7px;
}}

/* ---------- полосы прокрутки ---------- */
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{
    background: {c['border']}; border-radius: 5px; min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{ background: {c['muted']}; }}
QScrollBar::handle:vertical:pressed {{ background: {c['accent']}; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{
    background: {c['border']}; border-radius: 5px; min-width: 30px;
}}
QScrollBar::handle:horizontal:hover {{ background: {c['muted']}; }}
QScrollBar::handle:horizontal:pressed {{ background: {c['accent']}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0px; height: 0px; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* ---------- строка состояния и разделители ---------- */
QStatusBar {{ background: transparent; color: {c['muted']}; border-top: 1px solid {c['border']}; }}
QStatusBar::item {{ border: none; padding-left: 8px; }}
QSplitter::handle {{ background: transparent; width: 10px; }}
QFrame#PlayerFrame {{
    background: {c['player']}; border: 2px solid {c['border']}; border-radius: 12px;
}}
QFrame#Separator {{ background: {c['border']}; max-height: 1px; border: none; }}

/* ---------- диалоги ---------- */
QDialog {{ background: {c['bg']}; color: {c['text']}; }}
QGroupBox {{
    font-weight: 600; color: {c['text']}; background: {c['panel']};
    border: 1px solid {c['border']}; border-radius: 12px;
    margin-top: 20px; padding: 16px 12px 12px 12px;
}}
QGroupBox::title {{
    subcontrol-origin: margin; subcontrol-position: top left;
    left: 16px; padding: 0 8px; color: {c['accent']};
}}
QCheckBox {{ color: {c['text']}; spacing: 10px; }}
QCheckBox::indicator {{
    width: 20px; height: 20px; background: {c['input']};
    border: 1px solid {c['border']}; border-radius: 6px;
}}
QCheckBox::indicator:hover {{ border-color: {c['accent']}; background: {c['card']}; }}
QCheckBox::indicator:checked {{
    background: {c['accent']}; border-color: {c['accent_hover']};
    image: url({icon_check});
}}
QCheckBox:disabled {{ color: {c['muted']}; }}
QCheckBox::indicator:disabled {{ background: {c['bg']}; border-color: {c['border']}; }}
QToolTip {{
    background: {c['panel']}; color: {c['text']}; border: 1px solid {c['muted']};
    border-radius: 6px; padding: 6px 10px;
}}
"""


class AnimeCardDelegate(QStyledItemDelegate):
    """
    Рисует строку списка как карточку: постер, название, «год · тип · серии · оценка»,
    полосу прогресса просмотра и подсветку выбора/наведения.
    Цвета и высоту берёт из выбранной темы (см. build_qss / theme_colors).
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.posters: dict[int, QPixmap] = {}
        self.progress: dict[int, float] = {}     # строка -> доля просмотра 0..1
        self.theme: dict = dict(DEFAULT_THEME)
        self.row_height = int(DEFAULT_THEME["card_height"])
        self.show_posters = True
        self._min_height = self.row_height
        self.placeholder: Optional[QPixmap] = None      # заглушка, пока постера нет
        self.tag_icon: Optional[QPixmap] = None         # метка перед подписью

    # ---------- настройки темы ----------
    def apply_theme(self, theme: dict) -> None:
        self.theme = dict(theme or DEFAULT_THEME)
        self.row_height = max(60, int(self.theme.get("card_height", 100)))
        self.show_posters = bool(self.theme.get("show_posters", True))
        # минимальная высота, при которой три строки текста не наезжают друг на друга
        # (важно при крупном шрифте: 1.4× и карточках в 64 px)
        c = theme_colors(self.theme)
        self._min_height = c["font_px"] + 2 * c["font_small"] + 26
        # заглушка постера и метка — картинки оформления в цвете текущей темы
        self.placeholder: Optional[QPixmap] = None
        image = load_asset_image("poster_placeholder")
        if image is not None:
            self.placeholder = QPixmap.fromImage(image)
        self.tag_icon: Optional[QPixmap] = tinted_asset_icon("tag_icon", c["muted"], 14)

    # ---------- размеры ----------
    def sizeHint(self, option, index):  # noqa: N802 (Qt-стиль)
        return QSize(0, max(self.row_height, getattr(self, "_min_height", self.row_height)))

    # ---------- отрисовка ----------
    def paint(self, painter, option, index) -> None:  # noqa: N802 (Qt-стиль)
        """
        Карточка списка: постер, название, подпись, третья строка (для истории)
        и полоса прогресса. Выбранная строка получает акцентную рамку и полосу слева,
        наведение — подсветку фона.
        """
        from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen

        c = theme_colors(self.theme)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        card = option.rect.adjusted(4, 4, -4, -4)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)

        # 1) фон и состояние
        path = QPainterPath()
        path.addRoundedRect(card.x(), card.y(), card.width(), card.height(), 12.0, 12.0)
        if selected:
            painter.fillPath(path, QColor(c["card_hover"]))
            painter.setPen(QPen(QColor(c["accent"]), 1.5))
            painter.drawPath(path)
            bar = QPainterPath()
            bar.addRoundedRect(card.x() + 1, card.y() + 14, 4, card.height() - 28, 2.0, 2.0)
            painter.fillPath(bar, QColor(c["accent"]))
        elif hovered:
            painter.fillPath(path, QColor(c["card_hover"]))
            painter.setPen(QPen(QColor(c["border"]), 1.0))
            painter.drawPath(path)
        else:
            painter.fillPath(path, QColor(c["card"]))

        # 2) постер: ширина считается от высоты карточки, поэтому карточка
        #    выглядит одинаково и на 64 px, и на 150 px
        text_left = card.x() + 14
        if self.show_posters:
            poster_h = max(32, card.height() - 20)
            poster_w = max(44, int(round(poster_h * 0.7)))
            poster = QRect(card.x() + 10, card.y() + 10, poster_w, poster_h)
            clip = QPainterPath()
            clip.addRoundedRect(poster.x(), poster.y(), poster.width(), poster.height(), 8.0, 8.0)
            pixmap = self.posters.get(index.row())
            painter.save()
            painter.setClipPath(clip)
            if pixmap is not None and not pixmap.isNull():
                scaled = pixmap.scaled(
                    poster.size(),
                    Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation,
                )
                painter.drawPixmap(
                    poster.x() - (scaled.width() - poster.width()) // 2,
                    poster.y() - (scaled.height() - poster.height()) // 2,
                    scaled,
                )
            elif self.placeholder is not None:
                # заглушка из assets: пока постер не догрузился, карточка выглядит
                # оформленной, а не серым прямоугольником
                scaled = self.placeholder.scaled(
                    poster.size(),
                    Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation,
                )
                painter.drawPixmap(
                    poster.x() - (scaled.width() - poster.width()) // 2,
                    poster.y() - (scaled.height() - poster.height()) // 2,
                    scaled,
                )
            else:
                painter.fillRect(poster, QColor(c["border"]))
            painter.restore()
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(c["border"]), 1.0))
            painter.drawPath(clip)
            text_left = poster.right() + 14

        # 3) текст: три строки, по центру по вертикали
        title = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        meta = str(index.data(Qt.ItemDataRole.UserRole + 1) or "")
        extra = str(index.data(Qt.ItemDataRole.UserRole + 2) or "")
        share = float(self.progress.get(index.row(), 0.0) or 0.0)
        width = max(40, card.right() - 14 - text_left)

        title_font = QFont(painter.font())
        title_font.setBold(True)
        title_font.setPixelSize(c["font_px"])
        painter.setFont(title_font)
        title_metrics = painter.fontMetrics()
        title_h = title_metrics.height()

        small_font = QFont(painter.font())
        small_font.setBold(False)
        small_font.setPixelSize(c["font_small"])
        painter.setFont(small_font)
        small_metrics = painter.fontMetrics()
        small_h = small_metrics.height()

        has_extra = bool(extra)
        block_h = title_h + 4 + small_h + (small_h + 2 if has_extra else 0)
        top = card.y() + max(8, (card.height() - block_h) // 2)
        # если карточка низкая (масштаб шрифта большой) — третью строку не рисуем,
        # чтобы текст не наезжал на полосу прогресса
        if has_extra and top + block_h > card.bottom() - (18 if share > 0 else 8):
            has_extra = False
            block_h = title_h + 4 + small_h
            top = card.y() + max(8, (card.height() - block_h) // 2)

        painter.setFont(title_font)
        painter.setPen(QColor(c["accent_hover"] if selected else c["text"]))
        painter.drawText(
            text_left, top + title_metrics.ascent(),
            title_metrics.elidedText(title, Qt.TextElideMode.ElideRight, width),
        )

        painter.setFont(small_font)
        painter.setPen(QColor(c["muted"]))
        meta_y = top + title_h + 4
        meta_left = text_left
        if self.tag_icon is not None:
            # маленькая метка перед подписью (год · тип · серии): рисуется кодом,
            # поэтому подходит любой теме
            painter.drawPixmap(
                text_left,
                meta_y + (small_h - self.tag_icon.height()) // 2,
                self.tag_icon,
            )
            meta_left = text_left + self.tag_icon.width() + 6
        meta_width = max(20, card.right() - 14 - meta_left)
        painter.drawText(
            meta_left, meta_y + small_metrics.ascent(),
            small_metrics.elidedText(meta, Qt.TextElideMode.ElideRight, meta_width),
        )
        if has_extra:
            painter.drawText(
                text_left, meta_y + small_h + 2 + small_metrics.ascent(),
                small_metrics.elidedText(extra, Qt.TextElideMode.ElideRight, width),
            )

        # 4) полоса прогресса
        if share > 0:
            bar = QRect(card.x() + 16, card.bottom() - 11, max(40, card.width() - 32), 5)
            bar_path = QPainterPath()
            bar_path.addRoundedRect(bar.x(), bar.y(), bar.width(), bar.height(), 2.5, 2.5)
            painter.fillPath(bar_path, QColor(c["border"]))
            done = QPainterPath()
            done.addRoundedRect(
                bar.x(), bar.y(), int(bar.width() * min(share, 1.0)), bar.height(), 2.5, 2.5
            )
            painter.fillPath(done, QColor(c["accent"]))

        painter.restore()


# ======================================================================================
# История просмотров
# ======================================================================================
class HistoryStore:
    """
    История просмотров (history.json рядом с программой).

    Формат:
        {"items": [
            {"anime": {...снимок аниме из поиска...}, "anime_id": 11757,
             "title": "Мастера меча онлайн", "episode": 2, "dubbing": "AniLibria",
             "position": 512.4, "duration": 1423.0, "updated": 1712345678.9}
        ]}

    Хранится сам снимок аниме, поэтому запись можно продолжить даже без нового поиска.
    """

    LIMIT = 200   # сколько последних записей держим

    def __init__(self, path: Path = HISTORY_FILE) -> None:
        self.path = Path(path)
        self.items: list[dict] = []
        self.load()

    def load(self) -> None:
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding="utf-8"))
                items = data.get("items") if isinstance(data, dict) else data
                if isinstance(items, list):
                    self.items = [it for it in items if isinstance(it, dict)]
        except (OSError, ValueError):
            self.items = []

    def save(self) -> None:
        try:
            self.path.write_text(
                json.dumps({"items": self.items[: self.LIMIT]}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass

    # ---------- операции ----------
    @staticmethod
    def _same(entry: dict, anime_id: Any, episode: Any, dubbing: str) -> bool:
        return (
            str(entry.get("anime_id")) == str(anime_id)
            and int(entry.get("episode") or 0) == int(episode)
            and entry.get("dubbing") == dubbing
        )

    def touch(
        self,
        anime: dict,
        episode: int,
        dubbing: str,
        position: float = 0.0,
        duration: float = 0.0,
    ) -> None:
        """Добавляет или обновляет запись и поднимает её наверх."""
        anime_id = anime.get("id")
        entry = next(
            (e for e in self.items if self._same(e, anime_id, episode, dubbing)), None
        )
        if entry is None:
            entry = {
                "anime": {
                    "id": anime_id,
                    "russian": anime.get("russian"),
                    "title": anime.get("title"),
                    "year": anime.get("year"),
                    "kind": anime.get("kind"),
                    "episodes": anime.get("episodes"),
                    "episodes_aired": anime.get("episodes_aired"),
                    "poster_url": anime.get("poster_url"),
                    # id релиза AniLibria — чтобы продолжать просмотр из истории
                    # без повторного поиска по названию
                    "anilibria_id": anime.get("anilibria_id"),
                },
                "anime_id": anime_id,
                "title": anime.get("russian") or anime.get("title") or "Без названия",
                "episode": int(episode),
                "dubbing": dubbing,
            }
            self.items.insert(0, entry)
        else:
            self.items.remove(entry)
            self.items.insert(0, entry)

        if position:
            entry["position"] = round(float(position), 1)
        if duration:
            entry["duration"] = round(float(duration), 1)
        entry["updated"] = time.time()
        del self.items[self.LIMIT:]
        self.save()

    def remove(self, index: int) -> None:
        if 0 <= index < len(self.items):
            del self.items[index]
            self.save()

    def position_for(self, anime_id: Any, episode: Any, dubbing: str) -> float:
        """Сохранённая в истории позиция — чтобы продолжать даже без progress.json."""
        for entry in self.items:
            if self._same(entry, anime_id, episode, dubbing):
                return float(entry.get("position") or 0.0)
        return 0.0

    def clear(self) -> None:
        self.items = []
        self.save()


def time_ago(timestamp: float) -> str:
    """«5 минут назад», «2 часа назад», «вчера», «3 дня назад»."""
    if not timestamp:
        return ""
    delta = max(0, time.time() - float(timestamp))
    minutes = int(delta // 60)
    if minutes < 1:
        return "только что"
    if minutes < 60:
        return f"{minutes} мин назад"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} ч назад"
    days = hours // 24
    if days == 1:
        return "вчера"
    if days < 30:
        return f"{days} дн назад"
    return time.strftime("%d.%m.%Y", time.localtime(timestamp))


# ======================================================================================
# Диалог настройки интерфейса (тема, цвет, размеры)
# ======================================================================================
class SettingsDialog(QDialog):
    """
    Кастомизация интерфейса: режим темы, акцентный цвет, масштаб шрифта,
    высота карточек, показ постеров, автопереход к следующей серии, громкость.
    Изменения применяются сразу (живое превью) через callback on_change.
    """

    def __init__(self, theme: dict, player: dict, on_change, parent=None, manga: Optional[dict] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Настройки интерфейса")
        self.setMinimumWidth(460)
        self._theme = dict(theme)
        self._player = dict(player)
        self._manga = dict(manga or DEFAULT_MANGA)
        self._on_change = on_change

        from PySide6.QtWidgets import QCheckBox, QDialogButtonBox, QFormLayout, QGroupBox

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # ---- тема ----
        theme_box = QGroupBox("Оформление")
        form = QFormLayout(theme_box)

        self.mode_combo = QComboBox()
        for key, palette in THEME_MODES.items():
            self.mode_combo.addItem(palette["title"], key)
        idx = self.mode_combo.findData(self._theme.get("mode", "dark"))
        self.mode_combo.setCurrentIndex(max(0, idx))
        self.mode_combo.currentIndexChanged.connect(self._changed)
        form.addRow("Режим:", self.mode_combo)

        self.color_button = QPushButton(self._theme.get("accent", DEFAULT_THEME["accent"]))
        self.color_button.clicked.connect(self._pick_color)
        self._update_color_button()
        form.addRow("Акцентный цвет:", self.color_button)

        self.font_slider = QSlider(Qt.Orientation.Horizontal)
        self.font_slider.setRange(85, 140)
        self.font_slider.setValue(int(float(self._theme.get("font_scale", 1.0)) * 100))
        self.font_slider.valueChanged.connect(self._changed)
        self.font_slider_label = QLabel()
        form.addRow("Масштаб шрифта:", self.font_slider)
        form.addRow("", self.font_slider_label)

        self.height_slider = QSlider(Qt.Orientation.Horizontal)
        self.height_slider.setRange(64, 150)
        self.height_slider.setValue(int(self._theme.get("card_height", 96)))
        self.height_slider.valueChanged.connect(self._changed)
        self.height_label = QLabel()
        form.addRow("Высота карточек:", self.height_slider)
        form.addRow("", self.height_label)

        self.posters_check = QCheckBox("Показывать постеры в списках")
        self.posters_check.setChecked(bool(self._theme.get("show_posters", True)))
        self.posters_check.toggled.connect(self._changed)
        form.addRow(self.posters_check)

        self.wallpaper_check = QCheckBox("Фоновые картинки (папка assets)")
        self.wallpaper_check.setToolTip(
            "Обои окна и панелей, подложка области чтения и иллюстрация пустого экрана.\n"
            "Выключите, если нужен ровный цвет темы."
        )
        self.wallpaper_check.setChecked(bool(self._theme.get("wallpaper", True)))
        self.wallpaper_check.toggled.connect(self._changed)
        form.addRow(self.wallpaper_check)
        layout.addWidget(theme_box)

        # ---- плеер ----
        player_box = QGroupBox("Плеер")
        form2 = QFormLayout(player_box)
        self.auto_next_check = QCheckBox("Автоматически включать следующую серию")
        self.auto_next_check.setChecked(bool(self._player.get("auto_next", True)))
        self.auto_next_check.toggled.connect(self._changed)
        form2.addRow(self.auto_next_check)

        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(int(self._player.get("volume", 80)))
        self.volume_slider.valueChanged.connect(self._changed)
        self.volume_label = QLabel()
        form2.addRow("Громкость:", self.volume_slider)
        form2.addRow("", self.volume_label)
        layout.addWidget(player_box)

        # ---- читалка манги ----
        manga_box = QGroupBox("Читалка манги")
        form3 = QFormLayout(manga_box)

        self.manga_mode_combo = QComboBox()
        for code, title in MANGA_MODES:
            self.manga_mode_combo.addItem(title, code)
        index = self.manga_mode_combo.findData(str(self._manga.get("mode", "strip")))
        self.manga_mode_combo.setCurrentIndex(max(0, index))
        self.manga_mode_combo.currentIndexChanged.connect(self._changed)
        form3.addRow("Режим:", self.manga_mode_combo)

        self.manga_fit_combo = QComboBox()
        for code, title in MANGA_FITS:
            self.manga_fit_combo.addItem(title, code)
        index = self.manga_fit_combo.findData(str(self._manga.get("fit", "width")))
        self.manga_fit_combo.setCurrentIndex(max(0, index))
        self.manga_fit_combo.currentIndexChanged.connect(self._changed)
        form3.addRow("Масштаб страниц:", self.manga_fit_combo)

        self.manga_gap_slider = QSlider(Qt.Orientation.Horizontal)
        self.manga_gap_slider.setRange(0, 40)
        self.manga_gap_slider.setValue(int(self._manga.get("gap", DEFAULT_MANGA["gap"])))
        self.manga_gap_slider.valueChanged.connect(self._changed)
        self.manga_gap_label = QLabel()
        form3.addRow("Отступ между страницами:", self.manga_gap_slider)
        form3.addRow("", self.manga_gap_label)

        self.manga_fallback_check = QCheckBox("Добирать главы на английском, если нет на русском")
        self.manga_fallback_check.setChecked(bool(self._manga.get("fallback_en", True)))
        self.manga_fallback_check.toggled.connect(self._changed)
        form3.addRow(self.manga_fallback_check)

        self.manga_cache_check = QCheckBox("Хранить страницы в кэше на диске")
        self.manga_cache_check.setToolTip(
            "Повторное чтение уже открытых глав идёт с диска — мгновенно и без трафика.\n"
            "Кэш чистится автоматически (cache\\manga)."
        )
        self.manga_cache_check.setChecked(bool(self._manga.get("disk_cache", True)))
        self.manga_cache_check.toggled.connect(self._changed)
        form3.addRow(self.manga_cache_check)
        layout.addWidget(manga_box)

        # ---- кнопки ----
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.RestoreDefaults
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.RestoreDefaults).clicked.connect(self._reset)
        layout.addWidget(buttons)

        self._labels()

    # ---------- вспомогательное ----------
    def _labels(self) -> None:
        self.font_slider_label.setText(f"{self.font_slider.value() / 100:.2f}×")
        self.height_label.setText(f"{self.height_slider.value()} px")
        self.volume_label.setText(f"{self.volume_slider.value()}%")
        self.manga_gap_label.setText(f"{self.manga_gap_slider.value()} px")

    def _update_color_button(self) -> None:
        color = self._theme.get("accent", DEFAULT_THEME["accent"])
        self.color_button.setText(color)
        self.color_button.setStyleSheet(
            f"background: {color}; color: #ffffff; font-weight: 600; padding: 7px 14px;"
        )

    def _pick_color(self) -> None:
        from PySide6.QtGui import QColor

        current = QColor(self._theme.get("accent", DEFAULT_THEME["accent"]))
        chosen = QColorDialog.getColor(current, self, "Акцентный цвет")
        if chosen.isValid():
            self._theme["accent"] = chosen.name()
            self._update_color_button()
            self._changed()

    def _collect(self) -> None:
        self._theme["mode"] = self.mode_combo.currentData() or "dark"
        self._theme["font_scale"] = self.font_slider.value() / 100
        self._theme["card_height"] = self.height_slider.value()
        self._theme["show_posters"] = self.posters_check.isChecked()
        self._theme["wallpaper"] = self.wallpaper_check.isChecked()
        self._player["auto_next"] = self.auto_next_check.isChecked()
        self._player["volume"] = self.volume_slider.value()
        self._manga["mode"] = self.manga_mode_combo.currentData() or "strip"
        self._manga["fit"] = self.manga_fit_combo.currentData() or "width"
        self._manga["gap"] = self.manga_gap_slider.value()
        self._manga["fallback_en"] = self.manga_fallback_check.isChecked()
        self._manga["disk_cache"] = self.manga_cache_check.isChecked()

    def _changed(self) -> None:
        self._labels()
        self._collect()
        self._on_change(dict(self._theme), dict(self._player), dict(self._manga))

    def _reset(self) -> None:
        self._theme = dict(DEFAULT_THEME)
        self._player.update({"auto_next": True, "volume": 80})
        self._manga = dict(DEFAULT_MANGA)
        self.mode_combo.setCurrentIndex(max(0, self.mode_combo.findData("dark")))
        self.font_slider.setValue(int(DEFAULT_THEME["font_scale"] * 100))
        self.height_slider.setValue(DEFAULT_THEME["card_height"])
        self.posters_check.setChecked(DEFAULT_THEME["show_posters"])
        self.wallpaper_check.setChecked(DEFAULT_THEME["wallpaper"])
        self.auto_next_check.setChecked(True)
        self.volume_slider.setValue(80)
        self.manga_mode_combo.setCurrentIndex(
            max(0, self.manga_mode_combo.findData(DEFAULT_MANGA["mode"]))
        )
        self.manga_fit_combo.setCurrentIndex(
            max(0, self.manga_fit_combo.findData(DEFAULT_MANGA["fit"]))
        )
        self.manga_gap_slider.setValue(DEFAULT_MANGA["gap"])
        self.manga_fallback_check.setChecked(DEFAULT_MANGA["fallback_en"])
        self.manga_cache_check.setChecked(DEFAULT_MANGA["disk_cache"])
        self._update_color_button()
        self._changed()

    def result_values(self) -> tuple[dict, dict, dict]:
        self._collect()
        return self._theme, self._player, self._manga


# ======================================================================================
# Импорт ссылок пачкой (включает сразу целые озвучки)
# ======================================================================================
class LinkImportDialog(QDialog):
    """
    Позволяет за один раз описать несколько озвучек для выбранного аниме.
    Каждая строка: «Озвучка | ссылка-шаблон», где {episode} заменяется номером серии.

    Пример:
        AniDUB | https://example.com/sao/{episode}.m3u8
        JAM CLUB | https://example.com/other/sao-{episode}.mp4
    Если в ссылке нет {episode}, она считается ссылкой на текущую серию.
    """

    def __init__(self, title: str, parent=None) -> None:
        super().__init__(parent)
        from PySide6.QtWidgets import QDialogButtonBox, QPlainTextEdit

        self.setWindowTitle("Импорт ссылок")
        self.setMinimumSize(620, 380)
        layout = QVBoxLayout(self)
        hint = QLabel(
            f"Аниме: {title}\n\n"
            "Формат строки:  Озвучка | ссылка\n"
            "В ссылке можно писать {episode} — тогда она подойдёт для всех серий."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText(
            "AniDUB | https://example.com/sao/{episode}.m3u8\n"
            "JAM CLUB | https://example.com/other/{episode}.mp4"
        )
        layout.addWidget(self.editor, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def parse(self) -> list[tuple[str, str]]:
        """[(озвучка, ссылка), ...] — строки без разделителя игнорируются."""
        result: list[tuple[str, str]] = []
        for line in self.editor.toPlainText().splitlines():
            line = line.strip()
            if not line or "|" not in line:
                continue
            dubbing, url = (part.strip() for part in line.split("|", 1))
            if dubbing and url.startswith(("http://", "https://")):
                result.append((dubbing, url))
        return result


# ======================================================================================
# Список «Читаю» — манга с сохранённым прогрессом чтения
# ======================================================================================
class MangaReadingDialog(QDialog):
    """
    Список манги, которую вы начали читать: тайтл, глава, страница и когда читали.

    «Продолжить» возвращает выбранную запись (см. selected_entry) — приложение
    открывает ту же главу и страницу, что и в прошлый раз.
    """

    def __init__(self, store: "MangaProgressStore", parent=None, theme: Optional[dict] = None) -> None:
        super().__init__(parent)
        from PySide6.QtWidgets import QDialogButtonBox

        self.setWindowTitle("Читаю — прогресс чтения манги")
        self.setMinimumSize(640, 460)
        self._store = store
        self._rows: list[dict] = store.listed()
        self.selected_entry: Optional[dict] = None

        layout = QVBoxLayout(self)
        hint = QLabel(
            "Двойной клик или «Продолжить» — вернуться к чтению с той же страницы.\n"
            "«Забыть» убирает запись вместе с сохранённой главой и страницей."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.list = QListWidget()
        self.list.setObjectName("HistoryList")
        self.list.setMouseTracking(True)
        self.list.setUniformItemSizes(True)
        self.list.setFrameShape(QFrame.Shape.NoFrame)
        self._delegate = AnimeCardDelegate(self.list)
        self._delegate.apply_theme(theme or DEFAULT_THEME)   # окно в оформлении приложения
        self.list.setItemDelegate(self._delegate)
        layout.addWidget(self.list, 1)

        self._fill()

        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        self.continue_button = QPushButton("Продолжить")
        self.forget_button = QPushButton("Забыть")
        self.clear_button = QPushButton("Очистить всё")
        for button in (self.continue_button, self.forget_button, self.clear_button):
            buttons.addWidget(button)
        layout.addLayout(buttons)

        close_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_box.rejected.connect(self.reject)
        layout.addWidget(close_box)

        self.continue_button.clicked.connect(self._accept_selected)
        self.list.itemDoubleClicked.connect(lambda _item: self._accept_selected())
        self.forget_button.clicked.connect(self._forget_selected)
        self.clear_button.clicked.connect(self._clear_all)

    # ---------- наполнение ----------
    def _fill(self) -> None:
        self.list.clear()
        self._delegate.posters.clear()
        self._delegate.progress.clear()
        for row, entry in enumerate(self._rows):
            manga = entry.get("manga") or {}
            title = manga.get("russian") or manga.get("title") or "Без названия"
            parts = [f"глава {entry.get('chapter') or '?'}"]
            if entry.get("lang"):
                parts.append(dict(MANGA_LANGS).get(entry["lang"], entry["lang"].upper()))
            parts.append(f"стр. {int(entry.get('page') or 0) + 1}")
            item = QListWidgetItem(title)
            item.setData(Qt.ItemDataRole.UserRole + 1, "  ·  ".join(parts))
            item.setData(Qt.ItemDataRole.UserRole + 2, time_ago(entry.get("updated") or 0))
            self.list.addItem(item)
        if self._rows:
            self.list.setCurrentRow(0)

    def _current(self) -> Optional[dict]:
        row = self.list.currentRow()
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    # ---------- действия ----------
    def _accept_selected(self) -> None:
        entry = self._current()
        if entry is None:
            return
        self.selected_entry = entry
        self.accept()

    def _forget_selected(self) -> None:
        entry = self._current()
        if entry is None:
            return
        manga = entry.get("manga") or {}
        self._store.remove(manga.get("id"), manga.get("source"))
        self._rows = self._store.listed()
        self._fill()

    def _clear_all(self) -> None:
        self._store.clear()
        self._rows = []
        self._fill()


# ======================================================================================
# Диалог «Плагины»: что подключено и что сломалось
# ======================================================================================
class PluginsDialog(QDialog):
    """
    Показывает, какие плагины подключены, что они добавили и где лежат.

    Отдельно выводятся ошибки и предупреждения: плагин, который не загрузился,
    виден здесь целиком, с текстом ошибки — искать причину в консоли не нужно.
    """

    def __init__(self, manager: "PluginManager", parent=None) -> None:
        super().__init__(parent)
        from PySide6.QtWidgets import QDialogButtonBox

        self.setWindowTitle("Плагины")
        self.setMinimumSize(660, 520)
        self.manager = manager

        layout = QVBoxLayout(self)
        hint = QLabel(
            f"Плагины лежат в папке: {PLUGINS_DIR}\n"
            "Файл .py с классом-наследником Plugin подключается автоматически при запуске.\n"
            f"Версия приложения: {APP_VERSION} · API плагинов: {PLUGIN_API_VERSION}"
        )
        hint.setObjectName("Hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.list = QListWidget()
        self.list.setObjectName("AnimeList")
        self.list.setUniformItemSizes(False)
        self.list.setFrameShape(QFrame.Shape.NoFrame)
        self._delegate = AnimeCardDelegate(self.list)
        self._delegate.apply_theme(getattr(parent, "theme", None) or DEFAULT_THEME)
        self.list.setItemDelegate(self._delegate)
        layout.addWidget(self.list, 3)

        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setPlaceholderText("Ошибки и замечания по плагинам появятся здесь.")
        layout.addWidget(self.details, 2)

        buttons = QHBoxLayout()
        self.folder_button = QPushButton("Открыть папку плагинов")
        self.folder_button.setObjectName("GhostButton")
        self.copy_button = QPushButton("Скопировать отчёт")
        self.copy_button.setObjectName("GhostButton")
        buttons.addWidget(self.folder_button)
        buttons.addWidget(self.copy_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        close_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_box.rejected.connect(self.reject)
        layout.addWidget(close_box)

        self.folder_button.clicked.connect(self._open_folder)
        self.copy_button.clicked.connect(self._copy_report)
        self.list.currentRowChanged.connect(self._show_error_for_row)
        self._fill()

    # ---------- наполнение ----------
    def _fill(self) -> None:
        self.list.clear()
        self._delegate.posters.clear()
        rows: list[str] = []
        for loaded in self.manager.plugins:
            plugin = loaded.plugin
            if plugin is None:
                title = f"✕ {loaded.path.name}"
                meta = "не загрузился"
                extra = loaded.error
            else:
                title = plugin.title or plugin.name
                parts = [f"v{plugin.version}"]
                if plugin.author:
                    parts.append(plugin.author)
                parts.append(plugin.name)
                added = []
                if plugin.video_sources():
                    added.append("источники видео")
                if plugin.manga_sources():
                    added.append("источники манги")
                if plugin.themes():
                    added.append("темы")
                if plugin.actions():
                    added.append("кнопки")
                if added:
                    parts.append("добавляет: " + ", ".join(added))
                meta = "  ·  ".join(parts)
                extra = plugin.description or ""
                if loaded.errors:
                    extra = f"{extra}  (ошибок при работе: {len(loaded.errors)})"
            item = QListWidgetItem(title)
            item.setData(Qt.ItemDataRole.UserRole + 1, meta)
            item.setData(Qt.ItemDataRole.UserRole + 2, f"{loaded.path.name}   {extra}")
            self.list.addItem(item)
            rows.append(f"{title} — {loaded.path.name}")

        if not rows:
            item = QListWidgetItem("Плагинов нет")
            item.setData(
                Qt.ItemDataRole.UserRole + 1,
                "положите файл .py в папку plugins — он подхватится при следующем запуске",
            )
            item.setData(Qt.ItemDataRole.UserRole + 2, str(PLUGINS_DIR))
            self.list.addItem(item)
        self.details.setPlainText(self.report())

    def report(self) -> str:
        """Текстовый отчёт: плагины, предупреждения, ошибки загрузки и работы."""
        lines = [f"Anime Viewer {APP_VERSION}, API плагинов {PLUGIN_API_VERSION}",
                 f"Папка плагинов: {PLUGINS_DIR}", ""]
        for loaded in self.manager.plugins:
            plugin = loaded.plugin
            if plugin is None:
                lines.append(f"[ошибка] {loaded.path.name}: {loaded.error}")
                continue
            lines.append(f"[ок] {plugin.title or plugin.name} v{plugin.version}"
                         f" ({plugin.name}) — {loaded.path.name}")
            if plugin.description:
                lines.append(f"     {plugin.description}")
            for error in loaded.errors:
                lines.append(f"     ошибка при работе: {error}")
        for path_name, message in self.manager.warnings:
            lines.append(f"[замечание] {path_name}: {message}")
        for path_name, message in self.manager.errors:
            lines.append(f"[не загрузился] {path_name}: {message}")
        if len(lines) == 3:
            lines.append("Подключённых плагинов нет.")
        return "\n".join(lines)

    # ---------- действия ----------
    def _show_error_for_row(self, row: int) -> None:
        if 0 <= row < len(self.manager.plugins):
            loaded = self.manager.plugins[row]
            text = [f"Файл: {loaded.path}", f"Плагин: {loaded.name}"]
            if loaded.error:
                text.append(f"Ошибка загрузки: {loaded.error}")
            if loaded.errors:
                text.append("Ошибки при работе:")
                text.extend(f"  • {error}" for error in loaded.errors)
            if not loaded.error and not loaded.errors:
                text.append("Ошибок нет.")
            self.details.setPlainText("\n".join(text))
        else:
            self.details.setPlainText(self.report())

    def _open_folder(self) -> None:
        try:
            PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        try:
            from PySide6.QtCore import QUrl
            from PySide6.QtGui import QDesktopServices

            QDesktopServices.openUrl(QUrl.fromLocalFile(str(PLUGINS_DIR)))
        except Exception as exc:  # noqa: BLE001
            print(f"[плагины] не удалось открыть папку: {exc}")

    def _copy_report(self) -> None:
        try:
            from PySide6.QtWidgets import QApplication as _QApp

            _QApp.clipboard().setText(self.report())
            self.details.appendPlainText("\n(отчёт скопирован в буфер обмена)")
        except Exception as exc:  # noqa: BLE001
            print(f"[плагины] не удалось скопировать отчёт: {exc}")


# ======================================================================================
# Главное окно
# ======================================================================================
class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Anime Viewer — аниме и манга")
        # Минимальный размер подобран по содержимому: при меньшем окне строки правой
        # панели (эпизод/озвучка/кнопки) не помещаются и Qt обрезает надписи.
        self.setMinimumSize(1000, 640)
        self.setWindowIcon(load_app_icon())      # иконка окна и панели задач

        # ---- сервисы ----
        self.settings = load_settings()
        self.dubbings: list[str] = [str(d) for d in self.settings.get("dubbings", DUBBINGS)]
        self.theme: dict = dict(DEFAULT_THEME)
        self.theme.update(self.settings.get("theme") or {})
        self.player_settings: dict = {"volume": 80, "auto_next": True, "seek_step": 10}
        self.player_settings.update(self.settings.get("player") or {})
        self.window_settings: dict = {"width": 1290, "height": 840, "last_query": ""}
        self.window_settings.update(self.settings.get("window") or {})
        self.history = HistoryStore(HISTORY_FILE)
        self._history_rows: list[dict] = []      # записи, показанные в списке истории
        self._history_poster_loader: Optional[PosterLoader] = None
        self.catalog_items: list[dict] = []      # релизы AniLibria с текущей страницы
        self._catalog_page: int = 0              # сколько страниц уже загружено
        self._catalog_total: int = 0
        self._catalog_loader: Optional[PosterLoader] = None
        self._catalog_busy: bool = False
        self._history_dirty: bool = True         # нужно ли перерисовать список истории
        self._fullscreen = False
        self._normal_size = None                 # размер окна до полного экрана
        self.client = ShikimoriClient()

        # ---- плагины (папка plugins/, см. plugins/README.txt) ----
        # Загружаем их ДО построения интерфейса: плагин может добавить темы оформления,
        # источники видео и манги — всё это нужно уже на этапе сборки окна.
        self.plugin_manager = PluginManager()
        self.plugin_manager.load()
        self.plugin_store = PluginStore()
        self.plugin_themes: dict[str, dict] = {}
        for key, palette in self.plugin_manager.themes().items():
            # Темы плагинов дописываем в общую таблицу тем: её читают build_qss()
            # и theme_colors(), поэтому тема сразу появляется в «Оформлении».
            THEME_MODES[key] = palette
            self.plugin_themes[key] = palette

        # Источники видео: официальный API AniLibria + ссылки, добавленные вручную.
        # Свой парсер (Kodik/Sibnet) добавляется третьим элементом списка —
        # см. образец AniLibriaVideoSource и шаблон PlaceholderVideoSource.
        self.source: VideoSource = MultiVideoSource(
            [
                LocalLibrarySource(
                    str((self.settings.get("library") or {}).get("path", "")), self.dubbings
                ),
                AniLibriaVideoSource(),
                *load_plugin_sources(),                    # свои источники из sources_local
                *self.plugin_manager.video_sources(),      # источники из плагинов
                ManualLinkSource(),
            ]
        )
        self.progress = ProgressStore(PROGRESS_FILE)

        # ---- манга: источники (встроенный MangaDex + то, что добавили плагины) ----
        self.manga_sources: list[MangaSource] = [MangaDexSource()]
        for source in self.plugin_manager.manga_sources():
            if all(source.name != existing.name for existing in self.manga_sources):
                self.manga_sources.append(source)
        self.manga_settings: dict = dict(DEFAULT_MANGA)
        self.manga_settings.update(self.settings.get("manga") or {})
        self.manga_source: MangaSource = self._pick_manga_source(self.manga_settings.get("source"))
        self.manga_progress = MangaProgressStore(MANGA_PROGRESS_FILE)
        self.manga_items: list[dict] = []        # тайтлы из текущего поиска манги
        self._manga_limit: int = MANGA_SEARCH_LIMIT
        self._manga_exhausted: bool = False       # у поиска закончились результаты
        self._manga_more_pending: bool = False    # текущий ответ — на «Показать ещё»
        self._manga_mode: str = "catalog"         # "catalog" — список всех тайтлов, "search" — поиск
        self._manga_catalog_offset: int = 0       # сколько тайтлов каталога уже показано
        self._manga_catalog_total: int = 0        # сколько всего в каталоге (сообщает источник)
        self._manga_catalog_busy: bool = False
        self._manga_poster_loader: Optional[PosterLoader] = None
        self._manga_poster_loaders: list[PosterLoader] = []   # живые загрузчики обложек
        self.current_manga: Optional[dict] = None
        self.manga_chapters: list[dict] = []
        self.current_chapter: Optional[dict] = None
        self._manga_loader: Optional[MangaPageLoader] = None
        self._manga_loaders: list[MangaPageLoader] = []   # живые потоки загрузки страниц
        self._manga_token: int = 0                        # защита от старых ответов поиска
        self._manga_chapter_token: int = 0                # то же для списка глав
        self._manga_progress_changed: bool = False
        self._manga_host_note: str = ""                   # как качаются страницы (для статуса)

        # ---- состояние ----
        self.animes: list[dict] = []
        self.current_anime: Optional[dict] = None
        self.current_episode: int = 1
        self.current_dubbing: str = self.dubbings[0] if self.dubbings else "AniLibria"
        self.current_key: Optional[str] = None      # ключ прогресса текущего файла (для текста/логов)
        self.current_parts: Optional[tuple] = None  # (anime_id, episode, dubbing) текущего файла
        self.current_label: str = ""                # человекочитаемое имя текущего файла
        self._workers: list[Worker] = []            # держим ссылки, иначе QThread умрёт
        self._seek_dragging: bool = False
        self._duration: float = 0.0
        self._poster_loader: Optional[PosterLoader] = None
        self._live_loaders: list[PosterLoader] = []   # запущенные загрузчики постеров
        self._search_token: int = 0                 # защита от «догоняющих» старых ответов
        self._play_token: int = 0                   # защита от устаревших запросов потока

        self._build_ui()
        self._wire_signals()
        self._setup_shortcuts()

        # автопоиск по мере набора текста (пауза 550 мс) — ощущается как мгновенный
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(SEARCH_DEBOUNCE_MS)
        self._debounce.timeout.connect(self.on_search)

        # то же для манги: свой таймер, чтобы поиски не мешали друг другу
        self._manga_debounce = QTimer(self)
        self._manga_debounce.setSingleShot(True)
        self._manga_debounce.setInterval(SEARCH_DEBOUNCE_MS)
        self._manga_debounce.timeout.connect(lambda: self.on_manga_search(more=False))

        # запись позиции чтения: страницы листаются часто, файл дёргать каждый раз незачем
        self._manga_save_debounce = QTimer(self)
        self._manga_save_debounce.setSingleShot(True)
        self._manga_save_debounce.setInterval(1200)
        self._manga_save_debounce.timeout.connect(self.save_manga_progress)

        # автосохранение прогресса каждые 5 секунд
        self._save_timer = QTimer(self)
        self._save_timer.setInterval(5000)
        self._save_timer.timeout.connect(self.save_progress)
        self._save_timer.timeout.connect(self.save_manga_progress)
        self._save_timer.start()

        # применяем сохранённую тему, громкость, размеры окна и показываем историю
        self.resize(
            int(self.window_settings.get("width", 1290)),
            int(self.window_settings.get("height", 840)),
        )
        self.volume_slider.setValue(int(self.player_settings.get("volume", 80)))
        self.player.set_volume(int(self.player_settings.get("volume", 80)))
        if self.window_settings.get("last_query"):
            # восстанавливаем прошлый запрос и сразу ищем его: поле уже заполнено,
            # значит textChanged не сработает и результаты нужно запросить самим
            self.search_edit.blockSignals(True)
            self.search_edit.setText(str(self.window_settings["last_query"]))
            self.search_edit.blockSignals(False)
            self._debounce.start()
        self.refresh_history()
        self._apply_source_dubbings()
        self._update_continue_button()
        self._apply_manga_settings()
        # главная страница — каталог AniLibria: грузим его сразу в фоне
        self.load_catalog(reset=True)
        if not self.window_settings.get("last_query"):
            self.side_tabs.setCurrentIndex(0)
        # если библиотека уже указана — обновляем индекс в фоне, чтобы озвучки
        # из ваших файлов были готовы к первому выбору аниме
        _library = self.source.library() if isinstance(self.source, MultiVideoSource) else None
        if _library is not None and _library.ready:
            self.status(f"Индексирую библиотеку: {_library.root}…")
            self._run_task(
                lambda: _library.scan(force=True),
                self.on_library_scanned,
                on_error=lambda msg: self.status(f"Библиотека: {msg[:120]}"),
            )
        self._update_app_subtitle()
        self.status(
            "Готово. Вводите название аниме (вкладка «Поиск») или манги (вкладка «Манга») — "
            "поиск пойдёт сам."
        )

        # прогрев: в фоне выбираем рабочее зеркало Shikimori, пока пользователь печатает
        self._run_task(
            lambda: self.client.warm_up(),
            lambda _result: None,
            on_error=lambda _msg: None,   # прогрев не должен показывать ошибки
        )
        # в фоне подчищаем кэш страниц манги, если он разросся
        self._run_task(
            lambda: MangaDexClient.prune_cache(),
            lambda _removed: None,
            on_error=lambda _msg: None,
        )
        # плагины получают доступ к приложению только теперь, когда окно уже собрано:
        # в on_started() они могут писать в статусную строку и добавлять кнопки
        self.plugin_manager.bind(self, self.plugin_store)
        if self.plugin_manager.errors:
            bad = ", ".join(name for name, _message in self.plugin_manager.errors[:3])
            self.status(f"Плагины: не загрузились — {bad}. Подробности: «Плагины» в шапке.")
        elif not self.plugin_manager.is_empty():
            self.status(f"Плагины подключены: {self.plugin_manager.summary()}.")

    def _pick_manga_source(self, code: Optional[str]) -> MangaSource:
        """Находит источник манги по коду из настроек (или встроенный, если кода нет)."""
        for source in self.manga_sources:
            if source.name == str(code or ""):
                return source
        return self.manga_sources[0]

    # ------------------------------------------------------------------ построение UI
    def _build_ui(self) -> None:
        # Панели рисуют свой фон сами (см. WallpaperWidget): так фоновая картинка
        # масштабируется под размер окна, чего QSS не умеет.
        central = WallpaperWidget("app_bg", "bg", 0.5, rounded=False, parent=self)
        central.setObjectName("Root")
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(14, 12, 14, 8)
        root.setSpacing(12)

        # ---------- шапка: название приложения + текущий источник ----------
        header = QHBoxLayout()
        title_box = QHBoxLayout()
        title_box.setSpacing(10)
        self.logo_label = QLabel()
        self.logo_label.setObjectName("AppLogo")
        self.logo_label.setFixedHeight(38)
        self.logo_label.setAlignment(
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft
        )
        self.logo_label.setVisible(False)     # покажется, если логотип есть в assets
        text_box = QVBoxLayout()
        text_box.setSpacing(1)
        app_title = QLabel("Anime Viewer")
        app_title.setObjectName("AppTitle")
        self.app_subtitle = QLabel("Shikimori · AniLibria · встроенный плеер mpv")
        self.app_subtitle.setObjectName("AppSubtitle")
        text_box.addWidget(app_title)
        text_box.addWidget(self.app_subtitle)
        title_box.addWidget(self.logo_label)
        title_box.addLayout(text_box)
        header.addLayout(title_box)
        header.addStretch(1)

        self.continue_button = QPushButton("Продолжить")
        self.continue_button.setObjectName("GhostButton")
        self.continue_button.setIcon(self._standard_icon("SP_MediaPlay"))
        self.continue_button.setToolTip("Продолжить последнюю серию из истории")
        self.continue_button.setVisible(False)

        self.theme_button = QPushButton("Оформление")
        self.theme_button.setObjectName("GhostButton")
        self.theme_button.setToolTip("Тема, акцентный цвет, размеры карточек")
        self.plugins_button = QPushButton("Плагины")
        self.plugins_button.setObjectName("GhostButton")
        self.plugins_button.setToolTip(
            "Плагины приложения: свои источники, темы, кнопки.\n"
            "Кнопка показывает меню, а «Список плагинов…» — что подключено."
        )
        self.fullscreen_button = QPushButton("Полный экран")
        self.fullscreen_button.setObjectName("GhostButton")
        self.fullscreen_button.setToolTip("Развернуть плеер (F, выход — Esc)")
        header.addWidget(self.continue_button)
        header.addWidget(self.theme_button)
        header.addWidget(self.plugins_button)
        header.addWidget(self.fullscreen_button)
        root.addLayout(header)

        # ---------- строка поиска ----------
        search_row = QHBoxLayout()
        search_row.setSpacing(8)
        self.search_edit = QLineEdit()
        self.search_edit.setObjectName("SearchEdit")
        self.search_edit.setPlaceholderText("Название аниме — поиск начнётся сам (например: Мастера меча онлайн)")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setMinimumHeight(38)
        self.search_button = QPushButton("Найти")
        self.search_button.setObjectName("AccentButton")
        self.search_button.setDefault(True)
        self.search_button.setMinimumHeight(38)
        search_row.addWidget(self.search_edit, 1)
        search_row.addWidget(self.search_button)
        root.addLayout(search_row)

        # ---------- основная область: слева карточки аниме, справа плеер ----------
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        root.addWidget(splitter, 1)

        # === ЛЕВАЯ ПАНЕЛЬ: список аниме ===
        left = WallpaperWidget("app_bg", "panel", 0.35, rounded=True)
        left.setObjectName("SidePanel")
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(12, 12, 12, 12)
        left_layout.setSpacing(8)
        self.side_tabs = QTabWidget()
        self.side_tabs.setDocumentMode(True)

        # ---- вкладка каталога AniLibria (главная) ----
        catalog_tab = QWidget()
        catalog_layout = QVBoxLayout(catalog_tab)
        catalog_layout.setContentsMargins(0, 8, 0, 0)
        catalog_layout.setSpacing(8)

        catalog_top = QHBoxLayout()
        catalog_top.setSpacing(8)
        catalog_title = QLabel("КАТАЛОГ ANILIBRIA")
        catalog_title.setObjectName("SectionTitle")
        catalog_top.addWidget(catalog_title)
        catalog_top.addStretch(1)
        self.catalog_sort = QComboBox()
        for label in CATALOG_SORTINGS:
            self.catalog_sort.addItem(label)
        self.catalog_sort.setToolTip("Порядок релизов в каталоге")
        catalog_top.addWidget(self.catalog_sort)
        catalog_layout.addLayout(catalog_top)

        self.catalog_info = QLabel("Загружаю каталог…")
        self.catalog_info.setObjectName("Hint")
        catalog_layout.addWidget(self.catalog_info)

        self.catalog_list = QListWidget()
        self.catalog_list.setObjectName("AnimeList")
        self.catalog_list.setMouseTracking(True)
        self.catalog_list.setUniformItemSizes(True)
        self.catalog_list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.catalog_list.setFrameShape(QFrame.Shape.NoFrame)
        self._catalog_delegate = AnimeCardDelegate(self.catalog_list)
        self.catalog_list.setItemDelegate(self._catalog_delegate)
        catalog_layout.addWidget(self.catalog_list, 1)

        self.catalog_more_button = QPushButton("Показать ещё")
        self.catalog_more_button.setObjectName("GhostButton")
        catalog_layout.addWidget(self.catalog_more_button)
        self.side_tabs.addTab(catalog_tab, "Каталог")

        # ---- вкладка поиска ----
        search_tab = QWidget()
        search_tab_layout = QVBoxLayout(search_tab)
        search_tab_layout.setContentsMargins(0, 8, 0, 0)
        search_tab_layout.setSpacing(8)
        left_title = QLabel("НАЙДЕННЫЕ АНИМЕ")
        left_title.setObjectName("SectionTitle")
        search_tab_layout.addWidget(left_title)
        self.anime_list = QListWidget()
        self.anime_list.setObjectName("AnimeList")
        self.anime_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.anime_list.setUniformItemSizes(True)
        self.anime_list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.anime_list.setMouseTracking(True)   # нужен для подсветки при наведении
        self.anime_list.setFrameShape(QFrame.Shape.NoFrame)
        self._card_delegate = AnimeCardDelegate(self.anime_list)
        self.anime_list.setItemDelegate(self._card_delegate)
        self._poster_pixmaps: dict[int, QPixmap] = {}
        search_tab_layout.addWidget(self.anime_list, 1)
        self.side_tabs.addTab(search_tab, "Поиск")   # индекс 1

        # ---- вкладка истории просмотров ----
        history_tab = QWidget()
        history_tab_layout = QVBoxLayout(history_tab)
        history_tab_layout.setContentsMargins(0, 8, 0, 0)
        history_tab_layout.setSpacing(8)
        history_title = QLabel("ИСТОРИЯ ПРОСМОТРОВ")
        history_title.setObjectName("SectionTitle")
        history_tab_layout.addWidget(history_title)
        self.history_list = QListWidget()
        self.history_list.setObjectName("HistoryList")
        self.history_list.setMouseTracking(True)
        self.history_list.setUniformItemSizes(True)
        self.history_list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.history_list.setFrameShape(QFrame.Shape.NoFrame)
        self._history_delegate = AnimeCardDelegate(self.history_list)
        self.history_list.setItemDelegate(self._history_delegate)
        history_tab_layout.addWidget(self.history_list, 1)

        # Две строки кнопок: в один ряд четыре кнопки в узкой панели обрезались
        history_buttons = QHBoxLayout()
        history_buttons.setSpacing(6)
        self.history_play_button = QPushButton("Продолжить")
        self.history_play_button.setIcon(self._standard_icon("SP_MediaPlay"))
        self.history_forget_button = QPushButton("Забыть")
        self.history_forget_button.setToolTip("Сбросить позицию у выбранной записи")
        self.history_remove_button = QPushButton("Убрать")
        self.history_remove_button.setToolTip("Удалить запись из истории")
        self.history_clear_button = QPushButton("Очистить")
        self.history_clear_button.setToolTip("Удалить всю историю просмотров")
        for button in (self.history_play_button, self.history_forget_button):
            button.setMinimumHeight(32)
            history_buttons.addWidget(button)
        history_tab_layout.addLayout(history_buttons)

        history_buttons2 = QHBoxLayout()
        history_buttons2.setSpacing(6)
        for button in (self.history_remove_button, self.history_clear_button):
            button.setMinimumHeight(32)
            history_buttons2.addWidget(button)
        history_tab_layout.addLayout(history_buttons2)
        self.side_tabs.addTab(history_tab, "История")   # индекс 2

        # ---- вкладка манги (MangaDex) ----
        manga_tab = QWidget()
        manga_tab_layout = QVBoxLayout(manga_tab)
        manga_tab_layout.setContentsMargins(0, 8, 0, 0)
        manga_tab_layout.setSpacing(8)

        manga_title = QLabel("МАНГА")
        manga_title.setObjectName("SectionTitle")
        manga_tab_layout.addWidget(manga_title)

        # источник манги: встроенный MangaDex плюс то, что добавили плагины.
        # Если источник один, строка не нужна и скрывается.
        source_row = QHBoxLayout()
        source_row.setSpacing(6)
        source_label = QLabel("Источник")
        source_label.setObjectName("SectionTitle")
        source_row.addWidget(source_label)
        self.manga_source_combo = QComboBox()
        for source in self.manga_sources:
            self.manga_source_combo.addItem(source.label, source.name)
        self.manga_source_combo.setToolTip(
            "Откуда берутся тайтлы, главы и страницы манги.\n"
            "Свой источник можно добавить плагином (папка plugins)."
        )
        index = self.manga_source_combo.findData(self.manga_source.name)
        if index >= 0:
            self.manga_source_combo.setCurrentIndex(index)
        source_row.addWidget(self.manga_source_combo, 1)
        manga_tab_layout.addLayout(source_row)
        if len(self.manga_sources) < 2:
            self.manga_source_combo.setVisible(False)
            source_label.setVisible(False)

        manga_search_row = QHBoxLayout()
        manga_search_row.setSpacing(6)
        self.manga_search_edit = QLineEdit()
        self.manga_search_edit.setPlaceholderText("Название манги (можно по-русски)")
        self.manga_search_edit.setClearButtonEnabled(True)
        self.manga_search_button = QPushButton("Найти")
        self.manga_search_button.setObjectName("AccentButton")
        self.manga_search_button.setMinimumHeight(34)
        manga_search_row.addWidget(self.manga_search_edit, 1)
        manga_search_row.addWidget(self.manga_search_button)
        manga_tab_layout.addLayout(manga_search_row)

        # каталог: готовый список всех тайтлов источника, без поиска
        catalog_row = QHBoxLayout()
        catalog_row.setSpacing(6)
        self.manga_catalog_button = QPushButton("Каталог")
        self.manga_catalog_button.setObjectName("GhostButton")
        self.manga_catalog_button.setMinimumWidth(92)
        self.manga_catalog_button.setToolTip(
            "Показать все доступные тайтлы источника — список загружается сам,\n"
            "искать ничего не нужно"
        )
        self.manga_sort_combo = QComboBox()
        self.manga_sort_combo.setMinimumWidth(120)
        self.manga_sort_combo.setToolTip("Порядок тайтлов в каталоге")
        self.manga_ru_check = QCheckBox("RU")
        self.manga_ru_check.setMinimumWidth(60)
        self.manga_ru_check.setToolTip("Только тайтлы, у которых есть перевод на русский")
        catalog_row.addWidget(self.manga_catalog_button)
        catalog_row.addWidget(self.manga_sort_combo, 1)
        catalog_row.addWidget(self.manga_ru_check)
        manga_tab_layout.addLayout(catalog_row)

        self.manga_info = QLabel("Загружаю каталог…")
        self.manga_info.setObjectName("Hint")
        self.manga_info.setWordWrap(True)
        manga_tab_layout.addWidget(self.manga_info)

        self.manga_list = QListWidget()
        self.manga_list.setObjectName("AnimeList")
        self.manga_list.setMouseTracking(True)
        self.manga_list.setUniformItemSizes(True)
        self.manga_list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.manga_list.setFrameShape(QFrame.Shape.NoFrame)
        self._manga_delegate = AnimeCardDelegate(self.manga_list)
        self.manga_list.setItemDelegate(self._manga_delegate)
        manga_tab_layout.addWidget(self.manga_list, 1)

        manga_buttons = QHBoxLayout()
        manga_buttons.setSpacing(6)
        self.manga_more_button = QPushButton("Показать ещё")
        self.manga_more_button.setObjectName("GhostButton")
        self.manga_more_button.setToolTip("Следующая страница результатов поиска")
        self.manga_reading_button = QPushButton("Читаю")
        self.manga_reading_button.setObjectName("GhostButton")
        self.manga_reading_button.setToolTip("Список манги с сохранённым прогрессом чтения")
        manga_buttons.addWidget(self.manga_more_button)
        manga_buttons.addWidget(self.manga_reading_button)
        manga_tab_layout.addLayout(manga_buttons)
        self.side_tabs.addTab(manga_tab, "Манга")   # индекс 3
        self._fill_manga_catalog_controls()

        left_layout.addWidget(self.side_tabs, 1)
        self.side_panel = left
        # левая панель может сужаться: при окне 900 px правой панели нужна
        # ширина побольше, иначе её строки не влезают
        left.setMinimumWidth(300)
        splitter.addWidget(left)

        # === ПРАВАЯ ПАНЕЛЬ: плеер и читалка манги (переключаются) ===
        right = WallpaperWidget("player_bg", "panel", 0.4, rounded=True)
        right.setObjectName("PlayerPanel")
        right.setMinimumWidth(430)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(12, 12, 12, 12)
        right_layout.setSpacing(10)

        # Стек из двух страниц: 0 — видео (плеер mpv), 1 — чтение манги.
        # Переключается сам: включаете серию — показывается плеер, открываете главу —
        # читалка. Так обе функции живут в одном окне и не мешают друг другу.
        from PySide6.QtWidgets import QStackedWidget

        self.right_stack = QStackedWidget()
        right_layout.addWidget(self.right_stack)

        # ---------------- страница 1: видео ----------------
        video_page = QWidget()
        video_layout = QVBoxLayout(video_page)
        video_layout.setContentsMargins(0, 0, 0, 0)
        video_layout.setSpacing(10)

        # --- верхняя строка: эпизод + озвучка + кнопки ---
        top_row = QHBoxLayout()
        top_row.setSpacing(8)
        ep_label = QLabel("Эпизод")
        ep_label.setObjectName("SectionTitle")
        top_row.addWidget(ep_label)
        self.episode_combo = QComboBox()
        self.episode_combo.setMinimumWidth(110)
        top_row.addWidget(self.episode_combo)

        dub_label = QLabel("Озвучка")
        dub_label.setObjectName("SectionTitle")
        top_row.addWidget(dub_label)
        self.dubbing_combo = QComboBox()
        self.dubbing_combo.setMinimumWidth(120)
        top_row.addWidget(self.dubbing_combo)

        self.play_button = QPushButton("Смотреть")
        self.play_button.setObjectName("PlayButton")
        self.play_button.setIcon(self._standard_icon("SP_MediaPlay"))
        self.play_button.setMinimumWidth(96)
        top_row.addWidget(self.play_button)
        top_row.addStretch(1)
        video_layout.addLayout(top_row)

        # строка-подсказка: что играет сейчас и что делать с остальными озвучками
        self.dubbing_hint = QLabel()
        self.dubbing_hint.setObjectName("AppSubtitle")
        self.dubbing_hint.setWordWrap(True)
        video_layout.addWidget(self.dubbing_hint)

        # --- вторая строка: вспомогательные действия ---
        # Две строки, а не одна: при узкой правой панели (окно 900 px) четыре кнопки
        # в одну строку не влезали, и Qt сжимал виджеты до обрезанных надписей.
        tools_row = QHBoxLayout()
        tools_row.setSpacing(8)
        self.next_button = QPushButton("Следующая серия")
        self.next_button.setObjectName("GhostButton")
        self.next_button.setIcon(self._standard_icon("SP_MediaSkipForward"))
        self.next_button.setToolTip("Следующая серия (N)")
        self.add_link_button = QPushButton("Добавить озвучку")
        self.add_link_button.setObjectName("GhostButton")
        self.add_link_button.setToolTip(
            "Привязать свою ссылку (mp4/m3u8) к выбранной серии и студии —\n"
            "так подключается озвучка, которой нет в источнике"
        )
        tools_row.addWidget(self.next_button)
        tools_row.addWidget(self.add_link_button)
        tools_row.addStretch(1)
        video_layout.addLayout(tools_row)

        open_row = QHBoxLayout()
        open_row.setSpacing(8)
        self.open_file_button = QPushButton("Файл…")
        self.open_file_button.setObjectName("GhostButton")
        self.open_file_button.setToolTip("Открыть видеофайл с диска")
        self.open_url_button = QPushButton("Поток…")
        self.open_url_button.setObjectName("GhostButton")
        self.open_url_button.setToolTip("Прямая ссылка на видео: mp4, m3u8")
        open_row.addStretch(1)
        open_row.addWidget(self.open_file_button)
        open_row.addWidget(self.open_url_button)
        video_layout.addLayout(open_row)

        # --- заголовок того, что сейчас играет ---
        self.now_playing = QLabel("Ничего не воспроизводится")
        self.now_playing.setObjectName("NowPlaying")
        self.now_playing.setWordWrap(True)
        video_layout.addWidget(self.now_playing)

        # --- ВСТРОЕННЫЙ ПЛЕЕР mpv (через winId) ---
        player_frame = QFrame()
        player_frame.setObjectName("PlayerFrame")
        player_layout = QVBoxLayout(player_frame)
        player_layout.setContentsMargins(2, 2, 2, 2)
        self.player = MpvWidget()
        player_layout.addWidget(self.player)
        video_layout.addWidget(player_frame, 1)

        # --- нижняя панель управления плеером ---
        controls = QHBoxLayout()
        controls.setSpacing(10)
        self.pause_button = QPushButton()
        self.pause_button.setObjectName("IconButton")
        self.pause_button.setIcon(self._standard_icon("SP_MediaPause"))
        self.pause_button.setToolTip("Пауза / продолжить (Space)")
        controls.addWidget(self.pause_button)

        self.position_slider = QSlider(Qt.Orientation.Horizontal)
        self.position_slider.setRange(0, 1000)
        self.position_slider.setValue(0)
        controls.addWidget(self.position_slider, 1)

        self.time_label = QLabel("00:00 / 00:00")
        self.time_label.setObjectName("TimeLabel")
        self.time_label.setMinimumWidth(110)
        controls.addWidget(self.time_label)

        self.volume_icon = QLabel()
        self.volume_icon.setPixmap(self._standard_icon("SP_MediaVolume").pixmap(QSize(16, 16)))
        self.volume_icon.setToolTip("Громкость (стрелки ↑ / ↓)")
        controls.addWidget(self.volume_icon)
        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setObjectName("VolumeSlider")   # у ползунка громкости свой размер
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(80)
        self.volume_slider.setMaximumWidth(130)
        self.volume_slider.setToolTip("Громкость")
        controls.addWidget(self.volume_slider)

        video_layout.addLayout(controls)
        self.right_stack.addWidget(video_page)

        # ---------------- страница 2: чтение манги ----------------
        manga_page = QWidget()
        manga_layout = QVBoxLayout(manga_page)
        manga_layout.setContentsMargins(0, 0, 0, 0)
        manga_layout.setSpacing(10)

        chapter_row = QHBoxLayout()
        chapter_row.setSpacing(8)
        chapter_label = QLabel("Глава")
        chapter_label.setObjectName("SectionTitle")
        chapter_row.addWidget(chapter_label)
        self.chapter_combo = QComboBox()
        self.chapter_combo.setMinimumWidth(150)
        self.chapter_combo.setToolTip("Глава: номер, язык перевода и команда перевода")
        chapter_row.addWidget(self.chapter_combo, 1)

        lang_label = QLabel("Язык")
        lang_label.setObjectName("SectionTitle")
        chapter_row.addWidget(lang_label)
        self.manga_lang_combo = QComboBox()
        self.manga_lang_combo.setMinimumWidth(96)
        for code, title in MANGA_LANGS:
            self.manga_lang_combo.addItem(title, code)
        self.manga_lang_combo.setToolTip("Язык перевода: главы берутся на нём, "
                                        "остальные — с английского, если включён запас")
        chapter_row.addWidget(self.manga_lang_combo)

        self.manga_read_button = QPushButton("Читать")
        self.manga_read_button.setObjectName("PlayButton")
        self.manga_read_button.setMinimumWidth(100)
        chapter_row.addWidget(self.manga_read_button)
        manga_layout.addLayout(chapter_row)

        self.manga_hint = QLabel("Выберите тайтл во вкладке «Манга» слева.")
        self.manga_hint.setObjectName("AppSubtitle")
        self.manga_hint.setWordWrap(True)
        manga_layout.addWidget(self.manga_hint)

        # Инструменты чтения: переходы по главам — иконками (иначе семь кнопок
        # с подписями не влезают в панель и надписи обрезаются).
        reader_tools = QHBoxLayout()
        reader_tools.setSpacing(6)
        self.prev_chapter_button = QPushButton()
        self.prev_chapter_button.setObjectName("IconButton")
        self.prev_chapter_button.setIcon(self._standard_icon("SP_MediaSkipBackward"))
        self.prev_chapter_button.setToolTip("Предыдущая глава")
        self.next_chapter_button = QPushButton()
        self.next_chapter_button.setObjectName("IconButton")
        self.next_chapter_button.setIcon(self._standard_icon("SP_MediaSkipForward"))
        self.next_chapter_button.setToolTip("Следующая глава (N)")
        self.manga_mode_button = QPushButton("Постранично")
        self.manga_mode_button.setObjectName("GhostButton")
        self.manga_mode_button.setToolTip("Переключить ленту и постраничный режим (M)")
        self.manga_fit_button = QPushButton("Ширина")
        self.manga_fit_button.setObjectName("GhostButton")
        self.manga_fit_button.setToolTip("Масштаб страниц: по ширине / по высоте / 100% (W)")
        self.manga_zoom_out_button = QPushButton("−")
        self.manga_zoom_out_button.setObjectName("IconButton")
        self.manga_zoom_out_button.setToolTip("Уменьшить страницу")
        self.manga_zoom_in_button = QPushButton("+")
        self.manga_zoom_in_button.setObjectName("IconButton")
        self.manga_zoom_in_button.setToolTip("Увеличить страницу")
        self.manga_browser_button = QPushButton("Браузер")
        self.manga_browser_button.setObjectName("GhostButton")
        self.manga_browser_button.setToolTip(
            "Открыть главу на mangadex.org — выручает, если сеть блокирует загрузку страниц"
        )
        reader_tools.addWidget(self.prev_chapter_button)
        reader_tools.addWidget(self.next_chapter_button)
        reader_tools.addWidget(self.manga_mode_button)
        reader_tools.addWidget(self.manga_fit_button)
        reader_tools.addWidget(self.manga_zoom_out_button)
        reader_tools.addWidget(self.manga_zoom_in_button)
        reader_tools.addStretch(1)
        reader_tools.addWidget(self.manga_browser_button)
        manga_layout.addLayout(reader_tools)

        self.manga_title = QLabel("Ничего не читается")
        self.manga_title.setObjectName("NowPlaying")
        self.manga_title.setWordWrap(True)
        manga_layout.addWidget(self.manga_title)

        reader_frame = QFrame()
        reader_frame.setObjectName("PlayerFrame")
        reader_frame_layout = QVBoxLayout(reader_frame)
        reader_frame_layout.setContentsMargins(2, 2, 2, 2)
        self.reader = MangaReader()
        reader_frame_layout.addWidget(self.reader)
        manga_layout.addWidget(reader_frame, 1)

        page_controls = QHBoxLayout()
        page_controls.setSpacing(10)
        self.page_prev_button = QPushButton()
        self.page_prev_button.setObjectName("IconButton")
        self.page_prev_button.setIcon(self._standard_icon("SP_MediaSeekBackward"))
        self.page_prev_button.setToolTip("Предыдущая страница (←)")
        page_controls.addWidget(self.page_prev_button)

        self.page_slider = QSlider(Qt.Orientation.Horizontal)
        self.page_slider.setRange(0, 0)
        self.page_slider.setValue(0)
        self.page_slider.setToolTip("Страница главы")
        page_controls.addWidget(self.page_slider, 1)

        self.page_label = QLabel("стр. 0 / 0")
        self.page_label.setObjectName("TimeLabel")
        self.page_label.setMinimumWidth(110)
        page_controls.addWidget(self.page_label)

        self.page_next_button = QPushButton()
        self.page_next_button.setObjectName("IconButton")
        self.page_next_button.setIcon(self._standard_icon("SP_MediaSeekForward"))
        self.page_next_button.setToolTip("Следующая страница (→)")
        page_controls.addWidget(self.page_next_button)

        manga_layout.addLayout(page_controls)
        self.right_stack.addWidget(manga_page)

        self.player_panel = right
        splitter.addWidget(right)

        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([380, 900])

        self._card_delegate.apply_theme(self.theme)
        self._history_delegate.apply_theme(self.theme)
        self._apply_decor()          # обои панелей и логотип из assets
        self.setStyleSheet(build_qss(self.theme))
        self.setStatusBar(QStatusBar(self))

    def _apply_decor(self) -> None:
        """
        Оформление из папки assets: обои панелей и логотип в шапке.

        Логотип берётся из темы: для светлой темы — светлый вариант, для остальных —
        тёмный. Если файлов нет, остаётся только текст, ничего не ломается.
        """
        panels = (
            self.centralWidget(),                 # фон всего окна
            getattr(self, "side_panel", None),
            getattr(self, "player_panel", None),
        )
        for panel in panels:
            if panel is not None and hasattr(panel, "apply_theme"):
                panel.apply_theme(self.theme)

        logo_name = "logo_light" if str(self.theme.get("mode")) == "light" else "logo_dark"
        image = load_asset_image(logo_name)
        self._logo_ready = False
        if image is None or not hasattr(self, "logo_label"):
            self.logo_label.setVisible(False)
            return
        height = 34 if str(self.theme.get("mode")) != "light" else 30
        pixmap = QPixmap.fromImage(image).scaledToHeight(
            height, Qt.TransformationMode.SmoothTransformation
        )
        self.logo_label.setPixmap(pixmap)
        self._logo_ready = True
        self._adapt_header()

    def _adapt_header(self) -> None:
        """
        Убирает из шапки необязательное, когда окно узкое (или шрифт крупный).

        Логотип и подпись — украшение: лучше спрятать их, чем дать кнопкам и названию
        сжаться до обрезанных надписей. Полные сведения остаются в подсказке.
        """
        if not hasattr(self, "logo_label"):
            return
        try:
            scale = float(self.theme.get("font_scale", 1.0) or 1.0)
        except (TypeError, ValueError):
            scale = 1.0
        tight = self.width() < int(1180 * scale)
        self.logo_label.setVisible(bool(getattr(self, "_logo_ready", False)) and not tight)
        self.app_subtitle.setVisible(not tight)

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt-стиль)
        super().resizeEvent(event)
        self._adapt_header()

    # ------------------------------------------------------------------ соединения
    def _wire_signals(self) -> None:
        self.search_button.clicked.connect(self.on_search)
        self.search_edit.returnPressed.connect(self.on_search)
        self.search_edit.textChanged.connect(self.on_search_text_changed)  # автопоиск

        self.anime_list.currentRowChanged.connect(self.on_anime_selected)
        self.catalog_list.currentRowChanged.connect(self.on_catalog_selected)
        self.catalog_list.itemDoubleClicked.connect(self.on_catalog_activated)
        self.catalog_sort.currentIndexChanged.connect(lambda _i: self.load_catalog(reset=True))
        self.catalog_more_button.clicked.connect(lambda: self.load_catalog(reset=False))
        self.episode_combo.currentIndexChanged.connect(self.on_episode_changed)
        self.dubbing_combo.currentIndexChanged.connect(self.on_dubbing_changed)

        self.play_button.clicked.connect(self.on_play_clicked)
        self.pause_button.clicked.connect(self.on_pause_clicked)
        self.open_file_button.clicked.connect(self.on_open_file)
        self.open_url_button.clicked.connect(self.on_open_url)
        self.add_link_button.clicked.connect(self.on_add_link)
        self.next_button.clicked.connect(self.on_next_episode)
        self.theme_button.clicked.connect(self.on_settings_clicked)
        self.continue_button.clicked.connect(self.on_continue_last)
        self.fullscreen_button.clicked.connect(self.toggle_fullscreen)
        self.plugins_button.clicked.connect(self.on_plugins_clicked)

        # история просмотров
        self.history_list.itemActivated.connect(lambda _item: self.on_history_play())
        self.history_list.itemDoubleClicked.connect(lambda _item: self.on_history_play())
        self.history_play_button.clicked.connect(self.on_history_play)
        self.history_forget_button.clicked.connect(self.on_history_forget)
        self.history_remove_button.clicked.connect(self.on_history_remove)
        self.history_clear_button.clicked.connect(self.on_history_clear)
        self.side_tabs.currentChanged.connect(self.on_side_tab_changed)

        # манга: список слева
        self.manga_search_button.clicked.connect(lambda: self.on_manga_search(more=False))
        self.manga_search_edit.returnPressed.connect(lambda: self.on_manga_search(more=False))
        self.manga_search_edit.textChanged.connect(self.on_manga_search_text_changed)
        self.manga_list.currentRowChanged.connect(self.on_manga_selected)
        self.manga_list.itemDoubleClicked.connect(self.on_manga_activated)
        self.manga_more_button.clicked.connect(self.on_manga_more)
        self.manga_reading_button.clicked.connect(self.on_manga_reading_list)
        self.manga_source_combo.currentIndexChanged.connect(self.on_manga_source_changed)
        self.manga_catalog_button.clicked.connect(self.on_manga_catalog_clicked)
        self.manga_sort_combo.currentIndexChanged.connect(self.on_manga_catalog_sort_changed)
        self.manga_ru_check.toggled.connect(self.on_manga_only_russian_toggled)

        # манга: панель чтения справа
        self.chapter_combo.currentIndexChanged.connect(self.on_chapter_changed)
        self.manga_lang_combo.currentIndexChanged.connect(self.on_manga_lang_changed)
        self.manga_read_button.clicked.connect(self.on_manga_read_clicked)
        self.prev_chapter_button.clicked.connect(lambda: self.open_chapter_relative(-1))
        self.next_chapter_button.clicked.connect(lambda: self.open_chapter_relative(1))
        self.manga_mode_button.clicked.connect(self.on_manga_mode_clicked)
        self.manga_fit_button.clicked.connect(self.on_manga_fit_clicked)
        self.manga_zoom_in_button.clicked.connect(lambda: self.manga_zoom(1.25))
        self.manga_zoom_out_button.clicked.connect(lambda: self.manga_zoom(0.8))
        self.manga_browser_button.clicked.connect(self.on_manga_open_in_browser)
        self.page_prev_button.clicked.connect(lambda: self.reader.goto_prev_page())
        self.page_next_button.clicked.connect(lambda: self.reader.goto_next_page())
        self.page_slider.sliderReleased.connect(
            lambda: self.reader.goto_page(self.page_slider.value())
        )

        # читалка сообщает, что нужно скачать, и куда перешли
        self.reader.need_page.connect(self.on_manga_need_page)
        self.reader.page_changed.connect(self.on_manga_page_changed)
        self.reader.next_chapter.connect(lambda: self.open_chapter_relative(1))
        self.reader.prev_chapter.connect(lambda: self.open_chapter_relative(-1))
        self.reader.note.connect(self.status)

        self.volume_slider.valueChanged.connect(self.player.set_volume)

        # перемотка: пока тянем ползунок — не перезаписываем его позицией из mpv
        self.position_slider.sliderPressed.connect(self.on_seek_pressed)
        self.position_slider.sliderMoved.connect(self.on_seek_moved)
        self.position_slider.sliderReleased.connect(self.on_seek_released)

        # сигналы от плеера
        self.player.position_changed.connect(self.on_player_position)
        self.player.pause_changed.connect(self.on_player_pause)
        self.player.eof_reached.connect(self.on_player_eof)
        self.player.player_error.connect(self.on_player_error)
        self.player.file_started.connect(self.on_file_started)

    # ------------------------------------------------------------------ утилиты
    def _standard_icon(self, name: str):
        """
        Иконка из системного набора Qt — вместо эмодзи. Кнопки проигрывания, паузы,
        перехода и громкости выглядят нативно и не зависят от шрифтов и эмодзи.
        """
        from PySide6.QtGui import QIcon
        from PySide6.QtWidgets import QStyle

        pixmap = getattr(QStyle.StandardPixmap, name, None)
        if pixmap is None:
            return QIcon()
        return self.style().standardIcon(pixmap)

    def status(self, text: str) -> None:
        self.statusBar().showMessage(text, 8000)

    def _run_task(
        self,
        fn: Callable[..., Any],
        on_ok: Callable[[Any], None],
        on_error: Optional[Callable[[str], None]] = None,
    ) -> None:
        """Запускает fn в отдельном потоке; результат отдаёт в on_ok (GUI-поток)."""
        worker = Worker(fn)
        self._workers.append(worker)
        worker.result_ready.connect(on_ok)
        worker.error.connect(on_error if on_error is not None else self.on_worker_error)
        worker.finished.connect(lambda w=worker: self._drop_worker(w))
        worker.start()

    def _drop_worker(self, worker: Worker) -> None:
        if worker in self._workers:
            self._workers.remove(worker)

    # ------------------------------------------------------------------ поиск
    def on_search_text_changed(self, text: str) -> None:
        """Автопоиск по мере ввода: пауза 550 мс, минимум 3 символа (экономим лимит API)."""
        self._debounce.stop()
        if len(text.strip()) >= MIN_AUTOSEARCH_LEN:
            self._debounce.start()

    def on_search(self) -> None:
        query = self.search_edit.text().strip()
        if not query:
            self.status("Введите название для поиска.")
            return

        self._debounce.stop()
        self.search_button.setEnabled(False)
        self._search_token += 1
        token = self._search_token
        self.status(f"Ищем «{query}»…")

        def task() -> tuple[list[dict], float]:
            t0 = time.perf_counter()
            result = self.client.search_animes(query)
            return result, time.perf_counter() - t0

        self._run_task(task, lambda payload: self.on_search_done(payload, token))

    def on_search_done(self, payload: Any, token: int = 0) -> None:
        self.search_button.setEnabled(True)
        if token and token != self._search_token:
            return  # пришёл ответ на устаревший запрос — игнорируем
        animes, elapsed = payload if isinstance(payload, tuple) else (payload, 0.0)

        self.animes = animes or []
        self.anime_list.clear()
        self.episode_combo.clear()
        self.current_anime = None
        self.current_key = None
        self.current_parts = None

        if not self.animes:
            self.status(f"Ничего не найдено ({elapsed:.2f} с). Попробуйте другое название.")
            return

        # 1) сразу показываем список (без постеров) — это доли секунды
        poster_tasks: list[tuple[int, str]] = []
        self._poster_pixmaps.clear()
        self._card_delegate.posters.clear()
        for row, anime in enumerate(self.animes):
            title = anime.get("russian") or anime.get("title") or "Без названия"
            year = anime.get("year")
            kind = anime.get("kind") or ""
            ep_count = max(int(anime.get("episodes") or 0), int(anime.get("episodes_aired") or 0))
            parts = []
            if year:
                parts.append(str(year))
            if kind:
                parts.append(kind.upper())
            if ep_count:
                parts.append(f"{ep_count} эп.")
            score = anime.get("score")
            if score:
                parts.append(f"оценка {score}")
            item = QListWidgetItem(title)
            # вторая строка карточки (год · тип · серии · оценка) — рисует делегат
            item.setData(Qt.ItemDataRole.UserRole + 1, "  ·  ".join(parts) or "без описания")
            item.setData(Qt.ItemDataRole.UserRole, anime.get("id"))
            item.setToolTip(f"Shikimori ID: {anime.get('id')}\nОценка: {anime.get('score')}")
            self.anime_list.addItem(item)
            if anime.get("poster_url"):
                poster_tasks.append((row, anime["poster_url"]))

        # 2) постеры догружаются параллельно и появляются по мере готовности
        self._cancel_poster_loaders()
        if poster_tasks:
            loader = PosterLoader(self.client, poster_tasks)
            loader.poster_ready.connect(
                lambda row, data, l=loader: self.on_poster_ready(l, row, data)
            )
            loader.finished_all.connect(lambda l=loader: self.on_posters_done(l))
            self._poster_loader = self._start_poster_loader(loader)

        self.status(
            f"Найдено: {len(self.animes)} за {elapsed:.2f} с "
            f"(зеркало {self.client.active_host}). Постеры догружаются…"
        )
        self._refresh_progress_bars()

    def _start_poster_loader(self, loader: PosterLoader) -> PosterLoader:
        """
        Запускает загрузчик постеров и держит ссылку до его завершения.

        Важно: QThread нельзя удалять, пока он работает (Qt в этом случае ругается
        «QThread: Destroyed while thread is still running» и процесс может упасть),
        поэтому все запущенные загрузчики лежат в self._live_loaders и убираются
        только по сигналу finished_all.
        """
        self._live_loaders.append(loader)
        loader.finished_all.connect(lambda l=loader: self._drop_poster_loader(l))
        loader.start()
        return loader

    def _drop_poster_loader(self, loader: PosterLoader) -> None:
        if loader in self._live_loaders:
            self._live_loaders.remove(loader)

    def _cancel_poster_loaders(self) -> None:
        """Останавливает все загрузчики постеров (ссылки остаются до их завершения)."""
        for loader in list(self._live_loaders):
            try:
                loader.cancel()
            except Exception:
                pass
        self._poster_loader = None
        self._history_poster_loader = None

    def on_poster_ready(self, loader: "PosterLoader", row: int, data: object) -> None:
        """
        Постер готов — кладём его в делегат карточек (рисует он сам).
        Постеры от прошлого поиска отбрасываем: в новом списке те же номера строк
        означают уже другие аниме.
        """
        if loader is not self._poster_loader:
            return
        if row < 0 or row >= self.anime_list.count() or not isinstance(data, (bytes, bytearray)):
            return
        anime = self.animes[row] if row < len(self.animes) else None
        if anime is not None:
            anime["poster_bytes"] = bytes(data)
        pixmap = QPixmap()
        if pixmap.loadFromData(bytes(data)):
            self._poster_pixmaps[row] = pixmap
            self._card_delegate.posters[row] = pixmap
            self.anime_list.update(self.anime_list.model().index(row, 0))

    def on_posters_done(self, loader: Optional["PosterLoader"] = None) -> None:
        """
        Постеры догружены. Сообщение меняем только если ничего более важного
        не произошло: иначе оно перезатирало статус вида «серий — 25».
        """
        if loader is not None and loader is not self._poster_loader:
            return    # это закончился загрузчик прошлого поиска
        if not self.animes:
            return
        current = self.statusBar().currentMessage()
        if not current or current.startswith(("Найдено", "Постеры")):
            self.status(f"Готово: {len(self.animes)} аниме, постеры загружены.")

    # ------------------------------------------------------------------ выбор аниме/эпизода
    # ------------------------------------------------------------------ каталог AniLibria
    def load_catalog(self, reset: bool = False) -> None:
        """
        Загружает каталог AniLibria (то, что озвучено студией) — по 40 релизов за раз.
        Работает в фоне, поэтому интерфейс не подвисает.
        """
        if self._catalog_busy:
            return
        if reset:
            self._catalog_page = 0
            self.catalog_items = []
            self.catalog_list.clear()
            self._catalog_delegate.posters.clear()
            self._catalog_delegate.progress.clear()
            self.catalog_list.setCurrentRow(-1)
        self._catalog_busy = True
        self.catalog_info.setText("Загружаю каталог AniLibria…")
        self.catalog_more_button.setEnabled(False)

        page = self._catalog_page + 1
        sorting = CATALOG_SORTINGS.get(self.catalog_sort.currentText(), "FRESH_AT_DESC")
        client = self.source.catalog_client() if isinstance(self.source, MultiVideoSource) else None
        if client is None:
            self._catalog_busy = False
            self.catalog_info.setText("Каталог недоступен: источник AniLibria не подключён.")
            return

        def task():
            return page, client.catalog(page=page, limit=CATALOG_PAGE_SIZE, sorting=sorting)

        self._run_task(task, self.on_catalog_loaded, on_error=self.on_catalog_error)

    def on_catalog_error(self, message: str) -> None:
        self._catalog_busy = False
        self.catalog_more_button.setEnabled(True)
        self.catalog_info.setText(f"Не удалось загрузить каталог: {message[:120]}")
        self.status(f"Каталог AniLibria: {message[:120]}")

    def on_catalog_loaded(self, payload: Any) -> None:
        self._catalog_busy = False
        self.catalog_more_button.setEnabled(True)
        page, (items, total) = payload
        if not items:
            self.catalog_info.setText(
                f"В каталоге AniLibria релизов: {self._catalog_total or total}"
            )
            return

        self._catalog_page = page
        self._catalog_total = total or self._catalog_total
        start_row = len(self.catalog_items)
        self.catalog_items.extend(items)

        poster_tasks: list[tuple[int, str]] = []
        for offset, item in enumerate(items):
            row = start_row + offset
            title = item.get("title") or item.get("title_en") or "Без названия"
            parts = [str(item.get("year")) if item.get("year") else ""]
            if item.get("episodes"):
                parts.append(f"{item['episodes']} эп.")
            if item.get("rating"):
                parts.append(f"{item['rating']}")
            if item.get("genres"):
                parts.append(", ".join(item["genres"]))
            entry = QListWidgetItem(title)
            entry.setData(Qt.ItemDataRole.UserRole + 1, "  ·  ".join(p for p in parts if p))
            entry.setData(Qt.ItemDataRole.UserRole, item.get("anilibria_id"))
            entry.setToolTip(
                f"{title}\nShikimori ID: {item.get('shikimori_id') or '—'}\n"
                f"Релиз AniLibria: {item.get('anilibria_id')}"
            )
            self.catalog_list.addItem(entry)
            if item.get("poster_url"):
                poster_tasks.append((row, item["poster_url"]))

        self.catalog_info.setText(
            f"В каталоге AniLibria: {self._catalog_total} релизов · "
            f"показано {len(self.catalog_items)}"
        )
        self.catalog_more_button.setVisible(len(self.catalog_items) < self._catalog_total)

        if poster_tasks:
            loader = PosterLoader(self.client, poster_tasks)
            loader.poster_ready.connect(
                lambda row, data, l=loader: self.on_catalog_poster_ready(l, row, data)
            )
            self._catalog_loader = self._start_poster_loader(loader)

    def on_catalog_poster_ready(self, loader: "PosterLoader", row: int, data: object) -> None:
        if loader is not self._catalog_loader:
            return    # постеры от прежней страницы каталога
        if row < 0 or row >= self.catalog_list.count() or not isinstance(data, (bytes, bytearray)):
            return
        pixmap = QPixmap()
        if pixmap.loadFromData(bytes(data)):
            self._catalog_delegate.posters[row] = pixmap
            self.catalog_list.viewport().update()

    def on_catalog_selected(self, row: int) -> None:
        """Выбор релиза из каталога: подставляем его в обычный поток (серии, озвучки)."""
        if row < 0 or row >= len(self.catalog_items):
            return
        client = self.source.catalog_client() if isinstance(self.source, MultiVideoSource) else None
        if client is None:
            return
        item = self.catalog_items[row]
        anime = client.as_anime(item)
        self.current_anime = anime
        self._show_video()      # релиз аниме — значит смотрим, а не читаем
        self._load_episodes_for(anime)
        self.status(f"Из каталога: {anime['russian']} — загружаю серии…")

    def on_catalog_activated(self, _item) -> None:
        """Двойной клик по релизу — сразу включаем первую серию."""
        row = self.catalog_list.currentRow()
        if row < 0:
            return
        self.on_catalog_selected(row)
        self.current_episode = 1
        self.start_playback()

    def on_anime_selected(self, row: int) -> None:
        if row < 0 or row >= len(self.animes):
            return
        self.save_progress()  # сохраняем позицию предыдущего аниме
        anime = self.animes[row]
        # выбран аниме-тайтл — показываем плеер (иначе читалка манги скрыла бы
        # список серий и озвучки)
        self._show_video()
        self._load_episodes_for(anime)
        self.side_tabs.setCurrentIndex(1)

    def _load_episodes_for(self, anime: dict) -> None:
        """Общий путь для списка поиска и каталога: настроить источник и загрузить серии."""
        self.current_anime = anime
        self.current_episode = 1

        fallback = max(int(anime.get("episodes") or 0), int(anime.get("episodes_aired") or 0))
        title = anime.get("russian") or anime.get("title") or ""
        # источник ищет по названию, поэтому сообщаем ему текущий тайтл, романдзи и id релиза
        self._set_source_anime(anime)
        self._apply_source_dubbings()      # у нового аниме свой набор доступных озвучек
        self._refresh_progress_bars()
        self.status(f"Загружаем список серий: {title}…")

        # мгновенно показываем предполагаемое число серий, затем уточняем
        self._fill_episodes([(i, "") for i in range(1, max(fallback, 1) + 1)])

        anime_id = int(anime.get("id") or 0)

        def task() -> tuple[list[tuple[int, str]], bool]:
            """Возвращает (серии, найдены_ли_в_источнике)."""
            # 1) реальные серии источника (AniLibria — с названиями и потоком)
            try:
                episodes = self.source.get_episodes(anime_id)
            except Exception as exc:
                print(f"[источник] get_episodes: {exc}")
                episodes = None
            if episodes:
                return episodes, True
            # 2) иначе — данные Shikimori (у зеркал эндпоинт /episodes часто отсутствует)
            return self.client.get_episodes(anime_id, fallback), False

        self._run_task(task, self.on_episodes_done)

    def _set_source_anime(self, anime: dict) -> None:
        """Сообщает источникам, какой тайтл выбран (нужно для поиска по названию)."""
        anime_id = int(anime.get("id") or 0)
        title = anime.get("russian") or anime.get("title") or ""
        title_alt = anime.get("title") or ""
        year = anime.get("year")
        catalog_id = anime.get("anilibria_id")
        # событие для плагинов: выбран тайтл аниме
        self.plugin_manager.call("on_anime_selected", dict(anime))
        if isinstance(self.source, MultiVideoSource):
            self.source.set_anime(anime_id, title, title_alt, year, catalog_id)
        else:
            self.source.anime_title = title
            self.source.anime_title_alt = title_alt
            self.source.anime_year = year
            self.source.anime_catalog_id = catalog_id
        self.source.last_status = ""

    def _refresh_progress_bars(self) -> None:
        """
        Показывает в карточках, сколько серий аниме уже начато/просмотрено
        (полоса внизу карточки) — по данным progress.json.
        """
        marks: dict[int, float] = {}
        items = self.progress._items if hasattr(self.progress, "_items") else {}
        for row, anime in enumerate(self.animes):
            anime_id = str(anime.get("id"))
            watched = 0
            for key, rec in items.items():
                if key.startswith(f"{anime_id}|") and isinstance(rec, dict) and rec.get("pos", 0) > 0:
                    watched += 1
            if not watched:
                continue
            total = max(
                int(anime.get("episodes") or 0),
                int(anime.get("episodes_aired") or 0),
                watched,
            )
            marks[row] = watched / total if total else 1.0
        self._card_delegate.progress = marks
        self.anime_list.viewport().update()

    def on_episodes_done(self, payload: Any) -> None:
        episodes, from_source = payload if isinstance(payload, tuple) else (payload, True)
        note = getattr(self.source, "last_status", "")
        missing = (not from_source) and ("тайтла нет" in note)

        if missing:
            # Не показываем «мёртвые» серии: сразу видно, что тайтла нет в источнике.
            self._fill_episodes([])
            self.episode_combo.addItem("— нет в каталоге AniLibria —", None)
            self.episode_combo.setEnabled(False)
            self.current_key = None
            self.current_parts = None
            self.status(f"В каталоге AniLibria этого тайтла нет ({note}).")
            self.now_playing.setText(
                "Этого тайтла нет в каталоге AniLibria (неполный каталог источника). "
                "Выберите другое аниме, откройте файл/URL или подключите свой VideoSource."
            )
            return

        self.episode_combo.setEnabled(True)
        self._fill_episodes(episodes)
        if self.current_anime:
            title = self.current_anime.get("russian") or self.current_anime.get("title")
            tail = f" · AniLibria: {note}" if note else ""
            self.status(f"{title}: серий — {len(episodes)}{tail}.")

    def _fill_episodes(self, episodes: list[tuple[int, str]]) -> None:
        """Заполняет QComboBox эпизодов, не вызывая лишних автоплеев."""
        combo = self.episode_combo
        combo.blockSignals(True)  # чтобы не запускать воспроизведение при перезаполнении
        combo.clear()
        for num, name in episodes:
            label = f"Серия {num}" + (f" — {name}" if name else "")
            combo.addItem(label, num)
        combo.blockSignals(False)
        if combo.count():
            combo.setCurrentIndex(0)
            self.current_episode = int(combo.currentData() or 1)

    def on_episode_changed(self, _index: int) -> None:
        episode = self.episode_combo.currentData()
        if episode is None:
            return
        self.save_progress()                      # сохраняем позицию прошлого эпизода
        self.current_episode = int(episode)
        self.start_playback(auto=True)

    def on_dubbing_changed(self, index: int) -> None:
        if index < 0:
            return
        self.save_progress()                      # сохраняем позицию прошлой озвучки
        text = self.dubbing_combo.currentText()
        if text.startswith("Дорожка:"):   # звуковая дорожка внутри текущего потока
            self._select_audio_track(text)
            return
        self.current_dubbing = text.replace(DUBBING_UNAVAILABLE, "").strip()
        available = self._available_dubbings()
        if self.current_dubbing not in available:
            # честно говорим, почему не играем, вместо обращения к сети
            self.status(
                f"Озвучки «{self.current_dubbing}» нет ни в источнике, ни в ваших ссылках. "
                f"Работают: {', '.join(available) or '—'}. "
                f"Можно добавить ссылку кнопкой «＋ ссылка»."
            )
            self.now_playing.setText(
                f"«{self.current_dubbing}» пока недоступна. Нажмите «＋ ссылка» и вставьте ссылку "
                "на поток для этой серии — озвучка сразу станет рабочей."
            )
            self.current_key = None
            self.current_parts = None
            # сразу предлагаем добавить ссылку — это единственный способ включить студию
            answer = QMessageBox.question(
                self,
                "Озвучка недоступна",
                f"«{self.current_dubbing}» не отдаётся источником AniLibria.\n\n"
                "Добавить ссылку на поток для этой серии прямо сейчас?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self.on_add_link()
            return
        self.start_playback(auto=True)

    # ---------- озвучки ----------
    def _available_dubbings(self) -> list[str]:
        """
        Что реально может играть прямо сейчас: возможности источника
        (AniLibria отдаёт только свою озвучку) + озвучки, для которых пользователь
        добавил ссылку вручную для текущего аниме.
        """
        available = list(self.source.available_dubbings())
        if isinstance(self.source, MultiVideoSource) and self.current_anime:
            anime_id = self.current_anime.get("id")
            title = self.current_anime.get("russian") or self.current_anime.get("title") or ""
            alt = self.current_anime.get("title") or ""
            manual = self.source.manual()
            if manual is not None:
                for dubbing in manual.dubbings_for(anime_id):
                    if dubbing not in available:
                        available.append(dubbing)
            library = self.source.library()
            if library is not None:
                for dubbing in library.dubbings_for(anime_id, title, alt):
                    if dubbing not in available:
                        available.append(dubbing)
        return available

    def _apply_source_dubbings(self) -> None:
        """
        В список озвучек попадают ТОЛЬКО рабочие варианты: та, что отдаёт источник
        (AniLibria), плюс озвучки из ваших ссылок, плейлистов, файлов библиотеки и
        плагинов. Недоступные студии в списке не показываются — их можно подключить
        кнопкой «＋ ссылка» (там же выбирается или вводится название студии).
        """
        available = self._available_dubbings()
        preferred = self.current_dubbing
        combo = self.dubbing_combo

        combo.blockSignals(True)
        combo.clear()
        for name in available:
            combo.addItem(name)
        if not available:
            combo.addItem("— нет доступных озвучек —")

        target = None
        for i in range(combo.count()):
            if combo.itemText(i) == preferred:
                target = i
                break
        if target is None and available:
            target = 0
        if target is not None:
            combo.setCurrentIndex(target)
        combo.blockSignals(False)

        self.current_dubbing = combo.currentText().replace(DUBBING_UNAVAILABLE, "").strip()
        self._update_dubbing_hint()

    def _update_dubbing_hint(self) -> None:
        """
        Поясняет прямо в окне, какие озвучки играют, а какие нужно «включить» ссылкой.
        Без этого список из 30 студий выглядел так, будто остальные просто сломаны.
        """
        try:
            available = self._available_dubbings()
        except Exception:
            available = []
        if not available:
            self.dubbing_hint.setText(
                "Озвучек пока нет: выберите аниме в списке слева."
            )
            return
        short = ", ".join(available[:5]) + ("…" if len(available) > 5 else "")
        extra = "" if len(available) == 1 else f"  (+{len(available) - 1} добавлено вами)"
        self.dubbing_hint.setText(
            f"Доступные озвучки: {short}{extra}.  Добавить свою — кнопка «Добавить озвучку»."
        )

    def on_dubbing_help(self) -> None:
        """Справка: почему не все студии играют и как это исправить."""
        available = self._available_dubbings()
        text = (
            "<b>Откуда берутся озвучки</b><br><br>"
            "Приложение работает с <b>официальным API AniLibria</b>. У него в API "
            "только своя звуковая дорожка — поля с выбором другой озвучки там нет, "
            "поэтому автоматически играет только <b>AniLibria</b>.<br><br>"
            "Остальные студии (Dream Cast, JAM CLUB, AniDUB, Studio Band, KANSAI, SHIZA…) "
            "распространяются другими сервисами, поэтому в списке озвучек показываются "
            "только те, что реально могут играть: AniLibria плюс всё, что вы подключили.<br><br>"
            "<b>Как добавить озвучку</b><br>"
            "Нажмите «Добавить озвучку»: выберите или введите название студии и вставьте "
            "ссылку на поток (mp4/m3u8) для текущей серии. Если в ссылке написать "
            "<code>{episode}</code>, она подойдёт для всех серий.<br><br>"
            "После этого озвучка окажется сверху списка без пометки и будет играть.<br><br>"
            "Если в вашем m3u8 несколько звуковых дорожек, они появятся в списке "
            "автоматически, с пометкой «Дорожка»."
        )
        from PySide6.QtWidgets import QMessageBox

        box = QMessageBox(self)
        box.setWindowTitle("Озвучки")
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(text)
        add_button = box.addButton("＋ Добавить ссылку", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Понятно", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is add_button:
            self.on_add_link()
        self.status(f"Сейчас играет: {', '.join(available) or '—'}")

    def on_add_link(self) -> None:
        """
        Привязывает ссылку к (аниме, серия, озвучка). После этого озвучка доступна,
        а тайтлы, которых нет в AniLibria, можно смотреть своими ссылками.
        """
        if not self.current_anime:
            self.status("Сначала выберите аниме в списке слева.")
            return
        # сначала выбираем студию: список известных названий + можно ввести своё
        catalog = list(dict.fromkeys(
            [self.current_dubbing] + [d for d in self.dubbings if d != self.current_dubbing]
        ))
        catalog = [name for name in catalog if name] or ["AniLibria"]
        dubbing, ok = QInputDialog.getItem(
            self,
            "Озвучка",
            "Для какой студии добавляем ссылку?\n(можно ввести своё название)",
            catalog,
            0,
            True,
        )
        if not ok or not dubbing.strip():
            return
        dubbing = dubbing.strip()
        url, ok = QInputDialog.getText(
            self,
            "Своя ссылка на поток",
            f"Ссылка (mp4/m3u8) для:\n{self.current_anime.get('russian') or ''}\n"
            f"серия {self.current_episode}, озвучка «{dubbing}»:\n\n"
            "Совет: {episode} в ссылке заменится номером серии.",
            text=self._clipboard_url(),
        )
        if not ok or not url.strip():
            return
        self.current_dubbing = dubbing
        manual = self.source.manual() if isinstance(self.source, MultiVideoSource) else None
        if manual is None:
            self.status("Источник ручных ссылок недоступен.")
            return
        manual.add_link(self.current_anime.get("id"), self.current_episode, dubbing, url.strip())
        self.status(f"Ссылка сохранена для «{dubbing}» — запускаю.")
        self._apply_source_dubbings()
        self.start_playback(auto=False)

    def _clipboard_url(self) -> str:
        """Если в буфере обмена лежит ссылка — подставляем её в диалог."""
        try:
            from PySide6.QtWidgets import QApplication as _QApp

            text = _QApp.clipboard().text().strip()
            if text.startswith(("http://", "https://")):
                return text
        except Exception:
            pass
        return ""

    # ------------------------------------------------------------------ воспроизведение
    def on_play_clicked(self) -> None:
        """Кнопка «Смотреть»: запускает поток, а если файл уже играет — просто снимает паузу."""
        if self.current_key is None:
            self.start_playback(auto=False)
            return
        if self.player.is_paused():
            self.player.set_pause(False)
        else:
            self.start_playback(auto=False)

    def start_playback(self, auto: bool = False) -> None:
        anime = self.current_anime
        if not anime:
            if not auto:
                self.status("Сначала выберите аниме и эпизод.")
            return
        # включаем серию — значит смотрим видео: показываем плеер вместо читалки
        self._show_video()

        anime_id = int(anime.get("id") or 0)
        episode = int(self.current_episode)
        dubbing = self.current_dubbing
        title = anime.get("russian") or anime.get("title") or str(anime_id)
        # источник ищет по названию: сообщаем ему текущий тайтл
        self._set_source_anime(anime)

        if dubbing not in self._available_dubbings():
            self.status(
                f"Озвучки «{dubbing}» нет ни в источнике, ни в ваших ссылках. "
                f"Работают: {', '.join(self._available_dubbings()) or '—'}. "
                f"Добавьте ссылку кнопкой «＋ ссылка»."
            )
            return

        self.status(f"Готовим поток: {title} — серия {episode} [{dubbing}]…")

        # токен: если пользователь успел запустить другой поток, ответ устаревшего
        # потока игнорируем (иначе он мог сбросить состояние уже играющей серии)
        self._play_token += 1
        token = self._play_token

        # Парсер может ходить в сеть, поэтому вызываем его в отдельном потоке.
        # Ошибку сети возвращаем как текст, а не исключением: модальные окна здесь
        # мешают (пользователь как раз смотрит видео).
        def task() -> tuple[Optional[str], str]:
            try:
                return self.source.get_stream_url(anime_id, episode, dubbing), ""
            except Exception as exc:
                return None, f"{type(exc).__name__}: {exc}"

        def on_ok(payload: Any) -> None:
            if token != self._play_token:
                return   # устаревший ответ
            url, error = payload if isinstance(payload, tuple) else (payload, "")
            if error and not url:
                self.status(f"Источник не ответил: {error[:160]}. Попробуйте ещё раз.")
                self.now_playing.setText(
                    f"Ошибка получения потока: {error[:160]}. Нажмите «Смотреть» повторно."
                )
                self.current_key = None
                self.current_parts = None
                return
            if not url:
                note = getattr(self.source, "last_status", "")
                detail = f" ({note})" if note else ""
                if "тайтла нет" in note:
                    # Каталог AniLibria ограничен: часть тайтлов там просто отсутствует.
                    self.status(f"В каталоге AniLibria этого тайтла нет{detail}.")
                    self.now_playing.setText(
                        f"«{title}» отсутствует в каталоге AniLibria — это не сбой, "
                        "у источника неполный каталог. Доступно: кнопка «Файл…» "
                        "или «Открыть URL…», либо подключите свой VideoSource (Kodik/Sibnet)."
                    )
                else:
                    self.status(
                        f"Источник «{getattr(self.source, 'label', self.source.name)}» не нашёл серию {episode} "
                        f"в озвучке {dubbing}{detail}."
                    )
                    self.now_playing.setText(
                        f"В источнике {getattr(self.source, 'label', self.source.name)} этой серии нет{detail}. "
                        "Для этой озвучки добавьте свою ссылку кнопкой «＋ ссылка» "
                        "(или подключите свой VideoSource)."
                    )
                self.current_key = None
                self.current_parts = None
                return

            # Позиция для продолжения просмотра: максимум из progress.json и истории
            saved = max(
                self.progress.get(anime_id, episode, dubbing),
                self.history.position_for(anime_id, episode, dubbing),
            )
            start_at = 0.0
            if saved > 5.0:
                start_at = saved

            self.current_parts = (anime_id, episode, dubbing)
            self.current_key = self.progress.key(anime_id, episode, dubbing)
            self.current_label = f"{title} — серия {episode} [{dubbing}]"
            self.now_playing.setText(self.current_label)
            # попадает в историю просмотров
            self.history.touch(anime, episode, dubbing, saved, 0.0)
            self._history_dirty = True
            if self.side_tabs.currentIndex() == 1:
                self.refresh_history()
            self.player.play_url(url, start_at=start_at)
            # событие для плагинов: началось воспроизведение серии
            self.plugin_manager.call(
                "on_playback_started", dict(anime), episode, dubbing, url
            )
            if start_at:
                self.status(f"Продолжаем с {fmt_time(start_at)}")
            self._auto_load_subtitles(url)

        self._run_task(task, on_ok)

    def _auto_load_subtitles(self, url: str) -> None:
        """
        Для локальных файлов подключает субтитры, лежащие рядом
        («серия 02.mkv» + «серия 02.ru.srt»). Русские версии — первыми.
        """
        if not url or url.startswith(("http://", "https://")):
            return
        for path in find_subtitles(url):
            if self.player.add_subtitle(path):
                self.status(f"Субтитры подключены: {Path(path).name}")
                return

    def on_open_subtitles(self) -> None:
        """Ручное подключение файла субтитров к текущему видео."""
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Файл субтитров",
            "",
            "Субтитры (*.srt *.ass *.ssa *.vtt *.sub);;Все файлы (*.*)",
        )
        if not path:
            return
        if self.player.add_subtitle(path):
            self.status(f"Субтитры подключены: {Path(path).name}")
            tracks = self.player.subtitle_tracks()
            if tracks:
                self.status(f"Субтитры подключены ({len(tracks)} дорожек): {Path(path).name}")

    def on_pause_clicked(self) -> None:
        self.player.toggle_pause()

    def on_file_started(self) -> None:
        self.pause_button.setIcon(self._standard_icon("SP_MediaPause"))
        # у потока может быть несколько звуковых дорожек — покажем их в списке озвучек
        QTimer.singleShot(1200, self._update_audio_tracks)

    def _update_audio_tracks(self) -> None:
        """
        Если у загруженного потока несколько звуковых дорожек (бывает у чужих
        m3u8-плейлистов с мультиозвучкой), добавляем их в список озвучек с пометкой
        «Дорожка».
        Переключение такой «озвучки» меняет дорожку в mpv, без новой загрузки файла.
        """
        player = self.player.raw_player()
        if player is None:
            return
        try:
            tracks = player.track_list or []
        except Exception:
            return
        audio = [t for t in tracks if t.get("type") == "audio"]
        # убираем ранее добавленные строки дорожек
        for i in range(self.dubbing_combo.count() - 1, -1, -1):
            if self.dubbing_combo.itemText(i).startswith("Дорожка:"):
                self.dubbing_combo.removeItem(i)
        if len(audio) < 2:
            return
        self.dubbing_combo.blockSignals(True)
        for track in audio:
            title = track.get("title") or track.get("lang") or f"дорожка {track.get('id')}"
            self.dubbing_combo.addItem(f"Дорожка: {title}")
        self.dubbing_combo.blockSignals(False)
        self.status(f"В потоке {len(audio)} звуковых дорожек — они добавлены в список озвучек.")

    def _select_audio_track(self, text: str) -> None:
        """Переключение звуковой дорожки внутри текущего файла (пункты «Дорожка …»)."""
        player = self.player.raw_player()
        if player is None:
            return
        wanted = text.replace("Дорожка:", "", 1).strip()
        try:
            for track in player.track_list or []:
                if track.get("type") != "audio":
                    continue
                title = track.get("title") or track.get("lang") or f"дорожка {track.get('id')}"
                if title == wanted:
                    player.aid = track.get("id")
                    self.status(f"Звуковая дорожка: {title}")
                    return
        except Exception as exc:
            self.status(f"Не удалось переключить дорожку: {exc}")

    def on_player_pause(self, paused: bool) -> None:
        self.pause_button.setIcon(
            self._standard_icon("SP_MediaPlay" if paused else "SP_MediaPause")
        )
        self.pause_button.setToolTip("Продолжить (Space)" if paused else "Пауза (Space)")
        if paused:
            self.save_progress()

    def on_player_position(self, pos: float, duration: float) -> None:
        self._duration = duration
        self.time_label.setText(f"{fmt_time(pos)} / {fmt_time(duration)}")
        if not self._seek_dragging and duration > 0:
            value = int(pos / duration * 1000)
            # setValue не вызовет рекурсию: sliderMoved/sliderReleased не срабатывают
            self.position_slider.blockSignals(True)
            self.position_slider.setValue(max(0, min(1000, value)))
            self.position_slider.blockSignals(False)

    def on_player_eof(self) -> None:
        self.save_progress()
        if self.player_settings.get("auto_next", True) and self.episode_combo.count() > 1:
            index = self.episode_combo.currentIndex()
            if 0 <= index < self.episode_combo.count() - 1:
                self.status("Серия закончилась — включаю следующую.")
                self.episode_combo.setCurrentIndex(index + 1)
                return
        self.status("Серия просмотрена до конца.")

    def on_next_episode(self) -> None:
        """Вручную переключить на следующую серию (N)."""
        index = self.episode_combo.currentIndex()
        if index < 0 or self.episode_combo.count() == 0:
            self.status("Сначала выберите серию.")
            return
        if index >= self.episode_combo.count() - 1:
            self.status("Это последняя серия в списке.")
            return
        self.episode_combo.setCurrentIndex(index + 1)

    def on_player_error(self, text: str) -> None:
        self.status(f"Плеер: {text}")

    def on_worker_error(self, text: str) -> None:
        """
        Ошибка фоновой задачи (поиск, получение серий). Раньше показывалось модальное
        окно — при автопоиске по мере набора это мешало (окно перекрывало работу),
        поэтому теперь сообщение идёт в статусную строку и над плеером.
        """
        self.search_button.setEnabled(True)
        self.status(f"Ошибка: {text[:160]}")
        self.now_playing.setText(f"Не удалось получить данные: {text[:160]}")

    # ------------------------------------------------------------------ перемотка
    def on_seek_pressed(self) -> None:
        self._seek_dragging = True

    def on_seek_moved(self, value: int) -> None:
        if self._duration > 0:
            preview = value / 1000.0 * self._duration
            self.time_label.setText(f"{fmt_time(preview)} / {fmt_time(self._duration)}")

    def on_seek_released(self) -> None:
        self._seek_dragging = False
        if self._duration > 0:
            target = self.position_slider.value() / 1000.0 * self._duration
            self.player.seek(target)

    # ------------------------------------------------------------------ открытие локального файла
    def on_open_file(self) -> None:
        """Позволяет проверить плеер на локальном файле, не подключая парсер."""
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Выберите видеофайл",
            "",
            "Видео (*.mp4 *.mkv *.webm *.avi *.m4v *.mov *.ts *.m3u8);;Все файлы (*.*)",
        )
        if not path:
            return
        self.save_progress()
        self.current_parts = ("local", path, "-")
        self.current_key = self.progress.key("local", path, "-")
        self.current_label = f"Локальный файл: {Path(path).name}"
        self.now_playing.setText(self.current_label)
        saved = self.progress.get("local", path, "-")
        self.player.play_url(path, start_at=saved if saved > 5 else 0.0)
        self.status("Локальный файл загружен в плеер.")

    def on_open_url(self) -> None:
        """
        Открывает поток по прямой ссылке (mp4/m3u8/любой URL, который понимает mpv).
        Нужно для проверки плеера и для ссылок, которые вернёт ваш парсер.
        """
        url, ok = QInputDialog.getText(
            self,
            "Открыть поток по URL",
            "Прямая ссылка на видео (mp4, m3u8, webm...):",
        )
        if not ok or not url.strip():
            return
        url = url.strip()
        self.save_progress()
        self.current_parts = ("url", url, "-")
        self.current_key = self.progress.key("url", url, "-")
        self.current_label = f"Поток: {urlparse(url).netloc or url}"
        self.now_playing.setText(self.current_label)
        saved = self.progress.get("url", url, "-")
        self.player.play_url(url, start_at=saved if saved > 5 else 0.0)
        self.status("Поток отправлен в плеер.")


    # ------------------------------------------------------------------ тема и настройки
    # ------------------------------------------------------------------ плагины
    def build_plugins_menu(self):
        """
        Собирает меню кнопки «Плагины»: кнопки, которые добавили плагины, плюс
        «Список плагинов…» и «Открыть папку плагинов».

        Сборка вынесена отдельным методом, чтобы меню можно было проверить,
        не показывая его (показ блокирует поток интерфейса до закрытия меню).
        """
        from PySide6.QtWidgets import QMenu

        menu = QMenu(self)
        actions = self.plugin_manager.actions()
        if actions:
            for loaded, action in actions:
                item = menu.addAction(action.title)
                item.setToolTip(action.tooltip or f"Плагин «{loaded.title}»")
                item.triggered.connect(
                    lambda _checked=False, l=loaded, a=action: self._run_plugin_action(l, a)
                )
        else:
            empty = menu.addAction("Плагины не добавили кнопок")
            empty.setEnabled(False)
        menu.addSeparator()
        list_item = menu.addAction("Список плагинов…")
        list_item.triggered.connect(self.on_plugins_list)
        folder_item = menu.addAction("Открыть папку плагинов")
        folder_item.triggered.connect(self.on_open_plugins_folder)
        return menu

    def on_plugins_clicked(self) -> None:
        """Показывает меню «Плагины» под кнопкой в шапке окна."""
        menu = self.build_plugins_menu()
        menu.exec(self.plugins_button.mapToGlobal(self.plugins_button.rect().bottomLeft()))

    def _run_plugin_action(self, loaded: "LoadedPlugin", action: PluginAction) -> None:
        """Вызывает кнопку плагина, проверяя, что нужный выбор уже сделан."""
        if action.needs == "anime" and not self.current_anime:
            self.status(f"«{action.title}»: сначала выберите аниме в списке слева.")
            return
        if action.needs == "manga" and not self.current_manga:
            self.status(f"«{action.title}»: сначала выберите мангу в списке слева.")
            return
        context = loaded.context
        if context is None:
            self.status(f"Плагин «{loaded.title}» не готов к работе.")
            return
        try:
            action.callback(context)
        except Exception as exc:  # noqa: BLE001 — плагин не должен ронять приложение
            self.plugin_manager.note_hook_error(loaded, f"кнопка «{action.title}»", exc)
            self.status(f"Плагин «{loaded.title}»: {type(exc).__name__}: {exc}")

    def on_plugins_list(self) -> None:
        PluginsDialog(self.plugin_manager, self).exec()

    def on_open_plugins_folder(self) -> None:
        try:
            PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        try:
            from PySide6.QtCore import QUrl
            from PySide6.QtGui import QDesktopServices

            QDesktopServices.openUrl(QUrl.fromLocalFile(str(PLUGINS_DIR)))
        except Exception as exc:  # noqa: BLE001
            print(f"[плагины] не удалось открыть папку: {exc}")

    def on_settings_clicked(self) -> None:
        """
        Диалог кастомизации интерфейса: изменения видны сразу (живое превью),
        но по «Отмене» возвращаются прежние значения.
        """
        backup_theme = dict(self.theme)
        backup_player = dict(self.player_settings)
        backup_manga = dict(self.manga_settings)
        dialog = SettingsDialog(
            self.theme, self.player_settings, self._apply_live_settings, self,
            manga=self.manga_settings,
        )
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        if accepted:
            theme, player, manga = dialog.result_values()
            self.theme = theme
            self.player_settings = player
            self.manga_settings = manga
            self.apply_theme()
            self.save_settings()
            self.status("Настройки сохранены.")
        else:
            self.theme = backup_theme
            self.player_settings = backup_player
            self.manga_settings = backup_manga
            self._apply_live_settings(dict(backup_theme), dict(backup_player), dict(backup_manga))
            self.status("Изменения оформления отменены.")

    def _apply_live_settings(self, theme: dict, player: dict, manga: dict) -> None:
        """Вызывается из диалога на каждое изменение — сразу перерисовываем окно."""
        self.theme = dict(theme)
        self.player_settings = dict(player)
        if manga:
            self.manga_settings = dict(manga)
        self.apply_theme()
        self.volume_slider.setValue(int(self.player_settings.get("volume", 80)))
        # читалка подхватывает режим, масштаб, отступ и кэш сразу
        self._apply_manga_settings()

    def apply_theme(self) -> None:
        """Применяет текущую тему ко всему окну и ко всем спискам (аниме, история, манга)."""
        self._card_delegate.apply_theme(self.theme)
        self._history_delegate.apply_theme(self.theme)
        self._catalog_delegate.apply_theme(self.theme)
        self._manga_delegate.apply_theme(self.theme)
        self.reader.apply_theme(self.theme)
        self._apply_decor()
        self.setStyleSheet(build_qss(self.theme))
        self.anime_list.viewport().update()
        self.history_list.viewport().update()
        self.catalog_list.viewport().update()
        self.manga_list.viewport().update()

    def save_settings(self) -> None:
        """Сохраняет тему, настройки плеера, чтения манги и размеры окна в settings.json."""
        self.settings["theme"] = dict(self.theme)
        self.settings["player"] = dict(self.player_settings)
        self.settings["manga"] = dict(self.manga_settings)
        size = self._normal_size if (self._fullscreen and self._normal_size) else self.size()
        self.settings["window"] = {
            "width": size.width(),
            "height": size.height(),
            "last_query": self.search_edit.text().strip(),
        }
        try:
            SETTINGS_FILE.write_text(
                json.dumps(self.settings, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass

    # ------------------------------------------------------------------ полный экран
    def toggle_fullscreen(self) -> None:
        """F / кнопка «Полный экран» — плеер на весь экран, Esc — вернуться."""
        if self._fullscreen:
            self.showNormal()
            if self._normal_size is not None:
                self.resize(self._normal_size)
            self.side_tabs.show()
            self.statusBar().show()
            self._fullscreen = False
        else:
            self._normal_size = self.size()
            self.side_tabs.hide()
            self.statusBar().hide()
            self.showFullScreen()
            self._fullscreen = True
            self.status("Полный экран: F — выйти, Esc — выйти, Space — пауза.")

    # ------------------------------------------------------------------ горячие клавиши
    def _setup_shortcuts(self) -> None:
        """
        Горячие клавиши. Часть из них работает по-разному в зависимости от того,
        что открыто в правой панели: плеер (видео) или читалка манги. Например,
        Space ставит видео на паузу, а в манге — листает на экран вперёд.
        """
        from PySide6.QtGui import QKeySequence, QShortcut

        step = int(self.player_settings.get("seek_step", 10))
        bindings = [
            ("Space", self.on_space_key),
            ("Left", lambda: self.on_arrow_key(-step)),
            ("Right", lambda: self.on_arrow_key(step)),
            ("Shift+Left", lambda: self.on_arrow_key(-step * 6)),
            ("Shift+Right", lambda: self.on_arrow_key(step * 6)),
            ("Up", lambda: self.on_volume_key(5)),
            ("Down", lambda: self.on_volume_key(-5)),
            ("PageDown", lambda: self.on_screen_key(1)),
            ("PageUp", lambda: self.on_screen_key(-1)),
            ("F", self.toggle_fullscreen),
            ("F11", self.toggle_fullscreen),
            ("Esc", self._exit_fullscreen),
            ("N", self.on_next_key),
            ("M", self.on_manga_mode_key),
            ("W", self.on_manga_fit_key),
            ("+", lambda: self.on_manga_zoom_key(1.25)),
            ("=", lambda: self.on_manga_zoom_key(1.25)),
            ("-", lambda: self.on_manga_zoom_key(0.8)),
            ("0", lambda: self.on_manga_zoom_key(0.0)),
            ("Ctrl+F", lambda: (self.side_tabs.setCurrentIndex(1),
                                self.search_edit.setFocus())),
            ("Ctrl+H", lambda: self.side_tabs.setCurrentIndex(2)),
            ("Ctrl+S", lambda: self.side_tabs.setCurrentIndex(1)),
            ("Ctrl+T", self.on_settings_clicked),
            ("Ctrl+1", lambda: self.side_tabs.setCurrentIndex(0)),
            ("Ctrl+2", lambda: self.side_tabs.setCurrentIndex(1)),
            ("Ctrl+3", lambda: self.side_tabs.setCurrentIndex(2)),
            ("Ctrl+4", self.on_manga_tab_shortcut),
        ]
        self._shortcuts = []
        for keys, handler in bindings:
            shortcut = QShortcut(QKeySequence(keys), self)
            shortcut.activated.connect(handler)
            self._shortcuts.append(shortcut)

    # ---------- обработчики клавиш (с учётом того, что открыто) ----------
    def on_space_key(self) -> None:
        if self._reader_active():
            self.reader.next_screen()
        else:
            self.player.toggle_pause()

    def on_arrow_key(self, delta: int) -> None:
        if self._reader_active():
            if delta < 0:
                self.reader.goto_prev_page()
            else:
                self.reader.goto_next_page()
            return
        self.seek_relative(delta)

    def on_volume_key(self, delta: int) -> None:
        if self._reader_active():
            self.reader.scroll_by(-delta * 30)   # вверх/вниз — прокрутка текста
            return
        self.volume_delta(delta)

    def on_screen_key(self, direction: int) -> None:
        if not self._reader_active():
            return
        if direction > 0:
            self.reader.next_screen()
        else:
            self.reader.prev_screen()

    def on_next_key(self) -> None:
        if self._reader_active():
            self.open_chapter_relative(1)
        else:
            self.on_next_episode()

    def on_manga_mode_key(self) -> None:
        if self._reader_active():
            self.on_manga_mode_clicked()

    def on_manga_fit_key(self) -> None:
        if self._reader_active():
            self.on_manga_fit_clicked()

    def on_manga_zoom_key(self, factor: float) -> None:
        if not self._reader_active():
            return
        if factor <= 0:
            self.reader.reset_zoom()
            self.status("Масштаб страниц: 100%")
            return
        self.manga_zoom(factor)

    def on_manga_tab_shortcut(self) -> None:
        """Ctrl+4 — вкладка манги и фокус в поле поиска."""
        self.side_tabs.setCurrentIndex(3)
        self.manga_search_edit.setFocus()

    def _exit_fullscreen(self) -> None:
        if self._fullscreen:
            self.toggle_fullscreen()

    def seek_relative(self, seconds: int) -> None:
        target = self.player.current_position() + seconds
        self.player.seek(max(0.0, target))
        self.status(f"Перемотка: {fmt_time(max(0.0, target))}")

    def volume_delta(self, delta: int) -> None:
        value = max(0, min(100, self.volume_slider.value() + delta))
        self.volume_slider.setValue(value)
        self.player_settings["volume"] = value
        self.status(f"Громкость: {value}%")

    # ------------------------------------------------------------------ история просмотров
    def refresh_history(self) -> None:
        """Перерисовывает вкладку «История» по данным history.json."""
        self._history_dirty = False
        selected = self.history_list.currentRow()      # сохраняем выделение
        self._history_rows = list(self.history.items)
        self.history_list.clear()
        self._history_delegate.posters.clear()
        self._history_delegate.progress.clear()

        for row, entry in enumerate(self._history_rows):
            title = entry.get("title") or "Без названия"
            episode = int(entry.get("episode") or 0)
            dubbing = entry.get("dubbing") or ""
            position = float(entry.get("position") or 0.0)
            duration = float(entry.get("duration") or 0.0)

            parts = [f"серия {episode}", dubbing]
            if position:
                parts.append(
                    f"{fmt_time(position)} / {fmt_time(duration)}" if duration else fmt_time(position)
                )
            item = QListWidgetItem(title)
            item.setData(Qt.ItemDataRole.UserRole + 1, "  ·  ".join(p for p in parts if p))
            item.setData(Qt.ItemDataRole.UserRole + 2, time_ago(entry.get("updated", 0)))
            item.setData(Qt.ItemDataRole.UserRole, entry.get("anime_id"))
            self.history_list.addItem(item)
            if duration > 0:
                self._history_delegate.progress[row] = min(1.0, position / duration)

        # выделение не сбрасываем — иначе кнопки «Убрать»/«Забыть»
        # перестают работать сразу после обновления списка
        if 0 <= selected < self.history_list.count():
            self.history_list.setCurrentRow(selected)

        # постеры для истории берём из того же дискового кэша, что и поиск.
        # прежние загрузчики останавливаем, чтобы не плодить потоки
        for loader in list(self._live_loaders):
            try:
                loader.cancel()
            except Exception:
                pass
        tasks = [
            (row, entry.get("anime", {}).get("poster_url"))
            for row, entry in enumerate(self._history_rows[:30])   # не грузим сотни сразу
            if entry.get("anime", {}).get("poster_url")
        ]
        if tasks:
            loader = PosterLoader(self.client, tasks)
            loader.poster_ready.connect(
                lambda row, data, l=loader: self.on_history_poster_ready(l, row, data)
            )
            self._history_poster_loader = self._start_poster_loader(loader)

        if hasattr(self, "continue_button"):
            self._update_continue_button()
        self._update_app_subtitle()

    def on_side_tab_changed(self, index: int) -> None:
        """
        При переходе на вкладку «История» обновляем её (только если есть изменения),
        а на вкладку «Манга» — полосы прогресса чтения.
        """
        if index == 2 and self._history_dirty:
            self.refresh_history()
        if index == 3:
            # каталог манги грузим при первом открытии вкладки: держать его
            # в памяти с самого запуска незачем, а «готовый список» нужен сразу
            if self._manga_mode == "catalog" and not self.manga_items and not self._manga_catalog_busy:
                if getattr(self.manga_source, "supports_catalog", False):
                    self.load_manga_catalog(reset=True)
            if self._manga_progress_changed:
                self._manga_progress_changed = False
                self._refresh_manga_progress_bars()
                self._update_app_subtitle()

    def on_history_poster_ready(self, loader: "PosterLoader", row: int, data: object) -> None:
        if loader is not self._history_poster_loader:
            return    # постеры от прежнего списка истории
        if row < 0 or row >= self.history_list.count() or not isinstance(data, (bytes, bytearray)):
            return
        pixmap = QPixmap()
        if pixmap.loadFromData(bytes(data)):
            self._history_delegate.posters[row] = pixmap
            self.history_list.viewport().update()

    def on_history_play(self) -> None:
        """Продолжить просмотр записи из истории (двойной клик или кнопка)."""
        row = self.history_list.currentRow()
        if row < 0 or row >= len(self._history_rows):
            self.status("Выберите запись в истории.")
            return
        entry = self._history_rows[row]
        anime = entry.get("anime") or {}
        if not anime.get("id"):
            self.status("В записи нет данных об аниме.")
            return

        self.save_progress()
        self.current_anime = anime
        self._set_source_anime(anime)

        # список серий: восстановим по снимку (число серий из Shikimori)
        total = max(int(anime.get("episodes") or 0), int(anime.get("episodes_aired") or 0), 1)
        self._fill_episodes([(i, "") for i in range(1, total + 1)])
        target = int(entry.get("episode") or 1)
        index = self.episode_combo.findData(target)
        if index >= 0:
            self.episode_combo.blockSignals(True)
            self.episode_combo.setCurrentIndex(index)
            self.episode_combo.blockSignals(False)
        self.current_episode = target

        dubbing = entry.get("dubbing") or self.current_dubbing
        self._apply_source_dubbings()
        for i in range(self.dubbing_combo.count()):
            if self.dubbing_combo.itemText(i).startswith(dubbing):
                self.dubbing_combo.blockSignals(True)
                self.dubbing_combo.setCurrentIndex(i)
                self.dubbing_combo.blockSignals(False)
                break
        self.current_dubbing = dubbing

        self.side_tabs.setCurrentIndex(2)     # остаёмся на вкладке истории
        self.status(f"Продолжаем: {entry.get('title')} — серия {target} [{dubbing}]")
        self.start_playback(auto=True)

    def _update_continue_button(self) -> None:
        """Показывает кнопку «Продолжить», если в истории есть что продолжить."""
        items = self.history.items
        if not items:
            self.continue_button.setVisible(False)
            return
        entry = items[0]
        title = entry.get("title") or "без названия"
        episode = entry.get("episode")
        dubbing = entry.get("dubbing") or ""
        position = float(entry.get("position") or 0)
        tail = f" · {fmt_time(position)}" if position else ""
        # надпись обрезаем по фактической ширине шрифта: у QPushButton нет
        # автоматического многоточия, длинный тайтл вылезал за кнопку
        prefix = "Продолжить: "
        suffix = f" — серия {episode}{tail}"
        available = 250 - self.continue_button.fontMetrics().horizontalAdvance(prefix + suffix)
        short_title = self.continue_button.fontMetrics().elidedText(
            str(title), Qt.TextElideMode.ElideRight, max(60, available)
        )
        self.continue_button.setText(f"{prefix}{short_title}{suffix}")
        self.continue_button.setMaximumWidth(320)
        self.continue_button.setToolTip(
            f"{title}\nсерия {episode} · {dubbing}{tail}\n\nНажмите, чтобы продолжить"
        )
        self.continue_button.setVisible(True)

    def on_continue_last(self) -> None:
        """Продолжает самую свежую запись истории одним кликом."""
        if not self.history.items:
            self.status("История пуста.")
            return
        self.side_tabs.setCurrentIndex(2)     # вкладка истории
        if self._history_dirty:
            self.refresh_history()
        self.history_list.setCurrentRow(0)
        self.on_history_play()

    def on_history_forget(self) -> None:
        """Сбрасывает сохранённую позицию у выбранной записи истории."""
        row = self.history_list.currentRow()
        if row < 0 or row >= len(self._history_rows):
            self.status("Выберите запись в истории.")
            return
        entry = self._history_rows[row]
        anime_id = entry.get("anime_id")
        episode = entry.get("episode")
        dubbing = entry.get("dubbing") or ""
        entry["position"] = 0.0
        self.progress.forget(anime_id, episode, dubbing)
        self.history.save()
        self.refresh_history()
        self._update_continue_button()
        self.status(f"Прогресс сброшен: {entry.get('title')} — серия {episode}")

    def on_history_remove(self) -> None:
        row = self.history_list.currentRow()
        if row < 0:
            self.status("Выберите запись в истории.")
            return
        title = self._history_rows[row].get("title")
        self.history.remove(row)
        self.refresh_history()
        self.status(f"Запись удалена: {title}")

    def on_history_clear(self) -> None:
        if not self.history.items:
            self.status("История уже пуста.")
            return
        self.history.clear()
        self.refresh_history()
        self.status("История просмотров очищена.")

    # ------------------------------------------------------------------ импорт ссылок пачкой
    def on_choose_library(self) -> None:
        """
        Выбирает папку с вашими видеофайлами. Озвучки определяются по именам файлов
        («[Dream Cast]», «(JAM CLUB)», «AniDUB» и т.п.) и появляются в списке озвучек.
        """
        library = self.source.library() if isinstance(self.source, MultiVideoSource) else None
        if library is None:
            self.status("Источник локальной библиотеки недоступен.")
            return
        start_dir = str(library.root) if library.ready else ""
        folder = QFileDialog.getExistingDirectory(self, "Папка с аниме на диске", start_dir)
        if folder:
            library.set_root(folder)
            self.settings.setdefault("library", {})["path"] = folder
            self.save_settings()
            self.status(f"Сканирую библиотеку: {folder}…")
            self._run_task(lambda: library.scan(force=True), self.on_library_scanned)
        elif library.ready:
            self.status("Сканирую библиотеку заново…")
            self._run_task(lambda: library.scan(force=True), self.on_library_scanned)

    def on_library_scanned(self, files: Any) -> None:
        library = self.source.library() if isinstance(self.source, MultiVideoSource) else None
        count = len(files or [])
        dubbings: list[str] = []
        for item in files or []:
            if item.get("dubbing") and item["dubbing"] not in dubbings:
                dubbings.append(item["dubbing"])
        note = f", озвучки в файлах: {', '.join(dubbings[:6])}" if dubbings else ""
        self.status(f"В библиотеке найдено файлов: {count}{note}")
        self._apply_source_dubbings()
        self.refresh_history()
        if library is not None and library.root:
            self._update_app_subtitle(extra=f"библиотека: {count} файлов")

    def on_import_playlist(self) -> None:
        """
        Импорт плейлиста: название записи разбирается так же, как имя файла
        («… серия 2 [JAM CLUB]»), поэтому серии и озвучки подхватываются сами.
        Если озвучка в плейлисте не указана, берётся выбранная в списке.
        """
        if not self.current_anime:
            self.status("Сначала выберите аниме в списке слева.")
            return
        manual = self.source.manual() if isinstance(self.source, MultiVideoSource) else None
        if manual is None:
            self.status("Источник ручных ссылок недоступен.")
            return

        path, _ = QFileDialog.getOpenFileName(
            self,
            "Плейлист с сериями",
            "",
            "Плейлисты (*.m3u *.m3u8 *.json);;Все файлы (*.*)",
        )
        if not path:
            return
        entries = parse_playlist(Path(path))
        if not entries:
            self.status("В плейлисте не нашлось ссылок (нужны строки с http/https).")
            return

        anime_id = self.current_anime.get("id")
        fallback_dubbing = self.dubbing_combo.currentText().replace(DUBBING_UNAVAILABLE, "").strip()
        added = 0
        dubbings: list[str] = []
        for index, (entry_title, url) in enumerate(entries, start=1):
            # притворяемся именем файла: парсер достанет номер серии и озвучку
            parsed = LocalLibrarySource._parse_name(f"{entry_title}.mkv", self.dubbings)
            episode = parsed.get("episode") or index
            dubbing = parsed.get("dubbing") or fallback_dubbing
            if "{episode}" in url or "{ep}" in url:
                manual.add_template(anime_id, dubbing, url)
            else:
                manual.add_link(anime_id, episode, dubbing, url)
            added += 1
            if dubbing not in dubbings:
                dubbings.append(dubbing)

        self.status(
            f"Импортировано записей: {added}. Озвучки: {', '.join(dubbings[:8])}"
            + ("…" if len(dubbings) > 8 else "")
        )
        self._apply_source_dubbings()

    def on_import_links(self) -> None:
        """
        Позволяет включить сразу несколько озвучек: строки «Озвучка | ссылка с {episode}»
        сохраняются как шаблоны для выбранного аниме (все серии сразу).
        """
        if not self.current_anime:
            self.status("Сначала выберите аниме в списке слева.")
            return
        manual = self.source.manual() if isinstance(self.source, MultiVideoSource) else None
        if manual is None:
            self.status("Источник ручных ссылок недоступен.")
            return
        title = self.current_anime.get("russian") or self.current_anime.get("title") or ""
        dialog = LinkImportDialog(title, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        parsed = dialog.parse()
        if not parsed:
            self.status("Не понял ни одной строки: нужен формат «Озвучка | https://…».")
            return
        anime_id = self.current_anime.get("id")
        for dubbing, url in parsed:
            if "{episode}" in url or "{ep}" in url:
                manual.add_template(anime_id, dubbing, url)
            else:
                manual.add_link(anime_id, self.current_episode, dubbing, url)
        self.status(f"Добавлено озвучек: {len(parsed)} — они появятся сверху списка.")
        self._apply_source_dubbings()
        self.refresh_history()

    # ------------------------------------------------------------------ манга (MangaDex)
    def _apply_manga_settings(self) -> None:
        """Применяет настройки чтения к читалке и к элементам интерфейса."""
        self.manga_source.cache_pages_to_disk = bool(
            self.manga_settings.get("disk_cache", DEFAULT_MANGA["disk_cache"])
        )
        self.reader.apply_reader_settings(dict(self.manga_settings))
        self.reader.strip.apply_theme(self.theme)
        # язык перевода есть не у всех источников (плагин может его не различать)
        self.manga_lang_combo.setVisible(bool(self.manga_source.multi_language))
        index = self.manga_lang_combo.findData(str(self.manga_settings.get("lang", "ru")))
        if index >= 0:
            self.manga_lang_combo.blockSignals(True)
            self.manga_lang_combo.setCurrentIndex(index)
            self.manga_lang_combo.blockSignals(False)
        self.manga_mode_button.setText(
            "Лента" if str(self.manga_settings.get("mode")) == "single" else "Постранично"
        )
        self.manga_fit_button.setText(
            MANGA_FIT_SHORT.get(str(self.manga_settings.get("fit")), "Ширина")
        )

    def _fill_manga_catalog_controls(self) -> None:
        """
        Настраивает элементы каталога манги под текущий источник: список сортировок
        и доступность переключателей (у источника может не быть каталога вовсе).
        """
        if not hasattr(self, "manga_sort_combo"):
            return
        source = self.manga_source
        supports = bool(getattr(source, "supports_catalog", False))
        sorts = dict(getattr(source, "catalog_sorts", {}) or {})
        saved = str(self.manga_settings.get("catalog_sort") or "popular")

        self.manga_sort_combo.blockSignals(True)
        self.manga_sort_combo.clear()
        for code, title in (sorts or {"popular": "Популярные"}).items():
            self.manga_sort_combo.addItem(title, code)
        index = self.manga_sort_combo.findData(saved)
        self.manga_sort_combo.setCurrentIndex(index if index >= 0 else 0)
        self.manga_sort_combo.blockSignals(False)

        self.manga_sort_combo.setEnabled(supports)
        self.manga_ru_check.setEnabled(supports)
        self.manga_catalog_button.setEnabled(supports)
        self.manga_ru_check.setChecked(bool(self.manga_settings.get("catalog_only_ru", False)))
        if not supports:
            self.manga_sort_combo.setToolTip("У этого источника нет каталога — пользуйтесь поиском")
            self.manga_info.setText(
                f"Источник {source.label} не даёт каталог: введите название и нажмите «Найти»."
            )

    def load_manga_catalog(self, reset: bool = False) -> None:
        """
        Загружает страницу каталога манги (все доступные тайтлы источника).

        Каталог листается страницами: «Показать ещё» догружает следующую.
        MangaDex сообщает общее число тайтлов, поэтому в подписи видно, сколько
        всего и сколько уже показано.
        """
        if not getattr(self.manga_source, "supports_catalog", False):
            self.status(f"Источник {self.manga_source.label} каталог не поддерживает.")
            return
        if self._manga_catalog_busy:
            return

        self._manga_mode = "catalog"
        if reset:
            self._manga_catalog_offset = 0
            self._manga_catalog_total = 0
            self._manga_exhausted = False
        self._manga_catalog_busy = True
        self.manga_catalog_button.setEnabled(False)
        self.manga_more_button.setEnabled(False)
        self.manga_info.setText("Загружаю каталог манги…")

        source = self.manga_source
        offset = self._manga_catalog_offset
        sort = str(self.manga_sort_combo.currentData() or "popular")
        only_russian = self.manga_ru_check.isChecked()
        limit = MANGA_CATALOG_PAGE

        def task() -> tuple[list[dict], int, float]:
            started = time.perf_counter()
            items, total = source.catalog(
                offset=offset, limit=limit, sort=sort, only_russian=only_russian
            )
            return items, total, time.perf_counter() - started

        self._manga_token += 1
        token = self._manga_token
        self._manga_more_pending = not reset
        self._run_task(
            task,
            lambda payload: self.on_manga_catalog_loaded(payload, token),
            on_error=self.on_manga_catalog_error,
        )

    def on_manga_catalog_loaded(self, payload: Any, token: int = 0) -> None:
        self._manga_catalog_busy = False
        self.manga_catalog_button.setEnabled(True)
        self.manga_more_button.setEnabled(True)
        if token and token != self._manga_token:
            return    # ответ устарел: каталог уже переключили или начали поиск

        items, total, elapsed = payload if isinstance(payload, tuple) else (payload, 0, 0.0)
        items = list(items or [])
        source_code = self.manga_source.name
        for item in items:
            item["source"] = source_code
        self._manga_catalog_total = int(total or 0)

        if not self._manga_more_pending:
            self.manga_items = []
            self.manga_list.clear()
            self._manga_delegate.posters.clear()
            self._manga_delegate.progress.clear()
            self._cancel_manga_poster_loader()

        known = {item.get("id") for item in self.manga_items}
        fresh = [item for item in items if item.get("id") not in known]
        if fresh:
            poster_tasks = self._add_manga_rows(len(self.manga_items), fresh)
            self.manga_items.extend(fresh)
            self._start_manga_poster_loader(poster_tasks)
        self._manga_catalog_offset += len(items)

        if not self.manga_items:
            self.manga_info.setText(
                f"В каталоге {self.manga_source.label} ничего не нашлось — попробуйте другую сортировку."
            )
            return

        self._refresh_manga_progress_bars()
        shown = len(self.manga_items)
        left = max(0, self._manga_catalog_total - shown)
        self._manga_exhausted = left <= 0
        self.manga_more_button.setVisible(left > 0)
        tail = f" · осталось {left}" if left else " · это всё"
        self.manga_info.setText(
            f"В каталоге {self.manga_source.label}: {self._manga_catalog_total} тайтлов · "
            f"показано {shown}{tail} · {elapsed:.1f} с"
        )
        self.status(f"Каталог манги: показано {shown} из {self._manga_catalog_total}.")

    def on_manga_catalog_error(self, message: str) -> None:
        self._manga_catalog_busy = False
        self.manga_catalog_button.setEnabled(True)
        self.manga_more_button.setEnabled(True)
        self.manga_info.setText(f"Каталог манги не загрузился: {message[:160]}")
        self.status(f"Каталог манги: {message[:140]}")

    def on_manga_catalog_sort_changed(self, _index: int) -> None:
        code = str(self.manga_sort_combo.currentData() or "popular")
        self.manga_settings["catalog_sort"] = code
        self.save_settings()
        self.load_manga_catalog(reset=True)

    def on_manga_only_russian_toggled(self, checked: bool) -> None:
        self.manga_settings["catalog_only_ru"] = bool(checked)
        self.save_settings()
        self.load_manga_catalog(reset=True)

    def on_manga_catalog_clicked(self) -> None:
        """Возврат к каталогу из режима поиска (очищает строку поиска)."""
        self._manga_debounce.stop()
        self.manga_search_edit.blockSignals(True)
        self.manga_search_edit.clear()
        self.manga_search_edit.blockSignals(False)
        self.manga_more_button.setVisible(True)
        self.load_manga_catalog(reset=True)

    def on_manga_more(self) -> None:
        """«Показать ещё»: в каталоге — следующая страница, в поиске — расширение выдачи."""
        if self._manga_mode == "catalog":
            self.load_manga_catalog(reset=False)
        else:
            self.on_manga_search(more=True)

    def on_manga_search_text_changed(self, text: str) -> None:
        """Автопоиск манги по мере ввода (та же пауза, что и у аниме)."""
        self._manga_debounce.stop()
        if len(text.strip()) >= MIN_AUTOSEARCH_LEN:
            self._manga_debounce.start()

    def on_manga_source_changed(self, _index: int) -> None:
        """
        Смена источника манги: сбрасываем список и читалку, чтобы не показывать
        тайтлы и страницы одного источника в интерфейсе другого.
        """
        code = str(self.manga_source_combo.currentData() or "")
        if code and code != self.manga_source.name:
            self.manga_source = self._pick_manga_source(code)
            self.manga_settings["source"] = self.manga_source.name
            self.save_settings()
            self._reset_reader_state()
            self.current_manga = None
            self.manga_items = []
            self.manga_list.clear()
            self._manga_delegate.posters.clear()
            self._manga_delegate.progress.clear()
            self._cancel_manga_poster_loader()
            self._manga_mode = "catalog"
            self._manga_catalog_offset = 0
            self._manga_catalog_total = 0
            self._fill_manga_catalog_controls()
            if getattr(self.manga_source, "supports_catalog", False):
                # у нового источника свой каталог — показываем его сразу
                self.load_manga_catalog(reset=True)
            else:
                self.manga_info.setText(
                    f"Источник {self.manga_source.label} не даёт каталог: "
                    "введите название и нажмите «Найти»."
                )
        # от языка перевода зависит только источник, который их различает
        self.manga_lang_combo.setVisible(bool(self.manga_source.multi_language))
        self.manga_hint.setText(
            f"Источник: {self.manga_source.label}. "
            + ("Поиск можно вести по-русски — название уточнится через Shikimori."
               if self.manga_source.multi_language else "Введите название и выберите тайтл.")
        )

    @staticmethod
    def _has_cyrillic(text: str) -> bool:
        return any("а" <= ch.lower() <= "я" or ch.lower() == "ё" for ch in (text or ""))

    def _manga_search(self, query: str) -> tuple[list[dict], str]:
        """
        Поиск манги в MangaDex.

        Тонкость: в MangaDex названия в основном на латинице, поэтому русский запрос
        («Мастера меча онлайн») там ничего не находит. В этом случае название сначала
        уточняется через Shikimori (у него есть русские названия), а затем ищется уже
        латиница. Найденным тайтлам подставляется русское название — так список
        выглядит привычно.
        """
        langs = [str(self.manga_settings.get("lang", "ru"))] if self.manga_source.multi_language else []
        try:
            items = self.manga_source.search(query, limit=self._manga_limit, langs=langs)
        except Exception as exc:
            return [], f"{self.manga_source.label} не ответил: {exc}"

        if items or not self._has_cyrillic(query):
            return items, ""

        merged: dict[str, dict] = {}
        for entry in self.client.search_mangas(query, limit=5):
            for title in (entry.get("name"), entry.get("english"), entry.get("japanese")):
                title = (title or "").strip()
                if len(title) < 3:
                    continue
                try:
                    found = self.manga_source.search(title, limit=8, langs=langs)
                except Exception:
                    continue
                for item in found:
                    if item.get("id") in merged:
                        continue
                    item = dict(item)
                    russian = self._match_manga_title(item, entry)
                    if russian:
                        item["russian"] = russian
                        item["title"] = russian
                        item["shikimori"] = entry
                    merged[item["id"]] = item
            if len(merged) >= self._manga_limit:
                break

        note = ""
        if merged:
            note = "название уточнено через Shikimori — в MangaDex оно на латинице"
        return list(merged.values())[: self._manga_limit], note

    @staticmethod
    def _match_manga_title(item: dict, entry: dict) -> str:
        """
        Русское название из Shikimori подставляем только при совпадении названий:
        иначе похожие тайтлы получат чужое имя (а это хуже, чем латиница).
        """
        russian = (entry.get("russian") or "").strip()
        if not russian:
            return ""
        ours = [item.get("title"), item.get("title_alt")]
        theirs = [entry.get("name"), entry.get("english"), entry.get("japanese")]
        best = 0.0
        for left in ours:
            for right in theirs:
                if left and right:
                    best = max(best, _title_similarity(left, right))
        return russian if best >= 0.6 else ""

    def on_manga_search(self, more: bool = False) -> None:
        """Поиск манги; more=True — «Показать ещё» (расширяем лимит выдачи)."""
        query = self.manga_search_edit.text().strip()
        if not query:
            self.status("Введите название манги.")
            return
        self._manga_debounce.stop()
        self._manga_mode = "search"
        if more:
            if self._manga_exhausted:
                self.status("Больше результатов нет — уточните название.")
                return
            self._manga_limit += MANGA_SEARCH_LIMIT
        else:
            self._manga_limit = MANGA_SEARCH_LIMIT
            self._manga_exhausted = False
        self._manga_more_pending = bool(more)
        self.manga_search_button.setEnabled(False)
        self._manga_token += 1
        token = self._manga_token
        self.manga_info.setText(f"Ищу «{query}» в MangaDex…")
        self.status(f"Ищем мангу: {query}…")

        def task() -> tuple[list[dict], str, float]:
            started = time.perf_counter()
            items, note = self._manga_search(query)
            return items, note, time.perf_counter() - started

        self._run_task(task, lambda payload: self.on_manga_search_done(payload, token))

    def on_manga_search_done(self, payload: Any, token: int = 0) -> None:
        self.manga_search_button.setEnabled(True)
        if token and token != self._manga_token:
            return    # ответ на устаревший запрос
        items, note, elapsed = payload if isinstance(payload, tuple) else (payload, "", 0.0)
        items = list(items or [])
        # помечаем, из какого источника пришёл каждый тайтл: по этому коду прогресс
        # чтения не путается между источниками
        source_code = self.manga_source.name
        for item in items:
            item["source"] = source_code
        # если MangaDex отдал меньше, чем просили, — результатов больше нет
        self._manga_exhausted = len(items) < self._manga_limit

        if self._manga_more_pending:
            # «Показать ещё»: дописываем только то, чего ещё нет в списке
            known = {item.get("id") for item in self.manga_items}
            fresh = [item for item in items if item.get("id") not in known]
        else:
            # новый запрос: список полностью заменяется, иначе смешались бы
            # результаты разных поисков
            fresh = items
            self.manga_items = []
            self.manga_list.clear()
            self._manga_delegate.posters.clear()
            self._manga_delegate.progress.clear()
            self._cancel_manga_poster_loader()

        if fresh:
            poster_tasks = self._add_manga_rows(len(self.manga_items), fresh)
            self.manga_items.extend(fresh)
            self._start_manga_poster_loader(poster_tasks)

        if not self.manga_items:
            self.manga_info.setText(
                f"Ничего не найдено ({elapsed:.2f} с). Попробуйте другое название — "
                "можно и по-русски: название уточнится через Shikimori."
            )
            self.status("Манга не найдена.")
            return

        self._refresh_manga_progress_bars()
        tail = f" · {note}" if note else ""
        more_hint = "" if self._manga_exhausted else " · «Показать ещё» — дальше"
        self.manga_more_button.setVisible(not self._manga_exhausted)
        self.manga_info.setText(
            f"Найдено: {len(self.manga_items)} тайтлов за {elapsed:.2f} с{tail}{more_hint}"
        )
        self.status(f"Манга: найдено {len(self.manga_items)} тайтлов ({elapsed:.2f} с).")

    def _add_manga_rows(self, start_row: int, items: list[dict]) -> list[tuple[int, str]]:
        """
        Добавляет карточки тайтлов в список начиная со строки start_row.
        Возвращает задачи для загрузчика постеров: [(строка, ссылка), ...].
        """
        poster_tasks: list[tuple[int, str]] = []
        for offset, item in enumerate(items):
            row = start_row + offset
            title = item.get("title") or "Без названия"
            parts = []
            if item.get("year"):
                parts.append(str(item["year"]))
            if item.get("status"):
                parts.append(str(item["status"]))
            if item.get("last_chapter"):
                parts.append(f"гл. {item['last_chapter']}")
            langs = [code for code in (item.get("langs") or []) if code][:4]
            if langs:
                parts.append("перевод: " + ", ".join(code.upper() for code in langs))

            list_item = QListWidgetItem(title)
            list_item.setData(Qt.ItemDataRole.UserRole + 1, "  ·  ".join(parts) or "без описания")
            alt = item.get("title_alt") or ""
            list_item.setData(Qt.ItemDataRole.UserRole + 2, alt if alt and alt != title else "")
            list_item.setData(Qt.ItemDataRole.UserRole, item.get("id"))
            description = (item.get("description") or "").replace("\n", " ")
            list_item.setToolTip(f"{title}\n{alt}\n\n{description[:400]}")
            self.manga_list.addItem(list_item)
            if item.get("poster_url"):
                poster_tasks.append((row, item["poster_url"]))
        return poster_tasks

    def _start_manga_poster_loader(self, tasks: list[tuple[int, str]]) -> None:
        """
        Загружает обложки манги. Загрузчик держим в отдельном списке: отмена
        загрузчиков постеров аниме не должна обрывать загрузку обложек манги.
        Ответы старого загрузчика отбрасываются по ссылке.
        """
        if not tasks:
            return
        loader = PosterLoader(self.manga_source, tasks)
        loader.poster_ready.connect(
            lambda row, data, l=loader: self._on_manga_poster_ready(l, row, data)
        )
        loader.finished_all.connect(lambda l=loader: self._drop_manga_poster_loader(l))
        self._manga_poster_loader = loader
        self._manga_poster_loaders.append(loader)
        loader.start()

    def _drop_manga_poster_loader(self, loader: "PosterLoader") -> None:
        if loader in self._manga_poster_loaders:
            self._manga_poster_loaders.remove(loader)
        if self._manga_poster_loader is loader:
            self._manga_poster_loader = None

    def _cancel_manga_poster_loader(self) -> None:
        loader = self._manga_poster_loader
        self._manga_poster_loader = None
        if loader is None:
            return
        try:
            loader.cancel()
        except Exception:  # noqa: BLE001
            pass

    def _on_manga_poster_ready(self, loader: "PosterLoader", row: int, data: object) -> None:
        # обложка от прежнего поиска: строки в новом списке означают другие тайтлы
        if loader is not self._manga_poster_loader:
            return
        if row < 0 or row >= self.manga_list.count() or not isinstance(data, (bytes, bytearray)):
            return
        pixmap = QPixmap()
        if pixmap.loadFromData(bytes(data)):
            self._manga_delegate.posters[row] = pixmap
            self.manga_list.viewport().update()

    @staticmethod
    def _chapter_number(value: Any) -> float:
        try:
            return float(str(value).strip())
        except (TypeError, ValueError):
            return 0.0

    def _refresh_manga_progress_bars(self) -> None:
        """Полоса в карточках манги: какая доля глав уже прочитана."""
        marks: dict[int, float] = {}
        for row, item in enumerate(self.manga_items):
            entry = self.manga_progress.get(item.get("id"), item.get("source"))
            if not entry:
                continue
            read = self._chapter_number(entry.get("chapter"))
            total = self._chapter_number(item.get("last_chapter"))
            marks[row] = min(1.0, read / total) if (read and total > 0) else 0.05
        self._manga_delegate.progress = marks
        self.manga_list.viewport().update()

    # ---------- выбор тайтла и глав ----------
    def _reset_reader_state(self) -> None:
        """
        Подготовка к загрузке другого тайтла, главы или языка перевода.

        Сначала сохраняем позицию текущего места, затем полностью убираем прошлую
        главу из читалки. Без этого между переключением тайтла и загрузкой новых глав
        читалка ещё держала бы старые страницы, и автосохранение успело бы записать
        чужую страницу в новую запись прогресса.
        """
        self.save_manga_progress()
        self._stop_manga_loader()
        # ответы уже запущенных запросов главы больше не нужны: иначе «запоздавший»
        # ответ прошлой главы наполнил бы читалку без текущей главы
        self._manga_chapter_token += 1
        self.current_chapter = None
        self.manga_chapters = []
        self.chapter_combo.blockSignals(True)
        self.chapter_combo.clear()
        self.chapter_combo.blockSignals(False)
        self.reader.clear()
        self.on_manga_page_changed(0, 0)     # ползунок и подпись страниц — в исходное

    def on_manga_selected(self, row: int) -> None:
        """Выбор тайтла в списке: загружаем главы и открываем читалку."""
        if row < 0 or row >= len(self.manga_items):
            return
        item = self.manga_items[row]
        if self.current_manga and self.current_manga.get("id") == item.get("id") and self.manga_chapters:
            return    # уже открыт — не перезагружаем списки
        self._reset_reader_state()
        self.current_manga = item
        self._show_reader()
        self.manga_title.setText(f"{item.get('title')} — загружаю список глав…")
        self._load_manga_chapters(item, restore=self.manga_progress.get(item.get("id"), item.get("source")))

    def on_manga_activated(self, _item) -> None:
        """Двойной клик по тайтлу — то же, что выбор: главы и чтение."""
        self.on_manga_selected(self.manga_list.currentRow())

    def _load_manga_chapters(self, manga: dict, restore: Optional[dict] = None) -> None:
        """
        Загружает главы. Язык берём из настроек, а если включён «запас» — доливаем
        английские главы для тех номеров, которых нет на приоритетном языке.
        """
        lang = str(self.manga_settings.get("lang", "ru"))
        langs = [lang]
        if self.manga_settings.get("fallback_en", True) and lang != "en":
            langs.append("en")
        self._manga_chapter_token += 1
        token = self._manga_chapter_token

        def task() -> list[dict]:
            return self.manga_source.chapters(manga.get("id"), tuple(langs))

        def on_ok(chapters: Any) -> None:
            if token != self._manga_chapter_token:
                return    # ответ устарел: пользователь выбрал другой тайтл
            self.manga_chapters = list(chapters or [])
            self._fill_chapters(restore)

        def on_error(message: str) -> None:
            if token != self._manga_chapter_token:
                return
            self.manga_chapters = []
            self.manga_title.setText(f"Главы не загрузились: {message[:200]}")
            self.status(f"Главы MangaDex: {message[:160]}")

        self._run_task(task, on_ok, on_error=on_error)

    @staticmethod
    def _chapter_label(chapter: dict) -> str:
        """Подпись главы в списке: номер, название, команда перевода, язык, страницы."""
        head = f"Глава {chapter.get('chapter') or '?'}"
        if chapter.get("title"):
            head = f"{head} — {chapter['title']}"
        tail = []
        if chapter.get("group"):
            tail.append(chapter["group"])
        if chapter.get("lang_label"):
            tail.append(chapter["lang_label"])
        if chapter.get("pages"):
            tail.append(f"{chapter['pages']} стр.")
        return f"{head}  ·  {', '.join(tail)}" if tail else head

    def _fill_chapters(self, restore: Optional[dict] = None) -> None:
        combo = self.chapter_combo
        combo.blockSignals(True)
        combo.clear()
        for index, chapter in enumerate(self.manga_chapters):
            combo.addItem(self._chapter_label(chapter), index)
        if not self.manga_chapters:
            combo.blockSignals(False)
            self.manga_title.setText("У этого тайтла нет глав на выбранном языке перевода.")
            self.manga_hint.setText(
                "Попробуйте другой язык в списке «Язык» (например, Английский) "
                "или включите запас английских глав в «Оформлении»."
            )
            return

        target = 0
        if restore:
            for index, chapter in enumerate(self.manga_chapters):
                if str(chapter.get("id")) == str(restore.get("chapter_id")):
                    target = index
                    break
            else:
                number = str(restore.get("chapter") or "")
                for index, chapter in enumerate(self.manga_chapters):
                    if chapter.get("chapter") == number:
                        target = index
                        break
        else:
            # Начинаем с первой читаемой главы: на MangaDex встречаются пустые
            # «главы 0» и прологи без картинок, открывать их по умолчанию незачем
            # (число страниц источник сообщает заранее). Сначала ищем такую главу
            # на выбранном языке перевода, и только потом — на любом.
            preferred = str(self.manga_settings.get("lang", "ru"))
            target = next(
                (index for index, chapter in enumerate(self.manga_chapters)
                 if int(chapter.get("pages") or 0) > 0 and chapter.get("lang") == preferred),
                None,
            )
            if target is None:
                target = next(
                    (index for index, chapter in enumerate(self.manga_chapters)
                     if int(chapter.get("pages") or 0) > 0),
                    0,
                )
        combo.setCurrentIndex(target)
        combo.blockSignals(False)

        # У лицензированных тайтлов главы в API есть, а картинок в них нет: страницы
        # остаются только в приложении издателя. Честно говорим об этом сразу,
        # а не после открытия пустой главы.
        if not any(int(chapter.get("pages") or 0) > 0 for chapter in self.manga_chapters):
            self.manga_hint.setText(
                "У этого тайтла на MangaDex нет страниц: главы доступны только на сайте "
                "издателя. Кнопка «Браузер» откроет главу в браузере."
            )
        self._open_chapter(target, restore=restore)

    def on_chapter_changed(self, index: int) -> None:
        """Смена главы в списке — сразу открываем её страницы."""
        if 0 <= index < len(self.manga_chapters):
            self._open_chapter(index)

    def on_manga_lang_changed(self, _index: int) -> None:
        code = str(self.manga_lang_combo.currentData() or "ru")
        if code == self.manga_settings.get("lang"):
            return
        self.manga_settings["lang"] = code
        self.save_settings()
        if not self.current_manga:
            return
        self.status(f"Язык перевода: {dict(MANGA_LANGS).get(code, code)} — перечитываю главы…")
        self._reset_reader_state()      # сохранит позицию в этой озвучке/языке
        self._load_manga_chapters(
            self.current_manga,
            restore=self.manga_progress.get(self.current_manga.get("id"), self.current_manga.get("source")),
        )

    def on_manga_read_clicked(self) -> None:
        """
        Кнопка «Читать»: открывает выбранную главу. Если это та же глава, что уже
        открыта, позиция чтения сохраняется — то есть кнопка работает как «обновить»
        (например, если часть страниц не загрузилась).
        """
        index = self.chapter_combo.currentIndex()
        if index < 0:
            self.status("Сначала выберите тайтл и главу.")
            return
        restore: Optional[dict] = None
        if (
            self.current_chapter
            and 0 <= index < len(self.manga_chapters)
            and str(self.manga_chapters[index].get("id")) == str(self.current_chapter.get("id"))
            and self.reader.has_chapter()
        ):
            page, offset = self.reader.state()
            restore = {"page": page, "offset": offset}
        self._open_chapter(index, restore=restore)

    def _open_chapter(self, index: int, restore: Optional[dict] = None) -> None:
        """Загружает ссылки на страницы главы и открывает её в читалке."""
        if not (0 <= index < len(self.manga_chapters)) or not self.current_manga:
            return
        self.save_manga_progress()      # прогресс предыдущей главы
        self._stop_manga_loader()
        chapter = self.manga_chapters[index]
        self.current_chapter = chapter
        self._show_reader()
        self.reader.clear()
        self.manga_title.setText(
            f"{self.current_manga.get('title')} — глава {chapter.get('chapter')} "
            f"[{chapter.get('lang_label')}], загружаю страницы…"
        )
        self._manga_chapter_token += 1
        token = self._manga_chapter_token

        def task() -> list[str]:
            return self.manga_source.pages(chapter.get("id"))

        def on_ok(urls: Any) -> None:
            if token != self._manga_chapter_token:
                return
            pages = list(urls or [])
            self.reader.set_chapter(pages)
            self._start_manga_loader()
            # событие для плагинов: открыта глава манги
            self.plugin_manager.call(
                "on_chapter_opened", dict(self.current_manga or {}), dict(chapter)
            )
            page, offset = 0, 0.0
            if restore:
                page = int(restore.get("page") or 0)
                offset = float(restore.get("offset") or 0.0)
            self.reader.goto_page(page, offset)
            group = f" · {chapter['group']}" if chapter.get("group") else ""
            if not pages:
                # На MangaDex хватает таких глав: у лицензированных тайтлов картинки
                # убраны и оставлены ссылки на приложение издателя
                self.manga_title.setText(
                    f"{self.current_manga.get('title')} — глава {chapter.get('chapter')}: "
                    "страниц нет"
                )
                self.status(
                    f"В главе {chapter.get('chapter')} нет страниц: у лицензированных "
                    "тайтлов MangaDex оставляет только ссылку на сайт издателя."
                )
                return
            self.manga_title.setText(
                f"{self.current_manga.get('title')} — глава {chapter.get('chapter')} "
                f"[{chapter.get('lang_label')}{group}] · страниц: {len(pages)}"
            )
            if restore and page:
                self.status(
                    f"Продолжаем чтение: глава {chapter.get('chapter')}, страница {page + 1}"
                )
            else:
                self.status(f"Глава {chapter.get('chapter')}: страниц — {len(pages)}")

        def on_error(message: str) -> None:
            if token != self._manga_chapter_token:
                return
            self.manga_title.setText(f"Страницы главы не загрузились: {message[:200]}")
            self.status(f"MangaDex: {message[:160]}")

        self._run_task(task, on_ok, on_error=on_error)

    def open_chapter_relative(self, delta: int) -> None:
        """Следующая/предыдущая глава (кнопки и клавиша N)."""
        if not self.manga_chapters or self.current_chapter is None:
            self.status("Сначала выберите главу.")
            return
        current_id = str(self.current_chapter.get("id"))
        index = next(
            (i for i, c in enumerate(self.manga_chapters) if str(c.get("id")) == current_id), -1
        )
        if index < 0:
            return
        target = index + delta
        if target < 0:
            self.status("Это первая глава.")
            return
        if target >= len(self.manga_chapters):
            self.status("Это последняя глава тайтла.")
            return
        self.chapter_combo.blockSignals(True)
        self.chapter_combo.setCurrentIndex(target)
        self.chapter_combo.blockSignals(False)
        self._open_chapter(target)

    # ---------- загрузка страниц ----------
    def _start_manga_loader(self) -> None:
        """
        Запускает поток загрузки страниц. Потоки страниц держим отдельным списком
        (а не в _live_loaders): отмена загрузчиков постеров не должна останавливать
        скачивание страниц главы.

        Ответы принимаем только от текущего загрузчика: у прошлой главы число страниц
        могло быть больше, и её «запоздавшие» страницы иначе попали бы в новую главу
        на те же номера.
        """
        if self._manga_loader is not None and self._manga_loader.isRunning():
            return
        loader = MangaPageLoader(self.manga_source)
        loader.page_ready.connect(
            lambda index, data, l=loader: self._on_manga_page_ready(l, index, data)
        )
        loader.page_failed.connect(
            lambda index, reason, l=loader: self._on_manga_page_failed(l, index, reason)
        )
        loader.finished_all.connect(lambda l=loader: self._drop_manga_loader(l))
        self._manga_loader = loader
        self._manga_loaders.append(loader)
        loader.start()

    def _on_manga_page_ready(self, loader: "MangaPageLoader", index: int, data: object) -> None:
        if loader is not self._manga_loader:
            return    # страница из главы, которую уже закрыли
        self.reader.page_ready(index, data)

    def _on_manga_page_failed(self, loader: "MangaPageLoader", index: int, reason: str) -> None:
        if loader is not self._manga_loader:
            return
        self.reader.page_failed(index, reason)

    def _drop_manga_loader(self, loader: MangaPageLoader) -> None:
        if loader in self._manga_loaders:
            self._manga_loaders.remove(loader)
        if self._manga_loader is loader:
            self._manga_loader = None

    def _stop_manga_loader(self) -> None:
        """Останавливает загрузку страниц прошлой главы (её ссылки больше не нужны)."""
        loader = self._manga_loader
        self._manga_loader = None
        if loader is None:
            return
        try:
            loader.cancel()
        except Exception:  # noqa: BLE001
            pass

    def on_manga_need_page(self, index: int, url: str) -> None:
        """Читалка просит страницу — ставим её в очередь загрузчика."""
        loader = self._manga_loader
        if loader is None:
            self._start_manga_loader()
            loader = self._manga_loader
        if loader is None:
            return
        loader.submit(index, url)
        # если MangaDex@Home недоступен и включился обходной хост — скажем об этом один раз
        note = self.manga_source.last_note
        if note and note != self._manga_host_note:
            self._manga_host_note = note
            self.manga_hint.setText(f"{note}. Если страницы всё равно не грузятся — «Браузер».")

    def on_manga_page_changed(self, page: int, total: int) -> None:
        """Обновляет ползунок и подпись страниц (в том числе для пустой главы)."""
        if total > 0:
            self.page_label.setText(f"стр. {page + 1} / {total}")
        else:
            self.page_label.setText("стр. — / —")
        self.page_slider.blockSignals(True)
        self.page_slider.setRange(0, max(0, total - 1))
        self.page_slider.setValue(max(0, page))
        self.page_slider.setEnabled(total > 1)
        self.page_slider.blockSignals(False)
        if self.current_chapter and self.reader.has_chapter():
            self._manga_save_debounce.start()   # позицию запишем чуть позже

    # ---------- управление чтением ----------
    def _show_reader(self) -> None:
        """Показывает читалку вместо плеера и ставит видео на паузу."""
        if self.player.is_available() and not self.player.is_paused():
            self.player.set_pause(True)
        if self.right_stack.currentIndex() != 1:
            self.right_stack.setCurrentIndex(1)

    def _show_video(self) -> None:
        if self.right_stack.currentIndex() != 0:
            self.right_stack.setCurrentIndex(0)

    def _reader_active(self) -> bool:
        """Открыта ли читалка (от этого зависят горячие клавиши)."""
        return self.right_stack.currentIndex() == 1 and self.reader.has_chapter()

    def on_manga_mode_clicked(self) -> None:
        single = str(self.manga_settings.get("mode")) != "single"
        self.manga_settings["mode"] = "single" if single else "strip"
        self.reader.set_single(single)
        self.manga_mode_button.setText("Лента" if single else "Постранично")
        self.save_settings()
        self.status("Режим чтения: постранично" if single else "Режим чтения: лента")

    def on_manga_fit_clicked(self) -> None:
        label = self.reader.toggle_fit()
        self.manga_settings["fit"] = self.reader.settings.get("fit", "width")
        self.manga_fit_button.setText(
            MANGA_FIT_SHORT.get(str(self.manga_settings["fit"]), "Ширина")
        )
        self.save_settings()
        self.status(f"Масштаб страниц: {label}")

    def manga_zoom(self, factor: float) -> None:
        zoom = self.reader.zoom_by(factor)
        self.manga_fit_button.setText(MANGA_FIT_SHORT["none"])
        self.manga_settings["fit"] = "none"
        self.save_settings()
        self.status(f"Масштаб: {zoom * 100:.0f}%")

    def on_manga_open_in_browser(self) -> None:
        """Открывает главу на mangadex.org — запасной путь, если CDN недоступен."""
        if not self.current_chapter:
            self.status("Сначала выберите главу.")
            return
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        QDesktopServices.openUrl(QUrl(f"https://mangadex.org/chapter/{self.current_chapter.get('id')}"))

    def on_manga_reading_list(self) -> None:
        """Список «Читаю»: манга с сохранённым прогрессом."""
        self.save_manga_progress()
        dialog = MangaReadingDialog(self.manga_progress, self, theme=self.theme)
        dialog.exec()
        entry = dialog.selected_entry
        self._manga_progress_changed = True
        self._refresh_manga_progress_bars()
        self._update_app_subtitle()
        if entry and entry.get("manga"):
            self._continue_manga(entry)

    def _continue_manga(self, entry: dict) -> None:
        """Продолжает чтение записи: тайтл, глава, страница, язык."""
        manga = dict(entry.get("manga") or {})
        if not manga.get("id"):
            self.status("В записи нет данных о манге.")
            return
        self._reset_reader_state()   # сохраняем позицию текущего места и очищаем читалку
        # запись могла быть сделана в другом источнике манги — переключаемся на него
        entry_source = str(manga.get("source") or "")
        if entry_source and entry_source != self.manga_source.name:
            self.manga_source = self._pick_manga_source(entry_source)
            index = self.manga_source_combo.findData(self.manga_source.name)
            if index >= 0:
                self.manga_source_combo.blockSignals(True)
                self.manga_source_combo.setCurrentIndex(index)
                self.manga_source_combo.blockSignals(False)
        self.current_manga = manga
        restore = dict(entry)
        self.manga_settings["lang"] = str(entry.get("lang") or self.manga_settings.get("lang", "ru"))
        index = self.manga_lang_combo.findData(str(self.manga_settings["lang"]))
        if index >= 0:
            self.manga_lang_combo.blockSignals(True)
            self.manga_lang_combo.setCurrentIndex(index)
            self.manga_lang_combo.blockSignals(False)
        self.side_tabs.setCurrentIndex(3)
        self.manga_hint.setText("Продолжаем чтение…")
        self._load_manga_chapters(manga, restore=restore)

    # ---------- прогресс чтения ----------
    def save_manga_progress(self) -> None:
        """Сохраняет главу и страницу, чтобы вернуться к чтению с того же места."""
        if not self.current_manga or not self.current_chapter or not self.reader.has_chapter():
            return
        page, offset = self.reader.state()
        self.manga_progress.touch(
            self.current_manga,
            self.current_chapter.get("id"),
            self.current_chapter.get("chapter"),
            page,
            offset,
            self.current_chapter.get("lang") or "",
        )
        self._manga_progress_changed = True
        self._update_app_subtitle()   # в шапке видно, сколько тайтлов читается

    def _update_app_subtitle(self, extra: str = "") -> None:
        """
        Строка под названием приложения.

        В самой строке — только короткая суть (она узкая, длинный текст обрезался бы),
        а полные счётчики прячутся в подсказку, которая видна при наведении.
        """
        if not hasattr(self, "app_subtitle"):
            return
        host = self.client.active_host.replace("https://", "")
        self.app_subtitle.setText(f"Shikimori: {host} · аниме и манга")
        details = [
            f"Зеркало Shikimori: {host}",
            "Источник видео: AniLibria",
            f"Озвучек в списке: {len(self.dubbings)}",
            f"Записей в истории: {len(self.history.items)}",
            f"Манга: {self.manga_source.label}, в процессе чтения: "
            f"{len(self.manga_progress.items)}",
            f"Плагины: {self.plugin_manager.summary()}",
        ]
        if extra:
            details.append(extra)
        self.app_subtitle.setToolTip("\n".join(details))

    # ------------------------------------------------------------------ прогресс
    def save_progress(self) -> None:
        """Сохраняет текущую секунду просмотра выбранного аниме/эпизода/озвучки в JSON."""
        if not self.current_key or not self.current_parts:
            return
        pos = self.player.current_position()
        dur = self.player.current_duration()
        if pos <= 0:
            return
        # Если до конца осталось меньше 15 секунд — считаем эпизод просмотренным.
        if dur > 0 and dur - pos < 15:
            pos = 0.0
        anime_id, episode, dubbing = self.current_parts
        self.progress.set(anime_id, episode, dubbing, pos)
        self.progress.save()
        if self.current_anime:
            self.history.touch(self.current_anime, episode, dubbing, pos, dur)
            self._history_dirty = True   # вкладку «История» обновим при переходе в неё
        self._refresh_progress_bars()

    # ------------------------------------------------------------------ закрытие
    def closeEvent(self, event) -> None:  # noqa: N802 (Qt-стиль)
        self.save_progress()
        self.save_manga_progress()
        # плагины могут сохранить свои данные
        self.plugin_manager.call("on_closing")
        self.plugin_store.save()
        self.player_settings["volume"] = self.volume_slider.value()
        self.save_settings()
        self._save_timer.stop()
        self._debounce.stop()
        self._manga_debounce.stop()
        self._manga_save_debounce.stop()
        self._cancel_poster_loaders()
        self._cancel_manga_poster_loader()
        self._stop_manga_loader()
        for loader in list(self._live_loaders) + list(self._manga_poster_loaders):
            try:
                if loader.isRunning():
                    loader.wait(2000)
            except Exception:
                pass
        self._live_loaders.clear()
        self._manga_poster_loaders.clear()
        # потоки страниц могут ждать медленную ноду — при выходе не ждём их,
        # но обязательно завершаем: иначе объект QThread умрёт «на ходу»
        for loader in list(self._manga_loaders):
            try:
                if loader.isRunning() and not loader.wait(2000):
                    loader.terminate()
                    loader.wait(500)
            except Exception:
                pass
        self._manga_loaders.clear()
        for worker in list(self._workers):
            try:
                if worker.isRunning() and not worker.wait(2000):
                    worker.terminate()
                    worker.wait(500)
            except Exception:
                pass
        self._workers.clear()
        try:
            self.player.shutdown()
        except Exception:
            pass
        super().closeEvent(event)


# ======================================================================================
# Точка входа
# ======================================================================================
def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Anime Viewer")
    app.setOrganizationName("AnimeViewer")
    icon = load_app_icon()
    if not icon.isNull():
        app.setWindowIcon(icon)          # иконка для всех окон и панели задач

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

