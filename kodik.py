# -*- coding: utf-8 -*-
"""
Источник видео Kodik для Anime Viewer (отдельный модуль рядом с main.py).

Почему отдельным файлом: main.py и без того большой, а источник — вещь сменная.
Модуль подключается автоматически (см. main.py -> _load_external_source): он лежит
рядом с main.py, поэтому `from main import VideoSource` внутри него получает ТОТ ЖЕ
класс, что и у приложения (main.py заранее кладёт себя в sys.modules["main"]).

Как устроена выдача Kodik (проверено на живом API, не по догадкам):

    1) Токен. Публичный токен лежит в открытом JS-скрипте виджета:
       https://kodik-add.com/add-players.min.js  ->  token="<32 hex>".
       Kodik периодически его меняет, поэтому токен читается динамически;
       KODIK_FALLBACK_TOKEN используется, только если скрипт недоступен.
       Свой токен можно задать переменной окружения KODIK_TOKEN.

    2) GET https://kodik-api.com/get-player?token=..&shikimori_id=..&episode=..
       -> {"found":true,"allowed":1,"quality":"BDRip 720p",
           "translation":"Onibaku",
           "link":"//kodikplayer.com/serial/6574/<hash>/720p"}

    3) Страница плеера (ссылка из п.2) отдаётся сервером готовой: в ней есть
       подписи запроса (d_sign, pd_sign, ref_sign), список ВСЕХ озвучек
       (data-id, data-title, data-translation-type, data-media-id, data-media-hash)
       и список серий выбранной озвучки (value="1", data-id, data-hash).

    4) Ссылка на поток: POST https://kodikplayer.com/ftor (обычная форма, не JSON!)
       с подписями + {type, id, hash} конкретной серии. В ответе — links по
       качествам, где src закодирован (сдвиг букв +18 и base64). Декодер повторён
       в decode_src() — он же используется самим плеером Kodik.

Ограничения и честные статусы: если тайтла нет, если у озвучки нет серии, если
Kodik ответил 401/404 — источник возвращает None и пишет причину в last_status,
чтобы пользователь увидел её в статусной строке, а не пустой экран.
"""

from __future__ import annotations

import base64
import json
import os
import re
import threading
import time
from typing import Any, Optional

import requests

# Класс-родитель и общие помощники берём из приложения: так источник считается
# «своим» (MultiVideoSource сравнивает типы) и переиспользует настройки сети.
from main import REQUEST_TIMEOUT, USER_AGENT, VideoSource, _title_similarity

# Кэш с TTL (см. cache_manager.py): список качеств серии и сами ссылки живут
# 10 минут, поэтому переключение озвучки/качества не бьёт по сети каждый раз.
from cache_manager import QUALITY_CACHE

# Хосты API: kodik-api.com — текущий, kodikapi.com — старый (оставлен на случай
# возврата; на него в коде парсеров чаще всего и ссылаются).
KODIK_API_HOSTS = ("https://kodik-api.com", "https://kodikapi.com")
# Откуда берём публичный токен: официальный виджет Kodik.
KODIK_JS_URLS = (
    "https://kodik-add.com/add-players.min.js",
    "https://kodik.biz/add-players.min.js",
)
# Резервный токен: проверен 21.09.2026. Kodik меняет его время от времени —
# если источник вдруг пишет «токен не принят», обновите значение (или задайте
# переменную окружения KODIK_TOKEN).
KODIK_FALLBACK_TOKEN = "447d179e875efe44217f20d1ee2146be"
# Качества в порядке предпочтения (в ответе /ftor ключи могут быть не все).
KODIK_QUALITIES = ("1080", "720", "480", "360")
# Сколько живёт разобранная страница плеера: подписи в ней содержат метку времени.
PAGE_TTL = 25 * 60
# Сколько живёт кэш ссылок на серию (и, значит, список доступных качеств).
STREAM_TTL = 10 * 60


def quality_label(key: Any) -> str:
    """
    «1080» / «1080p» / «BDRip 1080» -> «1080p» — подпись качества для списка в UI.

    Kodik называет качества по-разному (в /ftor это ключи вида «480», в ссылке
    плеера — «720p»), поэтому приводим к одному виду.
    """
    digits = re.findall(r"\d+", str(key or ""))
    return f"{digits[0]}p" if digits else str(key or "").strip()


