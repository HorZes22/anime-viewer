# -*- coding: utf-8 -*-
"""
Источники ранобэ (текста) для Anime Viewer — модуль рядом с main.py.

Что даёт:

  * TextSource — общий интерфейс источника текста (как MangaSource для манги):
    search() -> chapters() -> text(). Плагин может добавить свой сайт, унаследовав
    этот класс и вернув его из Plugin.text_sources();
  * LocalTextSource — ГАРАНТИРОВАННО работает без сети: читает .txt, .md, .html,
    .fb2 и .epub из папки books рядом с программой. Главы делятся по заголовкам
    («Глава 3», «# Глава 3», «<h2>Глава 3</h2>»), у fb2/epub — по структуре файла;
  * RanobeHubSource — ranobehub.org (русские ранобэ): поиск /api/search?q=,
    список глав /api/books/{id}/chapters, текст /api/chapters/{id} (поле html);
  * RanobeLibSource — ranobelib.me через их API (api.lib.social). API может быть
    недоступен из вашей сети — источник тогда честно пишет причину в last_note;
  * RoyalRoadSource — royalroad.com (английские веб-новеллы): поиск по разметке,
    главы со страницы книги, текст из блока chapter-content;
  * RanobeLibrary — набор источников и поиск «сразу везде» (режим «Все источники»
    во вкладке «Ранобэ»), с пометкой источника у каждой найденной книги.

Приложение не ходит в сеть из интерфейса: поиск и загрузка главы выполняются в
фоновых потоках (Worker), а сюда передаются готовые строки текста.
"""

from __future__ import annotations

import html
import re
import zipfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, urljoin
from xml.etree import ElementTree

import requests

REQUEST_TIMEOUT = 25
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
ACCENT_UA = (
    "AnimeViewer/1.0 (local desktop app; python-requests)"
)
# Заголовок главы в обычном текстовом файле и в markdown
CHAPTER_RE = re.compile(
    r"^\s*(?:глава|часть|том|chapter|part|volume)\s*[№#:]?\s*([0-9]+|[IVXLC]+)\b[^\n]*$",
    re.IGNORECASE | re.MULTILINE,
)
MARKDOWN_HEADER_RE = re.compile(r"^#{1,3}\s+.*$", re.MULTILINE)
HTML_HEADER_RE = re.compile(r"(?is)<h[12][^>]*>.*?</h[12]>")
TXT_ENCODINGS = ("utf-8-sig", "utf-8", "cp1251", "cp866", "latin-1")


