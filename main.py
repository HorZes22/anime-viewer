# -*- coding: utf-8 -*-
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

# Портативный Python этой сборки берёт пути только из runtime\*. _pth, поэтому
# корень приложения (где лежит пакет и модули вроде kodik.py) добавляем сами —
# иначе `import anime_viewer` упадёт, а источники молча не подключатся.
_APP_DIR = Path(__file__).resolve().parent
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

# отдаём плагинам и внешним источникам ровно этот модуль
sys.modules.setdefault("main", sys.modules[__name__])

# Заставка (splash.py) запускается ЗДЕСЬ, до импорта PySide6: пока грузятся Qt и
# код приложения, пользователь видит окно «Загружаю…», а не пустой экран.
# Если tkinter в сборке нет (в портативном Python его обычно нет) — ничего не
# происходит, и заставку покажет уже PySide6 (см. anime_viewer/_core.py → main()).
_splash_tk = False
try:
    import splash as _splash

    _splash_tk = _splash.show_tkinter()
except Exception as _splash_error:  # noqa: BLE001 — заставка не важнее запуска
    print(f"[заставка] не подключена: {_splash_error}")

from anime_viewer._core import *          # noqa: E402,F401,F403


def _close_splash() -> None:
    """Убирает tkinter-заставку, когда окно PySide6 уже показано."""
    if not _splash_tk:
        return
    try:
        _splash.close_tkinter()
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    try:
        sys.exit(main())                  # noqa: F821 — main() приходит из _core
    finally:
        _close_splash()
