# -*- coding: utf-8 -*-
"""
Пример плагина Anime Viewer: показывает все точки расширения сразу.

Этот файл НЕ подключается автоматически (имя начинается с подчёркивания).
Чтобы включить пример, скопируйте его под другим именем, например my_plugin.py,
и поправьте атрибуты под себя.

Подробное описание API — в plugins/README.txt.
"""

from main import (
    MangaSource,
    Plugin,
    PluginAction,
    PluginContext,
    VideoSource,
    fmt_time,
)


# --------------------------------------------------------------------------------------
# 1. Источник видео: свой сайт вместо AniLibria
# --------------------------------------------------------------------------------------
class ExampleVideoSource(VideoSource):
    """
    Заготовка источника видео.

    Приложение спрашивает источник в таком порядке:
      1) get_episodes(anime_id)      — список серий (можно вернуть None);
      2) get_stream_url(...)         — ссылку на поток для выбранной серии и озвучки.
    """

    name = "example-site"
    label = "Пример-сайт"

    def available_dubbings(self) -> list[str]:
        """Какие озвучки реально может отдать источник (важно для честного списка)."""
        return ["Моя озвучка"]

    def get_episodes(self, anime_id: int):
        # Верните None, если источник не знает список серий — приложение возьмёт
        # количество серий из Shikimori.
        return None

    def get_stream_url(self, anime_id: int, episode: int, dubbing: str):
        # Здесь был бы реальный поиск ссылки на сайте. Показываем, как это выглядит:
        # self.anime_title / self.anime_title_alt / self.anime_year — данные о тайтле,
        # self.last_status — текст-пояснение для статусной строки.
        self.last_status = "пример источника: ссылки не ищутся"
        return None


# --------------------------------------------------------------------------------------
# 2. Источник манги: свой сайт вместо MangaDex
# --------------------------------------------------------------------------------------
class ExampleMangaSource(MangaSource):
    """
    Заготовка источника манги. Достаточно вернуть словари описанного вида —
    интерфейс и читалка подхватят их сами.
    """

    name = "example-manga"
    label = "Пример-манга"
    multi_language = False

    def search(self, query: str, limit: int = 24, langs=None) -> list[dict]:
        return [
            {
                "id": "demo-1",
                "title": f"Демо-тайтл по запросу «{query}»",
                "title_alt": "Demo title",
                "year": 2024,
                "status": "выходит",
                "last_chapter": "3",
                "poster_url": "",          # ссылка на обложку (необязательно)
                "langs": [],
                "description": "Пример того, что возвращает источник манги.",
            }
        ]

    def chapters(self, manga_id: str, langs=("ru", "en")) -> list[dict]:
        return self.fit_chapters(
            [
                {
                    "id": f"{manga_id}-ch{n}",
                    "chapter": str(n),
                    "volume": "",
                    "title": "Демо-глава",
                    "lang": "ru",
                    "lang_label": "Русский",
                    "group": "Пример",
                    "pages": 2,
                }
                for n in (1, 2, 3)
            ]
        )

    def pages(self, chapter_id: str) -> list[str]:
        # Обычные ссылки на картинки. Загрузку и кэш приложение берёт на себя.
        return [
            "https://upload.wikimedia.org/wikipedia/commons/thumb/4/47/PNG_transparency_demonstration_1.png/280px-PNG_transparency_demonstration_1.png",
            "https://upload.wikimedia.org/wikipedia/commons/thumb/8/89/PNG_transparency_demonstration_2.png/240px-PNG_transparency_demonstration_2.png",
        ]

    def chapter_web_url(self, chapter_id: str) -> str:
        return f"https://example.com/read/{chapter_id}"


# --------------------------------------------------------------------------------------
# 3. Сам плагин: тема, кнопки и обработчики событий
# --------------------------------------------------------------------------------------
class ExamplePlugin(Plugin):
    name = "example-plugin"
    title = "Пример плагина"
    version = "1.0"
    author = "Anime Viewer"
    description = "Показывает, как добавить источник, тему, кнопку и обработчики."

    # ---------- точки расширения ----------
    def video_sources(self) -> list[VideoSource]:
        return [ExampleVideoSource()]

    def manga_sources(self) -> list[MangaSource]:
        return [ExampleMangaSource()]

    def themes(self) -> dict[str, dict]:
        """Новая тема появится в «Оформление → Режим»."""
        return {
            "example-sunset": {
                "title": "Закат (пример)",
                "bg": "#1b1016", "panel": "#261a22", "card": "#2d2029",
                "card_hover": "#35262f", "text": "#ffeee6", "muted": "#b18e9b",
                "border": "#3c2c35", "input": "#261a22", "input_focus": "#2d2029",
                "player": "#130a10",
            }
        }

    def actions(self) -> list[PluginAction]:
        """Кнопки в меню «Плагины» в шапке окна."""
        return [
            PluginAction(
                "Что сейчас выбрано",
                self.show_selection,
                tooltip="Показать текущий тайтл и открытую главу",
            ),
            PluginAction(
                "Открыть поиск на сайте",
                self.open_search,
                tooltip="Открывает поиск примера в браузере",
                needs="anime",
            ),
            PluginAction("Счётчик запусков", self.show_counter,
                         tooltip="Демонстрация своих данных в plugin_settings.json"),
        ]

    # ---------- обработчики кнопок ----------
    def show_selection(self, ctx: PluginContext) -> None:
        anime = ctx.current_anime() or {}
        manga = ctx.current_manga() or {}
        chapter = ctx.current_chapter() or {}
        parts = []
        if anime:
            parts.append(f"аниме: {anime.get('russian') or anime.get('title')}")
        if manga:
            parts.append(f"манга: {manga.get('title')}")
        if chapter:
            parts.append(f"глава: {chapter.get('chapter')}")
        ctx.status("Выбрано — " + ("; ".join(parts) if parts else "пока ничего"))

    def open_search(self, ctx: PluginContext) -> None:
        anime = ctx.current_anime() or {}
        query = str(anime.get("title") or anime.get("russian") or "").replace(" ", "+")
        ctx.open_url(f"https://example.com/search?q={query}")

    def show_counter(self, ctx: PluginContext) -> None:
        count = int(ctx.data.get("starts", 0))
        ctx.status(f"Плагин «{ctx.plugin.title}»: приложение запускалось {count} раз(а).")

    # ---------- события ----------
    def on_started(self, ctx: PluginContext) -> None:
        """Считаем запуски и проверяем версию API — как это делают плагины."""
        if ctx.api != 1:
            ctx.status(f"Плагин собран под API 1, приложение даёт {ctx.api}")
        ctx.data["starts"] = int(ctx.data.get("starts", 0)) + 1
        ctx.save_data()

    def on_anime_selected(self, anime: dict) -> None:
        title = anime.get("russian") or anime.get("title") or "без названия"
        print(f"[пример плагина] выбран аниме-тайтл: {title}")

    def on_playback_started(self, anime: dict, episode: int, dubbing: str, url: str) -> None:
        title = anime.get("russian") or anime.get("title") or "без названия"
        print(f"[пример плагина] играет {title} — серия {episode} [{dubbing}]: {url[:60]}")

    def on_chapter_opened(self, manga: dict, chapter: dict) -> None:
        print(f"[пример плагина] открыта глава {chapter.get('chapter')} — {manga.get('title')}")

    def on_closing(self) -> None:
        print(f"[пример плагина] закрытие; время работы неизвестно, но событие дошло. "
              f"Последняя подпись серии: {fmt_time(0)}")
