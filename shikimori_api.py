# -*- coding: utf-8 -*-
"""
Синхронизация с Shikimori: списки пользователя, статусы, оценки, серии.

Работает поверх ShikimoriAuth (shikimori_oauth.py): токен берётся и обновляется
там, здесь — только запросы к API и понятные ошибки.

Что умеет (эндпоинты Shikimori API v2):
    * whoami()                        — кто вошёл (id, ник, аватар);
    * rates()                         — список аниме пользователя («Моё») со статусами,
                                        оценками и числом просмотренных серий;
    * rate_for(anime_id)              — запись для конкретного аниме (или None);
    * add_to_list(anime_id, status)   — «Добавить в список» (POST/PUT user_rates);
    * set_score(anime_id, score)      — «Оценить» 1–10;
    * set_episodes(anime_id, episode) — сколько серий просмотрено;
    * set_status(anime_id, status)    — «Просмотрено» и т.п.

Статусы Shikimori: planned (буду смотреть), watching (смотрю), rewatching
(пересматриваю), completed (просмотрено), on_hold (отложено), dropped (брошено).

Модуль ничего не знает про интерфейс: все методы синхронные, их вызывают из
фоновых потоков приложения (Worker).
"""

from __future__ import annotations

import threading
from typing import Any, Optional

import requests

# Статусы: код -> подпись для интерфейса (порядок — как в Shikimori).
SHIKIMORI_STATUSES: list[tuple[str, str]] = [
    ("watching", "Смотрю"),
    ("planned", "Буду смотреть"),
    ("completed", "Просмотрено"),
    ("on_hold", "Отложено"),
    ("dropped", "Брошено"),
    ("rewatching", "Пересматриваю"),
]

# Хосты API: первый — официальный домен, остальные — его зеркала.
SHIKIMORI_API_HOSTS = (
    "https://shikimori.one",
    "https://shikimori.net",
    "https://shikimori.org",
)

REQUEST_TIMEOUT = 20


class ShikimoriApiError(RuntimeError):
    """Ошибка API с понятным текстом (показывается в статусной строке)."""


def status_label(code: str) -> str:
    """Код статуса -> подпись («watching» -> «Смотрю»)."""
    for value, label in SHIKIMORI_STATUSES:
        if value == code:
            return label
    return code or "—"


