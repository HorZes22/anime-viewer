# -*- coding: utf-8 -*-
"""
Дополнительные темы оформления и утилиты для них (отдельный модуль рядом с main.py).

Что делает модуль:

  * THEMES_EXT — 8 цветовых схем (Dracula, Nord, Solarized Dark, Catppuccin Mocha,
    Gruvbox Dark, Tokyo Night, Kanagawa, Ayu Dark) в формате приложения;
  * register_themes() — добавляет их в THEME_MODES, не затрагивая встроенные темы
    (Тёмная, Полночь, Светлая, Контрастная) и темы плагинов;
  * theme_alias_keys() / normalize_theme() — принимают «человеческие» имена цветов
    (background, surface, primary, text_muted, hover, selected…) и раскладывают их
    по ключам, которые понимает приложение. Благодаря этому тема плагина может
    задавать цвета как ей удобно;
  * validate_theme() — проверяет палитру и возвращает список замечаний: тема с
    опечаткой не должна тихо ломать оформление;
  * gradient_css() — значение для QSS, если в теме указан градиент
    («gradient»: [цвет1, цвет2]).

В теме можно задавать:
    "title"        — название в списке тем;
    "gradient"     — [цвет сверху, цвет снизу] (вертикальный градиент окна);
    "font_family"  — имя шрифта (см. fonts_ext.py — шрифты из assets/fonts).
"""

from __future__ import annotations

from typing import Any, Optional

# Цвета: сначала ключи приложения, затем «человеческие» синонимы.
# Порядок важен: theme_alias_keys() разрешает конфликт в пользу ключа приложения.
APP_COLOR_KEYS = (
    "bg", "panel", "card", "card_hover", "player",
    "text", "muted", "border", "input", "input_focus",
)
ALIAS_KEYS = {
    "background": "bg",
    "surface": "panel",
    "surface_alt": "card",
    "hover": "card_hover",
    "selected": "card_hover",
    "primary": "muted",
    "accent_text": "muted",
    "text_muted": "muted",
    "foreground": "text",
    "outline": "border",
    "divider": "border",
    "field": "input",
    "field_focus": "input_focus",
    "video": "player",
}

# --------------------------------------------------------------------------------------
# 8 дополнительных схем. Для каждой — полный набор ключей приложения, поэтому тема
# выглядит цельно, а не «покрашенной местами».
# --------------------------------------------------------------------------------------
THEMES_EXT: dict[str, dict] = {
    "dracula": {
        "title": "Dracula",
        "bg": "#282a36", "panel": "#21222c", "card": "#343746", "card_hover": "#3f4254",
        "player": "#191a21", "text": "#f8f8f2", "muted": "#9aa0b5", "border": "#44475a",
        "input": "#21222c", "input_focus": "#2b2d3a",
        "gradient": ["#2d2f3d", "#21222c"],
        "accent_default": "#bd93f9",
    },
    "nord": {
        "title": "Nord",
        "bg": "#2e3440", "panel": "#2b303b", "card": "#3b4252", "card_hover": "#434c5e",
        "player": "#242933", "text": "#eceff4", "muted": "#a3adc2", "border": "#4c566a",
        "input": "#3b4252", "input_focus": "#434c5e",
        "gradient": ["#343b48", "#272c36"],
        "accent_default": "#88c0d0",
    },
    "solarized": {
        "title": "Solarized Dark",
        "bg": "#002b36", "panel": "#073642", "card": "#0b3c47", "card_hover": "#12505c",
        "player": "#001f27", "text": "#eee8d5", "muted": "#93a1a1", "border": "#14454f",
        "input": "#073642", "input_focus": "#0b3c47",
        "gradient": ["#06333d", "#00232c"],
        "accent_default": "#268bd2",
    },
    "catppuccin": {
        "title": "Catppuccin Mocha",
        "bg": "#1e1e2e", "panel": "#181825", "card": "#313244", "card_hover": "#45475a",
        "player": "#11111b", "text": "#cdd6f4", "muted": "#a6adc8", "border": "#45475a",
        "input": "#313244", "input_focus": "#45475a",
        "gradient": ["#232336", "#181825"],
        "accent_default": "#cba6f7",
    },
    "gruvbox": {
        "title": "Gruvbox Dark",
        "bg": "#282828", "panel": "#1d2021", "card": "#3c3836", "card_hover": "#504945",
        "player": "#1d2021", "text": "#ebdbb2", "muted": "#a89984", "border": "#504945",
        "input": "#3c3836", "input_focus": "#504945",
        "gradient": ["#32302f", "#1d2021"],
        "accent_default": "#d79921",
    },
    "tokyonight": {
        "title": "Tokyo Night",
        "bg": "#1a1b26", "panel": "#16161e", "card": "#24283b", "card_hover": "#2f3549",
        "player": "#13131a", "text": "#c0caf5", "muted": "#7f8cb8", "border": "#414868",
        "input": "#24283b", "input_focus": "#2f3549",
        "gradient": ["#1f2333", "#16161e"],
        "accent_default": "#7aa2f7",
    },
    "kanagawa": {
        "title": "Kanagawa",
        "bg": "#1f1f28", "panel": "#16161d", "card": "#2a2a37", "card_hover": "#363646",
        "player": "#16161d", "text": "#dcd7ba", "muted": "#9e9b93", "border": "#54546d",
        "input": "#2a2a37", "input_focus": "#363646",
        "gradient": ["#25252f", "#16161d"],
        "accent_default": "#7e9cd8",
    },
    "ayu": {
        "title": "Ayu Dark",
        "bg": "#0b0e14", "panel": "#0f131a", "card": "#1c212b", "card_hover": "#242936",
        "player": "#06080d", "text": "#bfbdb6", "muted": "#8a9199", "border": "#2d3640",
        "input": "#1c212b", "input_focus": "#242936",
        "gradient": ["#101521", "#080b11"],
        "accent_default": "#e6b450",
    },
}


