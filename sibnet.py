# -*- coding: utf-8 -*-
"""
Источник видео Sibnet (video.sibnet.ru) для Anime Viewer.

Отдельный модуль рядом с main.py, подключается автоматически (см. main.py ->
_load_external_source). Из главного модуля берётся только базовый класс и
помощник сравнения названий — так источник считается «своим» для приложения.

Что проверено на живом сайте:

  * Поток. Страница-обёртка плеера отдаёт ссылку на mp4 в JS:
        GET https://video.sibnet.ru/shell.php?videoid=<id>
        ->  player.src([{src: "/v/<hash>/<id>.mp4", type: "video/mp4"},]);
    Абсолютный адрес: https://video.sibnet.ru/v/<hash>/<id>.mp4

  * Referer обязателен. Без заголовка Referer: https://video.sibnet.ru/ файл
    отдаёт 403, с ним — 206 video/mp4. Поэтому источник заполняет last_referer,
    а плеер (MpvWidget.play_url) передаёт его в mpv опцией --referrer.

  * Поиск закрыт антибот-защитой: search.php отвечает редиректом на robot.php
    (проверка браузера на JS) и результатов не отдаёт. Поэтому:

        1) обычная озвучка «Sibnet» пробует поиск по названию — если Kodik-
           подобная удача не случилась, пользователь видит честную причину
           в статусной строке, а не пустой список;
        2) работающий путь — явная привязка к видео: озвучка с именем
           «Sibnet: <id>» или «Sibnet: <ссылка на видео>» разрешается всегда
           (это удобно для серий, найденных в браузере).

Никакой магии: если Sibnet недоступен, источник просто ничего не возвращает и
пишет причину в last_status.
"""

from __future__ import annotations

import re
import threading
from typing import Any, Optional
from urllib.parse import quote, urlparse

import requests

from main import REQUEST_TIMEOUT, VideoSource, _title_similarity

SISNET_BASE = "https://video.sibnet.ru"
SISNET_REFERER = "https://video.sibnet.ru/"
# Sibnet отдаёт страницы-обёртки только «браузерному» клиенту: с собственным
# User-Agent приложения (AnimeViewer/1.0 …) shell.php отвечает 400 Bad Request —
# проверено. Сам файл mp4 при этом отдаётся любому клиенту, но только с Referer.
SISNET_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
SISNET_EPISODE_RE = re.compile(
    r"(?:серия|seriya|seria|episode|ep|выпуск)[\s._-]*(\d{1,3})"
    r"|(?:[Ss](\d{1,2})\s?[Ee](\d{1,3}))"
    r"|(?<![0-9A-Za-zА-Яа-я])(\d{1,3})(?![0-9A-Za-zА-Яа-я])",
    re.IGNORECASE,
)
# Разбор номера серии из названия видео. Порядок проверок важен и проверяется тестами:
#   1) «S01E05» — серия во второй группе («01» — это НЕ серия, это сезон);
#   2) «серия 5» / «episode 5» — номер после слова;
#   3) «5 серия» — номер перед словом;
#   4) иначе — последнее отдельное число в названии.
SISNET_EPISODE_PATTERNS = (
    r"[Ss](\d{1,2})\s?[Ee](\d{1,3})",
    r"(?:серия|seriya|seria|episode|ep|выпуск)[\s._-]*(\d{1,3})",
    r"(\d{1,3})\s*(?:серия|seriya|seria|episode|выпуск)",
)


