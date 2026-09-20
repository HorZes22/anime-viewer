# -*- coding: utf-8 -*-
"""
Разделение main.py на пакет anime_viewer (шаг 1 миграции) — запускается один раз.

Что делает инструмент:

  1. сохраняет исходник в .split_backup\\main.py (чтобы прогон можно было повторить);
  2. переносит код приложения в anime_viewer\\_core.py, поправив вычисление BASE_DIR
     (раньше это была папка main.py, теперь папка на уровень выше пакета — иначе
     settings.json и cache уехали бы внутрь пакета);
  3. создаёт файлы структуры, которые просили в плане: app.py, sources\\*, core\\*,
     manga\\*, ui\\*, widgets\\*, utils\\* — каждый модуль объявляет свои имена и
     реэкспортирует их из _core.py (перенос кода блоками — шаг 2, список имен в
     каждом модуле уже готов);
  4. переписывает main.py в shim: `from main import Plugin` в плагинах продолжает
     работать, потому что shim реэкспортирует всё и заранее прописывает себя в
     sys.modules["main"].

Запуск: .\\runtime\\python.exe tools\\split_main.py
"""

from __future__ import annotations

import io
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKUP_DIR = ROOT / ".split_backup"
PACKAGE = ROOT / "anime_viewer"

try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

# --------------------------------------------------------------------------------------
# Структура пакета: какой модуль какие имена объявляет (и что из _core.py забирает).
# Это же и план второго шага: код переносится в модуль ровно по этому списку.
# --------------------------------------------------------------------------------------
STRUCTURE: dict[str, tuple[str, list[str]]] = {
    "app.py": ("Точка входа: QApplication, шрифты из assets, запуск главного окна.",
               ["main"]),
    "ui/main_window.py": ("Главное окно приложения: MainWindow.",
                          ["MainWindow"]),
    "ui/cards.py": ("Карточки списков: AnimeCardDelegate.",
                    ["AnimeCardDelegate"]),
    "ui/themes.py": ("Темы, палитры, картинки оформления, иконки и таблица стилей.",
                     ["THEME_MODES", "DEFAULT_THEME", "ASSET_FILES", "asset_path",
                      "load_asset_image", "load_app_icon", "wallpaper_pixmap",
                      "tinted_asset_icon", "WallpaperWidget", "theme_colors",
                      "ensure_icons", "build_qss"]),
    "ui/dialogs.py": ("Диалоги: настройки, плагины, Shikimori, импорт ссылок, «Читаю».",
                      ["SettingsDialog", "LinkImportDialog", "MangaReadingDialog",
                       "PluginsDialog", "ShikimoriDialog"]),
    "widgets/player.py": ("Виджет плеера mpv (встраивание через winId).",
                          ["MpvWidget"]),
    "manga/reader.py": ("Читалка манги: загрузчик страниц, полотно, виджет чтения.",
                        ["MangaPageLoader", "MangaStrip", "MangaReader"]),
    "manga/cache.py": ("Кэш страниц манги: очистка по лимиту.",
                       ["prune_manga_cache"]),
    "core/history.py": ("История и прогресс: ProgressStore, HistoryStore, MangaProgressStore.",
                        ["ProgressStore", "HistoryStore", "MangaProgressStore", "time_ago"]),
    "core/settings.py": ("Настройки приложения: чтение и запись settings.json.",
                         ["load_settings"]),
    "core/plugins.py": ("Плагины: Plugin, PluginAction, PluginContext, PluginManager.",
                        ["Plugin", "PluginAction", "PluginStore", "PluginContext",
                         "LoadedPlugin", "PluginManager", "_SourcesOnlyPlugin",
                         "load_plugin_sources", "_expose_main_module"]),
    "sources/base.py": ("Абстрактные источники видео и манги.",
                        ["VideoSource", "MangaSource", "MultiVideoSource"]),
    "sources/anilibria.py": ("Источник AniLibria: клиент официального API и источник.",
                             ["AniLibriaClient", "AniLibriaVideoSource",
                              "PlaceholderVideoSource"]),
    "sources/mangadex.py": ("Источник манги MangaDex: клиент API и источник.",
                            ["MangaDexClient", "MangaDexSource"]),
    "sources/manual.py": ("Ручные ссылки и локальная библиотека файлов.",
                          ["ManualLinkSource", "LocalLibrarySource", "find_subtitles",
                           "parse_playlist"]),
    "utils/network.py": ("Сетевые помощники: сессии на поток, картинки, фоновые задачи.",
                         ["_thread_session", "fetch_image", "Worker", "PosterLoader"]),
    "utils/certs.py": ("Сертификаты для mpv (CA-бандл certifi).",
                       ["find_ca_bundle"]),
    "utils/logging.py": ("Логирование в logs/app.log (обёртка вокруг applog.py).",
                         []),
}