def theme_alias_keys(palette: dict) -> dict:
    """
    Переводит «человеческие» имена цветов в ключи приложения.

    Ключи приложения имеют приоритет: если заданы и «background», и «bg», побеждает «bg»
    (иначе тема плагина могла бы неожиданно переопределить цвет).
    """
    result: dict[str, Any] = {}
    for alias, target in ALIAS_KEYS.items():
        if alias in palette and target not in palette:
            result[target] = palette[alias]
    result.update(palette)
    for alias in ALIAS_KEYS:
        result.pop(alias, None)
    return result


def normalize_theme(palette: dict, name: str = "") -> dict:
    """
    Приводит палитру к виду приложения: синонимы цветов + разумные значения по умолчанию.

    Недостающие цвета берутся из «Тёмной премиум» (их всё равно досчитает
    theme_colors), но лучше отдать полный набор — тогда тема предсказуема.
    """
    palette = dict(palette or {})
    if name and not palette.get("title"):
        palette["title"] = name
    palette = theme_alias_keys(palette)
    # «accent_default» — подсказка для акцента: приложение читает ключ "accent"
    if palette.get("accent_default") and not palette.get("accent"):
        palette["accent"] = palette["accent_default"]
    palette.pop("accent_default", None)
    return palette


def validate_theme(palette: dict) -> list[str]:
    """
    Проверяет палитру: список замечаний (пустой — всё хорошо).

    Проверяем ровно то, что ломает оформление: цвета заданы строками и являются
    корректными значениями, градиент — пара цветов, шрифт — строка.
    """
    from PySide6.QtGui import QColor

    problems: list[str] = []
    if not isinstance(palette, dict):
        return ["палитра не словарь"]
    for key in APP_COLOR_KEYS:
        value = palette.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or not QColor(value).isValid():
            problems.append(f"цвет «{key}» = {value!r} не распознан")
    gradient = palette.get("gradient")
    if gradient is not None:
        ok = (
            isinstance(gradient, (list, tuple)) and len(gradient) == 2
            and all(isinstance(c, str) and QColor(c).isValid() for c in gradient)
        )
        if not ok:
            problems.append("градиент должен быть парой цветов: [\"#111111\", \"#222222\"]")
    font_family = palette.get("font_family")
    if font_family is not None and not isinstance(font_family, str):
        problems.append("font_family должен быть строкой (название шрифта)")
    return problems


def register_themes(modes: dict, themes: Optional[dict] = None,
                    overwrite: bool = False) -> list[str]:
    """
    Добавляет темы в THEME_MODES приложения.

    Возвращает список добавленных ключей. Существующие темы не перезаписываются
    (кроме overwrite=True) — встроенные схемы и темы плагинов остаются на месте.
    """
    sources = THEMES_EXT if themes is None else themes
    added: list[str] = []
    for key, palette in (sources or {}).items():
        if not key or not isinstance(palette, dict):
            continue
        if key in modes and not overwrite:
            continue
        modes[str(key)] = normalize_theme(palette, str(key))
        added.append(str(key))
    return added


def gradient_css(theme: dict, fallback: str = "") -> str:
    """
    Значение фона для QSS: линейный градиент, если в теме он задан, иначе цвет.

    Нужно для «Фона окна»: QSS понимает qlineargradient, поэтому вертикальный
    градиент можно отдать прямо в таблицу стилей (см. build_qss).
    """
    theme = theme or {}
    gradient = theme.get("gradient")
    if isinstance(gradient, (list, tuple)) and len(gradient) == 2:
        first, second = str(gradient[0]), str(gradient[1])
        return (
            "qlineargradient(x1:0, y1:0, x2:0, y2:1, "
            f"stop:0 {first}, stop:1 {second})"
        )
    return fallback


def theme_font_family(theme: dict, fallback: str) -> str:
    """Шрифт темы (если задан) или стандартный набор шрифтов приложения."""
    family = str((theme or {}).get("font_family") or "").strip()
    if not family:
        return fallback
    return f'"{family}", {fallback}'