class SibnetClient:
    """
    Клиент Sibnet: поиск (может быть закрыт), разбор потока по id видео.

    Все методы возвращают None/[] вместо исключений: сайт чужой, он может
    сменить разметку — приложение не должно падать.
    """

    def __init__(self, timeout: int = REQUEST_TIMEOUT) -> None:
        self.timeout = max(5, int(timeout))
        self.session = requests.Session()
        # UA именно браузерный: с UA приложения Sibnet отвечает 400 (см. SISNET_UA)
        self.session.headers.update({
            "User-Agent": SISNET_UA,
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        })
        self.last_error: str = ""
        # поиск закрыт антиботом — запоминаем, чтобы не долбить сайт запросами
        self.search_blocked: bool = False

    # ---------- поиск ----------
    def search(self, query: str, limit: int = 20) -> list[dict]:
        """
        Поиск видео по названию.

        Возвращает [{"id", "title", "url"}]. Пустой список — либо ничего не
        нашлось, либо включилась антибот-защита (см. self.search_blocked и
        last_error: приложение покажет это в статусной строке).
        """
        query = (query or "").strip()
        if not query:
            return []
        if self.search_blocked:
            self.last_error = "поиск Sibnet закрыт антибот-проверкой (robot.php)"
            return []
        try:
            resp = self.session.get(
                SISNET_BASE + "/search.php",
                params={"text": query},
                timeout=self.timeout,
                headers={"Referer": SISNET_BASE + "/"},
                allow_redirects=True,
            )
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            return []

        page_url = str(resp.url or "")
        if "robot.php" in page_url:
            self.search_blocked = True
            self.last_error = "Sibnet: поиск закрыт антибот-проверкой (robot.php)"
            return []
        if resp.status_code >= 400:
            self.last_error = f"Sibnet: HTTP {resp.status_code}"
            return []

        text = resp.text
        try:
            text = resp.content.decode("windows-1251", "replace")
        except Exception:  # noqa: BLE001 — кодировка у сайта старая
            pass

        results: list[dict] = []
        seen: set[str] = set()
        # ссылки на видео вида /video6288803-slug/ или /day/.../video6288803-slug/
        for match in re.finditer(
            r'href="([^"]*?/video(\d+)(?:-[^"/]*)?/?)"[^>]*>(?:<[^>]+>)*([^<]{0,120})',
            text,
        ):
            video_id = match.group(2)
            title = re.sub(r"\s+", " ", match.group(3 or 0) or "").strip()
            if video_id in seen:
                continue
            seen.add(video_id)
            results.append({
                "id": video_id,
                "title": title,
                "url": f"{SISNET_BASE}/video{video_id}/",
            })
            if len(results) >= max(1, int(limit)):
                break
        if not results:
            self.last_error = "Sibnet: ничего не нашлось (или изменилась разметка поиска)"
        return results

    # ---------- поток ----------
    def stream(self, video_id: Any) -> Optional[str]:
        """
        Прямая ссылка на mp4 по id видео.

        Ссылку отдаёт страница-обёртка плеера (shell.php) — в ней есть
        player.src([{src: "/v/<hash>/<id>.mp4"}]).
        """
        video_id = str(video_id or "").strip()
        if not video_id.isdigit():
            return None
        try:
            resp = self.session.get(
                f"{SISNET_BASE}/shell.php",
                params={"videoid": video_id},
                timeout=self.timeout,
                headers={"Referer": f"{SISNET_BASE}/video{video_id}/"},
            )
            resp.raise_for_status()
            html = resp.text
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"Sibnet: {type(exc).__name__}: {exc}"
            return None

        # 1) обычный путь: player.src([{src: "/v/<hash>/<id>.mp4", type: "video/mp4"}])
        match = re.search(
            r'player\.src\(\s*\[\s*\{\s*src\s*:\s*"([^"]+\.mp4[^"]*)"', html
        )
        if not match:
            # 2) запасной путь: любой mp4 в разметке (у сайта бывают разные плееры)
            match = re.search(r'"(/[^"]+\.mp4[^"]*)"', html) or re.search(
                r"'(/[^']+\.mp4[^']*)'", html
            )
        if not match:
            self.last_error = "Sibnet: в плеере нет ссылки на mp4"
            return None
        path = match.group(1)
        if path.startswith("//"):
            return "https:" + path
        if path.startswith("/"):
            return SISNET_BASE + path
        return path if path.startswith("http") else ""

    # ---------- вспомогательное ----------
    @staticmethod
    def video_id_from(text: str) -> str:
        """
        Достаёт id видео из ссылки или строки: поддерживаются
        «6288803», «sibnet:6288803», «https://video.sibnet.ru/video6288803-slug/».
        """
        text = (text or "").strip()
        if text.isdigit():
            return text
        match = re.search(r"/video(\d+)", text) or re.search(r"videoid=(\d+)", text)
        if match:
            return match.group(1)
        match = re.search(r"\b(\d{5,10})\b", text)
        return match.group(1) if match else ""