PACKAGE_INIT = '''# -*- coding: utf-8 -*-
"""
Anime Viewer — пакет приложения.

Здесь лежит разделённый main.py: рабочая точка входа — корневой main.py (он
реэкспортирует всё отсюда, поэтому `from main import Plugin` в плагинах работает
как раньше), а весь код приложения — в модуле _core.py этого пакета.

Структура (модули объявляют свои имена и реэкспортируют их из _core.py):

    anime_viewer/
        app.py            — точка входа (QApplication, шрифты, MainWindow)
        ui/               — main_window, cards, themes, dialogs
        widgets/          — player (MpvWidget)
        manga/            — reader, cache
        core/             — history, settings, plugins, config
        sources/          — base, anilibria, mangadex, manual
        utils/            — network, certs, logging

Код переносится блоками: сначала модуль появляется как точка импорта, затем в него
физически переезжает код (список имён — в каждом модуле, порядок переноса — в
tools/split_main.py). Так перенос не ломает ни приложение, ни плагины.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Портативный Python из этой сборки настраивает sys.path через файл runtime\\*. _pth
# и каталог приложения в него НЕ попадает. Поэтому корень приложения и .libs
# добавляем сами: иначе модули рядом с main.py (kodik.py, accent.py, themes_ext.py…)
# молча не подключатся, а пакет просто не найдётся.
_APP_DIR = Path(__file__).resolve().parent.parent
for _path in (str(_APP_DIR), str(_APP_DIR / ".libs")):
    if _path not in sys.path:
        sys.path.append(_path)

__version__ = "1.5"
'''

SHIM_TEMPLATE = '''# -*- coding: utf-8 -*-
"""
Anime Viewer — точка входа (shim над пакетом anime_viewer).

Зачем этот файл остался: приложение запускается как `python main.py`, а плагины и
свои источники пишут `from main import Plugin, PluginAction, VideoSource`. Чтобы всё
это продолжало работать после разделения кода на пакет, main.py реэкспортирует
содержимое anime_viewer._core (и, значит, всего пакета).

Важно: строка sys.modules.setdefault("main", ...) ниже — не украшение. Без неё
`from main import VideoSource` внутри kodik.py/sibnet.py загрузило бы этот файл
ВТОРОЙ раз под именем «main», и источник получил бы чужой (второй) класс-родитель,
после чего молча не подключился бы.
"""

import sys
from pathlib import Path

# Портативный Python этой сборки берёт пути только из runtime\\*. _pth, поэтому
# корень приложения (где лежит пакет и модули вроде kodik.py) добавляем сами —
# иначе `import anime_viewer` упадёт, а источники молча не подключатся.
_APP_DIR = Path(__file__).resolve().parent
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

# отдаём плагинам и внешним источникам ровно этот модуль
sys.modules.setdefault("main", sys.modules[__name__])

from anime_viewer._core import *          # noqa: E402,F401,F403


if __name__ == "__main__":
    sys.exit(main())                      # noqa: F821 — main() приходит из _core
'''


def read_original() -> str:
    """Исходник main.py (при повторном запуске — из резервной копии)."""
    backup = BACKUP_DIR / "main.py"
    if backup.exists():
        return backup.read_text(encoding="utf-8")
    BACKUP_DIR.mkdir(exist_ok=True)
    original = (ROOT / "main.py").read_text(encoding="utf-8")
    backup.write_text(original, encoding="utf-8")
    return original


