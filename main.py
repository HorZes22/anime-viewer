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

from anime_viewer._core import *          # noqa: E402,F401,F403


if __name__ == "__main__":
    sys.exit(main())                      # noqa: F821 — main() приходит из _core
