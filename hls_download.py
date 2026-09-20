# -*- coding: utf-8 -*-
"""
Скачивание серии для просмотра без интернета — модуль рядом с main.py.

Что делает: берёт ссылку на поток (как её отдаёт источник) и сохраняет серию на
диск в cache/video/<тайтл>/<серия>.mp4.

Как:
  * HLS (m3u8) — playlist разбирается вручную, сегменты качаются в несколько
    потоков, склеиваются в .ts; если рядом есть ffmpeg (или .libs/ffmpeg.exe),
    файл перепаковывается в .mp4 (без перекодирования, «-c copy») и .ts удаляется;
  * обычный файл (mp4/webm/…) — просто копируется потоком с прогрессом;
  * шифрованные HLS (EXT-X-KEY) без ffmpeg не поддерживаются: скачивание честно
    сообщает об этом, а не сохраняет мусор.

Прогресс: callback(готово, всего) вызывается из рабочего потока — интерфейс сам
превращает это в обновление полоски (см. MainWindow.on_download_progress).
"""

from __future__ import annotations

import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urljoin, urlparse

import requests

REQUEST_TIMEOUT = 30
CHUNK = 1024 * 256
DEFAULT_WORKERS = 4
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class DownloadCancelled(RuntimeError):
    """Скачивание отменено пользователем."""