def collect_names(source: str) -> list[str]:
    """
    Все верхнеуровневые имена main.py: определения, присваивания и импорты.

    Импорты тоже нужны в __all__: код приложения не раз обращается к Qt и requests
    через имена модуля (main.QMessageBox), и плагины могут делать так же — после
    разделения эти имена должны остаться доступными через shim.
    """
    import ast as _ast

    names: set[str] = set()
    try:
        tree = _ast.parse(source)
    except SyntaxError as exc:
        print(f"не удалось разобрать исходник: {exc}")
        return []
    for node in tree.body:
        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, _ast.Assign):
            for target in node.targets:
                if isinstance(target, _ast.Name):
                    names.add(target.id)
        elif isinstance(node, _ast.AnnAssign) and isinstance(node.target, _ast.Name):
            names.add(node.target.id)
        elif isinstance(node, _ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, _ast.ImportFrom):
            for alias in node.names:
                if alias.name != "*":
                    names.add(alias.asname or alias.name)
    # имена, которые появляются внутри try/except НА УРОВНЕ МОДУЛЯ: подключаемые
    # модули (accent, mica, shikimori_api…), их функции и флаги вида X_AVAILABLE.
    # Внутрь функций не заглядываем: там свои локальные имена.
    for node in tree.body:
        if not isinstance(node, _ast.Try):
            continue
        for sub in node.body:
            if isinstance(sub, _ast.Assign):
                for target in sub.targets:
                    if isinstance(target, _ast.Name):
                        names.add(target.id)
            elif isinstance(sub, _ast.Import):
                for alias in sub.names:
                    names.add(alias.asname or alias.name.split(".")[0])
            elif isinstance(sub, _ast.ImportFrom):
                for alias in sub.names:
                    if alias.name != "*":
                        names.add(alias.asname or alias.name)
            elif isinstance(sub, (_ast.FunctionDef, _ast.ClassDef)):
                names.add(sub.name)
    return sorted(n for n in names if not n.startswith("__"))


def build_core(source: str) -> str:
    """
    Содержимое _core.py: исходник main.py с поправленным BASE_DIR.

    BASE_DIR раньше указывал на папку main.py. Теперь файл лежит в подпапке пакета,
    поэтому берём родительскую папку — иначе настройки, кэш и прогресс уехали бы
    внутрь anime_viewer.
    """
    fixed = source.replace(
        "BASE_DIR = Path(__file__).resolve().parent          # папка, рядом с которой лежит main.py",
        "# ВАЖНО: файл лежит в подпапке пакета, поэтому папка приложения — на уровень\n"
        "# выше. Иначе settings.json, cache и progress.json оказались бы внутри\n"
        "# anime_viewer вместо папки с ярлыком запуска.\n"
        "BASE_DIR = Path(__file__).resolve().parent.parent   # папка приложения (рядом с main.py)",
    )
    if fixed == source:
        # строка могла отличаться — правим по общему правилу
        fixed = source.replace(
            "BASE_DIR = Path(__file__).resolve().parent",
            "BASE_DIR = Path(__file__).resolve().parent.parent",
        )
    header = (
        "# -*- coding: utf-8 -*-\n"
        '"""\n'
        "Anime Viewer: весь код приложения (перенесён из main.py).\n\n"
        "Разделение на модули идёт по шагам: сначала код собран здесь целиком (так\n"
        "поведение приложения не меняется), затем блоки переезжают в модули пакета —\n"
        "список имён для каждого модуля уже объявлен в его файле (см. tools/split_main.py).\n"
        '"""\n\n'
    )
    # убираем прежнюю шапку модуля (она переехала в этот docstring)
    body = fixed
    if body.lstrip().startswith("# -*- coding: utf-8 -*-"):
        body = "\n".join(body.splitlines()[1:])
    marker = body.find('"""')
    if marker != -1:
        end = body.index('"""', marker + 3) + 3
        body = body[end:]
    export_names = collect_names(source)
    export_block = (
        "\n\n# Полный список имён модуля (включая приватные и импортированные):\n"
        "# нужен, чтобы `from main import *` и `from anime_viewer._core import *`\n"
        "# отдавали ровно то же, что раньше отдавал main.py (плагины пишут\n"
        "# `from main import Plugin, VideoSource` и иногда берут имена модуля напрямую).\n"
        "__all__ = [\n" + "".join(f"    {name!r},\n" for name in export_names) + "]\n"
    )
    return header + body.lstrip("\n").rstrip("\n") + "\n" + export_block


