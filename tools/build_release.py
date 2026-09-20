# -*- coding: utf-8 -*-
"""
Сборка портативной версии Anime Viewer для передачи друзьям.

Что делает:
  1. создаёт чистую папку dist\\Anime Viewer
  2. копирует программу, портативный Python (.\\runtime), библиотеки (.libs),
     картинки оформления, плагины и короткие инструкции
  3. НЕ копирует личные данные: settings.json, history.json, progress.json,
     manga_progress.json, manual_streams.json, plugin_settings.json, cache\\
  4. выкидывает из .libs лишние части Qt (QML, переводы, дизайнер, отладочные
     утилиты) — приложение использует только QtCore/QtGui/QtWidgets

Запуск:  .\\runtime\\python.exe tools\\build_release.py            (только папка)
         .\\runtime\\python.exe tools\\build_release.py --zip      (папка + архив)
         .\\runtime\\python.exe tools\\build_release.py --zip-only (только архив)
Затем:   папку или архив из dist\\ можно отдавать кому угодно.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist" / "Anime Viewer"

# ---------------------------------------------------------------------------
# что кладём рядом с main.py
# ---------------------------------------------------------------------------
# README.md не копируется как есть: для сборки он переписывается (make_dist_readme),
# иначе в нём остаются разделы для разработчика (сборка, публикация, файлы, которых
# в готовой папке нет).
COPY_FILES = [
    "main.py",
    "requirements.txt",
    "run.bat",
    "run-detached.bat",
    "run.ps1",
    # модули рядом с main.py: источники, оформление, трей, скачивание, Shikimori
    "kodik.py",
    "sibnet.py",
    "themes_ext.py",
    "accent.py",
    "mica.py",
    "fonts_ext.py",
    "tray_icon.py",
    "mini_player.py",
    "hotkeys.py",
    "hls_download.py",
    "ongoings.py",
    "ranobe.py",
    "applog.py",
    "shikimori_oauth.py",
    "shikimori_api.py",
]
COPY_DIRS = [
    "anime_viewer",      # пакет: в него разделён main.py (см. tools/split_main.py)
    "assets",
    "plugins",
    "sources_local",
    "tools",
    "runtime",
]

# ---------------------------------------------------------------------------
# что выкидываем из .libs (пути относительно .libs)
# ---------------------------------------------------------------------------
LIBS_SKIP_DIRS = {
    "PySide6/qml",             # QML-модули: приложение на Qt Widgets
    "PySide6/translations",    # переводы Qt: интерфейс русский свой
    "PySide6/designer",        # Qt Designer
    "PySide6/include",         # C++ заголовки
    "PySide6/doc",             # справка
    "PySide6/glue",            # исходники обвязки
    "PySide6/scripts",         # вспомогательные сценарии сборки
    "PySide6/plugins/designer",
    "PySide6/plugins/qmllint",
    "PySide6/plugins/qmltooling",
    "PySide6/plugins/sqldrivers",
    "PySide6/plugins/networkinformation",
    "PySide6/plugins/tls",
}

# DLL/.pyd Qt, которые реально нужны приложению (остальные — в мусор)
LIBS_KEEP_QT_MODULES = {
    "Qt6Core.dll",
    "Qt6Gui.dll",
    "Qt6Widgets.dll",
    "Qt6Svg.dll",              # иконки/картинки SVG (мелочь, пусть будет)
    "Qt6Xml.dll",              # нужен Qt6Svg
    "QtCore.pyd",
    "QtGui.pyd",
    "QtWidgets.pyd",
    "pyside6.abi3.dll",        # ядро PySide6: без него QtCore.pyd не грузится
    # рантайм MSVC, который Qt-сборка кладёт рядом
    "msvcp140.dll",
    "msvcp140_1.dll",
    "msvcp140_2.dll",
    "msvcp140_codecvt_ids.dll",
    "concrt140.dll",
    "vcruntime140.dll",
    "vcruntime140_1.dll",
    "vcomp140.dll",
    "vccorlib140.dll",
    "vcamp140.dll",
}

# системные библиотеки Windows, которые Qt-сборка иногда кладёт рядом
LIBS_KEEP_EXTRA_PREFIXES = ("d3dcompiler", "opengl32sw", "libEGL", "libGLESv2")


def skip_libs_file(path: Path) -> bool:
    """Оставляем только нужные модули Qt, остальное — лишнее.

    Внимание: файлы вне корня PySide6 (shiboken6, requests, mpv и пр.) не трогаем
    вообще — иначе легко выкинуть что-нибудь нужное вроде Shiboken.pyd.
    """
    name = path.name

    if path.suffix in (".pyi", ".exe"):            # подсказки для редакторов, утилиты Qt
        return True
    if path.parent.name != "PySide6":              # чужие библиотеки не фильтруем
        return False
    if name.endswith((".dll", ".pyd")):
        if name in LIBS_KEEP_QT_MODULES:
            return False
        return not name.startswith(LIBS_KEEP_EXTRA_PREFIXES)
    return False


def copy_libs() -> None:
    src_root = ROOT / ".libs"
    dst_root = DIST / ".libs"
    skipped: list[str] = []

    for src in src_root.rglob("*"):
        rel = src.relative_to(src_root)
        rel_posix = rel.as_posix()

        if any(rel_posix == d or rel_posix.startswith(d + "/") for d in LIBS_SKIP_DIRS):
            if src.is_dir():
                skipped.append(rel_posix)
            continue
        if "__pycache__" in rel.parts:
            continue

        dst = dst_root / rel
        if src.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
            continue
        if skip_libs_file(src):
            skipped.append(rel_posix)
            continue

        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    print(f"  .libs: пропущено объектов — {len(skipped)}")


def copy_tree(name: str) -> None:
    src = ROOT / name
    if not src.exists():
        print(f"  {name}: нет — пропускаю")
        return
    shutil.copytree(
        src,
        DIST / name,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )


README_FRIEND = """Anime Viewer 1.5 — что это и как запустить
================================================================