def quality_rank(label: str) -> int:
    """Число из подписи качества — для сортировки от лучшего к худшему."""
    digits = re.findall(r"\d+", str(label or ""))
    try:
        return int(digits[0]) if digits else 0
    except (IndexError, ValueError):
        return 0


def decode_src(src: str) -> str:
    """
    Расшифровывает поле src из ответа /ftor.

    Kodik отдаёт ссылку в «сдвинутом» виде: каждая буква сдвинута на +18 по
    алфавиту (с переносом через Z/z), затем всё это — base64. Ровно так же делает
    сам плеер (atob после replace), см. комментарий в шапке модуля.
    """
    if not src:
        return ""
    if "//" in src or src.startswith("http"):
        return src          # уже обычная ссылка, декодировать нечего
    shifted = []
    for ch in src:
        if ch.isalpha():
            code = ord(ch) + 18
            limit = 90 if ch <= "Z" else 122
            shifted.append(chr(code if code <= limit else code - 26))
        else:
            shifted.append(ch)
    text = "".join(shifted)
    text += "=" * (-len(text) % 4)      # в JS atob добирает паддинг сам
    try:
        return base64.b64decode(text).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 — чужой формат не должен ронять источник
        return ""


class KodikPage:
    """Разобранная страница плеера Kodik: подписи, озвучки и серии."""

    def __init__(self, link: str, html: str) -> None:
        self.link = link
        self.quality = link.rsplit("/", 1)[-1] if "/" in link else "720p"
        self.signs: dict[str, str] = {
            "d": self._grab(html, r'var domain = "([^"]+)"'),
            "d_sign": self._grab(html, r'var d_sign = "([^"]+)"'),
            "pd": self._grab(html, r'var pd = "([^"]+)"'),
            "pd_sign": self._grab(html, r'var pd_sign = "([^"]+)"'),
            "ref": self._grab(html, r'var ref = "([^"]*)"'),
            "ref_sign": self._grab(html, r'var ref_sign = "([^"]+)"'),
            "bad_user": "false",
            "cdn_is_working": "true",
        }
        # текущая озвучка страницы и её тип (seria — серия сериала, video — фильм)
        try:
            self.translation_id: Optional[int] = int(
                self._grab(html, r"var translationId = (\d+)") or 0
            ) or None
        except ValueError:
            self.translation_id = None
        self.vinfo = {
            "type": self._grab(html, r"vInfo\.type = '([^']+)'"),
            "hash": self._grab(html, r"vInfo\.hash = '([^']+)'"),
            "id": self._grab(html, r"vInfo\.id = '([^']+)'"),
        }
        self.translations = self._parse_translations(html)
        self.episodes = self._parse_episodes(html)
        self.fetched_at = time.time()

    # ---------- разбор HTML ----------
    @staticmethod
    def _grab(html: str, pattern: str, default: str = "") -> str:
        match = re.search(pattern, html)
        return match.group(1) if match else default

    @classmethod
    def _block(cls, html: str, css_class: str) -> str:
        """Содержимое <div class="..."> (до закрывающего </div> того же блока)."""
        match = re.search(
            r'<div class="' + re.escape(css_class) + r'">(.*?)</div>', html, re.S
        )
        return match.group(1) if match else ""

    @classmethod
    def _parse_translations(cls, html: str) -> list[dict]:
        """Озвучки тайтла: имя, id, тип (voice/subtitles) и «свои» id/hash тайтла."""
        items: list[dict] = []
        block = cls._block(html, "serial-translations-box")
        for match in re.finditer(r"<option(.*?)>", block, re.S):
            tag = match.group(1)
            trans_id = re.search(r'data-id="(\d+)"', tag)
            title = re.search(r'data-title="([^"]*)"', tag)
            if not trans_id or not title:
                continue
            kind = re.search(r'data-translation-type="([^"]*)"', tag)
            media_id = re.search(r'data-media-id="(\d+)"', tag)
            media_hash = re.search(r'data-media-hash="([0-9a-f]+)"', tag)
            count = re.search(r'data-episode-count="(\d+)"', tag)
            items.append({
                "id": int(trans_id.group(1)),
                "name": title.group(1),
                "type": kind.group(1) if kind else "",
                "media_id": media_id.group(1) if media_id else "",
                "media_hash": media_hash.group(1) if media_hash else "",
                "episodes": int(count.group(1)) if count else 0,
            })
        return items

    @classmethod
    def _parse_episodes(cls, html: str) -> dict[int, dict]:
        """Серии текущей озвучки: {номер: {"id", "hash"}}."""
        result: dict[int, dict] = {}
        block = cls._block(html, "serial-series-box")
        for match in re.finditer(r"<option(.*?)>", block, re.S):
            tag = match.group(1)
            number = re.search(r'value="(\d+)"', tag)
            media_id = re.search(r'data-id="(\d+)"', tag)
            media_hash = re.search(r'data-hash="([0-9a-f]+)"', tag)
            if not (number and media_id and media_hash):
                continue
            result[int(number.group(1))] = {
                "id": media_id.group(1),
                "hash": media_hash.group(1),
            }
        return result

    # ---------- удобные свойства ----------
    @property
    def is_serial(self) -> bool:
        """Сериал ли это (у фильмов серий нет — там один поток)."""
        return "/serial/" in self.link or bool(self.episodes)

    @property
    def media_type(self) -> str:
        """Что подставлять в /ftor: 'seria' для серии, иначе тип страницы."""
        if self.is_serial:
            return "seria"
        return self.vinfo.get("type") or "video"

    def translation_by_id(self, translation_id: Optional[int]) -> Optional[dict]:
        for item in self.translations:
            if item["id"] == translation_id:
                return item
        return None