def build_shell(module: str, description: str, names: list[str]) -> str:
    """Модуль-точка входа: объявляет имена и реэкспортирует их из _core.py."""
    depth = module.count("/")
    dots = "." * (depth + 1)
    lines = [
        "# -*- coding: utf-8 -*-",
        '"""',
        f"{description}",
        "",
        "Шаг 1 миграции: код пока лежит в anime_viewer/_core.py, а этот модуль объявляет",
        "свои имена — чтобы импорты (`from anime_viewer.sources.base import VideoSource`)",
        "уже работали и не менялись, когда код физически переедет сюда.",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        f"from {dots}_core import *          # noqa: F401,F403",
    ]
    if names:
        public = sorted(n for n in names if not n.startswith("_"))
        lines.append("")
        lines.append(f"__all__ = {public!r}")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    # Защита от повторного прогона: разделение уже выполнено, и код теперь живёт в
    # anime_viewer/_core.py. Повторный запуск перезаписал бы _core.py исходником из
    # .split_backup — то есть стёр бы все правки, сделанные после разделения
    # (интерфейс, боковая навигация, полный экран). Поэтому только с --force.
    if "--force" not in sys.argv and (PACKAGE / "_core.py").exists() and (BACKUP_DIR / "main.py").exists():
        print("Разделение уже выполнено: код живёт в anime_viewer/_core.py.")
        print("Повторный прогон перезапишет его исходником из .split_backup\\main.py.")
        print("Если это действительно нужно — запустите с ключом --force.")
        return 0

    source = read_original()
    PACKAGE.mkdir(exist_ok=True)
    (PACKAGE / "__init__.py").write_text(PACKAGE_INIT, encoding="utf-8")

    (PACKAGE / "_core.py").write_text(build_core(source), encoding="utf-8")
    print("anime_viewer/_core.py: код приложения перенесён (BASE_DIR поправлен)")

    for module, (description, names) in STRUCTURE.items():
        path = PACKAGE / module
        path.parent.mkdir(parents=True, exist_ok=True)
        if module == "app.py":
            text = (
                "# -*- coding: utf-8 -*-\n"
                '"""\n'
                "Точка входа приложения: QApplication, шрифты из assets, главное окно.\n\n"
                "Запуск приложения — через корневой main.py (он вызывает app.main()).\n"
                '"""\n\n'
                "from __future__ import annotations\n\n"
                "import sys\n\n"
                "from ._core import main as _run\n\n\n"
                "def main() -> int:\n"
                '    """Запускает приложение и возвращает код выхода Qt."""\n'
                "    return _run()\n\n\n"
                '__all__ = ["main"]\n'
            )
        elif module == "utils/logging.py":
            text = (
                "# -*- coding: utf-8 -*-\n"
                '"""\n'
                "Логирование приложения: logs/app.log с ротацией 5×5 МБ.\n\n"
                "Готовый модуль — applog.py рядом с main.py; здесь он реэкспортируется,\n"
                "чтобы импорт из пакета тоже работал: `from anime_viewer.utils.logging import setup_logging`.\n"
                '"""\n\n'
                "from __future__ import annotations\n\n"
                "import sys\n"
                "from pathlib import Path\n\n"
                "# applog.py лежит в папке приложения (рядом с main.py), а не в пакете\n"
                "_APP_DIR = Path(__file__).resolve().parent.parent.parent\n"
                "if str(_APP_DIR) not in sys.path:\n"
                "    sys.path.insert(0, str(_APP_DIR))\n\n"
                "from applog import log_path, log_report, get_logger, setup_logging  # noqa: E402\n\n"
                '__all__ = ["setup_logging", "get_logger", "log_path", "log_report"]\n'
            )
        else:
            text = build_shell(module, description, names)
        path.write_text(text, encoding="utf-8")
        print(f"  {module}: объявлено имён — {len(names)}")

    (ROOT / "main.py").write_text(SHIM_TEMPLATE, encoding="utf-8")
    print("main.py: стал shim'ом (плагины продолжают работать)")
    print("\nГотово. Проверка:")
    print("  .\\runtime\\python.exe -m py_compile main.py anime_viewer\\_core.py")
    print("  .\\runtime\\python.exe tools\\run_tests.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