# ======================================================================================
# Общие помощники разбора текста
# ======================================================================================
def clean_text(text: str) -> str:
    """Приводит текст к виду, удобному для чтения: без лишних пустых строк."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def html_to_text(markup: str) -> str:
    """
    HTML -> обычный текст с абзацами.

    Блочные теги превращаются в пустую строку (абзац), <br> — в перевод строки,
    остальные теги убираются, сущности (&nbsp;, &laquo;) раскодируются.
    """
    markup = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", markup or "")
    markup = re.sub(r"(?is)<br\s*/?>", "\n", markup)
    markup = re.sub(
        r"(?is)</(p|div|h[1-6]|li|section|article|blockquote|tr)>", "\n\n", markup
    )
    markup = re.sub(r"<[^>]+>", "", markup)
    return clean_text(html.unescape(markup))


def extract_block(markup: str, start_pattern: str) -> str:
    """
    Возвращает содержимое блока по его открывающему тегу, считая вложенные теги.

    Регулярное выражение «до первого </div>» обрывало бы текст на вложенном блоке
    (в главах их много), поэтому баланс тегов считается вручную.
    """
    match = re.search(start_pattern, markup, re.I)
    if not match:
        return ""
    start = match.end()
    depth = 1
    position = start
    for tag in re.finditer(r"(?i)<(/?)div\b[^>]*>", markup[start:]):
        depth += -1 if tag.group(1) else 1
        if depth == 0:
            position = start + tag.start()
            break
    else:
        position = len(markup)
    return markup[start:position]


def read_text_file(path: Path) -> str:
    """Читает текстовый файл, подбирая кодировку (русские .txt часто в cp1251)."""
    for encoding in TXT_ENCODINGS:
        try:
            return path.read_text(encoding=encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        except OSError:
            return ""
    return ""


# ======================================================================================
# Разбор локальных файлов
# ======================================================================================
def split_by_matches(text: str, matches: list, titles: list[str]) -> list[tuple[str, str]]:
    """Делит текст по позициям заголовков: [(заголовок, тело), ...]."""
    chapters: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        title = titles[index]
        body = text[match.end():end].strip()
        chapters.append((title or f"Глава {index + 1}", body))
    return chapters


def split_txt_chapters(text: str) -> list[tuple[str, str]]:
    """
    Делит .txt на главы по строкам-заголовкам.

    Если заголовков нет — весь текст считается одной главой: так «сплошной» файл
    всё равно можно читать (просто список глав будет из одного пункта).
    """
    matches = list(CHAPTER_RE.finditer(text))
    if not matches:
        return [("Весь текст", text)]
    titles = [text[m.start():m.end()].strip() for m in matches]
    return split_by_matches(text, matches, titles)


def split_md_chapters(text: str) -> list[tuple[str, str]]:
    """Делит markdown на главы по заголовкам #, ## и ###."""
    matches = list(MARKDOWN_HEADER_RE.finditer(text))
    if not matches:
        return [("Весь текст", text)]
    titles = [text[m.start():m.end()].lstrip("# ").strip() for m in matches]
    return split_by_matches(text, matches, titles)


def split_html_chapters(markup: str) -> list[tuple[str, str]]:
    """Делит HTML на главы по заголовкам <h1>/<h2>."""
    matches = list(HTML_HEADER_RE.finditer(markup))
    if not matches:
        text = html_to_text(markup)
        return [("Весь текст", text)] if text else []
    chapters: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markup)
        title = html_to_text(match.group(0)) or f"Глава {index + 1}"
        body = html_to_text(markup[match.end():end])
        if body:
            chapters.append((title, body))
    return chapters


def fb2_chapters(path: Path) -> list[tuple[str, str]]:
    """Главы из fb2 (это XML: секции <section> с заголовками <title>)."""
    try:
        tree = ElementTree.parse(path)
    except (ElementTree.ParseError, OSError):
        return []
    root = tree.getroot()
    body = None
    for element in root.iter():
        if element.tag.endswith("body"):
            body = element
            break
    if body is None:
        return []

    def title_of(section: Any) -> str:
        for node in section.iter():
            if node.tag.endswith("title"):
                parts = [str(t.text or "").strip() for t in node.iter()]
                text = " ".join(part for part in parts if part)
                if text:
                    return re.sub(r"\s+", " ", text)
        return ""

    def text_of(section: Any) -> str:
        lines: list[str] = []
        for node in section.iter():
            if node.tag.endswith(("p", "subtitle", "cite", "empty-line")):
                line = "".join(node.itertext()).strip()
                if line:
                    lines.append(line)
        return clean_text("\n\n".join(lines))

    sections = [s for s in body.iter() if s.tag.endswith("section")]
    if not sections:
        return []
    chapters: list[tuple[str, str]] = []
    for index, section in enumerate(sections):
        # берём только «верхние» секции: вложенные попадут в текст родительской
        if any(section is not other and section in list(other) for other in sections):
            continue
        title = title_of(section) or f"Глава {index + 1}"
        body_text = text_of(section)
        if body_text:
            chapters.append((title, body_text))
    return chapters


