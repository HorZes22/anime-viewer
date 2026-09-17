# -*- coding: utf-8 -*-
"""
Шаблон собственного источника видео для Anime Viewer.

Скопируйте этот файл (например, в `my_source.py`), заполните своими запросами
и перезапустите приложение — источник подключится автоматически.

Приложение вызовет get_stream_url(anime_id, episode, dubbing) и ожидает:
  * строку с прямой ссылкой на поток (mp4 / m3u8) — её сыграет встроенный mpv;
  * None — «у меня этого нет», приложение попробует следующий источник.

Класс обязательно должен наследоваться от VideoSource и переопределять
available_dubbings(), если он поддерживает не все озвучки (иначе интерфейс
покажет доступными все студии из settings.json).
"""

from __future__ import annotations

from typing import Optional

import requests

from main import USER_AGENT, VideoSource


class MyVideoSource(VideoSource):
    """Свой источник: замените запросы на нужные вам."""

    name = "my_source"
    label = "Мой источник"          # как называть его в интерфейсе

    #: какие озвучки умеет отдавать именно этот источник
    DUBBINGS_SUPPORTED = ["Dream Cast", "JAM CLUB", "AniDUB", "Studio Band"]

    #: шаблон ссылки на поток. {anime_id}, {episode}, {dubbing} подставляются автоматически.
    #: Пример: "https://example.com/embed/{anime_id}/{episode}/{dubbing}/index.m3u8"
    URL_TEMPLATE = ""

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})

    def available_dubbings(self) -> list[str]:
        return list(self.DUBBINGS_SUPPORTED)

    # ------------------------------------------------------------------ серии (необязательно)
    def get_episodes(self, anime_id: int) -> Optional[list[tuple[int, str]]]:
        """
        Если ваш источник знает список серий — верните [(номер, название), ...].
        None = «не знаю», тогда список серий придёт из данных Shikimori/AniLibria.
        """
        return None

    # ------------------------------------------------------------------ поток (обязательно)
    def get_stream_url(self, anime_id: int, episode: int, dubbing: str) -> Optional[str]:
        if dubbing not in self.DUBBINGS_SUPPORTED:
            return None

        # --- вариант 1: простая подстановка в шаблон ---
        if self.URL_TEMPLATE:
            return self.URL_TEMPLATE.format(
                anime_id=anime_id, episode=episode, dubbing=dubbing
            )

        # --- вариант 2: свой запрос к API источника ---
        #
        # Подсказки:
        #   * название текущего тайтла лежит в self.anime_title / self.anime_title_alt
        #   * self.anime_year — год выпуска
        #   * если для сопоставления нужен поиск, используйте _title_similarity из main
        #
        # try:
        #     resp = self.session.get(
        #         "https://example.com/api/stream",
        #         params={"anime": anime_id, "ep": episode, "dub": dubbing},
        #         timeout=15,
        #     )
        #     resp.raise_for_status()
        #     data = resp.json()
        #     url = data.get("hls") or data.get("url")
        #     if url:
        #         self.last_status = "ссылка из моего источника"
        #         return url
        # except Exception as exc:
        #     print(f"[{self.name}] {type(exc).__name__}: {exc}")
        #
        return None
