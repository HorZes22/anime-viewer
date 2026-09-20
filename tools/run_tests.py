# -*- coding: utf-8 -*-
"""
Запуск тестов одной командой: .\\runtime\\python.exe tools\\run_tests.py

Зачем обёртка, если есть pytest: в папке проекта (OneDrive) pytest пытается
удалить свою временную папку basetemp, и OneDrive может держать её занятой — тогда
вместо результата тестов появляется PermissionError. Обёртка:

  * даёт каждой сессии СВОЮ временную папку (cache/pytest_tmp_<pid>), поэтому
    pytest не удаляет чужую/старую (и не спотыкается о занятую);
  * передаёт всё остальное в pytest как есть: любые аргументы командной строки
    (например, «-k kodik» или «-x») пройдут дальше;
  * печатает итог и возвращает правильный код возврата.
"""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# библиотеки портативной сборки: pytest лежит в .libs, как и остальные пакеты
sys.path.insert(0, str(ROOT / ".libs"))

try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass


def main() -> int:
    try:
        import pytest
    except ImportError:
        print(
            "pytest не установлен. Для портативной сборки он лежит в .libs;\n"
            "если его там нет — поставьте в свою среду: pip install pytest"
        )
        return 2

    basetemp = ROOT / "cache" / f"pytest_tmp_{os.getpid()}"
    # -p no:cacheprovider — pytest не ведёт свой .pytest_cache в корне проекта:
    # в OneDrive он периодически оказывается занят, оставляя пустые каталоги
    # pytest-cache-files-* и предупреждения
    args = [
        str(ROOT / "tests"), "-q", "-p", "no:cacheprovider",
        "--basetemp", str(basetemp),
    ]
    args += [a for a in sys.argv[1:] if a not in ("--basetemp",)]
    print(f"pytest: {' '.join(args)}")
    code = pytest.main(args)

    def cleanup(path: Path) -> None:
        """
        Убирает временную папку сессии.

        Удаляем сами и по-тихому: если OneDrive держит файл, это не ошибка тестов.
        """
        try:
            import shutil

            shutil.rmtree(path, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass

    cleanup(basetemp)
    print("тесты завершены, код:", code)
    return int(code)


if __name__ == "__main__":
    sys.exit(main())
