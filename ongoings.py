# -*- coding: utf-8 -*-
"""
Расписание онгоингов из Shikimori — модуль рядом с main.py.

Эндпоинт: GET /api/calendar — отдаёт список тайтлов с датой следующей серии
(проверено: у зеркала shikimori.net приходит ~144 записи, у shikimori.one в РФ
часто таймаут — поэтому хосты перебираются, как и в остальных запросах).

Модуль ничего не знает про интерфейс: он возвращает готовый список словарей, а
вкладка «Онгоинги» в main.py раскладывает его по дням недели.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import requests

CALENDAR_HOSTS = (
    "https://shikimori.one",
    "https://shikimori.net",
    "https://shikimori.org",
)
REQUEST_TIMEOUT = 20
UA = "AnimeViewer/1.0 (local desktop app; python-requests)"

WEEKDAYS = ("Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье")


def _parse_time(value: Any) -> Optional[datetime]:
    """ISO-время Shikimori -> datetime (или None, если формат неожиданный)."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone()


class CalendarClient:
    """Клиент расписания Shikimori с перебором зеркал."""

    def __init__(self, timeout: int = REQUEST_TIMEOUT, preferred_host: str = "",
                 race: bool = True) -> None:
        self.timeout = max(5, int(timeout))
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": UA, "Accept": "application/json"})
        self.host: str = preferred_host if preferred_host in CALENDAR_HOSTS else CALENDAR_HOSTS[0]
        self.last_error: str = ""
        self.last_note: str = ""
        # «Гонка зеркал»: shikimori.one у части провайдеров виснет до таймаута,
        # поэтому все адреса опрашиваются одновременно и берётся ответивший первым
        # (так же работает поиск аниме в приложении).
        self.race = bool(race)

    def _hosts(self) -> list[str]:
        return [self.host] + [h for h in CALENDAR_HOSTS if h != self.host]

    def _fetch_host(self, host: str) -> list[dict]:
        """Запрос расписания у одного зеркала (список записей или исключение)."""
        resp = self.session.get(f"{host}/api/calendar", timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list):
            raise ValueError("ответ не список")
        return data

    def fetch(self) -> list[dict]:
        """
        Список ближайших серий: [{anime_id, title, russian, episode, at, at_text,
        kind, score, image}].

        Пустой список означает «не получилось» — причина в last_error.
        """
        if self.race:
            items = self._fetch_raced()
            if items:
                return items
        return self._fetch_sequential()

    def _fetch_raced(self) -> list[dict]:
        """Параллельный опрос зеркал: побеждает тот, кто ответил первым."""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        hosts = list(CALENDAR_HOSTS)
        errors: list[str] = []
        executor = ThreadPoolExecutor(max_workers=len(hosts))
        try:
            futures = {executor.submit(self._fetch_host, host): host for host in hosts}
            for future in as_completed(futures):
                host = futures[future]
                try:
                    data = future.result()
                except Exception as exc:  # noqa: BLE001 — зеркало может не ответить
                    errors.append(f"{host}: {type(exc).__name__}: {exc}")
                    continue
                self.host = host
                self.last_error = ""
                items = self._prepare(data)
                if items:
                    self.last_note = (
                        f"расписание: {len(items)} тайтлов ({host.replace('https://', '')})"
                    )
                return items
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        if errors:
            self.last_error = "; ".join(errors[:2])
        return []

    def _fetch_sequential(self) -> list[dict]:
        """Запасной путь: перебор зеркал по очереди (если гонка выключена)."""
        errors: list[str] = []
        for host in self._hosts():
            try:
                data = self._fetch_host(host)
            except Exception as exc:  # noqa: BLE001 — зеркало может не ответить
                errors.append(f"{host}: {type(exc).__name__}: {exc}")
                continue
            self.host = host
            self.last_error = ""
            items = self._prepare(data)
            self.last_note = (
                f"расписание: {len(items)} тайтлов ({host.replace('https://', '')})"
            )
            return items
        self.last_error = "; ".join(errors[:2]) or "нет ответа"
        return []

    def _prepare(self, data: list) -> list[dict]:
        """Сырые записи Shikimori -> удобный список, отсортированный по времени."""
        items = [self._as_item(raw) for raw in data if isinstance(raw, dict)]
        items = [item for item in items if item.get("at") is not None]
        items.sort(key=lambda item: item["at"])
        return items

    @staticmethod
    def _as_item(raw: dict) -> dict:
        """Запись календаря -> удобный словарь (названия, серия, дата)."""
        anime = raw.get("anime") or {}
        image = anime.get("image") or {}
        at = _parse_time(raw.get("next_episode_at"))
        return {
            "anime_id": anime.get("id"),
            "title": str(anime.get("name") or ""),
            "russian": str(anime.get("russian") or ""),
            "episode": raw.get("next_episode"),
            "duration": raw.get("duration"),
            "at": at,
            "at_text": at.strftime("%d.%m %H:%M") if at else "",
            "weekday": WEEKDAYS[at.weekday()] if at else "",
            "kind": str(anime.get("kind") or ""),
            "score": anime.get("score") or 0,
            "episodes": anime.get("episodes") or 0,
            "image": str(image.get("preview") or image.get("original") or ""),
        }


def group_by_day(items: list[dict], days: int = 7) -> list[tuple[str, list[dict]]]:
    """
    Раскладывает расписание по дням: [(«Понедельник, 22.09», [тайтлы]), …].

    Дни идут по порядку, начиная с сегодняшнего; пустые дни тоже попадают в
    список — так видно, что в этот день просто нет серий.
    """
    today = datetime.now().date()
    buckets: dict[Any, list[dict]] = {}
    for item in items:
        moment = item.get("at")
        if moment is None:
            continue
        day = moment.date()
        if day < today or (day - today).days >= days:
            continue
        buckets.setdefault(day, []).append(item)

    result: list[tuple[str, list[dict]]] = []
    for offset in range(days):
        day = today + timedelta(days=offset)
        label = day.strftime("%d.%m")
        name = WEEKDAYS[day.weekday()]
        if offset == 0:
            name = "Сегодня"
        elif offset == 1:
            name = "Завтра"
        result.append((f"{name}, {label}", buckets.get(day, [])))
    return result