Это портативное приложение для просмотра аниме и чтения манги.
Устанавливать ничего не нужно: всё уже внутри папки (свой Python,
библиотеки и плеер). Просто распакуйте папку ЦЕЛИКОМ (не запускайте
из архива) и запустите.

ЗАПУСК
------
1. Двойной клик по  run-detached.bat  — приложение откроется без чёрного
   окна консоли. Это обычный способ запуска.
2. Если что-то не работает, запустите  run.bat  — он открывает то же
   приложение, но с окном консоли, где видно сообщения об ошибках.
3. Ярлык на рабочий стол: запустите  "Создать ярлык на рабочем столе.bat"
   (один раз) — на рабочем столе появится значок с иконкой приложения.

ЧТО НУЖНО
---------
* Windows 10 или 11, 64-бит.
* Интернет: каталоги, поиск, серии и страницы манги берутся из сети.
* Папку можно переносить куда угодно (флешка, другой компьютер) — пути
  внутри приложения относительные, ничего не прописывается в систему.

ЕСЛИ WINDOWS ПРЕДУПРЕЖДАЕТ
--------------------------
"Windows защитила ваш компьютер" появляется у любых файлов, скачанных из
интернета. Нажмите "Подробнее" → "Выполнить в любом случае".
То же самое, если антивирус недоволен — в папке только Python, библиотеки
и плеер mpv, никаких установщиков.

ГДЕ ЧТО ЛЕЖИТ
-------------
main.py            — вся программа
runtime\\           — портативный Python
.libs\\            — PySide6, requests, плеер mpv
assets\\           — картинки оформления и иконка
plugins\\          — плагины и примеры (см. plugins\\README.txt)
README.md          — полное описание всех возможностей
run.bat / run-detached.bat / run.ps1 — способы запуска
tools\\            — служебные скрипты (разведка сайтов, сборка иконки)

Данные, которые создаются при работе, лежат рядом с main.py:
settings.json — настройки, history.json — история просмотров,
progress.json — позиции видео, manga_progress.json — место в манге,
cache\\ — кэш постеров и страниц. Хотите начать «с нуля» — удалите эти
файлы и папку cache.

УДАЛЕНИЕ
--------
Просто удалите папку. В системе ничего не остаётся.

ПОДСКАЗКИ
---------
* Клик по картинке видео — пауза и продолжить (как в YouTube).
* Space — пауза, ← / → — перемотка на 10 секунд, ↑ / ↓ — громкость,
  N — следующая серия, F или F11 — полный экран.