def epub_chapters(path: Path) -> list[tuple[str, str]]:
    """Главы из epub: zip с xhtml-файлами, которые идут в порядке чтения."""
    result: list[tuple[str, str]] = []
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            pages = [name for name in names if name.lower().endswith((".xhtml", ".html", ".htm"))]
            pages.sort()
            for index, name in enumerate(pages):
                try:
                    markup = archive.read(name).decode("utf-8", "replace")
                except (KeyError, OSError):
                    continue
                text = html_to_text(markup)
                if not text:
                    continue
                title_match = re.search(r"(?is)<h[1-3][^>]*>(.*?)</h[1-3]>", markup)
                title = html_to_text(title_match.group(1)) if title_match else ""
                if not title:
                    title = text.split("\n", 1)[0][:60].strip() or f"Часть {index + 1}"
                result.append((title, text))
    except (zipfile.BadZipFile, OSError):
        return []
    return result


# ======================================================================================
# Источник текста (базовый класс)
# ======================================================================================
class TextSource(ABC):
    """Источник текста: поиск -> список глав -> текст главы."""

    name: str = "text"
    label: str = "Ранобэ"
    supports_catalog: bool = False
    last_note: str = ""

    @abstractmethod
    def search(self, query: str, limit: int = 20) -> list[dict]:
        """
        Поиск книг: [{"id", "title", "author", "year", "description", "poster_url"}].

        id должен однозначно определять книгу внутри источника — по нему потом
        запрашиваются главы и текст.
        """

    @abstractmethod
    def chapters(self, book_id: str) -> list[dict]:
        """Главы книги: [{"id", "title", "index"}] в порядке чтения."""

    @abstractmethod
    def text(self, book_id: str, chapter_id: str) -> str:
        """Текст главы (обычный текст с абзацами через пустую строку)."""

    def catalog(self, limit: int = 20) -> list[dict]:
        """Все книги источника (если он умеет). По умолчанию — пусто."""
        return []

    # ---------- готовое ----------
    def _http(self) -> requests.Session:
        """Сессия запросов с браузерным User-Agent (её же использует текст глав)."""
        if getattr(self, "_session", None) is None:
            session = requests.Session()
            session.headers.update({
                "User-Agent": BROWSER_UA,
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            })
            self._session = session
        return self._session