class ShikimoriAccount:
    """
    Операции с аккаунтом Shikimori.

    Все методы возвращают данные либо None/[] и записывают причину в last_error:
    сбой сети в фоновой задаче не должен ломать интерфейс.
    """

    def __init__(self, auth: Any, user_agent: str = "AnimeViewer/1.0") -> None:
        self.auth = auth                     # ShikimoriAuth
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": user_agent,
            "Accept": "application/json",
        })
        self.host: str = getattr(auth, "host", SHIKIMORI_API_HOSTS[0])
        self.last_error: str = ""
        self._me: Optional[dict] = None
        self._rates: dict[str, dict] = {}     # target_id (str) -> запись
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ низкий уровень
    @property
    def ready(self) -> bool:
        """Можно ли вообще обращаться к API (есть токен)."""
        return bool(getattr(self.auth, "authorized", False))

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        payload: Optional[dict] = None,
        need_auth: bool = True,
    ) -> Any:
        headers: dict[str, str] = {}
        if need_auth:
            token = self.auth.token()
            if not token:
                raise ShikimoriApiError(
                    self.auth.last_error or "нет доступа: войдите в Shikimori"
                )
            headers["Authorization"] = f"Bearer {token}"

        errors: list[str] = []
        for host in self._hosts():
            try:
                resp = self.session.request(
                    method,
                    host + path,
                    params=params,
                    json=payload,
                    headers=headers,
                    timeout=REQUEST_TIMEOUT,
                )
            except requests.RequestException as exc:
                errors.append(f"{host}: {type(exc).__name__}: {exc}")
                continue
            if resp.status_code == 401:
                # токен успел истечь — один раз пробуем обновить и повторить
                token = self.auth.token(force_refresh=True)
                if token:
                    headers["Authorization"] = f"Bearer {token}"
                    try:
                        resp = self.session.request(
                            method, host + path, params=params, json=payload,
                            headers=headers, timeout=REQUEST_TIMEOUT,
                        )
                    except requests.RequestException as exc:
                        errors.append(f"{host}: {type(exc).__name__}: {exc}")
                        continue
                if resp.status_code == 401:
                    self.last_error = "Shikimori не принял токен: войдите заново"
                    raise ShikimoriApiError(self.last_error)
            if resp.status_code == 429:
                self.last_error = "Shikimori: слишком много запросов (429), подождите минуту"
                raise ShikimoriApiError(self.last_error)
            if resp.status_code >= 400:
                errors.append(f"{host}: HTTP {resp.status_code} {resp.text[:140]}")
                continue
            self.host = host
            self.last_error = ""
            if resp.status_code == 204 or not resp.content:
                return None
            try:
                return resp.json()
            except ValueError:
                errors.append(f"{host}: ответ не JSON")
                continue
        self.last_error = "; ".join(errors[:2]) or "нет ответа"
        raise ShikimoriApiError(self.last_error)

    def _hosts(self) -> list[str]:
        return [self.host] + [h for h in SHIKIMORI_API_HOSTS if h != self.host]

    def _safe(self, fn, *args: Any, **kwargs: Any) -> Any:
        """Вызов с перехватом ошибок: наружу — None, причина — в last_error."""
        try:
            return fn(*args, **kwargs)
        except ShikimoriApiError as exc:
            self.last_error = str(exc)
        except Exception as exc:  # noqa: BLE001 — чужой API, бывает всякое
            self.last_error = f"{type(exc).__name__}: {exc}"
        print(f"[Shikimori] {self.last_error}")
        return None

    # ------------------------------------------------------------------ кто вошёл
    def me(self, refresh: bool = False) -> Optional[dict]:
        if self._me is not None and not refresh:
            return self._me
        data = self._safe(self._request, "GET", "/api/v2/users/whoami")
        if isinstance(data, dict) and data.get("id"):
            self._me = data
            self.auth.user_id = data.get("id")
            self.auth.nickname = str(data.get("nickname") or "")
            self.auth.save()
        return self._me

    # ------------------------------------------------------------------ список «Моё»
    def rates(self, limit: int = 50, page: int = 1,
              status: Optional[str] = None) -> list[dict]:
        """
        Список аниме пользователя (для вкладки «Моё»).

        В каждой записи есть вложенный объект anime (название, постер, серии),
        поэтому дополнительных запросов не нужно.
        """
        me = self.me()
        if not me:
            return []
        params: dict[str, Any] = {
            "user_id": me.get("id"),
            "target_type": "Anime",
            "limit": max(1, min(100, int(limit))),
            "page": max(1, int(page)),
        }
        if status:
            params["status"] = status
        data = self._safe(self._request, "GET", "/api/v2/user_rates", params)
        items = [item for item in (data or []) if isinstance(item, dict)]
        with self._lock:
            for item in items:
                target = str(item.get("target_id") or "")
                if target:
                    self._rates[target] = item
        return items

    def rate_for(self, anime_id: Any, refresh: bool = False) -> Optional[dict]:
        """Запись пользователя для конкретного аниме (или None, если его нет в списке)."""
        key = str(anime_id)
        if not refresh:
            with self._lock:
                if key in self._rates:
                    return self._rates[key]
        me = self.me()
        if not me:
            return None
        params = {
            "user_id": me.get("id"),
            "target_id": int(anime_id),
            "target_type": "Anime",
            "limit": 1,
        }
        data = self._safe(self._request, "GET", "/api/v2/user_rates", params)
        items = [item for item in (data or []) if isinstance(item, dict)]
        with self._lock:
            self._rates[key] = items[0] if items else {}
        return items[0] if items else None

    # ------------------------------------------------------------------ запись
    def _save_rate(self, anime_id: Any, fields: dict) -> Optional[dict]:
        """Создаёт или обновляет запись: нужный метод выбирается сам."""
        rate = self.rate_for(anime_id)
        me = self.me()
        if not me:
            return None
        payload = {"user_rate": dict(fields)}
        if rate and rate.get("id"):
            payload["user_rate"]["id"] = rate.get("id")
        else:
            payload["user_rate"]["target_id"] = int(anime_id)
            payload["user_rate"]["target_type"] = "Anime"
            payload["user_rate"]["user_id"] = me.get("id")
        if rate and rate.get("id"):
            data = self._safe(
                self._request, "PUT", f"/api/v2/user_rates/{rate['id']}", None, payload
            )
        else:
            data = self._safe(self._request, "POST", "/api/v2/user_rates", None, payload)
        if isinstance(data, dict) and data.get("id"):
            with self._lock:
                self._rates[str(anime_id)] = data
            return data
        return None

    def add_to_list(self, anime_id: Any, status: str = "watching",
                    score: Optional[int] = None,
                    episodes: Optional[int] = None) -> Optional[dict]:
        """«Добавить в список» с нужным статусом (и, при желании, оценкой/сериями)."""
        fields: dict[str, Any] = {"status": status}
        if score is not None:
            fields["score"] = int(score)
        if episodes is not None:
            fields["episodes"] = int(episodes)
        return self._save_rate(anime_id, fields)

    def set_status(self, anime_id: Any, status: str) -> Optional[dict]:
        """Сменить статус («Просмотрено», «Брошено», …)."""
        return self._save_rate(anime_id, {"status": status})

    def set_score(self, anime_id: Any, score: int) -> Optional[dict]:
        """Поставить оценку 1–10 (0 — снять оценку)."""
        score = max(0, min(10, int(score)))
        return self._save_rate(anime_id, {"score": score})

    def set_episodes(self, anime_id: Any, episodes: int,
                     status: Optional[str] = None) -> Optional[dict]:
        """
        Записать число просмотренных серий (не уменьшая его).

        Статус меняется только если он передан: автосинхронизация при просмотре
        не должна самовольно менять «Брошено» на «Смотрю».
        """
        current = self.rate_for(anime_id) or {}
        try:
            already = int(current.get("episodes") or 0)
        except (TypeError, ValueError):
            already = 0
        fields: dict[str, Any] = {"episodes": max(already, int(episodes))}
        if status:
            fields["status"] = status
        return self._save_rate(anime_id, fields)

    def remove_from_list(self, anime_id: Any) -> bool:
        """Убирает запись из списка пользователя."""
        rate = self.rate_for(anime_id)
        if not rate or not rate.get("id"):
            return False
        result = self._safe(self._request, "DELETE", f"/api/v2/user_rates/{rate['id']}")
        with self._lock:
            self._rates.pop(str(anime_id), None)
        return result is None
