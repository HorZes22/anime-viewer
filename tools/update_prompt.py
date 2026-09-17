# -*- coding: utf-8 -*-
"""
Обновляет в PROMPT_UI.md раздел «Как устроено оформление»: берёт текущие палитры,
функции цветов/иконок/QSS и отрисовку карточки прямо из main.py.

Запуск: .\\runtime\\python.exe tools\\update_prompt.py
"""
import io
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, str(ROOT))

source_text = (ROOT / "main.py").read_text(encoding="utf-8")


def extract(start_marker: str, end_marker: str) -> str:
    start = source_text.index(start_marker)
    end = source_text.index(end_marker, start)
    return source_text[start:end].rstrip() + "\n"


themes_block = extract("THEME_MODES = {", "ICONS_DIR = CACHE_DIR")
colors_block = extract("def _relative_luminance", "def ensure_icons")
icons_block = extract("def ensure_icons", "def build_qss")
qss_block = extract("def build_qss", "class AnimeCardDelegate")
card_block = extract(
    "class AnimeCardDelegate",
    "# ======================================================================================\n# История просмотров",
)

prompt = (ROOT / "PROMPT_UI.md").read_text(encoding="utf-8")
start = prompt.index("## 3. Как устроено оформление")
end = prompt.index("## 4. Что нужно сделать")

new_section = f"""## 3. Как устроено оформление (текущее состояние)

Оформление держится на четырёх местах в `main.py`: палитры тем, вычисление
производных цветов, генерация иконок кодом и таблица стилей QSS. Карточки списков
рисуются вручную (делегат), QSS их не касается.

Палитры и значения по умолчанию (**сейчас в коде именно это**):

```python
{themes_block}```

Производные цвета: оттенки акцента, цвет текста поверх акцента и размеры шрифтов
считаются из базовой палитры и настроек пользователя (масштаб шрифта):

```python
{colors_block}```

Иконки рисуются кодом в `cache/icons` (никаких эмодзи и внешних файлов):

```python
{icons_block}```

Таблица стилей целиком (это база, которую нужно сделать красивее):

```python
{qss_block}```

Отрисовка карточки списка (постер, три строки, полоса прогресса) — вручную,
через `QPainter`:

```python
{card_block}```

"""

prompt = prompt[:start] + new_section + prompt[end:]
(ROOT / "PROMPT_UI.md").write_text(prompt, encoding="utf-8")
print("PROMPT_UI.md обновлён:", len(prompt), "символов")
print("палитры на месте:", "#0d0d0e" in prompt and "Тёмная премиум" in prompt)
print("раздел QSS на месте:", "def build_qss" in prompt)
print("старые палитры убраны:", "#0e1015" not in prompt)