class LocalTextSource(TextSource):
    """
    Ранобэ из папки books рядом с программой (.txt, .md, .html, .fb2, .epub).

    Работает полностью без сети: это основной путь, если сайты недоступны.
    Книгой считается файл, главой — часть внутри файла.
    """

    name = "books"
    label = "мои файлы (books)"
    EXTENSIONS = (".txt", ".md", ".html", ".htm", ".fb2", ".epub")

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._books: dict[str, Path] = {}      # id книги -> файл
        self._chapters: dict[str, list[tuple[str, str]]] = {}
        self._scanned = False

    # ---------- обход папки ----------
    def ready(self) -> bool:
        return self.root.is_dir()

    def scan(self, force: bool = False) -> None:
        if self._scanned and not force:
            return
        self._books = {}
        self._chapters = {}
        self._scanned = True
        if not self.ready():
            self.last_note = f"папки {self.root} нет — положите туда .txt/.md/.fb2/.epub"
            return
        try:
            for path in sorted(self.root.rglob("*")):
                if path.is_file() and path.suffix.lower() in self.EXTENSIONS:
                    self._books[self._book_id(path)] = path
        except OSError as exc:
            self.last_note = f"не удалось прочитать {self.root}: {exc}"
            return
        self.last_note = f"книг в папке: {len(self._books)}"

    @staticmethod
    def _book_id(path: Path) -> str:
        """Устойчивый id книги: относительный путь (книга не перепутается)."""
        return str(path).replace("\\", "/")

    def _path(self, book_id: str) -> Optional[Path]:
        self.scan()
        if book_id in self._books:
            return self._books[book_id]
        for path in self._books.values():
            if path.name == book_id or str(path).endswith(book_id):
                return path
        return None

    # ---------- содержимое ----------
    def _load_chapters(self, book_id: str) -> list[tuple[str, str]]:
        if book_id in self._chapters:
            return self._chapters[book_id]
        path = self._path(book_id)
        if path is None:
            return []
        suffix = path.suffix.lower()
        if suffix == ".fb2":
            chapters = fb2_chapters(path)
        elif suffix == ".epub":
            chapters = epub_chapters(path)
        elif suffix in (".html", ".htm"):
            chapters = split_html_chapters(read_text_file(path))
        elif suffix == ".md":
            chapters = split_md_chapters(read_text_file(path))
        else:
            chapters = split_txt_chapters(read_text_file(path))
        if not chapters:
            raw = read_text_file(path)
            chapters = [(path.stem, raw)] if raw else []
        self._chapters[book_id] = chapters
        return chapters

    # ---------- TextSource ----------
    def search(self, query: str, limit: int = 20) -> list[dict]:
        self.scan()
        query_norm = (query or "").strip().lower()
        books: list[dict] = []
        for book_id, path in self._books.items():
            name = path.stem
            if query_norm and query_norm not in name.lower():
                continue
            books.append({
                "id": book_id,
                "title": name,
                "author": "",
                "year": "",
                "description": f"{path.suffix.lstrip('.').upper()} · {path.parent}",
                "poster_url": "",
            })
            if len(books) >= max(1, int(limit)):
                break
        if not books:
            self.last_note = (
                f"в папке {self.root} ничего не нашлось"
                + (f" по запросу «{query}»" if query else "")
            )
        return books

    def catalog(self, limit: int = 20) -> list[dict]:
        return self.search("", limit)

    def chapters(self, book_id: str) -> list[dict]:
        return [
            {"id": str(index), "title": title, "index": index}
            for index, (title, _body) in enumerate(self._load_chapters(book_id))
        ]

    def text(self, book_id: str, chapter_id: str) -> str:
        chapters = self._load_chapters(book_id)
        try:
            index = int(chapter_id)
        except (TypeError, ValueError):
            index = 0
        if not (0 <= index < len(chapters)):
            return ""
        title, body = chapters[index]
        return f"{title}\n\n{body}".strip()


