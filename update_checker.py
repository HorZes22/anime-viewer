# -*- coding: utf-8 -*-
"""
Проверка обновлений на GitHub — модуль рядом с main.py.

Что делает:

  * при запуске и далее раз в 6 часов запрашивает
    GET https://api.github.com/repos/HorZes22/anime-viewer/releases/latest;
  * сравнивает tag_name релиза с установленной версией приложения;
  * если версия новее — говорит об этом уведомлением в трее (tray_icon.py) и в
    статусной строке: «Доступна версия X.Y. Скачать»;
  * кнопка «Скачать» открывает страницу релиза в браузере (ссылка берётся из
    ответа GitHub, а не собирается руками).

Про лимит запросов: без авторизации GitHub разрешает 60 запросов в час на IP —
для проверки раз в 6 часов этого более чем достаточно. Если хочется больше (или
проверку гоняют несколько копий приложения), в настройках можно указать
`GITHUB_TOKEN` (settings.json → «updates» → «github_token»): тогда лимит — 5000/час.

Модуль ничего не знает про интерфейс: он работает в фоновом потоке и отдаёт
результат сигналами Qt. Проверка никогда не мешает работе приложения: любая
ошибка (нет сети, лимит, 404) приводит к тихому сообщению в статусной строке.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional

import requests

# Репозиторий проекта: владелец/имя. Владелец и имя вместе образуют API-путь.
DEFAULT_REPO = "HorZes22/anime-viewer"
GITHUB_API = "https://api.github.com"
RELEASES_URL = "https://github.com/{repo}/releases/latest"
# Раз в шесть часов — как просили. Проверку можно вызвать и вручную (кнопка).
CHECK_INTERVAL_SEC = 6 * 60 * 60
REQUEST_TIMEOUT = 15
USER_AGENT = "AnimeViewer/1.6 (local desktop app; update check)"
# Минимальная пауза между ручными проверками: чтобы кнопка не выбивала лимит.
MANUAL_COOLDOWN_SEC = 60


def parse_version(text: str) -> tuple[int, ...]:
    """
    «v1.6», «1.6.1», «1.6-beta» -> (1, 6) / (1, 6, 1) / (1, 6).

    Сравнивать версии строками нельзя: «1.10» < «1.9» строкой, но не по-настоящему.
    Всё, что не цифры (префикс «v», суффиксы вроде «-beta»), отбрасывается.
    """
    numbers = re.findall(r"\d+", str(text or ""))
    return tuple(int(item) for item in numbers[:4]) or (0,)


def is_newer(candidate: str, current: str) -> bool:
    """Новее ли версия candidate, чем установленная current."""
    left, right = parse_version(candidate), parse_version(current)
    # выравниваем длину: (1, 5) и (1, 5, 0) — одна и та же версия
    length = max(len(left), len(right))
    left += (0,) * (length - len(left))
    right += (0,) * (length - len(right))
    return left > right


class UpdateInfo:
    """Сведения о найденном релизе (то, что нужно интерфейсу)."""

    def __init__(
        self,
        version: str,
        url: str,
        name: str = "",
        notes: str = "",
        published_at: str = "",
        size_mb: float = 0.0,
        asset_url: str = "",
        asset_name: str = "",
    ) -> None:
        self.version = version          # тег без «v»: «1.6»
        self.url = url                  # страница релиза (кнопка «Скачать»)
        self.name = name                # заголовок релиза
        self.notes = notes              # описание (обрезано)
        self.published_at = published_at
        self.size_mb = size_mb          # размер архива сборки, если приложен
        self.asset_url = asset_url      # прямая ссылка на архив (если есть)
        self.asset_name = asset_name

    def label(self) -> str:
        """Строка для статусной строки и уведомления."""
        return f"Доступна версия {self.version}"

    def describe(self) -> str:
        """Текст уведомления в трее: версия, размер и подсказка про кнопку."""
        parts = [f"Anime Viewer {self.version}"]
        if self.size_mb:
            parts.append(f"архив {self.size_mb:.0f} МБ")
        if self.published_at:
            parts.append(self.published_at[:10])
        tail = "Нажмите «Скачать обновление», чтобы открыть страницу релиза."
        return " · ".join(parts) + "\n" + tail

    def __repr__(self) -> str:  # для логов
        return f"<UpdateInfo {self.version} {self.url}>"


class UpdateChecker:
    """
    Проверка релизов GitHub.

    Использование в приложении (см. _core.py):
        checker = UpdateChecker(current_version=APP_VERSION, base_dir=BASE_DIR)
        checker.update_available.connect(self.on_update_available)
        checker.check(manual=False)          # при запуске — в фоне
        checker.start_timer()                # далее раз в 6 часов

    Сигналов нет в этом модуле намеренно? Они есть, но Qt подключается лениво: если
    PySide6 не установлен, модуль всё равно можно применять из консоли
    (`python update_checker.py --check`).
    """

    def __init__(
        self,
        current_version: str = "1.6",
        base_dir: Optional[Path] = None,
        repo: str = DEFAULT_REPO,
        token: str = "",
        interval: int = CHECK_INTERVAL_SEC,
    ) -> None:
        self.current_version = str(current_version)
        self.base_dir = Path(base_dir) if base_dir else Path(__file__).resolve().parent
        self.repo = str(repo or DEFAULT_REPO)
        # Токен: из переменной окружения, потом из настроек (передаётся сюда)
        self.token = (token or os.environ.get("GITHUB_TOKEN") or "").strip()
        self.interval = max(300, int(interval))
        self.state_file = self.base_dir / "cache" / "update_state.json"
        self.last_error = ""
        self.last_checked = 0.0
        self.latest: Optional[UpdateInfo] = None
        self.skipped_version = ""       # версию, которую пользователь «пропустил»
        self._busy = False
        self._last_manual = 0.0
        self._lock = threading.Lock()
        self._load_state()
        self._install_signals()

    # ------------------------------------------------------------------ состояние
    def _load_state(self) -> None:
        """Читает кэш последней проверки: при запуске видно результат без сети."""
        try:
            if self.state_file.exists():
                data = json.loads(self.state_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self.last_checked = float(data.get("checked_at") or 0.0)
                    self.skipped_version = str(data.get("skipped_version") or "")
                    seen = str(data.get("seen_version") or "")
                    if seen:
                        self.latest = UpdateInfo(
                            seen,
                            str(data.get("url") or RELEASES_URL.format(repo=self.repo)),
                            str(data.get("name") or ""),
                            str(data.get("notes") or ""),
                            str(data.get("published_at") or ""),
                            float(data.get("size_mb") or 0.0),
                            str(data.get("asset_url") or ""),
                            str(data.get("asset_name") or ""),
                        )
        except (OSError, ValueError, TypeError):
            pass

    def _save_state(self, info: Optional[UpdateInfo] = None) -> None:
        info = info or self.latest
        payload = {
            "checked_at": self.last_checked,
            "skipped_version": self.skipped_version,
            "seen_version": info.version if info else "",
            "url": info.url if info else "",
            "name": info.name if info else "",
            "notes": info.notes if info else "",
            "published_at": info.published_at if info else "",
            "size_mb": info.size_mb if info else 0.0,
            "asset_url": info.asset_url if info else "",
            "asset_name": info.asset_name if info else "",
        }
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass

    def set_token(self, token: str) -> None:
        """Токен GitHub (нужен только чтобы поднять лимит запросов)."""
        self.token = (token or "").strip()

    def skip_version(self, version: str) -> None:
        """«Больше не напоминать про эту версию» (сохраняется в состоянии)."""
        self.skipped_version = str(version or "")
        self._save_state()

    def due(self) -> bool:
        """Пора ли проверять (прошло больше интервала с прошлой проверки)."""
        return (time.time() - self.last_checked) >= self.interval

    # ------------------------------------------------------------------ сигналы Qt
    def _install_signals(self) -> None:
        """
        Qt-сигналы создаются только если PySide6 доступен.

        Так модуль остаётся обычным Python-модулем: его можно запустить из консоли
        (`python update_checker.py --check`), не поднимая QApplication.
        """
        try:
            from PySide6.QtCore import QObject, Signal  # noqa: F401

            class _Signals(QObject):
                update_available = Signal(object)   # UpdateInfo
                no_update = Signal()
                error = Signal(str)

            self.signals = _Signals()
            self.update_available = self.signals.update_available
            self.no_update = self.signals.no_update
            self.error = self.signals.error
        except Exception:  # noqa: BLE001 — без Qt модуль просто не имеет сигналов
            self.signals = None
            self.update_available = None
            self.no_update = None
            self.error = None

    def _emit(self, name: str, *args: Any) -> None:
        signal = getattr(self, name, None)
        if signal is None:
            return
        try:
            signal.emit(*args)
        except Exception as exc:  # noqa: BLE001 — сигнал не важнее проверки
            print(f"[обновления] сигнал {name} не отправлен: {exc}")

    # ------------------------------------------------------------------ запрос
    def _headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/vnd.github+json",
            # версия API закреплена: иначе GitHub однажды сменит формат ответа
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _fetch_release(self) -> dict:
        """Один запрос к GitHub. Исключения наружу не выходят — только last_error."""
        url = f"{GITHUB_API}/repos/{self.repo}/releases/latest"
        resp = requests.get(url, headers=self._headers(), timeout=REQUEST_TIMEOUT)
        if resp.status_code == 404:
            # У репозитория ещё нет ни одного релиза — это не ошибка приложения
            self.last_error = "на GitHub пока нет релизов"
            return {}
        if resp.status_code == 403:
            remaining = resp.headers.get("X-RateLimit-Remaining", "")
            self.last_error = (
                "GitHub ограничил запросы (лимит 60/час без токена). "
                "Укажите GITHUB_TOKEN в настройках обновлений."
                if remaining in ("", "0") else "GitHub ответил 403"
            )
            return {}
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _as_info(data: dict, repo: str) -> UpdateInfo:
        """Ответ GitHub -> UpdateInfo (с поиском архива сборки среди вложений)."""
        tag = str(data.get("tag_name") or data.get("name") or "")
        version = tag.lstrip("vV").strip() or tag
        url = str(data.get("html_url") or RELEASES_URL.format(repo=repo))
        notes = str(data.get("body") or "").replace("\r", "").strip()
        if len(notes) > 400:
            notes = notes[:400] + "…"

        asset_url, asset_name, size_mb = "", "", 0.0
        for asset in data.get("assets") or []:
            if not isinstance(asset, dict):
                continue
            name = str(asset.get("name") or "")
            # интересует именно портативная сборка (zip), не исходники
            if name.lower().endswith(".zip") and "portable" in name.lower():
                asset_url = str(asset.get("browser_download_url") or "")
                asset_name = name
                try:
                    size_mb = float(asset.get("size") or 0) / 1024 / 1024
                except (TypeError, ValueError):
                    size_mb = 0.0
                break

        return UpdateInfo(
            version=version,
            url=url,
            name=str(data.get("name") or tag),
            notes=notes,
            published_at=str(data.get("published_at") or ""),
            size_mb=size_mb,
            asset_url=asset_url,
            asset_name=asset_name,
        )

    # ------------------------------------------------------------------ проверка
    def check(self, manual: bool = False) -> Optional[UpdateInfo]:
        """
        Проверяет релиз (синхронно — вызывать из фонового потока).

        manual=True — проверка кнопкой: у неё есть своё ограничение по частоте,
        чтобы случайные нажатия не расходовали лимит GitHub.

        Возвращает UpdateInfo, если доступна версия новее, иначе None.
        Результат отдаётся и сигналом (для интерфейса), и возвращаемым значением
        (для консольного запуска).
        """
        with self._lock:
            if self._busy:
                return None
            if manual and (time.time() - self._last_manual) < MANUAL_COOLDOWN_SEC:
                return self.latest if self._is_newer(self.latest) else None
            self._busy = True
            self._last_manual = time.time() if manual else self._last_manual
        try:
            data = self._fetch_release()
            self.last_checked = time.time()
            if not data:
                self._save_state()
                if self.last_error:
                    self._emit("error", self.last_error)
                return None

            info = self._as_info(data, self.repo)
            self.latest = info
            self._save_state(info)

            if not self._is_newer(info):
                self.last_error = ""
                self._emit("no_update")
                print(f"[обновления] версия {info.version} не новее установленной "
                      f"{self.current_version}")
                return None

            if info.version == self.skipped_version:
                print(f"[обновления] версия {info.version} пропущена пользователем")
                self._emit("no_update")
                return None

            print(f"[обновления] доступна версия {info.version} "
                  f"(установлена {self.current_version})")
            self._emit("update_available", info)
            return info
        except Exception as exc:  # noqa: BLE001 — сеть бывает любой
            self.last_error = f"{type(exc).__name__}: {exc}"
            print(f"[обновления] проверка не удалась: {self.last_error}")
            self._emit("error", self.last_error)
            return None
        finally:
            with self._lock:
                self._busy = False

    def check_async(self, manual: bool = False) -> threading.Thread:
        """Проверка в отдельном потоке (так её и вызывает приложение)."""
        thread = threading.Thread(
            target=self.check, args=(manual,), name="update-check", daemon=True
        )
        thread.start()
        return thread

    def _is_newer(self, info: Optional[UpdateInfo]) -> bool:
        return bool(info and is_newer(info.version, self.current_version))

    # ------------------------------------------------------------------ таймер
    def start_timer(self) -> Any:
        """
        Включает периодическую проверку (раз в 6 часов) через QTimer.

        QTimer живёт в GUI-потоке, а сама проверка уходит в фон — интерфейс не
        подвисает. Если Qt нет, метод просто ничего не делает.
        """
        try:
            from PySide6.QtCore import QTimer
        except Exception:  # noqa: BLE001
            return None
        self.timer = QTimer()
        self.timer.setInterval(self.interval * 1000)
        self.timer.timeout.connect(lambda: self.check_async(manual=False))
        self.timer.start()
        return self.timer

    def stop_timer(self) -> None:
        timer = getattr(self, "timer", None)
        if timer is not None:
            try:
                timer.stop()
            except Exception:  # noqa: BLE001
                pass


def open_release_page(info: Optional[UpdateInfo], fallback_repo: str = DEFAULT_REPO) -> bool:
    """
    Открывает страницу релиза в браузере по умолчанию.

    Кнопка «Скачать» из интерфейса приходит сюда: если у релиза есть приложенный
    архив сборки, открывается страница релиза (там видно описание и файлы) —
    прямая ссылка на архив остаётся в info.asset_url для будущих нужд.
    """
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices

    url = (info.url if info and info.url else RELEASES_URL.format(repo=fallback_repo))
    try:
        return bool(QDesktopServices.openUrl(QUrl(url)))
    except Exception as exc:  # noqa: BLE001
        print(f"[обновления] не удалось открыть ссылку: {exc}")
        return False


def _console_check() -> int:
    """Запуск из консоли: python update_checker.py [--check] [версия] [токен]."""
    args = [arg for arg in sys_argv() if not arg.startswith("-")]
    version = args[1] if len(args) > 1 else "1.6"
    token = args[2] if len(args) > 2 else ""
    checker = UpdateChecker(current_version=version, token=token)
    info = checker.check(manual=True)
    if info is None:
        print(checker.last_error or "обновлений нет")
        return 0
    print(f"Доступна версия {info.version}: {info.url}")
    if info.asset_name:
        print(f"архив: {info.asset_name} ({info.size_mb:.0f} МБ) {info.asset_url}")
    return 0


def sys_argv() -> list[str]:
    import sys

    return sys.argv


if __name__ == "__main__":
    raise SystemExit(_console_check())
