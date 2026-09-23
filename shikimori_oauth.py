# -*- coding: utf-8 -*-
"""
Авторизация в Shikimori (OAuth2) для Anime Viewer — отдельный модуль рядом с main.py.

Как это работает:

    1. Приложение открывает в браузере страницу авторизации Shikimori:
       https://shikimori.one/oauth/authorize?client_id=..&redirect_uri=..&response_type=code
    2. Одновременно поднимается крошечный локальный сервер на 127.0.0.1:<порт>,
       который ждёт один запрос — /callback?code=...
    3. Код обменивается на токены (POST /oauth/token): access_token + refresh_token.
    4. Токены лежат в auth.json рядом с программой; access_token обновляется сам,
       когда истекает (refresh_token живёт долго).

Что нужно от пользователя один раз: завести своё OAuth-приложение на
https://shikimori.one/oauth/applications (это бесплатно, ключ выдаётся сразу) и
вписать client_id/client_secret в «Оформлении» → «Shikimori». Redirect URI в
приложении Shikimori должен совпадать с тем, что показывает приложение:
http://localhost:<порт>/callback (порт настраивается там же).

Свои ключи можно задать и переменными окружения SHIKIMORI_CLIENT_ID /
SHIKIMORI_CLIENT_SECRET — тогда они не попадут в settings.json.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlencode, urlparse

import requests

# Единое хранилище токенов (см. token_store.py): Shikimori, AniList и MyAnimeList
# складывают токены в один TokenStore, поэтому переключение площадки не требует
# повторного входа — токен уже лежит в хранилище.
from token_store import PROVIDERS, TokenStore, default_token_file

# OAuth-флоу на Qt: QOAuth2AuthorizationCodeFlow + QOAuthUriSchemeReplyHandler.
# Внимание: эти классы входят в PySide6-Addons, а не в PySide6-Essentials, поэтому
# в портативной сборке их может не быть — тогда приложение использует прежний
# локальный сервер (см. _CallbackServer) и работает точно так же.
try:  # pragma: no cover - зависит от состава PySide6
    from PySide6.QtCore import QEventLoop, QObject, QSettings, QTimer, QUrl, Signal
    from PySide6.QtNetworkAuth import (  # type: ignore
        QOAuth2AuthorizationCodeFlow,
        QOAuthHttpServerReplyHandler,
        QOAuthUriSchemeReplyHandler,
    )

    QT_OAUTH_AVAILABLE = True
    QT_OAUTH_ERROR = ""
except Exception as _qt_oauth_error:  # noqa: BLE001
    QT_OAUTH_AVAILABLE = False
    QT_OAUTH_ERROR = f"{type(_qt_oauth_error).__name__}: {_qt_oauth_error}"

    # Заглушки: класс QtOAuthFlow объявляется всегда (чтобы его можно было
    # импортировать из main.py), но без QtNetworkAuth он не используется —
    # приложение входит в аккаунт прежним способом, через локальный сервер.
    class QObject:  # type: ignore[no-redef]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

    class Signal:  # type: ignore[no-redef]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def emit(self, *args: Any, **kwargs: Any) -> None:
            pass

        def connect(self, *args: Any, **kwargs: Any) -> None:
            pass

# Зеркала Shikimori: у части провайдеров shikimori.one недоступен (как и у API).
SHIKIMORI_OAUTH_HOSTS = (
    "https://shikimori.one",
    "https://shikimori.net",
    "https://shikimori.org",
)
# На сколько секунд раньше истечения обновлять токен (запас на разницу часов).
REFRESH_MARGIN = 120
DEFAULT_PORT = 4567
REQUEST_TIMEOUT = 20


def default_auth_file(base_dir: Path) -> Path:
    """Прежний файл токенов: остаётся только для перехода на tokens.json."""
    return Path(base_dir) / "auth.json"


class QtOAuthFlow(QObject):
    """
    OAuth2-флоу на Qt (QOAuth2AuthorizationCodeFlow).

    Почему Qt, а не свой сервер: Qt сам открывает браузер, сам принимает редирект
    и сам обновляет токен по refresh_token — это меньше кода в приложении и одна
    схема для всех площадок (Shikimori, AniList, MyAnimeList).

    Как принимается ответ:
      * QOAuthUriSchemeReplyHandler — редирект на свою схему
        (`animeviewer://callback`). На Windows схему нужно один раз зарегистрировать
        в реестре; метод try_register_scheme() делает это и повторно проверяет
        готовность обработчика;
      * если схема не зарегистрирована — QOAuthHttpServerReplyHandler на localhost
        с портом из настроек (http://localhost:<порт>/callback).

    Сигналы:
        status_changed(str)      — что происходит (показывается в диалоге входа);
        tokens_ready(dict)       — получены токены (их же отдаёт wait_for_tokens);
        finished(bool, str)      — итог: успех и причина.
    """

    status_changed = Signal(str)
    tokens_ready = Signal(dict)
    finished = Signal(bool, str)

    SCHEME = "animeviewer"

    def __init__(
        self,
        provider: dict,
        client_id: str,
        client_secret: str = "",
        redirect_port: int = DEFAULT_PORT,
        host: str = "",
        parent: Any = None,
    ) -> None:
        super().__init__(parent)
        self.provider = dict(provider or {})
        self.client_id = str(client_id or "")
        self.client_secret = str(client_secret or "")
        self.redirect_port = int(redirect_port or DEFAULT_PORT)
        self.host = host or (self.provider.get("hosts") or ["https://shikimori.one"])[0]
        self.flow: Any = None
        self.handler: Any = None
        self.last_error = ""
        self.tokens: dict = {}
        self.redirect_uri = f"{self.SCHEME}://callback"

    # ------------------------------------------------------------------ подготовка
    def _build(self) -> bool:
        """Создаёт flow и обработчик редиректа. False — Qt-флоу недоступен."""
        if not QT_OAUTH_AVAILABLE:
            self.last_error = (
                "QtNetworkAuth не установлен (нужен пакет PySide6-Addons) — "
                f"используется вход через локальный сервер. {QT_OAUTH_ERROR}"
            )
            return False
        if not self.client_id:
            self.last_error = "не задан client_id"
            return False
        try:
            flow = QOAuth2AuthorizationCodeFlow(self)
            flow.setAuthorizationUrl(QUrl(self._authorize_url()))
            flow.setTokenUrl(QUrl(self._token_url()))
            flow.setClientIdentifier(self.client_id)
            if self.client_secret:
                flow.setClientIdentifierSharedKey(self.client_secret)
            scope = str(self.provider.get("scope") or "").strip()
            if scope:
                flow.setScope(scope)

            handler = self._reply_handler()
            if handler is None:
                return False
            flow.setReplyHandler(handler)

            flow.authorizeWithBrowser.connect(self._on_browser_requested)
            flow.granted.connect(self._on_granted)
            flow.error.connect(self._on_error)
            # обновление токена Qt делает сам по refresh_token
            self.flow = flow
            self.handler = handler
            return True
        except Exception as exc:  # noqa: BLE001 — Qt может ответить чем угодно
            self.last_error = f"{type(exc).__name__}: {exc}"
            return False

    def _authorize_url(self) -> str:
        """Адрес авторизации активного хоста (зеркала Shikimori отличаются доменом)."""
        url = str(self.provider.get("authorize_url") or "")
        default_host = (self.provider.get("hosts") or [""])[0]
        if url.startswith(default_host):
            return self.host + url[len(default_host):]
        return url

    def _token_url(self) -> str:
        url = str(self.provider.get("token_url") or "")
        default_host = (self.provider.get("hosts") or [""])[0]
        if url.startswith(default_host):
            return self.host + url[len(default_host):]
        return url

    def _reply_handler(self) -> Any:
        """
        Обработчик редиректа: сначала своя схема, затем локальный сервер.

        Схема удобнее (не занимает порт), но её нужно зарегистрировать в системе —
        если это не вышло, берём проверенный http://localhost:<порт>/callback.
        """
        try:
            handler = QOAuthUriSchemeReplyHandler(self)
            handler.setRedirectUrl(QUrl(self.redirect_uri))
            if not handler.isListening():
                self.try_register_scheme()
            if handler.isListening():
                print(f"[Shikimori] редирект через схему {self.redirect_uri}")
                return handler
        except Exception as exc:  # noqa: BLE001
            print(f"[Shikimori] схема {self.SCHEME} недоступна: {type(exc).__name__}: {exc}")

        try:
            port = self._free_port(self.redirect_port)
            handler = QOAuthHttpServerReplyHandler(port, self)
            self.redirect_port = port
            self.redirect_uri = f"http://localhost:{port}/callback"
            print(f"[Shikimori] редирект через локальный сервер: {self.redirect_uri}")
            return handler
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"не удалось поднять приём редиректа: {exc}"
            print(f"[Shikimori] {self.last_error}")
            return None

    def try_register_scheme(self) -> bool:
        """
        Регистрирует схему `animeviewer://` в реестре Windows (HKCU, без прав админа).

        Возвращает True, если обработчик после этого готов принимать редирект.
        На других системах регистрация не делается — используется локальный сервер.
        """
        if not sys.platform.startswith("win"):
            return False
        try:
            exe = sys.executable
            script = str(Path(sys.argv[0]).resolve())
            settings = QSettings(
                rf"HKEY_CURRENT_USER\Software\Classes\{self.SCHEME}", QSettings.Format.NativeFormat
            )
            settings.setValue("", f"URL:{self.SCHEME} protocol")
            settings.setValue("URL Protocol", "")
            command = QSettings(
                rf"HKEY_CURRENT_USER\Software\Classes\{self.SCHEME}\shell\open\command",
                QSettings.Format.NativeFormat,
            )
            command.setValue("", f'"{exe}" "{script}" "%1"')
            settings.sync()
            print(f"[Shikimori] схема {self.SCHEME} зарегистрирована в реестре")
        except Exception as exc:  # noqa: BLE001 — реестр может быть закрыт политикой
            print(f"[Shikimori] не удалось зарегистрировать схему: {type(exc).__name__}: {exc}")
            return False
        try:
            return bool(self.handler is not None and self.handler.isListening())
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------------ запуск
    def start(self) -> bool:
        """Открывает браузер и начинает ждать ответ (обработчики — сигналы)."""
        if not self._build():
            return False
        self.status_changed.emit(f"Открываю браузер: {self.redirect_uri}")
        try:
            self.flow.authorize()
            return True
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"не удалось начать вход: {exc}"
            print(f"[Shikimori] {self.last_error}")
            return False

    def wait_for_tokens(self, timeout: int = 300) -> Optional[dict]:
        """
        Блокирующая часть: ждёт токены, крутя локальный цикл событий.

        Вызывается из фонового потока (как и прежний вход через локальный сервер),
        поэтому цикл событий здесь свой — поток интерфейса не затрагивается.
        """
        loop = QEventLoop()
        result: dict = {}

        def on_ready(tokens: dict) -> None:
            result.update(tokens or {})
            loop.quit()

        def on_done(_ok: bool, _reason: str) -> None:
            loop.quit()

        self.tokens_ready.connect(on_ready)
        self.finished.connect(on_done)
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)
        timer.start(max(30, int(timeout)) * 1000)

        if not self.start():
            return None
        loop.exec()
        timer.stop()
        if not result:
            self.last_error = self.last_error or "время ожидания входа истекло"
            return None
        return result

    # ------------------------------------------------------------------ события
    def _on_browser_requested(self, url: Any) -> None:
        """Qt просит открыть страницу авторизации — открываем в браузере по умолчанию."""
        target = url.toString() if hasattr(url, "toString") else str(url)
        self.status_changed.emit(f"Откройте ссылку, если браузер не открылся: {target}")
        try:
            webbrowser.open(target)
        except Exception as exc:  # noqa: BLE001
            print(f"[Shikimori] браузер не открылся автоматически: {exc}")

    def _on_granted(self) -> None:
        """Токены получены: собираем их в тот же формат, что понимает TokenStore."""
        try:
            token = str(self.flow.token() or "")
            refresh = str(self.flow.refreshToken() or "")
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"Qt не отдал токен: {exc}"
            self.finished.emit(False, self.last_error)
            return
        tokens = {
            "access_token": token,
            "refresh_token": refresh,
            "expires_at": time.time() + float(self.flow.expiresIn() or 0),
            "token_type": "Bearer",
            "scope": str(self.provider.get("scope") or ""),
            "host": self.host,
        }
        self.tokens = tokens
        self.status_changed.emit("Токены получены — сохраняю в общее хранилище")
        self.tokens_ready.emit(tokens)
        self.finished.emit(True, "")

    def _on_error(self, *_args: Any) -> None:
        """Ошибка Qt-флоу (отказ пользователя, сеть, неверный client_secret)."""
        detail = ""
        try:
            detail = str(self.flow.error() or "")
        except Exception:  # noqa: BLE001
            pass
        self.last_error = detail or "Shikimori отклонил вход"
        self.finished.emit(False, self.last_error)

    @staticmethod
    def _free_port(preferred: int) -> int:
        """Порт из настроек, а если занят — любой свободный (как в прежнем флоу)."""
        import socket

        for candidate in (preferred, 0):
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    sock.bind(("127.0.0.1", candidate))
                    return int(sock.getsockname()[1])
            except OSError:
                continue
        return preferred

    def refresh(self) -> bool:
        """Обновляет access_token по refresh_token (Qt делает это сам)."""
        if self.flow is None:
            return False
        try:
            self.flow.refreshAccessToken()
            return True
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"не удалось обновить токен: {exc}"
            return False


class ShikimoriAuthError(RuntimeError):
    """Понятная ошибка авторизации (текст показывается пользователю как есть)."""


class _CallbackHandler(BaseHTTPRequestHandler):
    """Обработчик одного запроса: /callback?code=... (или ?error=...)."""

    server_version = "AnimeViewerOAuth/1.0"

    def do_GET(self) -> None:  # noqa: N802 — имя задано http.server
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        if parsed.path not in ("/callback", "/"):
            self._reply(404, "Не туда попали", "Откройте ссылку из приложения.")
            return
        if parsed.path == "/" and not params:
            self._reply(200, "Anime Viewer: ожидание входа",
                        "Вернитесь в приложение и войдите в Shikimori.")
            return

        server: Any = self.server
        code = (params.get("code") or [""])[0]
        error = (params.get("error") or [""])[0]
        state = (params.get("state") or [""])[0]

        if state and state != getattr(server, "expected_state", ""):
            # защита от подмены запроса: state генерируется приложением
            server.result = {"error": "state не совпал — вход отклонён"}
            self._reply(400, "Вход отклонён", "Проверка state не прошла. Попробуйте снова.")
            return
        if error or not code:
            server.result = {"error": error or "код авторизации не пришёл"}
            self._reply(400, "Вход не выполнен", f"Shikimori вернул: {error or 'нет кода'}")
            return

        server.result = {"code": code}
        self._reply(200, "Готово! Можно закрыть эту страницу",
                    "Anime Viewer получил доступ к вашему аккаунту Shikimori.")

    def _reply(self, status: int, title: str, text: str) -> None:
        body = (
            "<!doctype html><html lang='ru'><meta charset='utf-8'>"
            f"<title>{title}</title>"
            "<body style='font-family:Segoe UI,sans-serif;background:#0d0d0e;color:#e8e8e9;"
            "display:flex;align-items:center;justify-content:center;height:100vh;margin:0'>"
            f"<div style='max-width:460px;text-align:center'>"
            f"<h2 style='color:#5e5ce6'>{title}</h2><p>{text}</p></div></body></html>"
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def log_message(self, *args: Any) -> None:
        """Не засоряем консоль приложения: сообщения сервера не нужны."""


class _CallbackServer(HTTPServer):
    """Локальный сервер на один запрос: результат складывается в .result."""

    allow_reuse_address = True

    def __init__(self, address: tuple[str, int]) -> None:
        super().__init__(address, _CallbackHandler)
        self.result: dict[str, str] = {}
        self.expected_state = ""
        self.timeout = 1.0     # чтобы serve_forever() не блокировал остановку


class ShikimoriAuth:
    """
    Хранение и обслуживание токенов Shikimori.

    Потокобезопасно: токен может понадобиться и фоновой задаче (обновление
    прогресса), и интерфейсу (кнопки «В список»/«Оценить»).
    """

    def __init__(
        self,
        base_dir: Path,
        client_id: str = "",
        client_secret: str = "",
        redirect_port: int = DEFAULT_PORT,
        path: Optional[Path] = None,
    ) -> None:
        self.base_dir = Path(base_dir)
        # Прежний auth.json больше не используется как хранилище: он нужен только
        # для одноразового переноса токенов в общий tokens.json (см. token_store.py).
        self.path = Path(path) if path else default_auth_file(self.base_dir)
        self.service = "shikimori"      # код сервиса в TokenStore
        self.store = TokenStore(
            default_token_file(self.base_dir) if path is None else Path(path).with_name("tokens.json")
        )
        # токены могут уже лежать в старом auth.json — забираем их, чтобы вход не понадобился
        self.store.migrate_from_auth_json(self.path)
        self.client_id = (client_id or os.environ.get("SHIKIMORI_CLIENT_ID") or "").strip()
        self.client_secret = (
            client_secret or os.environ.get("SHIKIMORI_CLIENT_SECRET") or ""
        ).strip()
        self.redirect_port = int(redirect_port or DEFAULT_PORT)
        self.host: str = SHIKIMORI_OAUTH_HOSTS[0]
        self.user_id: Optional[int] = None
        self.nickname: str = ""
        self._access_token: str = ""
        self._refresh_token: str = ""
        self._expires_at: float = 0.0
        self._lock = threading.RLock()
        self.last_error: str = ""
        self.qt_flow: Any = None        # активный Qt-флоу (если он используется)
        self.load()

    # ------------------------------------------------------------------ хранилище токенов
    @property
    def provider(self) -> dict:
        """
        Параметры OAuth текущей площадки (token_store.PROVIDERS).

        Хост подставляем свой: у Shikimori зеркала отличаются доменом, и адреса
        авторизации/токена должны идти через то же зеркало, что и API.
        """
        provider = dict(PROVIDERS.get(self.service) or PROVIDERS["shikimori"])
        provider["hosts"] = list(SHIKIMORI_OAUTH_HOSTS)
        return provider

    def use_host(self, host: str) -> None:
        """Переключает площадку на рабочее зеркало (как это делает клиент API)."""
        if host in SHIKIMORI_OAUTH_HOSTS:
            self.host = host

    # ------------------------------------------------------------------ настройки
    @property
    def configured(self) -> bool:
        """Заполнены ли client_id/client_secret (без них OAuth невозможен)."""
        return bool(self.client_id and self.client_secret)

    @property
    def redirect_uri(self) -> str:
        """Адрес возврата — ровно такой же должен быть указан в приложении Shikimori."""
        return f"http://localhost:{self.redirect_port}/callback"

    def apply_settings(self, client_id: str, client_secret: str, port: int) -> None:
        """Обновляет ключи и порт (вызывается из диалога настроек)."""
        self.client_id = (client_id or "").strip() or os.environ.get(
            "SHIKIMORI_CLIENT_ID", ""
        )
        self.client_secret = (client_secret or "").strip() or os.environ.get(
            "SHIKIMORI_CLIENT_SECRET", ""
        )
        try:
            self.redirect_port = int(port)
        except (TypeError, ValueError):
            self.redirect_port = DEFAULT_PORT

    # ------------------------------------------------------------------ файл
    def load(self) -> None:
        """
        Читает токены из общего TokenStore (файл tokens.json или keyring).

        Прежний auth.json читается только при первом запуске новой версии — токены
        из него переносятся в TokenStore, поэтому заново входить не нужно.
        """
        record = self.store.get(self.service)
        if not record:
            return
        self._access_token = str(record.get("access_token") or "")
        self._refresh_token = str(record.get("refresh_token") or "")
        try:
            self._expires_at = float(record.get("expires_at") or 0.0)
        except (TypeError, ValueError):
            self._expires_at = 0.0
        self.user_id = record.get("user_id")
        self.nickname = str(record.get("nickname") or "")
        host = str(record.get("host") or "")
        if host in SHIKIMORI_OAUTH_HOSTS:
            self.host = host

    def save(self) -> None:
        """Сохраняет текущие токены в TokenStore (это и есть единое хранилище)."""
        try:
            if not self._access_token:
                self.store.clear(self.service)
                return
            self.store.set(
                self.service,
                access_token=self._access_token,
                refresh_token=self._refresh_token,
                expires_at=self._expires_at,
                user_id=self.user_id,
                nickname=self.nickname,
                host=self.host,
                token_type="Bearer",
            )
        except Exception as exc:  # noqa: BLE001 — сбой записи не должен ронять вход
            print(f"[Shikimori] не удалось сохранить токены: {exc}")

    # ------------------------------------------------------------------ состояние
    @property
    def authorized(self) -> bool:
        return bool(self._access_token)

    @property
    def account_label(self) -> str:
        """Как показывать аккаунт в интерфейсе."""
        if not self.authorized:
            return "не выполнен вход"
        return f"{self.nickname} (id {self.user_id})" if self.nickname else f"id {self.user_id}"

    def logout(self) -> None:
        """Убирает токены сервиса из общего хранилища (keyring и файл)."""
        with self._lock:
            self._access_token = ""
            self._refresh_token = ""
            self._expires_at = 0.0
            self.user_id = None
            self.nickname = ""
        self.store.clear(self.service)
        self.qt_flow = None

    def other_accounts(self) -> list[tuple[str, str]]:
        """
        Другие площадки, для которых уже есть токены в общем хранилище.

        Нужно для «переключения площадки без повторного входа»: приложение видит
        готовый токен AniList/MAL и не открывает браузер.
        """
        result: list[tuple[str, str]] = []
        for code, _record in self.store.records():
            if code == self.service:
                continue
            title = str((PROVIDERS.get(code) or {}).get("title") or code)
            result.append((code, self.store.account_label(code) or title))
        return result

    # ------------------------------------------------------------------ токен
    def token(self, force_refresh: bool = False) -> str:
        """
        Возвращает годный access_token, при необходимости обновляя его.

        Исключения не поднимаются: при неудаче возвращается пустая строка, а
        причина остаётся в last_error — интерфейс покажет её пользователю.
        """
        with self._lock:
            if not self._access_token:
                self.last_error = "вход в Shikimori не выполнен"
                return ""
            fresh = self._expires_at and time.time() + REFRESH_MARGIN < self._expires_at
            if fresh and not force_refresh:
                return self._access_token
            if not self._refresh_token:
                # токен без срока (или старый формат) — отдаём как есть
                return self._access_token
            if self._refresh():
                return self._access_token
            return ""

    def _refresh(self) -> bool:
        self.last_error = ""
        try:
            data = self._token_request({
                "grant_type": "refresh_token",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self._refresh_token,
            })
        except ShikimoriAuthError as exc:
            self.last_error = f"не удалось обновить токен: {exc}"
            print(f"[Shikimori] {self.last_error}")
            if "invalid_grant" in str(exc) or "401" in str(exc):
                # refresh_token отозван: просим войти заново
                self.logout()
            return False
        self._store_tokens(data)
        return True

    # ------------------------------------------------------------------ вход
    def login(self, open_browser: bool = True, timeout: int = 300,
              on_status: Optional[Callable[[str], None]] = None) -> bool:
        """
        Полный вход: локальный сервер -> браузер -> обмен кода на токены.

        Возвращает True при успехе; причина неудачи — в last_error.
        Блокирующий метод: вызывать в фоновом потоке.
        """
        def report(text: str) -> None:
            if on_status is not None:
                try:
                    on_status(text)
                except Exception:  # noqa: BLE001 — колбэк интерфейса не должен мешать
                    pass
            print(f"[Shikimori] {text}")

        if not self.configured:
            self.last_error = (
                "не заданы client_id и client_secret: заведите приложение на "
                "shikimori.one/oauth/applications и впишите ключи в «Оформлении»"
            )
            report(self.last_error)
            return False

        self.last_error = ""

        # 1) OAuth через Qt (QOAuth2AuthorizationCodeFlow + QOAuthUriSchemeReplyHandler).
        #    Это основной путь: Qt сам открывает браузер, принимает редирект и умеет
        #    обновлять токен. Требует PySide6-Addons (пакет QtNetworkAuth).
        if QT_OAUTH_AVAILABLE:
            tokens = self._login_qt(report, timeout)
            if tokens:
                self._store_tokens(tokens)
                report("Вход выполнен")
                return True
            if self.qt_flow is not None or self.last_error:
                # Флоу начался и не удался (отказ, сеть, ключи) — второй вход не пробуем
                report(self.last_error or "вход отменён")
                return False
            report("Qt-флоу недоступен — вхожу через локальный сервер")

        # 2) Запасной путь: свой локальный сервер (работает всегда, в том числе в
        #    портативной сборке, где QtNetworkAuth нет).
        port = self._free_port(self.redirect_port)
        try:
            server = _CallbackServer(("127.0.0.1", port))
        except OSError as exc:
            self.last_error = f"не удалось занять порт {port}: {exc}"
            report(self.last_error)
            return False
        self.redirect_port = port
        state = secrets.token_urlsafe(16)
        server.expected_state = state

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = self._authorize_url(state)
        report(f"Откройте ссылку и разрешите доступ: {url}")
        if open_browser:
            try:
                webbrowser.open(url)
            except Exception as exc:  # noqa: BLE001
                report(f"браузер не открылся автоматически ({exc}) — откройте ссылку вручную")

        deadline = time.time() + max(30, int(timeout))
        try:
            while time.time() < deadline:
                if server.result:
                    break
                time.sleep(0.2)
        finally:
            try:
                server.shutdown()
                server.server_close()
            except Exception:  # noqa: BLE001
                pass

        result = server.result
        if not result:
            self.last_error = "время ожидания входа истекло"
            report(self.last_error)
            return False
        if result.get("error"):
            self.last_error = f"Shikimori отклонил вход: {result['error']}"
            report(self.last_error)
            return False

        report("Код получен — получаю токены…")
        try:
            data = self._token_request({
                "grant_type": "authorization_code",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "code": result["code"],
                "redirect_uri": self.redirect_uri,
            })
        except ShikimoriAuthError as exc:
            self.last_error = f"не удалось получить токены: {exc}"
            report(self.last_error)
            return False
        self._store_tokens(data)
        report("Вход выполнен")
        return True

    def _authorize_url(self, state: str) -> str:
        query = urlencode({
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "state": state,
        })
        return f"{self.host}/oauth/authorize?{query}"

    @staticmethod
    def _free_port(preferred: int) -> int:
        """Порт из настроек, а если занят — любой свободный (локальный сервер)."""
        for candidate in (preferred, 0):
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    sock.bind(("127.0.0.1", candidate))
                    return int(sock.getsockname()[1])
            except OSError:
                continue
        return preferred

    # ------------------------------------------------------------------ обмен
    def _token_request(self, payload: dict) -> dict:
        """
        Запрос токенов к Shikimori.

        Shikimori принимает JSON-тело; на случай изменения поведения пробуем и
        обычную форму (application/x-www-form-urlencoded).
        """
        errors: list[str] = []
        for host in self._hosts():
            for as_json in (True, False):
                try:
                    if as_json:
                        resp = requests.post(
                            f"{host}/oauth/token", json=payload, timeout=REQUEST_TIMEOUT
                        )
                    else:
                        resp = requests.post(
                            f"{host}/oauth/token", data=payload, timeout=REQUEST_TIMEOUT
                        )
                except requests.RequestException as exc:
                    errors.append(f"{host}: {type(exc).__name__}: {exc}")
                    break       # хост недоступен — переходим к следующему зеркалу
                if resp.status_code in (415, 400) and as_json:
                    continue    # формат не подошёл — попробуем форму
                if resp.status_code >= 400:
                    errors.append(f"{host}: HTTP {resp.status_code} {resp.text[:120]}")
                    break
                try:
                    data = resp.json()
                except ValueError:
                    errors.append(f"{host}: ответ не JSON")
                    break
                if not data.get("access_token"):
                    errors.append(f"{host}: в ответе нет access_token")
                    break
                self.host = host
                return data
        raise ShikimoriAuthError("; ".join(errors[:3]) or "нет ответа")

    def _hosts(self) -> list[str]:
        return [self.host] + [h for h in SHIKIMORI_OAUTH_HOSTS if h != self.host]

    def _login_qt(self, report: Any, timeout: int) -> Optional[dict]:
        """
        Вход через QtNetworkAuth. Возвращает токены или None.

        Разница для вызывающего кода: если вернулся None, а self.qt_flow остался
        None — этим способом воспользоваться не удалось и нужен запасной путь.
        Если qt_flow создан, значит вход уже шёл (браузер открывался) и повторять
        его другим способом нельзя.
        """
        flow = QtOAuthFlow(
            self.provider, self.client_id, self.client_secret,
            self.redirect_port, host=self.host,
        )
        try:
            flow.status_changed.connect(lambda text: report(str(text)))
        except Exception:  # noqa: BLE001
            pass
        self.qt_flow = flow
        tokens = flow.wait_for_tokens(timeout)
        if tokens:
            self.redirect_port = int(flow.redirect_port)
            if flow.host in SHIKIMORI_OAUTH_HOSTS:
                self.host = flow.host
            report(f"Редирект принят: {flow.redirect_uri}")
            return tokens
        self.last_error = flow.last_error or self.last_error
        # вход даже не начался (нет QtNetworkAuth/handler) — разрешаем запасной путь
        if not self.last_error or "QtNetworkAuth" in self.last_error:
            self.qt_flow = None
        return None

    def _store_tokens(self, data: dict) -> None:
        """Сохраняет полученные токены: сначала в память, затем в TokenStore."""
        expires_in = data.get("expires_in")
        with self._lock:
            self._access_token = str(data.get("access_token") or "")
            self._refresh_token = str(data.get("refresh_token") or self._refresh_token)
            if data.get("expires_at"):
                # Qt-флоу отдаёт готовую метку времени, а не «сколько жить»
                self._expires_at = float(data.get("expires_at") or 0.0)
            else:
                try:
                    seconds = float(expires_in or 0)
                except (TypeError, ValueError):
                    seconds = 0.0
                self._expires_at = time.time() + seconds if seconds else 0.0
            host = str(data.get("host") or "")
            if host in SHIKIMORI_OAUTH_HOSTS:
                self.host = host
            self.save()