* Вкладки слева: Каталог AniLibria, Поиск, История, Манга
  (Ctrl+1, Ctrl+2, Ctrl+3, Ctrl+4).
"""

LICENSES_TXT = """Лицензии сторонних компонентов
================================================================
В этой папке лежит не только код Anime Viewer, но и готовые сторонние
программы. Их лицензии сохранены рядом с ними:

Python 3.12 (runtime\\python.exe)          — PSF License
                                             runtime\\LICENSE.txt
PySide6 / Qt 6 (.libs\\PySide6)             — LGPL v3 (или коммерческая Qt)
                                             .libs\\pyside6_essentials-6.11.2.dist-info\\licenses\\
shiboken6 (.libs\\shiboken6)                — LGPL v3
                                             .libs\\shiboken6-6.11.2.dist-info\\licenses\\
mpv / libmpv (.libs\\mpv-2.dll)             — LGPL v2.1+ / GPL v2+
                                             исходный код и лицензии: https://mpv.io
python-mpv (.libs\\mpv.py)                  — LGPL v2.1+
                                             .libs\\python_mpv-1.0.8.dist-info\\licenses\\
requests, urllib3, certifi, idna,          — Apache-2.0 / MIT / ISC / BSD
charset-normalizer (.libs)                   .libs\\<пакет>.dist-info\\licenses\\

Код самого приложения (main.py, plugins\\, sources_local\\, tools\\) — лицензия
автора проекта. Приложение только воспроизводит ссылки на видео и страницы
манги, которые отдают открытые API (Shikimori, AniLibria, MangaDex); сами
материалы ему не принадлежат.
"""

SHORTCUT_BAT = """@echo off
rem Creates the "Anime Viewer" shortcut with the application icon.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\\create_shortcut.ps1"
pause
"""

SHORTCUT_PS1 = """# Creates the "Anime Viewer" shortcut with the application icon.
$root = Split-Path -Parent $PSScriptRoot
$launcher = Join-Path $root 'run-detached.bat'
$icon = Join-Path $root 'assets\\app_icon.ico'
$desktop = [Environment]::GetFolderPath('Desktop')
$lnk = Join-Path $desktop 'Anime Viewer.lnk'

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($lnk)
$shortcut.TargetPath = $launcher
$shortcut.WorkingDirectory = $root
$shortcut.IconLocation = "$icon,0"
$shortcut.Description = 'Anime Viewer - anime and manga'
$shortcut.Save()

Write-Host ''
Write-Host "Shortcut created: $lnk"
Write-Host "It starts: $launcher"
"""


def _replace_once(text: str, old: str, new: str, what: str) -> str:
    """Заменяет блок ровно один раз; если блока нет — громко сообщает об этом."""
    if old not in text:
        raise SystemExit(
            f"build_release: в README.md не найден блок «{what}». "
            "Поправьте текст в tools/build_release.py (DIST_*), чтобы сборка "
            "получила правильный README."
        )
    return text.replace(old, new, 1)


def _cut_section(text: str, heading: str) -> str:
    """Убирает раздел «## Заголовок» вместе с телом — до следующего «## »."""
    marker = "\n" + heading + "\n"
    if marker not in text:
        raise SystemExit(f"build_release: в README.md нет раздела «{heading}»")
    start = text.index(marker)
    tail = text[start + 1:]
    nxt = tail.find("\n## ")
    if nxt == -1:
        return text[:start] + "\n"
    return text[:start] + "\n\n" + tail[nxt + 1:]


def make_dist_readme() -> str:
    """README для готовой сборки: без разделов и файлов, которых в ней нет."""
    text = (ROOT / "README.md").read_text(encoding="utf-8")

    # 1) верхний блок: вместо ссылки на релизы — как запускать эту папку
    text = _replace_once(text, README_TOP_SRC, README_TOP_DIST, "верхний блок про сборку")

    # 2) раздел «Запуск»: без ярлыка, которого в сборке нет
    text = _replace_once(text, README_LAUNCH_SRC, README_LAUNCH_DIST, "раздел «Запуск»")

    # 3) разделы для разработчика в сборке не нужны
    text = _cut_section(text, "## Передать друзьям")
    text = _cut_section(text, "## Публикация на GitHub")

    # 4) таблица «Что лежит в папке» — своя, по содержимому сборки
    head = text.index("## Что лежит в папке")
    tail = text[head:]
    nxt = tail.find("\n## ", 1)
    if nxt == -1:
        raise SystemExit("build_release: после «Что лежит в папке» нет следующего раздела")
    text = text[:head] + README_FILES_DIST + "\n" + tail[nxt + 1:]

    return text