class SibnetVideoSource(VideoSource):
    """
    Источник видео Sibnet.

    Озвучки: у Sibnet нет метаданных перевода, поэтому источник предлагает одну
    озвучку «Sibnet» (поиск по названию) и разрешает прямые привязки
    «Sibnet: <id видео>» — они работают независимо от поиска.
    """

    name = "sibnet"
    label = "Sibnet"
    annotate_dubbings = True

    PIN_PREFIX = "sibnet"

    def __init__(self, client: Optional[SibnetClient] = None) -> None:
        self.client = client or SibnetClient()
        self.last_referer = SISNET_REFERER
        self._streams: dict[str, str] = {}
        self._found: dict[int, list[dict]] = {}      # anime_id -> найденные видео
        self._lock = threading.Lock()

    # ---------- разбор имени озвучки ----------
    @classmethod
    def _pinned_id(cls, dubbing: str) -> str:
        """
        «Sibnet: 6288803» / «sibnet:https://video.sibnet.ru/video6288803-x/» ->
        id видео. Пусто — обычная озвучка «Sibnet» (поиск по названию).
        """
        text = (dubbing or "").strip()
        low = text.lower()
        if not low.startswith(cls.PIN_PREFIX):
            return ""
        tail = text[len(cls.PIN_PREFIX):].lstrip(" :—-")
        if not tail:
            return ""
        return SibnetClient.video_id_from(tail)

    @staticmethod
    def _episode_of(title: str) -> int:
        """
        Номер серии из названия видео: «Мастера меча онлайн 2 серия» -> 2,
        «Sword Art Online S01E05» -> 5 (номер сезона серией не считается).
        """
        text = title or ""
        # 1) SxxExx — серия во второй группе
        match = re.search(SISNET_EPISODE_PATTERNS[0], text)
        if match:
            try:
                return int(match.group(2))
            except (TypeError, ValueError):
                pass
        # 2) «серия 5» и 3) «5 серия»
        for pattern in SISNET_EPISODE_PATTERNS[1:]:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                try:
                    number = int(match.group(1))
                except (TypeError, ValueError):
                    continue
                if 0 < number < 1000:
                    return number
        # 4) последнее отдельное число в названии
        numbers = [
            m for m in re.finditer(
                r"(?<![0-9A-Za-zА-Яа-я])(\d{1,3})(?![0-9A-Za-zА-Яа-я])", text
            )
            if 0 < int(m.group(1)) < 1000
        ]
        return int(numbers[-1].group(1)) if numbers else 0

    # ---------- API источника ----------
    def available_dubbings(self) -> list[str]:
        """Статически известна только своя озвучка-поиск; остальное — по тайтлу."""
        return []

    def dubbings_for(self, anime_id: int, title: str = "",
                     title_alt: str = "") -> list[str]:
        """
        Что Sibnet может предложить для тайтла.

        Поиск может быть закрыт антиботом — тогда список пуст, а причина лежит в
        last_status (приложение покажет её пользователю). Явные привязки
        «Sibnet: <id>» добавляет сам пользователь кнопкой «Добавить озвучку».
        """
        title = (title or self.anime_title or "").strip()
        if not title:
            return []
        found = self._search_videos(int(anime_id), title, title_alt)
        if not found:
            self.last_status = self.client.last_error or "Sibnet: ничего не нашлось"
            return []
        self.last_status = f"Sibnet: найдено видео — {len(found)}"
        return ["Sibnet"]

    def _search_videos(self, anime_id: int, title: str,
                       title_alt: str = "") -> list[dict]:
        """Видео Sibnet, подходящие тайтлу по названию (с кэшем на сеанс)."""
        with self._lock:
            cached = self._found.get(anime_id)
        if cached is not None:
            return cached

        candidates = self.client.search(title)
        if not candidates and title_alt:
            candidates = self.client.search(title_alt)

        matched: list[dict] = []
        for item in candidates:
            score = max(
                _title_similarity(title, item.get("title") or ""),
                _title_similarity(title_alt, item.get("title") or ""),
            )
            # название видео часто длиннее («... 2 серия») — совпадения по
            # вложению _title_similarity уже даёт 0.95, порог мягкий
            if score >= 0.55:
                item = dict(item)
                item["score"] = score
                item["episode"] = self._episode_of(item.get("title") or "")
                matched.append(item)
        matched.sort(key=lambda it: (-it["score"], it.get("episode") or 0))
        with self._lock:
            self._found[anime_id] = matched
        return matched

    def get_episodes(self, anime_id: int) -> Optional[list[tuple[int, str]]]:
        """
        Серии, которые нашлись у Sibnet (по номерам в названиях видео).
        None — «источник не знает», тогда приложение берёт список Shikimori.
        """
        found = self._found.get(int(anime_id))
        if not found:
            return None
        episodes = sorted({item["episode"] for item in found if item.get("episode")})
        if not episodes:
            return None
        return [(number, "") for number in episodes]

    def get_stream_url(self, anime_id: int, episode: int, dubbing: str) -> Optional[str]:
        """Прямая ссылка на mp4 (mpv нужен Referer — см. last_referer)."""
        self.last_referer = SISNET_REFERER

        pinned = self._pinned_id(dubbing)
        if pinned:
            url = self._stream(pinned)
            if url:
                self.last_status = f"Sibnet: видео {pinned}"
                return url
            self.last_status = self.client.last_error or f"Sibnet: видео {pinned} недоступно"
            return None

        # обычная озвучка «Sibnet»: ищем видео нужной серии
        title = (self.anime_title or "").strip()
        if not title:
            self.last_status = "Sibnet: неизвестно название тайтла"
            return None
        found = self._search_videos(int(anime_id), title, self.anime_title_alt)
        if not found:
            self.last_status = self.client.last_error or "Sibnet: ничего не нашлось"
            return None

        exact = [item for item in found if item.get("episode") == int(episode)]
        if not exact:
            # в названии нет номера серии (одно видео на весь тайтл) — берём первое
            exact = [item for item in found if not item.get("episode")]
        if not exact:
            self.last_status = f"Sibnet: серия {episode} не найдена"
            return None

        url = self._stream(exact[0]["id"])
        if not url:
            self.last_status = self.client.last_error or "Sibnet: поток не получен"
            return None
        self.last_status = f"Sibnet: {exact[0].get('title') or exact[0]['id']}"
        return url

    def _stream(self, video_id: str) -> Optional[str]:
        with self._lock:
            cached = self._streams.get(video_id)
        if cached:
            return cached
        url = self.client.stream(video_id)
        if url:
            with self._lock:
                self._streams[video_id] = url
        return url
