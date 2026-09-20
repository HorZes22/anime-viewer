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
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlencode, urlparse

import requests

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
    return Path(base_dir) / "auth.json"


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
        self.path = Path(path) if path else default_auth_file(self.base_dir)
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
        self.load()

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
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._access_token = str(data.get("access_token") or "")
                    self._refresh_token = str(data.get("refresh_token") or "")
                    self._expires_at = float(data.get("expires_at") or 0.0)
                    self.user_id = data.get("user_id")
                    self.nickname = str(data.get("nickname") or "")
                    host = str(data.get("host") or "")
                    if host in SHIKIMORI_OAUTH_HOSTS:
                        self.host = host
        except (OSError, ValueError):
            pass

    def save(self) -> None:
        payload = {
            "access_token": self._access_token,
            "refresh_token": self._refresh_token,
            "expires_at": self._expires_at,
            "user_id": self.user_id,
            "nickname": self.nickname,
            "host": self.host,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
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
        with self._lock:
            self._access_token = ""
            self._refresh_token = ""
            self._expires_at = 0.0
            self.user_id = None
            self.nickname = ""
            self.save()

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

    def _store_tokens(self, data: dict) -> None:
        with self._lock:
            self._access_token = str(data.get("access_token") or "")
            self._refresh_token = str(data.get("refresh_token") or self._refresh_token)
            try:
                expires_in = float(data.get("expires_in") or 0)
            except (TypeError, ValueError):
                expires_in = 0.0
            self._expires_at = time.time() + expires_in if expires_in else 0.0
            self.save()
