# -*- coding: utf-8 -*-
"""
Единое хранилище токенов (SSO) для Anime Viewer — модуль рядом с main.py.

Задача: у приложения несколько площадок (Shikimori сегодня, AniList и MyAnimeList —
дальше), и каждая из них выдаёт свои токены. Раньше токены Shikimori лежали в
auth.json, привязанные к одному сервису: подключить вторую площадку означало бы
второй такой же файл и второй вход.

Теперь токены всех сервисов лежат в одном TokenStore:

    store = TokenStore()                  # tokens.json рядом с программой
    store.set("shikimori", access_token="…", refresh_token="…", expires_at=…)
    store.access_token("shikimori")        # готовый токен в любой момент
    store.authorized("shikimori")          # True — логин не нужен

Где именно хранятся токены — решает сам модуль, по порядку:

  1. **Windows Credential Manager** (пакет `keyring`), если он установлен.
     Внимание: Windows хранит пароль записи в 2560 байт, а JWT-токены бывают
     длиннее; тогда `keyring` падает с `WinError 1783 (ERROR_INSUFFICIENT_BUFFER)`.
     Такие значения НЕ пишутся в keyring: они уходят в файл, а keyring остаётся
     для коротких токенов (OAuth-токены AniList/MAL обычно короче).
  2. **Файл tokens.json** рядом с программой — резервный путь в любом случае.

Права: файл создаётся атомарно (временный файл + replace), в нём только токены и
сведения о пользователе, никаких паролей. В состав портативной сборки tokens.json
не попадает (см. tools/build_release.py).

Потокобезопасность: токен запрашивают и фоновая задача (синхронизация прогресса),
и интерфейс (кнопки «В список»/«Оценить»), поэтому все операции под RLock.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional

# Сервисы, которые приложение знает «из коробки». Ключ — код сервиса (он же
# используется в настройках и в tokens.json), значение — подпись для интерфейса.
SERVICES: dict[str, str] = {
    "shikimori": "Shikimori",
    "anilist": "AniList",
    "mal": "MyAnimeList",
}

# Параметры OAuth площадок. Shikimori отличается тем, что client_id/secret задаёт
# сам пользователь (см. shikimori_oauth.py), у AniList и MAL они тоже свои, но
# адреса авторизации фиксированы — их и держим здесь, чтобы «переключение площадки»
# не требовало правок в коде.
PROVIDERS: dict[str, dict[str, Any]] = {
    "shikimori": {
        "title": "Shikimori",
        "authorize_url": "https://shikimori.one/oauth/authorize",
        "token_url": "https://shikimori.one/oauth/token",
        "scope": "",
        # зеркала: у части провайдеров основной домен недоступен
        "hosts": ["https://shikimori.one", "https://shikimori.net", "https://shikimori.org"],
        "redirect": "http://localhost:4567/callback",
    },
    "anilist": {
        "title": "AniList",
        "authorize_url": "https://anilist.co/api/v2/oauth/authorize",
        "token_url": "https://anilist.co/api/v2/oauth/token",
        "scope": "",
        "hosts": ["https://anilist.co"],
        "redirect": "http://localhost:4567/callback",
    },
    "mal": {
        "title": "MyAnimeList",
        "authorize_url": "https://myanimelist.net/v1/oauth2/authorize",
        "token_url": "https://myanimelist.net/v1/oauth2/token",
        "scope": "",
        "hosts": ["https://myanimelist.net"],
        "redirect": "http://localhost:4567/callback",
    },
}

# Больше этого размера в Windows Credential Manager не лезет: у JWT-токенов
# Shikimori (RS256, ~700–1600 символов) json-запись легко переходит предел, и
# keyring отвечает WinError 1783. Такие записи целиком уходят в файл.
KEYRING_MAX_BYTES = 1000
# Имя «службы» в хранилище Windows: по нему записи видно в «Учётные данные Windows».
KEYRING_SERVICE = "AnimeViewer"
# На сколько секунд раньше срока считать токен просроченным (разница часов, сеть).
REFRESH_MARGIN = 120

# Поля, которые TokenStore понимает осмысленно; остальные сохраняются как есть.
_TOKEN_FIELDS = ("access_token", "refresh_token", "expires_at", "token_type", "scope")


def default_token_file(base_dir: Optional[Path] = None) -> Path:
    """Путь к общему файлу токенов: tokens.json рядом с программой."""
    base = Path(base_dir) if base_dir else Path(__file__).resolve().parent
    return base / "tokens.json"


class TokenStore:
    """
    Единое хранилище токенов всех площадок.

    Формат файла (tokens.json):
        {
          "version": 1,
          "services": {
            "shikimori": {"access_token": "…", "refresh_token": "…",
                          "expires_at": 1712345678.9, "user_id": 123,
                          "nickname": "vasya", "host": "https://shikimori.one",
                          "saved_at": 1712345678.9}
          }
        }
    """

    def __init__(
        self,
        path: Optional[Path] = None,
        use_keyring: bool = True,
        keyring_service: str = KEYRING_SERVICE,
    ) -> None:
        self.path = Path(path) if path else default_token_file()
        self.keyring_service = str(keyring_service)
        self.services: dict[str, dict] = {}
        # Диагностика: почему keyring не используется (видно в логе приложения)
        self.keyring_available = False
        self.keyring_error = ""
        self._keyring: Any = None
        self._lock = threading.RLock()
        self._load_keyring() if use_keyring else None
        self.load()

    # ------------------------------------------------------------------ keyring
    def _load_keyring(self) -> None:
        """
        Подключает keyring, если он есть.

        В портативной сборке пакета keyring нет — это нормальный сценарий:
        тогда хранилищем становится файл tokens.json, и приложение работает так
        же, просто токены не шифруются средствами Windows.
        """
        try:
            import keyring  # type: ignore

            self._keyring = keyring
            self.keyring_available = True
        except Exception as exc:  # noqa: BLE001 — модуля может не быть вовсе
            self._keyring = None
            self.keyring_available = False
            self.keyring_error = f"keyring недоступен ({type(exc).__name__}) — токены в файле"

    @staticmethod
    def _is_buffer_error(exc: BaseException) -> bool:
        """
        WinError 1783 (ERROR_INSUFFICIENT_BUFFER) и родственные ошибки записи —
        признак того, что значение не влезло в хранилище Windows.
        """
        winerror = getattr(exc, "winerror", None)
        if winerror in (1783, 87, 1312):
            return True
        text = str(exc).lower()
        return "1783" in text or "insufficient buffer" in text

    def _keyring_write(self, service: str, record: dict) -> bool:
        """Пишет запись в keyring. False — «не получилось, используем файл»."""
        if self._keyring is None:
            return False
        payload = json.dumps(record, ensure_ascii=False)
        # Длинные значения не отдаём keyring вообще: Windows их не примет, а
        # исключение в фоновом потоке выглядело бы как сбой синхронизации.
        if len(payload.encode("utf-8")) > KEYRING_MAX_BYTES:
            self.keyring_error = (
                f"токен «{service}» длиннее {KEYRING_MAX_BYTES} байт — "
                "хранится в файле (ограничение Windows Credential Manager)"
            )
            return False
        try:
            self._keyring.set_password(self.keyring_service, service, payload)
            self.keyring_error = ""
            return True
        except Exception as exc:  # noqa: BLE001 — WinError 1783 и любые другие
            if self._is_buffer_error(exc):
                self.keyring_error = (
                    f"Windows не принял токен «{service}» (WinError 1783) — "
                    "храним в файле"
                )
            else:
                self.keyring_error = f"keyring: {type(exc).__name__}: {exc} — храним в файле"
            print(f"[токены] {self.keyring_error}")
            return False

    def _keyring_read(self, service: str) -> Optional[dict]:
        if self._keyring is None:
            return None
        try:
            raw = self._keyring.get_password(self.keyring_service, service)
        except Exception as exc:  # noqa: BLE001
            self.keyring_error = f"keyring: {type(exc).__name__}: {exc}"
            return None
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else None
        except ValueError:
            return None

    def _keyring_delete(self, service: str) -> None:
        if self._keyring is None:
            return
        try:
            self._keyring.delete_password(self.keyring_service, service)
        except Exception:  # noqa: BLE001 — записи могло не быть
            pass

    # ------------------------------------------------------------------ файл
    def load(self) -> None:
        """Читает файл токенов (и, если файла нет, доливает записи из keyring)."""
        data: dict = {}
        try:
            if self.path.exists():
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    data = raw
        except (OSError, ValueError):
            data = {}

        services = data.get("services") if isinstance(data, dict) else None
        with self._lock:
            self.services = {
                str(code): dict(record)
                for code, record in (services or {}).items()
                if isinstance(record, dict)
            }
            # если файла нет (или сервиса в нём нет) — пробуем keyring
            for code in SERVICES:
                if code not in self.services:
                    found = self._keyring_read(code)
                    if found:
                        self.services[code] = found

    def save(self) -> bool:
        """Атомарно сохраняет все записи в файл. False — записать не удалось."""
        with self._lock:
            payload = {
                "version": 1,
                "services": self.services,
                # подсказка для глаз: где лежат токены и почему
                "note": (
                    "токены всех площадок Anime Viewer. keyring: "
                    + ("используется" if self.keyring_available and not self.keyring_error
                       else "файл")
                ),
            }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", delete=False, dir=str(self.path.parent), suffix=".tmp"
            ) as tmp:
                json.dump(payload, tmp, ensure_ascii=False, indent=2)
                tmp_path = Path(tmp.name)
            tmp_path.replace(self.path)
            return True
        except OSError as exc:
            print(f"[токены] не удалось сохранить {self.path}: {exc}")
            return False

    # ------------------------------------------------------------------ API
    def get(self, service: str) -> dict:
        """Копия записи сервиса (пустой словарь — «токенов нет»)."""
        with self._lock:
            return dict(self.services.get(str(service)) or {})

    def set(self, service: str, **fields: Any) -> dict:
        """
        Полностью заменяет запись сервиса переданными полями.

        Побочный эффект: запись пишется в keyring (если влезает) и в файл, поэтому
        после перезапуска приложения повторный вход не нужен.
        """
        record = {key: value for key, value in fields.items() if value is not None}
        record["saved_at"] = time.time()
        with self._lock:
            self.services[str(service)] = record
        self._keyring_write(str(service), record)
        self.save()
        return dict(record)

    def update(self, service: str, **fields: Any) -> dict:
        """Дописывает поля к существующей записи (например, обновляет access_token)."""
        record = self.get(service)
        record.update({key: value for key, value in fields.items() if value is not None})
        record["saved_at"] = time.time()
        with self._lock:
            self.services[str(service)] = record
        self._keyring_write(str(service), record)
        self.save()
        return dict(record)

    def clear(self, service: str) -> None:
        """Забывает токены сервиса (кнопка «Выйти»)."""
        with self._lock:
            self.services.pop(str(service), None)
        self._keyring_delete(str(service))
        self.save()

    def clear_all(self) -> None:
        with self._lock:
            codes = list(self.services)
        for code in codes:
            self.clear(code)

    def services_list(self) -> list[str]:
        """Коды сервисов, для которых есть токены (по порядку SERVICES, затем прочие)."""
        with self._lock:
            known = [code for code in SERVICES if code in self.services]
            extra = sorted(code for code in self.services if code not in SERVICES)
        return known + extra

    def records(self) -> list[tuple[str, dict]]:
        """[(код, запись), ...] — для списка аккаунтов в диалоге."""
        return [(code, self.get(code)) for code in self.services_list()]

    # ------------------------------------------------------------------ токен
    def access_token(self, service: str) -> str:
        return str(self.get(service).get("access_token") or "")

    def refresh_token(self, service: str) -> str:
        return str(self.get(service).get("refresh_token") or "")

    def expires_at(self, service: str) -> float:
        try:
            return float(self.get(service).get("expires_at") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def is_expired(self, service: str, margin: int = REFRESH_MARGIN) -> bool:
        """Просрочен ли access_token (с запасом на разницу часов)."""
        expires = self.expires_at(service)
        if not expires:
            return False          # бессрочный токен (нет expires_in) считаем годным
        return time.time() + max(0, margin) >= expires

    def authorized(self, service: str) -> bool:
        """Есть ли токен у сервиса. Именно это проверяет «нужен ли повторный вход»."""
        return bool(self.access_token(service))

    def account_label(self, service: str) -> str:
        """Как подписать аккаунт в интерфейсе."""
        record = self.get(service)
        if not record.get("access_token"):
            return "не выполнен вход"
        title = SERVICES.get(str(service), str(service))
        nickname = str(record.get("nickname") or "")
        user_id = record.get("user_id")
        if nickname and user_id:
            return f"{title}: {nickname} (id {user_id})"
        if nickname:
            return f"{title}: {nickname}"
        if user_id:
            return f"{title}: id {user_id}"
        return f"{title}: вход выполнен"

    def expires_in(self, service: str) -> float:
        """Сколько секунд осталось до истечения (0 — бессрочный/нет токена)."""
        expires = self.expires_at(service)
        return max(0.0, expires - time.time()) if expires else 0.0

    def describe(self) -> str:
        """Короткая сводка для логов и статусной строки."""
        codes = self.services_list()
        if not codes:
            backend = "keyring" if self.keyring_available and not self.keyring_error else "файл"
            return f"токенов нет (хранилище: {backend}, {self.path.name})"
        parts = []
        for code in codes:
            left = self.expires_in(code)
            tail = f", осталось {int(left // 60)} мин" if left else ""
            parts.append(f"{SERVICES.get(code, code)}{tail}")
        return "аккаунты: " + ", ".join(parts)

    # ------------------------------------------------------------------ переход с auth.json
    def migrate_from_auth_json(self, path: Optional[Path] = None) -> bool:
        """
        Переносит токены из старого auth.json (формат Shikimori) в TokenStore.

        Вызывается один раз при первом запуске новой версии: пользователю не нужно
        входить заново, старый файл остаётся на месте (его можно удалить руками).
        Возвращает True, если что-то перенесено.
        """
        legacy = Path(path) if path else self.path.parent / "auth.json"
        if not legacy.exists():
            return False
        try:
            data = json.loads(legacy.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if not isinstance(data, dict) or not data.get("access_token"):
            return False
        if self.authorized("shikimori"):
            return False      # новые токены уже есть — старое не трогаем

        record = {field: data[field] for field in _TOKEN_FIELDS if data.get(field)}
        for extra in ("user_id", "nickname", "host"):
            if data.get(extra):
                record[extra] = data[extra]
        self.set("shikimori", **record)
        print(f"[токены] токены Shikimori перенесены из {legacy.name} в {self.path.name}")
        return True


def store_for(base_dir: Optional[Path] = None, **kwargs: Any) -> TokenStore:
    """
    Удобный конструктор для приложения: хранилище в папке приложения + перенос
    старого auth.json, если он ещё лежит рядом.
    """
    store = TokenStore(default_token_file(base_dir), **kwargs)
    if not store.authorized("shikimori"):
        store.migrate_from_auth_json(Path(base_dir) / "auth.json" if base_dir else None)
    return store