class KodikClient:
    """
    Низкоуровневый клиент Kodik: токен, /get-player, страница плеера, /ftor.

    Все методы возвращают None вместо исключений: источник не должен ломать
    приложение из-за изменения чужого API.
    """

    def __init__(self, token: str = "", timeout: int = REQUEST_TIMEOUT) -> None:
        self.timeout = max(5, int(timeout))
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        })
        self._token = (token or os.environ.get("KODIK_TOKEN") or "").strip()
        self._token_lock = threading.Lock()
        self._host: Optional[str] = None
        self.last_error: str = ""

    # ---------- токен ----------
    @property
    def token(self) -> str:
        """Токен Kodik: сначала свой/из окружения, затем из виджета, затем резервный."""
        if self._token:
            return self._token
        with self._token_lock:
            if self._token:
                return self._token
            for url in KODIK_JS_URLS:
                extracted = self._token_from_js(url)
                if extracted:
                    self._token = extracted
                    print(f"[Kodik] токен получен из {url}")
                    return self._token
            self._token = KODIK_FALLBACK_TOKEN
            print("[Kodik] не удалось прочитать токен из виджета — использую резервный")
            return self._token

    def _token_from_js(self, url: str) -> str:
        try:
            resp = self.session.get(url, timeout=self.timeout)
            resp.raise_for_status()
            match = re.search(r'token\s*=\s*"([0-9a-fA-F]{16,64})"', resp.text)
            return match.group(1) if match else ""
        except Exception as exc:  # noqa: BLE001
            print(f"[Kodik] {url}: {type(exc).__name__}: {exc}")
            return ""

    # ---------- API ----------
    def _api_get(self, path: str, params: dict) -> Optional[dict]:
        """GET к API Kodik с перебором хостов (kodik-api.com / kodikapi.com)."""
        hosts = [self._host] if self._host else list(KODIK_API_HOSTS)
        for host in hosts:
            if not host:
                continue
            try:
                resp = self.session.get(
                    host + path, params=params, timeout=self.timeout
                )
            except Exception as exc:  # noqa: BLE001 — хост может быть заблокирован
                self.last_error = f"{host}: {type(exc).__name__}: {exc}"
                continue
            if resp.status_code == 401:
                self.last_error = "Kodik не принял токен (нужно обновить KODIK_FALLBACK_TOKEN)"
                continue
            if resp.status_code >= 400:
                self.last_error = f"{host}: HTTP {resp.status_code}"
                continue
            try:
                data = resp.json()
            except ValueError:
                self.last_error = f"{host}: ответ не JSON"
                continue
            self._host = host
            self.last_error = ""
            return data
        return None

    def get_player(
        self,
        shikimori_id: Any,
        episode: Optional[int] = None,
        translation_id: Optional[int] = None,
        quality: str = "720p",
    ) -> Optional[dict]:
        """
        Ссылка на страницу плеера для тайтла Shikimori.

        Без translation_id Kodik сам выбирает озвучку (она у него «плавающая» —
        от запроса к запросу бывает разной), поэтому полный список озвучек
        берётся со страницы плеера, см. open_page().
        """
        params: dict[str, Any] = {
            "token": self.token,
            "shikimori_id": int(shikimori_id),
            "quality": quality,
        }
        if episode:
            params["episode"] = int(episode)
        if translation_id:
            params["translation_id"] = int(translation_id)
        data = self._api_get("/get-player", params)
        if not isinstance(data, dict):
            return None
        if not data.get("found") or not data.get("allowed"):
            self.last_error = "тайтла нет в Kodik"
            return None
        link = str(data.get("link") or "")
        if not link:
            self.last_error = "Kodik не вернул ссылку на плеер"
            return None
        if link.startswith("//"):
            link = "https:" + link
        return {"link": link, "translation": data.get("translation") or "",
                "quality": data.get("quality") or ""}

    # ---------- страница плеера ----------
    def open_page(self, link: str) -> Optional[KodikPage]:
        try:
            resp = self.session.get(
                link, timeout=self.timeout, headers={"Referer": "https://kodik.info/"}
            )
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"страница плеера недоступна: {type(exc).__name__}: {exc}"
            return None
        page = KodikPage(link, resp.text)
        if not page.signs.get("d_sign"):
            self.last_error = "на странице плеера нет подписей запроса"
            return None
        return page

    def link_for_translation(self, page: KodikPage, translation: dict) -> str:
        """
        Ссылка на страницу плеера для другой озвучки.

        У каждой озвучки свой набор media-id/hash (в ссылке Kodik это id/hash),
        поэтому чужую озвучку нельзя взять с текущей страницы: открываем её
        собственную страницу — так же, как это делает выбор перевода в плеере.
        """
        media_id = translation.get("media_id") or ""
        media_hash = translation.get("media_hash") or ""
        if not media_id or not media_hash:
            return ""
        return f"https://kodikplayer.com/serial/{media_id}/{media_hash}/{page.quality}"

    # ---------- поток ----------
    def ftor(self, page: KodikPage, media_id: str, media_hash: str,
             media_type: str = "") -> list[tuple[str, str]]:
        """
        Запрашивает ссылки на серию: [(качество, ссылка), ...].

        Важно: /ftor принимает ОБЫЧНУЮ форму (application/x-www-form-urlencoded).
        Если отправить JSON, сервер отвечает 404 — проверено.
        """
        data = dict(page.signs)
        data.update({
            "type": media_type or page.media_type,
            "id": str(media_id),
            "hash": str(media_hash),
        })
        try:
            resp = self.session.post(
                "https://kodikplayer.com/ftor",
                data=data,
                timeout=self.timeout,
                headers={
                    "Referer": page.link,
                    "Origin": "https://kodikplayer.com",
                    "X-Requested-With": "XMLHttpRequest",
                },
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"/ftor: {type(exc).__name__}: {exc}"
            return []

        links = payload.get("links") or {}
        result: list[tuple[str, str]] = []
        if isinstance(links, dict):
            for quality, items in links.items():
                for item in items or []:
                    if not isinstance(item, dict):
                        continue
                    url = decode_src(str(item.get("src") or ""))
                    result.append((str(quality), self._absolute(url)))
        # иногда Kodik отдаёт одиночное поле link (готовый HLS-манифест)
        single = self._absolute(decode_src(str(payload.get("link") or "")))
        if single:
            result.append((str(payload.get("default") or "auto"), single))
        return [(quality, url) for quality, url in result if url]

    @staticmethod
    def _absolute(url: str) -> str:
        if not url:
            return ""
        if url.startswith("//"):
            return "https:" + url
        if url.startswith("/"):
            return "https://kodikplayer.com" + url
        return url if url.startswith("http") else ""

    @staticmethod
    def best_link(links: list[tuple[str, str]]) -> str:
        """
        Лучшая ссылка из доступных (1080 → 720 → 480 → 360).

        Сравниваем по числу из подписи качества, а не по порядку ключей в ответе:
        Kodik отдаёт их вперемешку, и «первый ключ» вполне может оказаться 360p.
        """
        if not links:
            return ""
        by_quality: dict[str, str] = {}
        for quality, url in links:
            label = quality_label(quality)
            if label and label not in by_quality:
                by_quality[label] = url
        if not by_quality:
            return links[0][1]
        return by_quality[max(by_quality, key=quality_rank)]

    # ---------- выбор качества ----------
    @staticmethod
    def qualities_of(links: list[tuple[str, str]]) -> list[str]:
        """
        Доступные качества серии: «1080p», «720p», «480p» — от лучшего к худшему.

        Именно этот список показывается в списке «Качество» рядом с озвучкой.
        """
        labels = {quality_label(quality) for quality, _url in links if str(quality).strip()}
        return sorted((label for label in labels if label), key=quality_rank, reverse=True)

    @staticmethod
    def link_for_quality(links: list[tuple[str, str]], quality: str = "") -> str:
        """
        Ссылка нужного качества. Пустая строка — «авто»: берём лучшее (best_link).

        Если запрошенного качества у серии нет (Kodik отдаёт не все), возвращаем
        лучшее доступное: серия должна играть, а не молча не запускаться.
        """
        if not links:
            return ""
        wanted = str(quality or "").strip()
        if not wanted:
            return KodikClient.best_link(links)
        target = quality_rank(wanted)
        for quality_key, url in links:
            if quality_rank(quality_label(quality_key)) == target:
                return url
        return KodikClient.best_link(links)


class KodikVideoSource(VideoSource):
    """
    Источник видео Kodik: озвучки, серии и прямые HLS-ссылки.

    В интерфейсе озвучки помечаются источником («AniDUB (Kodik)»), потому что
    Kodik отдаёт много переводов на один тайтл, а имена у них пересекаются с
    другими источниками.
    """

    name = "kodik"
    label = "Kodik"
    annotate_dubbings = True     # подписывать озвучки именем источника в списке
    supports_quality = True      # умеет отдавать несколько качеств (см. get_qualities)
    quality: str = ""            # выбранное качество: "" — «авто» (лучшее доступное)

    def __init__(self, client: Optional[KodikClient] = None) -> None:
        self.client = client or KodikClient()
        self.last_referer = ""
        # Выбранное качество: его ставит интерфейс (см. MultiVideoSource.set_quality),
        # а get_qualities() использует озвучку из quality_dubbing как значение по умолчанию
        self.quality = ""
        self.quality_dubbing = ""
        # кэши: страницы плеера, соответствие «озвучка -> перевод» и готовые ссылки
        self._pages: dict[str, KodikPage] = {}
        self._translation_pages: dict[tuple[int, int], KodikPage] = {}
        self._translations: dict[int, list[dict]] = {}
        self._translation_map: dict[tuple[int, str], dict] = {}
        self._translation_names: dict[int, str] = {}   # id тайтла -> имя озвучки Kodik
        # ключ ссылки включает качество: переключение туда-обратно идёт из кэша
        self._streams: dict[tuple[int, int, str, str], str] = {}
        self._lock = threading.Lock()

    # ---------- служебное ----------
    @staticmethod
    def _translation_label(item: dict) -> str:
        """
        Подпись озвучки для интерфейса.

        У Kodik есть служебный перевод «Субтитры»: помечаем его пояснением, но
        не дублируем, если слово уже есть в названии (иначе выходило
        «Субтитры (субтитры)»).
        """
        name = str(item.get("name") or "").strip()
        if item.get("type") == "subtitles" and "субтит" not in name.lower():
            return f"{name} (субтитры)"
        return name

    def _cached_page(self, key: tuple, store: dict, factory) -> Optional[KodikPage]:
        """Страница из кэша или новая (кэш живёт PAGE_TTL — подписи стареют)."""
        with self._lock:
            page = store.get(key)
        if page is not None and (time.time() - page.fetched_at) < PAGE_TTL:
            return page
        page = factory()
        if page is not None:
            with self._lock:
                store[key] = page
        return page

    def _default_page(self, anime_id: int) -> Optional[KodikPage]:
        """Страница плеера тайтла: озвучки и серии той озвучки, что дал Kodik."""
        cached = self._pages.get(str(anime_id))
        if cached is not None and (time.time() - cached.fetched_at) < PAGE_TTL:
            return cached

        def factory() -> Optional[KodikPage]:
            player = self.client.get_player(anime_id, episode=1)
            if not player:
                return None
            return self.client.open_page(player["link"])

        page = self._cached_page(str(anime_id), self._pages, factory)
        if page is not None:
            self.last_status = f"Kodik: озвучек — {len(page.translations)}"
        else:
            self.last_status = f"Kodik: {self.client.last_error or 'тайтла нет'}"
        return page

    def _page_for_translation(self, anime_id: int, page: KodikPage,
                              translation: dict) -> Optional[KodikPage]:
        """Страница плеера нужной озвучки (у каждой озвучки свои id серий)."""
        if translation.get("id") == page.translation_id:
            return page
        key = (int(anime_id), int(translation.get("id") or 0))

        def factory() -> Optional[KodikPage]:
            link = self.client.link_for_translation(page, translation)
            return self.client.open_page(link) if link else None

        return self._cached_page(key, self._translation_pages, factory)

    def _match_translation(self, anime_id: int, dubbing: str,
                           page: KodikPage) -> Optional[dict]:
        """
        Находит перевод по имени озвучки.

        Сначала точное совпадение, затем — по похожести названия: в интерфейсе
        могли написать «AniDUB», а Kodik называет перевод «AniDUB (25 эп.)» или
        «AniLibria.TV» вместо «AniLibria».
        """
        wanted = (dubbing or "").strip()
        for suffix in (" (субтитры)", " (Кодек)", " (Kodik)"):
            if wanted.endswith(suffix):
                wanted = wanted[: -len(suffix)].strip()
        wanted_subtitles = "(субтитры)" in (dubbing or "").lower()
        if not wanted:
            return None

        for item in page.translations:
            if item["name"].strip().lower() == wanted.lower():
                if wanted_subtitles and item.get("type") != "subtitles":
                    continue
                return item
        # было «AniLibria», а Kodik зовёт перевод «AniLibria.TV»
        base = page.translations[0] if page.translations else None
        best, best_score = None, 0.0
        for item in page.translations:
            score = _title_similarity(wanted, item["name"])
            if score > best_score:
                best, best_score = item, score
        if best is not None and best_score >= 0.75:
            return best
        # не нашли — запомним, что было в Kodik, чтобы объяснить пользователю
        names = ", ".join(item["name"] for item in page.translations[:8])
        print(f"[Kodik] для «{wanted}» нет перевода. В Kodik есть: {names}")
        return base if base is None else None

    # ---------- API источника ----------
    def available_dubbings(self) -> list[str]:
        """
        Статического списка нет: озвучки у Kodik зависят от тайтла, поэтому они
        приходят из dubbings_for() и подписываются именем источника.
        """
        return []

    def dubbings_for(self, anime_id: int, title: str = "",
                     title_alt: str = "") -> list[str]:
        """Озвучки Kodik для текущего тайтла (имена из его API)."""
        page = self._pages.get(str(anime_id))
        if page is None:
            page = self._default_page(int(anime_id))
        if page is None:
            return []
        names: list[str] = []
        for item in page.translations:
            label = self._translation_label(item)
            if label and label not in names:
                names.append(label)
        with self._lock:
            self._translations[int(anime_id)] = page.translations
            for item in page.translations:
                self._translation_map[(int(anime_id), self._translation_label(item))] = item
        return names

    def get_episodes(self, anime_id: int) -> Optional[list[tuple[int, str]]]:
        """Серии тайтла в Kodik (по умолчанию — той озвучки, что вернул Kodik)."""
        page = self._default_page(int(anime_id))
        if page is None or not page.episodes:
            return None
        return [(number, "") for number in sorted(page.episodes)]

    def _variants(self, anime_id: int, episode: int,
                  dubbing: str) -> list[tuple[str, str]]:
        """
        Все ссылки серии по качествам: [(«720p», url), («480p», url), …].

        Это «дорогой» путь (страница плеера + /ftor), поэтому результат кладётся в
        кэш на 10 минут: список качеств и переключение между ними после первого
        запроса мгновенные и не тратят трафик.
        """
        key = ("kodik", int(anime_id), int(episode), str(dubbing))
        cached = QUALITY_CACHE.get(key)
        if cached:
            return list(cached)

        page = self._default_page(int(anime_id))
        if page is None:
            self.last_status = f"Kodik: {self.client.last_error or 'тайтла нет'}"
            return []

        translation = (self._translation_map.get((int(anime_id), str(dubbing)))
                       or self._match_translation(int(anime_id), dubbing, page))
        if translation is None:
            names = ", ".join(item["name"] for item in page.translations[:10])
            self.last_status = f"в Kodik нет озвучки «{dubbing}» (есть: {names})"
            return []

        trans_page = self._page_for_translation(int(anime_id), page, translation)
        if trans_page is None:
            self.last_status = f"Kodik: {self.client.last_error or 'страница озвучки недоступна'}"
            return []

        media = trans_page.episodes.get(int(episode))
        if media is None:
            # фильмы и односерийные видео: у них нет списка серий, только поток
            if not trans_page.is_serial and trans_page.vinfo.get("id"):
                media = {"id": trans_page.vinfo["id"], "hash": trans_page.vinfo["hash"]}
            else:
                total = len(trans_page.episodes) or translation.get("episodes") or 0
                self.last_status = (
                    f"в Kodik у озвучки «{translation['name']}» нет серии {episode}"
                    + (f" (всего серий: {total})" if total else "")
                )
                return []

        links = self.client.ftor(
            trans_page, media["id"], media["hash"], trans_page.media_type
        )
        if not links:
            self.last_status = f"Kodik: {self.client.last_error or 'поток не получен'}"
            return []

        # ключ качества приводим к подписи («720» -> «720p»): так его понимает UI
        normalized = [(quality_label(quality), url) for quality, url in links if url]
        with self._lock:
            self._translation_names[int(anime_id)] = translation["name"]
        QUALITY_CACHE.set(key, normalized, ttl=STREAM_TTL)
        return normalized

    def get_qualities(self, anime_id: int, episode: int, dubbing: str = "") -> list[str]:
        """
        Доступные качества серии в выбранной озвучке: [«1080p», «720p», …].

        Метод ходит в сеть (как и get_stream_url), поэтому вызывается из фонового
        потока. Пустой список означает «источник качеств не знает» — тогда в списке
        останется только пункт «Авто (лучшее)».
        """
        dubbing = str(dubbing or self.quality_dubbing or "")
        if not dubbing:
            # без озвучки качеств не узнать: они зависят от страницы перевода
            return []
        links = self._variants(int(anime_id), int(episode), dubbing)
        return self.client.qualities_of(links)

    def get_stream_url(self, anime_id: int, episode: int, dubbing: str,
                       quality: str = "") -> Optional[str]:
        """
        Прямая HLS-ссылка (m3u8) нужной серии, озвучки и качества.

        quality: «720p» / «480p» / «» — авто (лучшее доступное). Если качество не
        указано явно, берётся self.quality — его выставляет интерфейс при выборе в
        списке «Качество» (сигнатуру метода менять не пришлось).
        """
        self.last_referer = ""
        wanted = str(quality or self.quality or "")
        key = (int(anime_id), int(episode), str(dubbing), wanted)
        cached = self._streams.get(key)
        if cached:
            self.last_status = f"Kodik: {dubbing} (из кэша)"
            return cached

        links = self._variants(int(anime_id), int(episode), dubbing)
        if not links:
            return None      # причина уже лежит в last_status (см. _variants)

        url = self.client.link_for_quality(links, wanted)
        if not url:
            self.last_status = f"Kodik: {self.client.last_error or 'поток не получен'}"
            return None

        best = self.client.qualities_of(links)
        best_label = best[0] if best else ""
        selected = quality_label(wanted) if wanted else ""
        with self._lock:
            self._streams[key] = url
        name = self._translation_names.get(int(anime_id), dubbing)
        self.last_status = (
            f"Kodik: {name}, серия {episode}"
            + (f" ({selected})" if selected else f" (авто, {best_label})")
        )
        return url

    # ---------- диагностика ----------
    def dump(self) -> str:
        """Текстовый отчёт о том, что знает источник (для консоли и логов)."""
        return json.dumps(
            {
                "token": self.client.token[:8] + "…",
                "страниц в кэше": len(self._pages) + len(self._translation_pages),
                "потоков в кэше": len(self._streams),
                "последняя ошибка": self.client.last_error,
            },
            ensure_ascii=False,
        )
