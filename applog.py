# -*- coding: utf-8 -*-
"""
Логирование приложения в файл — модуль рядом с main.py.

Что делает setup_logging():

  * заводит logs/app.log с ротацией (5 файлов по 5 МБ) и форматом
    «время · уровень · модуль · сообщение»;
  * перехватывает уже привычные print()-сообщения приложения (ошибки плагинов,
    сетевые сбои, сообщения mpv, замечания источников) — они попадают в лог,
    потому что вывод консоли подменяется «тройником» (Tee): текст уходит и в
    консоль, и в файл;
  * пишет необработанные исключения (в том числе из фоновых потоков), которые
    иначе терялись бы в консоли.

Зачем так: в приложении много мест, где сообщение печатается в консоль (в том
числе из плагинов и источников). Переписывать их все на logging — большой риск,
а перехват вывода даёт тот же результат: полная история в logs/app.log, а в
статусной строке остаётся короткая подсказка.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
import threading
from pathlib import Path
from typing import Optional

LOG_FORMAT = "%(asctime)s · %(levelname)-7s · %(name)s · %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
MAX_BYTES = 5 * 1024 * 1024     # 5 МБ на файл
BACKUP_COUNT = 5                # всего хранится 5 файлов: app.log + 4 архива
LOGGER_NAME = "animeviewer"

_configured = False


class _StreamTee:
    """
    Обёртка над потоком вывода: пишет и в исходный поток, и в лог.

    Нужна, чтобы print() из любого места (включая плагины) попадал в файл.
    Ошибки вывода глушим: логирование не должно ломать приложение.
    """

    def __init__(self, stream, logger: logging.Logger, level: int) -> None:
        self._stream = stream
        self._logger = logger
        self._level = level
        self._lock = threading.Lock()
        self._buffer = ""

    def write(self, text: str) -> int:
        if not text:
            return 0
        try:
            self._stream.write(text)
        except Exception:  # noqa: BLE001
            pass
        with self._lock:
            self._buffer += text
            while "\n" in self._buffer:
                line, self._buffer = self._buffer.split("\n", 1)
                line = line.rstrip()
                if line:
                    try:
                        self._logger.log(self._level, line)
                    except Exception:  # noqa: BLE001
                        pass
        return len(text)

    def flush(self) -> None:
        try:
            self._stream.flush()
        except Exception:  # noqa: BLE001
            pass

    def isatty(self) -> bool:
        try:
            return bool(self._stream.isatty())
        except Exception:  # noqa: BLE001
            return False

    def fileno(self) -> int:
        return self._stream.fileno()

    def __getattr__(self, name: str):     # encoding, reconfigure и прочее
        return getattr(self._stream, name)


def setup_logging(base_dir: Path, level: int = logging.INFO,
                  to_console: bool = True) -> Optional[Path]:
    """
    Включает логирование в logs/app.log. Возвращает путь к файлу лога.

    Повторный вызов безопасен: настройка выполняется один раз.
    """
    global _configured
    log_dir = Path(base_dir) / "logs"
    log_path = log_dir / "app.log"
    if _configured:
        return log_path

    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
        )
    except OSError as exc:
        print(f"[лог] не удалось открыть {log_path}: {exc}")
        return None

    handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
    handler.setLevel(level)

    root = logging.getLogger(LOGGER_NAME)
    root.setLevel(level)
    root.propagate = False
    if not any(isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers):
        root.addHandler(handler)
    # сторонние библиотеки (requests/urllib3) в файл не пишем — иначе лог
    # заполняется техническими строками о каждом HTTP-запросе
    for noisy in ("urllib3", "requests", "charset_normalizer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if to_console:
        try:
            if not isinstance(sys.stdout, _StreamTee):
                sys.stdout = _StreamTee(sys.stdout, root, logging.INFO)
            if not isinstance(sys.stderr, _StreamTee):
                sys.stderr = _StreamTee(sys.stderr, root, logging.ERROR)
        except Exception as exc:  # noqa: BLE001
            root.warning("не удалось перехватить вывод консоли: %s", exc)

    _install_excepthooks(root)
    _configured = True
    root.info("=== Anime Viewer запущен, лог: %s ===", log_path)
    return log_path


def _install_excepthooks(logger: logging.Logger) -> None:
    """Необработанные исключения (включая фоновые потоки) — в лог."""
    previous = sys.excepthook

    def hook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            previous(exc_type, exc_value, exc_tb)
            return
        logger.error("необработанная ошибка", exc_info=(exc_type, exc_value, exc_tb))
        try:
            previous(exc_type, exc_value, exc_tb)
        except Exception:  # noqa: BLE001
            pass

    sys.excepthook = hook

    def thread_hook(args) -> None:
        if issubclass(args.exc_type, SystemExit):
            return
        logger.error(
            "ошибка в потоке %s",
            getattr(args.thread, "name", "?"),
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    try:
        threading.excepthook = thread_hook
    except Exception:  # noqa: BLE001
        pass


def get_logger(name: str = "") -> logging.Logger:
    """Логгер приложения: get_logger("источники.kodik")."""
    return logging.getLogger(f"{LOGGER_NAME}.{name}" if name else LOGGER_NAME)


def log_path(base_dir: Path) -> Path:
    """Путь к файлу лога (для кнопки «Открыть лог»)."""
    return Path(base_dir) / "logs" / "app.log"


def log_report(base_dir: Path) -> str:
    """Короткая справка о состоянии логов (показывается в настройках)."""
    folder = Path(base_dir) / "logs"
    try:
        files = sorted(folder.glob("app.log*"))
    except OSError:
        files = []
    if not files:
        return "лог ещё не создан"
    total = sum(path.stat().st_size for path in files)
    return f"логов: {len(files)}, всего {total / 1024 / 1024:.1f} МБ"