# ======================================================================================
# RanobeHub (русские ранобэ)
# ======================================================================================
class RanobeHubSource(TextSource):
    """
    Ранобэ с ranobehub.org (публичный сайт, без авторизации).

    Проверенные адреса API:
        поиск:    /api/search?q=<запрос>            -> {"books": [...]}
        главы:    /api/books/<id книги>/chapters    -> {"items": [...]}
        текст:    /api/chapters/<id главы>          -> {"chapter": {..., "html": "..."}}

    Важно: параметр поиска именно «q». С «query» сайт отвечает пустым списком —
    на этом легко ошибиться, поэтому в коде оставлен комментарий.
    """

    name = "ranobehub"
    label = "RanobeHub (RU)"

    BASE = "https://ranobehub.org"

    def search(self, query: str, limit: int = 20) -> list[dict]:
        query = (query or "").strip()
        if not query:
            return []
        url = f"{self.BASE}/api/search"
        try:
            resp = self._http().get(
                url,
                params={"q": query, "take": max(1, int(limit))},
                timeout=REQUEST_TIMEOUT,
                headers={"Accept": "application/json", "Referer": f"{self.BASE}/search"},
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            self.last_note = f"RanobeHub: поиск недоступен ({type(exc).__name__}: {exc})"
            return []

        books: list[dict] = []
        for raw in (data.get("books") if isinstance(data, dict) else data) or []:
            if not isinstance(raw, dict):
                continue
            book_id = raw.get("id")
            title = str(raw.get("title") or "").strip()
            if not book_id or not title:
                continue
            poster = str(raw.get("posterUrl") or "")
            if poster.startswith("/"):
                poster = self.BASE + poster
            books.append({
                "id": str(book_id),
                "slug": str(raw.get("slug") or ""),
                "title": title,
                "original_title": str(raw.get("originalTitle") or ""),
                "author": "",
                "year": raw.get("year") or "",
                "status": str(raw.get("status") or ""),
                "description": str(
                    raw.get("shortDescription") or raw.get("description") or ""
                )[:400],
                "poster_url": poster,
                "tags": [str(t) for t in (raw.get("tags") or [])][:5],
            })
            if len(books) >= max(1, int(limit)):
                break
        if not books:
            self.last_note = f"RanobeHub: по запросу «{query}» ничего не нашлось"
        return books

    def chapters(self, book_id: str) -> list[dict]:
        book_id = str(book_id)
        try:
            resp = self._http().get(
                f"{self.BASE}/api/books/{book_id}/chapters",
                timeout=REQUEST_TIMEOUT,
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            self.last_note = f"RanobeHub: список глав недоступен ({type(exc).__name__})"
            return []

        chapters: list[dict] = []
        for index, raw in enumerate((data.get("items") if isinstance(data, dict) else data) or []):
            if not isinstance(raw, dict) or raw.get("isDraft"):
                continue
            chapter_id = raw.get("id")
            if not chapter_id:
                continue
            number = raw.get("number")
            volume = raw.get("volume")
            title = str(raw.get("title") or "").strip()
            if number not in (None, "", 0, "0"):
                head = f"Глава {number}"
            elif volume not in (None, ""):
                head = f"Том {volume}"
            else:
                head = f"Часть {index + 1}"
            chapters.append({
                "id": str(chapter_id),
                "title": f"{head} — {title}" if title else head,
                "index": index,
                "volume": volume,
                "number": number,
            })
        if not chapters:
            self.last_note = "RanobeHub: у книги нет глав"
        return chapters

    def text(self, book_id: str, chapter_id: str) -> str:
        try:
            resp = self._http().get(
                f"{self.BASE}/api/chapters/{chapter_id}",
                timeout=REQUEST_TIMEOUT,
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            self.last_note = f"RanobeHub: глава не открылась ({type(exc).__name__}: {exc})"
            return ""
        chapter = (data or {}).get("chapter") or {}
        markup = str(chapter.get("html") or "")
        title = str(chapter.get("title") or "").strip()
        body = html_to_text(markup)
        if not body:
            self.last_note = "RanobeHub: у главы нет текста (только иллюстрации?)"
            return ""
        return f"{title}\n\n{body}".strip() if title else body


# ======================================================================================
# RanobeLib (русские ранобэ; API api.lib.social)
# ======================================================================================
class RanobeLibSource(TextSource):
    """
    Ранобэ с ranobelib.me через их публичный API (api.lib.social).

    Оговорка: доступность этого API зависит от провайдера (в некоторых сетях он
    не отвечает — источник тогда просто сообщает причину в last_note, приложение
    продолжает работать с остальными источниками). Схема ответов у сайта не
    документирована, поэтому разбор «мягкий»: поля ищутся по известным именам, а
    если структура изменилась — текст ищется как самая длинная HTML-строка в JSON.

    Переменные окружения: RANOBELIB_API (адрес API), RANOBELIB_SITE_ID (код сайта).
    """

    name = "ranobelib"
    label = "RanobeLib (RU)"
    API = "https://api.lib.social"
    SITE_ID = "3"

    def __init__(self) -> None:
        import os

        self.api = os.environ.get("RANOBELIB_API", self.API).rstrip("/")
        self.site_id = os.environ.get("RANOBELIB_SITE_ID", self.SITE_ID)

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Site-Id": self.site_id,
            "Referer": "https://ranobelib.me/",
        }

    def search(self, query: str, limit: int = 20) -> list[dict]:
        query = (query or "").strip()
        if not query:
            return []
        try:
            resp = self._http().get(
                f"{self.api}/api/manga",
                params={
                    "search": query,
                    "site_id[]": self.site_id,
                    "fields[]": ["rate_avg", "summary", "cover", "year", "authors"],
                },
                timeout=REQUEST_TIMEOUT,
                headers=self._headers(),
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            self.last_note = (
                f"RanobeLib: API недоступен из вашей сети ({type(exc).__name__}). "
                "Остальные источники работают."
            )
            return []

        books: list[dict] = []
        for raw in self._iter_dicts(data):
            slug = raw.get("slug") or raw.get("url") or ""
            title = str(raw.get("rus_name") or raw.get("name") or "").strip()
            if not slug or not title or "cover" not in raw and not raw.get("id"):
                continue
            if len(title) > 200:
                continue
            cover = raw.get("cover") or {}
            cover_url = ""
            if isinstance(cover, dict):
                cover_url = str(cover.get("default") or cover.get("thumbnail") or "")
            elif isinstance(cover, str):
                cover_url = cover
            books.append({
                "id": str(slug),
                "title": title,
                "original_title": str(raw.get("name") or ""),
                "author": ", ".join(
                    str(a.get("name")) for a in (raw.get("authors") or [])
                    if isinstance(a, dict)
                ),
                "year": raw.get("year") or "",
                "description": str(raw.get("summary") or "")[:400],
                "poster_url": cover_url,
            })
            if len(books) >= max(1, int(limit)):
                break
        if not books:
            self.last_note = f"RanobeLib: по запросу «{query}» ничего не нашлось"
        return books

    def chapters(self, book_id: str) -> list[dict]:
        try:
            resp = self._http().get(
                f"{self.api}/api/manga/{book_id}/chapters",
                timeout=REQUEST_TIMEOUT,
                headers=self._headers(),
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            self.last_note = f"RanobeLib: главы недоступны ({type(exc).__name__})"
            return []

        chapters: list[dict] = []
        for index, raw in enumerate(self._iter_dicts(data)):
            number = raw.get("number")
            volume = raw.get("volume")
            if number is None:
                continue
            name = str(raw.get("name") or "").strip()
            head = f"Том {volume}, глава {number}" if volume else f"Глава {number}"
            chapters.append({
                "id": f"{volume or 0}/{number}",
                "title": f"{head} — {name}" if name else head,
                "index": index,
                "volume": volume,
                "number": number,
            })
        if not chapters:
            self.last_note = "RanobeLib: у книги нет глав"
        return chapters

    def text(self, book_id: str, chapter_id: str) -> str:
        volume, _, number = str(chapter_id).partition("/")
        try:
            resp = self._http().get(
                f"{self.api}/api/manga/{book_id}/chapter",
                params={"volume": volume, "number": number, "branch_id": "", "translate_id": ""},
                timeout=REQUEST_TIMEOUT,
                headers=self._headers(),
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            self.last_note = f"RanobeLib: текст главы недоступен ({type(exc).__name__})"
            return ""
        markup = self._longest_html(data)
        body = html_to_text(markup)
        if not body:
            self.last_note = "RanobeLib: в ответе API нет текста главы"
        return body

    # ---------- «мягкий» разбор неизвестной структуры ----------
    @staticmethod
    def _iter_dicts(node: Any, depth: int = 0):
        """Перебирает словари внутри ответа (структура у сайта меняется)."""
        if depth > 6:
            return
        if isinstance(node, dict):
            yield node
            for value in node.values():
                yield from RanobeLibSource._iter_dicts(value, depth + 1)
        elif isinstance(node, list):
            for item in node:
                yield from RanobeLibSource._iter_dicts(item, depth + 1)

    @classmethod
    def _longest_html(cls, node: Any) -> str:
        """Самая длинная строка с HTML-тегами во всём ответе — обычно это текст главы."""
        found: list[str] = []

        def visit(value: Any) -> None:
            if isinstance(value, str):
                if len(value) > 200 and ("<p" in value.lower() or "\n\n" in value):
                    found.append(value)
            elif isinstance(value, dict):
                for sub in value.values():
                    visit(sub)
            elif isinstance(value, list):
                for sub in value[:50]:
                    visit(sub)

        visit(node)
        return max(found, key=len) if found else ""


# ======================================================================================
# Royal Road (английские веб-новеллы)
# ======================================================================================
class RoyalRoadSource(TextSource):
    """
    Веб-новеллы с royalroad.com.

    Разметка проверена: результаты поиска — ссылки /fiction/<id>/<slug>, главы —
    ссылки /fiction/<id>/<slug>/chapter/<номер>/<слаг>, текст главы — блок
    <div class="chapter-inner chapter-content">. Источник английский, поэтому в
    поиске лучше писать название латиницей.
    """

    name = "royalroad"
    label = "Royal Road (EN)"
    BASE = "https://www.royalroad.com"
    _FICTION_RE = re.compile(r'href="(/fiction/(\d+)/([^"/]+))"')
    _TITLE_RE = re.compile(r'(?is)<h2[^>]*class="fiction-title"[^>]*>\s*<a[^>]*>(.*?)</a>')

    def search(self, query: str, limit: int = 20) -> list[dict]:
        query = (query or "").strip()
        if not query:
            return []
        try:
            resp = self._http().get(
                f"{self.BASE}/fictions/search",
                params={"title": query},
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            markup = resp.text
        except Exception as exc:  # noqa: BLE001
            self.last_note = f"Royal Road: поиск недоступен ({type(exc).__name__})"
            return []

        titles = [html_to_text(t) for t in self._TITLE_RE.findall(markup)]
        books: list[dict] = []
        seen: set[str] = set()
        for index, match in enumerate(self._FICTION_RE.finditer(markup)):
            fiction_id, slug = match.group(2), match.group(3)
            if fiction_id in seen:
                continue
            seen.add(fiction_id)
            title = titles[len(books)] if len(books) < len(titles) else slug.replace("-", " ").title()
            books.append({
                "id": fiction_id,
                "slug": slug,
                "title": title or slug,
                "author": "",
                "year": "",
                "description": f"{self.BASE}/fiction/{fiction_id}/{slug}",
                "poster_url": "",
            })
            if len(books) >= max(1, int(limit)):
                break
        if not books:
            self.last_note = f"Royal Road: по запросу «{query}» ничего не нашлось"
        return books

    def chapters(self, book_id: str) -> list[dict]:
        book_id = str(book_id)
        try:
            resp = self._http().get(f"{self.BASE}/fiction/{book_id}", timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            markup = resp.text
        except Exception as exc:  # noqa: BLE001
            self.last_note = f"Royal Road: книга не открылась ({type(exc).__name__})"
            return []

        pattern = re.compile(
            r'href="(/fiction/%s/[^"]*?/chapter/(\d+)/([^"/]+))"' % re.escape(book_id)
        )
        chapters: list[dict] = []
        seen: set[str] = set()
        for index, match in enumerate(pattern.finditer(markup)):
            path, number, slug = match.group(1), match.group(2), match.group(3)
            if path in seen:
                continue
            seen.add(path)
            # слаг главы у Royal Road обычно начинается с её номера («001-a-bright-day»),
            # но у части книг номера нет — тогда берём порядковый номер в списке
            slug_text = slug.replace("-", " ").strip()
            leading = re.match(r"(\d{1,4})\b", slug)
            label = f"Глава {int(leading.group(1))}" if leading else f"Глава {index + 1}"
            chapters.append({
                "id": path,                       # путь к главе на сайте
                "title": f"{label} — {slug_text}" if slug_text else label,
                "index": index,
                "number": number,                 # внутренний id главы (для отладки)
            })
        if not chapters:
            self.last_note = "Royal Road: на странице книги нет списка глав"
        return chapters

    def text(self, book_id: str, chapter_id: str) -> str:
        path = str(chapter_id)
        url = path if path.startswith("http") else self.BASE + path
        try:
            resp = self._http().get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            markup = resp.text
        except Exception as exc:  # noqa: BLE001
            self.last_note = f"Royal Road: глава не открылась ({type(exc).__name__})"
            return ""
        block = extract_block(markup, r'<div[^>]*class="[^"]*chapter-inner[^"]*"[^>]*>')
        # запасной путь: блок может быть без вложенных div
        if not block:
            match = re.search(
                r'(?is)<div[^>]*class="[^"]*chapter-content[^"]*"[^>]*>(.*?)</div>', markup
            )
            block = match.group(1) if match else ""
        body = html_to_text(block)
        if not body:
            self.last_note = "Royal Road: текст главы не найден в разметке"
        return body


# ======================================================================================
# Библиотека источников: поиск по одному или сразу по всем
# ======================================================================================
class RanobeLibrary:
    """
    Набор источников ранобэ: свои файлы + RanobeHub + RanobeLib + Royal Road.

    Приложение обращается к библиотеке, а не к конкретному сайту: поиск можно вести
    по одному источнику или сразу по всем («Все источники»), а главы и текст каждая
    книга берёт у того источника, из которого пришла (source_for_book).
    """

    ALL = "all"          # код режима «искать везде»

    def __init__(self, base_dir: Path) -> None:
        self.local = LocalTextSource(Path(base_dir) / "books")
        self.remote: list[TextSource] = [
            RanobeHubSource(),
            RanobeLibSource(),
            RoyalRoadSource(),
        ]
        self.sources: list[TextSource] = [self.local, *self.remote]

    # ---------- источники ----------
    def source_by_name(self, name: str) -> Optional[TextSource]:
        for source in self.sources:
            if source.name == str(name or ""):
                return source
        return None

    def source_for_book(self, book: dict) -> TextSource:
        """
        Источник, из которого пришла книга.

        Нужен для режима «Все источники»: главы и текст должны запрашиваться у того
        сайта, который нашёл книгу, а не у выбранного в списке.
        """
        source = self.source_by_name(str((book or {}).get("source") or ""))
        return source or self.local

    # ---------- поиск ----------
    def search(self, query: str, limit: int = 30, only: Optional[str] = None) -> list[dict]:
        """
        Поиск книг.

        only — код источника, «all» или None: None/«all» означают поиск по всем
        источникам подряд (в том числе по своим файлам). Ошибки источника не мешают
        остальным: причина остаётся в его last_note и видна в статусной строке.
        """
        if only and only not in ("", self.ALL):
            source = self.source_by_name(only)
            if source is None:
                return []
            return self._search_one(source, query, limit)

        result: list[dict] = []
        for source in self.sources:
            remaining = max(1, int(limit)) - len(result)
            if remaining <= 0:
                break
            result.extend(self._search_one(source, query, remaining))
        # свои файлы показываем первыми, дальше — по порядку источников
        return result[: max(1, int(limit))]

    def search_all(self, query: str, limit: int = 30) -> list[dict]:
        """То же, что search(only=None): явный вызов для режима «Все источники»."""
        return self.search(query, limit, only=self.ALL)

    def _search_one(self, source: TextSource, query: str, limit: int) -> list[dict]:
        try:
            if hasattr(source, "scan"):
                try:
                    source.scan(force=False)
                except Exception as exc:  # noqa: BLE001
                    print(f"[ранобэ] {source.name}: сканирование — {exc}")
            found = source.search(query, limit) or []
        except Exception as exc:  # noqa: BLE001 — источник не должен ломать поиск
            print(f"[ранобэ] {source.name}: {type(exc).__name__}: {exc}")
            return []
        result: list[dict] = []
        for book in found:
            book = dict(book)
            book["source"] = source.name
            book["source_label"] = source.label
            result.append(book)
        return result

    def notes(self) -> str:
        """Короткая сводка: кто что ответил (для статусной строки)."""
        parts = []
        for source in self.sources:
            note = str(getattr(source, "last_note", "") or "")
            if note:
                parts.append(f"{source.label}: {note}")
        return " · ".join(parts)
