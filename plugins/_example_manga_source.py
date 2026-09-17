# -*- coding: utf-8 -*-
"""Тестовый плагин: источник манги с локальными страницами + источник видео,
тема, кнопка и все обработчики. Используется для проверки системы плагинов."""

import io
import time

from main import MangaSource, Plugin, PluginAction, VideoSource

CALLS = []
START_TIME = time.time()


class LocalFolderManga(MangaSource):
    """Источник манги, который отдаёт синтетические страницы (для проверки)."""

    name = "test-manga"
    label = "Тест-манга"
    multi_language = False

    def search(self, query, limit=24, langs=None):
        CALLS.append(("search", query))
        return [
            {
                "id": "test-title-1",
                "title": f"Тестовый тайтл «{query}»",
                "title_alt": "Test title",
                "year": 2025,
                "status": "выходит",
                "last_chapter": "3",
                "poster_url": "",
                "langs": [],
                "description": "Проверка источника манги из плагина",
            }
        ]

    def chapters(self, manga_id, langs=("ru", "en")):
        CALLS.append(("chapters", manga_id))
        return self.fit_chapters([
            {"id": f"{manga_id}-ch1", "chapter": "1", "volume": "", "title": "Первая",
             "lang": "ru", "lang_label": "Русский", "group": "Тест", "pages": 3},
            {"id": f"{manga_id}-ch2", "chapter": "2", "volume": "", "title": "Вторая",
             "lang": "ru", "lang_label": "Русский", "group": "Тест", "pages": 3},
        ])

    def pages(self, chapter_id):
        CALLS.append(("pages", chapter_id))
        return [f"local://page-{n}" for n in range(1, 4)]

    def download_page(self, url):
        """Страницы рисуем сами: проверяем, что читалка работает с любым источником."""
        from PySide6.QtCore import QBuffer, QByteArray
        from PySide6.QtGui import QColor, QImage, QPainter

        number = int(str(url).rsplit("-", 1)[-1])
        image = QImage(600, 900, QImage.Format.Format_RGB32)   # QImage безопасен в потоке
        image.fill(QColor("#e8e0d0"))
        painter = QPainter(image)
        painter.setPen(QColor("#332211"))
        painter.drawText(image.rect(), 0x84, f"страница {number}\nиз плагина")
        painter.end()
        buffer = QByteArray()
        device = QBuffer(buffer)
        device.open(QBuffer.OpenModeFlag.WriteOnly)
        image.save(device, "PNG")
        return bytes(buffer)

    def chapter_web_url(self, chapter_id):
        return ""


class TestVideoSource(VideoSource):
    name = "test-video"
    label = "Тест-видео"

    def available_dubbings(self):
        return ["Тест-озвучка"]

    def get_stream_url(self, anime_id, episode, dubbing):
        CALLS.append(("stream", anime_id, episode, dubbing))
        return "https://example.com/test.m3u8"


class TestPlugin(Plugin):
    name = "test-plugin"
    title = "Тестовый плагин"
    version = "9.9"
    author = "Проверка"
    description = "Плагин для автотестов"

    def video_sources(self):
        return [TestVideoSource()]

    def manga_sources(self):
        return [LocalFolderManga()]

    def themes(self):
        return {
            "test-theme": {
                "title": "Тестовая тема",
                "bg": "#101010", "panel": "#181818", "card": "#202020",
                "card_hover": "#282828", "text": "#f0f0f0", "muted": "#909090",
                "border": "#303030", "input": "#181818", "input_focus": "#202020",
                "player": "#000000",
            }
        }

    def actions(self):
        return [
            PluginAction("Действие плагина", self.do_it, tooltip="тест", needs=""),
            PluginAction("Только для аниме", self.only_anime, needs="anime"),
        ]

    def do_it(self, ctx):
        CALLS.append(("action", ctx.plugin.name))
        ctx.status(f"Плагин работает: {ctx.version}/{ctx.api}")

    def only_anime(self, ctx):
        CALLS.append(("action-anime", (ctx.current_anime() or {}).get("title")))

    def on_started(self, ctx):
        CALLS.append(("started", ctx.api))
        ctx.data["starts"] = int(ctx.data.get("starts", 0)) + 1
        ctx.save_data()

    def on_anime_selected(self, anime):
        CALLS.append(("anime", anime.get("id")))

    def on_playback_started(self, anime, episode, dubbing, url):
        CALLS.append(("play", episode, dubbing, url))

    def on_chapter_opened(self, manga, chapter):
        CALLS.append(("chapter", chapter.get("chapter")))

    def on_closing(self):
        CALLS.append(("closing", round(time.time() - START_TIME, 1)))