README_TOP_SRC = """**Скачать готовую сборку** (Windows 10/11, 64 бит): [раздел Releases](../../releases/latest) —
архив ~90 МБ, распаковать целиком и запустить `run-detached.bat`. Устанавливать ничего не
нужно: Python, PySide6 и плеер mpv уже внутри. Подробнее — раздел «Передать друзьям».

Скриншот-описания в двух словах: слева список аниме с постерами и полосой просмотра,
справа встроенный плеер; четвёртая вкладка — читалка манги.
"""

README_TOP_DIST = """Это **портативная сборка**: устанавливать ничего не нужно — Python, PySide6 и плеер mpv
уже внутри папки. Распакуйте её целиком и запустите `run-detached.bat` (без чёрного окна
консоли); если что-то не работает — `run.bat`, он показывает сообщения об ошибках.
Короткая инструкция лежит в файле `КАК ЗАПУСТИТЬ.txt`.

Слева список аниме с постерами и полосой просмотра, справа встроенный плеер;
четвёртая вкладка — читалка манги.
"""

README_LAUNCH_SRC = """**Проще всего:** двойной клик по ярлыку **`Anime Viewer`** в этой папке — он запускает
приложение без консоли и с иконкой приложения. Ярлык создан при сборке; если он
потеряется, используйте лаунчеры:

```
run-detached.bat     — двойной клик, без консоли (окно не зависит от терминала)
run.bat              — то же, но с консолью (видны сообщения об ошибках)
.\\run.ps1            — из PowerShell
.\\run.ps1 --detach   — из PowerShell, без консоли
```

Устанавливать ничего не нужно: портативный Python и все библиотеки лежат в папке,
отдельный `.venv` не требуется. Папку можно целиком скопировать на другой компьютер
(или на флешку) — приложение запустится оттуда.
"""

README_LAUNCH_DIST = """**Проще всего:** двойной клик по `run-detached.bat` — приложение откроется без чёрного окна
консоли. Остальные способы:

```
run-detached.bat     — без консоли (обычный способ запуска)
run.bat              — то же, но с консолью: видно сообщения об ошибках
.\\run.ps1            — то же из PowerShell
.\\run.ps1 --detach   — из PowerShell, без консоли
Создать ярлык на рабочем столе.bat — один раз создаёт ярлык с иконкой приложения
```

Устанавливать ничего не нужно: портативный Python и все библиотеки лежат в папке,
отдельный `.venv` не требуется. Папку можно целиком скопировать на другой компьютер
(или на флешку) — приложение запустится оттуда.

Нужен Windows 10/11 (64 бит) и интернет: каталоги, поиск, серии и страницы манги
берутся из сети. Если Windows предупреждает «Windows защитила ваш компьютер» — это
обычное предупреждение для скачанных из интернета файлов: «Подробнее» →
«Выполнить в любом случае».
"""