def find_ffmpeg(base_dir: Optional[Path] = None) -> str:
    """Ищет ffmpeg: сначала в PATH, затем рядом с программой (.libs/ffmpeg.exe)."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    if base_dir:
        for candidate in (
            Path(base_dir) / ".libs" / "ffmpeg.exe",
            Path(base_dir) / "ffmpeg.exe",
            Path(base_dir) / "tools" / "ffmpeg.exe",
        ):
            if candidate.is_file():
                return str(candidate)
    return ""


def is_hls(url: str) -> bool:
    path = urlparse(str(url or "")).path.lower()
    return path.endswith(".m3u8") or "m3u8" in path


class HlsDownloader:
    """Скачивание HLS-потока или обычного файла с прогрессом и отменой."""

    def __init__(self, base_dir: Optional[Path] = None, workers: int = DEFAULT_WORKERS,
                 referer: str = "") -> None:
        self.base_dir = Path(base_dir) if base_dir else None
        self.workers = max(1, int(workers))
        self.ffmpeg = find_ffmpeg(self.base_dir)
        self.session = requests.Session()
        headers = {"User-Agent": UA}
        if referer:
            headers["Referer"] = referer
        self.session.headers.update(headers)
        self._cancel = threading.Event()
        self.last_error = ""

    # ------------------------------------------------------------------ отмена
    def cancel(self) -> None:
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def _check(self) -> None:
        if self._cancel.is_set():
            raise DownloadCancelled("скачивание отменено")

    # ------------------------------------------------------------------ playlist
    def _get_text(self, url: str) -> str:
        resp = self.session.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.text

    def _playlist_urls(self, url: str) -> tuple[list[str], bool]:
        """
        Разбирает плейлист. Возвращает (ссылки, это_сегменты).

        Мастер-плейлист (со качествами) разрешается в лучший вариант; если в
        плейлисте есть ключи шифрования и нет ffmpeg — скачивание отменится с
        понятным текстом.
        """
        text = self._get_text(url)
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if any(line.startswith("#EXT-X-KEY") for line in lines):
            if not self.ffmpeg:
                raise RuntimeError(
                    "поток зашифрован (EXT-X-KEY), для скачивания нужен ffmpeg "
                    "(положите ffmpeg.exe в .libs или добавьте в PATH)"
                )

        variants: list[tuple[int, str]] = []
        segments: list[str] = []
        pending_bandwidth = 0
        for line in lines:
            if line.startswith("#EXT-X-STREAM-INF"):
                bandwidth = 0
                for part in line.split(","):
                    if "BANDWIDTH" in part.upper():
                        digits = "".join(ch for ch in part if ch.isdigit())
                        bandwidth = int(digits or 0)
                pending_bandwidth = bandwidth
                continue
            if line.startswith("#"):
                continue
            absolute = urljoin(url, line)
            if pending_bandwidth:
                variants.append((pending_bandwidth, absolute))
                pending_bandwidth = 0
            else:
                segments.append(absolute)

        if variants:
            variants.sort(reverse=True)
            return self._playlist_urls(variants[0][1])
        return segments, True

    # ------------------------------------------------------------------ скачивание
    def download(self, url: str, target: Path,
                 progress: Optional[Callable[[int, int], None]] = None,
                 referer: str = "") -> Optional[Path]:
        """
        Сохраняет поток в target (для HLS — .ts, затем .mp4 при наличии ffmpeg).

        Возвращает путь к готовому файлу или None (причина — в last_error).
        """
        self.last_error = ""
        if referer:
            self.session.headers["Referer"] = referer
        target = Path(target)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.last_error = f"не удалось создать папку: {exc}"
            return None

        try:
            if is_hls(url):
                return self._download_hls(url, target, progress)
            return self._download_file(url, target, progress)
        except DownloadCancelled:
            self.last_error = "скачивание отменено"
            return None
        except Exception as exc:  # noqa: BLE001 — сеть/диск бывают любыми
            self.last_error = f"{type(exc).__name__}: {exc}"
            print(f"[скачивание] {self.last_error}")
            return None

    def _download_file(self, url: str, target: Path,
                       progress: Optional[Callable[[int, int], None]]) -> Optional[Path]:
        with self.session.get(url, timeout=REQUEST_TIMEOUT, stream=True) as resp:
            resp.raise_for_status()
            try:
                total = int(resp.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                total = 0
            done = 0
            with open(target, "wb") as handle:
                for chunk in resp.iter_content(CHUNK):
                    self._check()
                    if not chunk:
                        continue
                    handle.write(chunk)
                    done += len(chunk)
                    if progress is not None:
                        progress(done, total)
        return target

    def _download_hls(self, url: str, target: Path,
                      progress: Optional[Callable[[int, int], None]]) -> Optional[Path]:
        segments, _is_segments = self._playlist_urls(url)
        if not segments:
            self.last_error = "в плейлисте нет сегментов"
            return None
        total = len(segments)
        parts_dir = target.parent / f"{target.stem}_parts"
        try:
            parts_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.last_error = f"не удалось создать папку сегментов: {exc}"
            return None

        done = 0
        errors: list[str] = []
        lock = threading.Lock()
        from concurrent.futures import ThreadPoolExecutor

        def fetch(task: tuple[int, str]) -> Optional[Path]:
            index, segment_url = task
            self._check()
            path = parts_dir / f"{index:05d}.ts"
            if path.exists() and path.stat().st_size > 0:
                return path
            try:
                with self.session.get(segment_url, timeout=REQUEST_TIMEOUT, stream=True) as resp:
                    resp.raise_for_status()
                    with open(path, "wb") as handle:
                        for chunk in resp.iter_content(CHUNK):
                            self._check()
                            if chunk:
                                handle.write(chunk)
                return path
            except DownloadCancelled:
                raise
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(f"{index + 1}: {type(exc).__name__}")
                return None

        ts_path = target.with_suffix(".ts")
        with open(ts_path, "wb") as out:
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                for index, part in enumerate(
                    pool.map(fetch, [(i, s) for i, s in enumerate(segments)])
                ):
                    self._check()
                    if part is None:
                        continue
                    with open(part, "rb") as handle:
                        shutil.copyfileobj(handle, out)
                    done += 1
                    if progress is not None:
                        progress(done, total)

        shutil.rmtree(parts_dir, ignore_errors=True)
        if errors and done < total:
            self.last_error = (
                f"скачано серий-сегментов {done} из {total}; ошибки: {', '.join(errors[:3])}"
            )

        final = self._remux(ts_path, target)
        if progress is not None:
            progress(total, total)
        return final

    def _remux(self, ts_path: Path, target: Path) -> Path:
        """Перепаковка .ts -> .mp4 без перекодирования (если есть ffmpeg)."""
        if self.ffmpeg and ts_path.exists():
            try:
                result = subprocess.run(
                    [self.ffmpeg, "-y", "-loglevel", "error", "-i", str(ts_path),
                     "-c", "copy", "-bsf:a", "aac_adtstoasc", str(target)],
                    capture_output=True, timeout=60 * 30,
                )
                if result.returncode == 0 and target.exists() and target.stat().st_size > 0:
                    try:
                        ts_path.unlink()
                    except OSError:
                        pass
                    return target
                print(f"[скачивание] ffmpeg вернул код {result.returncode}: "
                      f"{result.stderr.decode('utf-8', 'replace')[:200]}")
            except Exception as exc:  # noqa: BLE001
                print(f"[скачивание] ffmpeg не сработал: {exc}")
        # без ffmpeg оставляем .ts — mpv играет его как обычно
        return ts_path


def download_target(base_dir: Path, anime_id: Any, episode: Any,
                    title: str = "") -> Path:
    """
    Куда сохранять серию: cache/video/<id>/<серия>.mp4.

    Название тайтла попадает в подпись рядом с файлом (см. save_title_note), а в
    имени папки — только id: так путь не зависит от символов в названии.
    """
    folder = Path(base_dir) / "cache" / "video" / str(anime_id)
    return folder / f"{int(episode):02d}.mp4"


def save_title_note(base_dir: Path, anime_id: Any, title: str) -> None:
    """Кладёт рядом с видео текстовый файл с названием тайтла (удобно искать руками)."""
    if not title:
        return
    try:
        folder = Path(base_dir) / "cache" / "video" / str(anime_id)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "название.txt").write_text(title, encoding="utf-8")
    except OSError:
        pass