README_FILES_DIST = """## Что лежит в папке

| Файл / папка | Назначение |
|---|---|
| `main.py` | вся программа: каталоги, поиск, источники, плеер, манга, плагины, история, темы |
| `run-detached.bat`, `run.bat`, `run.ps1` | запуск приложения |
| `Создать ярлык на рабочем столе.bat` | создаёт ярлык с иконкой приложения (запускать один раз) |
| `КАК ЗАПУСТИТЬ.txt` | короткая инструкция: запуск, что нужно, где лежат данные |
| `ЛИЦЕНЗИИ.txt` | лицензии вложенных компонентов (Python, Qt/PySide6, mpv, requests) |
| `README.md` | этот файл — полное описание возможностей |
| `runtime\\` | портативный Python 3.12.10 |
| `.libs\\` | PySide6 6.11.2, requests, python-mpv, `mpv-2.dll` (libmpv 0.41) |
| `plugins\\` | плагины: источники, темы, кнопки (см. «Плагины») |
| `assets\\` | картинки оформления и иконка приложения (см. «Картинки оформления») |
| `sources_local\\` | свои источники видео в старом формате |
| `tools\\` | служебные скрипты: разведка сайта-источника, подготовка картинок, иконка |
| `cache\\` | кэш постеров, страниц манги, иконок и запомненное зеркало Shikimori |
| `settings.json` | тема, список названий озвучек, настройки плеера и читалки, размеры окна |
| `plugin_settings.json` | данные, которые плагины хранят сами (появляется при первом использовании) |
| `manual_streams.json` | ваши ссылки (появляется после «Добавить озвучку») |
| `history.json`, `progress.json` | история просмотров и позиции видео |
| `manga_progress.json` | главы и страницы манги, на которых вы остановились |

Файлы `settings.json`, `history.json`, `progress.json`, `manga_progress.json` и папка
`cache\\` в сборке отсутствуют — приложение создаст их при первом запуске. Хотите начать
«с нуля» — удалите их и папку `cache`.
"""


def write_extras() -> None:
    (DIST / "README.md").write_text(make_dist_readme(), encoding="utf-8")
    (DIST / "КАК ЗАПУСТИТЬ.txt").write_text(README_FRIEND, encoding="utf-8")
    (DIST / "Создать ярлык на рабочем столе.bat").write_text(SHORTCUT_BAT, encoding="ascii")
    (DIST / "tools" / "create_shortcut.ps1").write_text(SHORTCUT_PS1, encoding="ascii")
    (DIST / "ЛИЦЕНЗИИ.txt").write_text(LICENSES_TXT, encoding="utf-8")


def human(path: Path) -> str:
    size = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    return f"{size / 1024 / 1024:,.1f} МБ".replace(",", " ")


ZIP_NAME = "Anime Viewer 1.5 portable.zip"


def make_zip() -> Path:
    """Архивирует сборку.

    Именно zipfile из Python, а не Compress-Archive и не tar.exe: только он
    ставит флаг UTF-8 в именах, поэтому русские имена файлов («КАК ЗАПУСТИТЬ.txt»)
    не превращаются в кракозябры при распаковке.
    """
    import zipfile

    zip_path = DIST.parent / ZIP_NAME
    if zip_path.exists():
        zip_path.unlink()

    files = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(DIST.rglob("*")):
            if "__pycache__" in path.parts or path.suffix in (".pyc", ".pyo"):
                continue
            inside = path.relative_to(DIST.parent).as_posix()   # с папкой «Anime Viewer/»
            if path.is_dir():
                archive.writestr(inside + "/", b"")
            else:
                archive.write(path, inside)
                files += 1

    print()
    print(f"Архив: {zip_path}")
    print(f"  файлов: {files}, размер: {zip_path.stat().st_size / 1024 / 1024:,.1f} МБ".replace(",", " "))
    return zip_path


def main() -> int:
    zip_only = "--zip-only" in sys.argv
    want_zip = zip_only or "--zip" in sys.argv

    if zip_only and not DIST.exists():
        print(f"Нет сборки {DIST} — сначала запустите без --zip-only")
        return 1

    if not zip_only:
        if DIST.exists():
            print(f"Удаляю старую сборку: {DIST}")
            shutil.rmtree(DIST)
        DIST.mkdir(parents=True)

        print("Копирую программу:")
        for name in COPY_FILES:
            src = ROOT / name
            if src.exists():
                shutil.copy2(src, DIST / name)
                print(f"  {name}")
            else:
                print(f"  {name}: нет — пропускаю")
        for name in COPY_DIRS:
            copy_tree(name)
            print(f"  {name}\\")

        print("Копирую библиотеки (без лишних модулей Qt):")
        copy_libs()

        write_extras()

        print()
        print(f"Готово: {DIST}")
        print(f"Размер сборки: {human(DIST)}")
        for folder in sorted(DIST.iterdir(), key=lambda p: p.name.lower()):
            if folder.is_dir():
                print(f"  {folder.name:<28} {human(folder)}")

    if want_zip:
        make_zip()
    return 0


if __name__ == "__main__":
    sys.exit(main())
