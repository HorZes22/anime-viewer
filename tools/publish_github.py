# -*- coding: utf-8 -*-
"""
Публикация Anime Viewer на GitHub — без git и без gh, одним пертативным Python.

Что делает:
  1. проверяет токен и показывает, за кого вход;
  2. создаёт публичный репозиторий (если его ещё нет) и заливает туда исходники
     через GitHub API (обычным git-коммитом, но без установленного git);
  3. создаёт релиз (тег v1.5) и прикладывает к нему готовую портативную сборку
     `dist\\Anime Viewer 1.5 portable.zip` — её и будут скачивать люди.

Почему сборка идёт в релиз, а не в репозиторий: внутри `mpv-2.dll` на 115 МБ,
а GitHub запрещает файлы больше 100 МБ. В релиз можно до 2 ГБ.

Запуск:
    $env:GITHUB_TOKEN = "ghp_..."     # токен, см. ниже
    .\\runtime\\python.exe tools\\publish_github.py --dry-run   # посмотреть, что зальётся
    .\\runtime\\python.exe tools\\publish_github.py             # опубликовать

Токен нужен один раз. Создать: GitHub → Settings → Developer settings →
Personal access tokens → Tokens (classic) → Generate new token: права `repo`
(и `workflow` не нужен). После публикации токен можно сразу удалить — он
больше не требуется.

Дополнительные ключи:
    --repo NAME     имя репозитория (по умолчанию anime-viewer)
    --tag TAG       тег релиза (по умолчанию v1.5)
    --zip PATH      файл сборки (по умолчанию dist\\Anime Viewer 1.5 portable.zip)
    --private       сделать репозиторий приватным
    --skip-source   не заливать исходники, только релиз
    --skip-release  не создавать релиз, только исходники
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / ".libs"))

try:
    import requests
except ImportError:
    print("Нет библиотеки requests — запускайте через runtime\\python.exe")
    raise SystemExit(1)

API = "https://api.github.com"
UPLOAD_API = "https://uploads.github.com"

# ---------------------------------------------------------------------------
# что попадает в репозиторий (только исходники, без личных данных и сборки)
# ---------------------------------------------------------------------------
SOURCE_FILES = [
    "main.py",
    "README.md",
    "requirements.txt",
    "run.bat",
    "run-detached.bat",
    "run.ps1",
    ".gitignore",
    "pytest.ini",
    "LICENSE",
    # модули рядом с main.py (источники, оформление, трей, Shikimori, ранобэ)
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
SOURCE_DIRS = ["anime_viewer", "assets", "plugins", "sources_local", "tools", "tests"]
SKIP_PARTS = {"__pycache__", "dist", "cache", "probe"}
SKIP_SUFFIXES = {".pyc", ".pyo", ".lnk"}

RELEASE_BODY = """**Anime Viewer 1.5 — портативная сборка для Windows 10/11 (64 бит).**

Ничего устанавливать не нужно: свой Python, библиотеки и плеер mpv уже внутри.

1. Скачайте архив ниже и распакуйте папку **целиком** (не запускайте из архива).
2. Запустите `run-detached.bat` — приложение откроется без чёрного окна консоли.
   Если что-то не так, запустите `run.bat`: он покажет сообщения об ошибках.
3. Ярлык на рабочий стол: `Создать ярлык на рабочем столе.bat` (один раз).

Нужен интернет: каталоги, поиск, серии и страницы манги берутся из сети.
Подробности — в файле `КАК ЗАПУСТИТЬ.txt` внутри архива.
"""


def collect_source() -> list[tuple[str, Path]]:
    """Список (путь в репозитории, файл на диске)."""
    items: list[tuple[str, Path]] = []

    for name in SOURCE_FILES:
        path = ROOT / name
        if path.exists():
            items.append((name, path))

    for folder in SOURCE_DIRS:
        base = ROOT / folder
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            rel_parts = path.relative_to(ROOT).parts
            if any(part in SKIP_PARTS for part in rel_parts):
                continue
            if path.suffix.lower() in SKIP_SUFFIXES:
                continue
            items.append((path.relative_to(ROOT).as_posix(), path))

    return items


class GitHub:
    def __init__(self, token: str) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "anime-viewer-publisher",
            }
        )

    def request(self, method: str, url: str, **kwargs):
        response = self.session.request(method, url, timeout=60, **kwargs)
        if response.status_code >= 400:
            detail = response.text[:400]
            raise RuntimeError(f"{method} {url} -> {response.status_code}: {detail}")
        if response.content:
            try:
                return response.json()
            except ValueError:
                return response.content
        return None

    def user(self) -> dict:
        return self.request("GET", f"{API}/user")

    def repo(self, owner: str, name: str):
        response = self.session.get(f"{API}/repos/{owner}/{name}", timeout=30)
        if response.status_code == 200:
            return response.json()
        if response.status_code == 404:
            return None
        raise RuntimeError(f"GET repo -> {response.status_code}: {response.text[:300]}")

    def create_repo(self, name: str, description: str, private: bool) -> dict:
        return self.request(
            "POST",
            f"{API}/user/repos",
            json={
                "name": name,
                "description": description,
                "private": private,
                "has_issues": True,
                "has_wiki": False,
                "has_projects": False,
                "auto_init": False,
            },
        )

    def put_files(self, owner: str, name: str, files: list[tuple[str, Path]], message: str) -> str:
        """Создаёт коммит из файлов через Git Data API (git на машине не нужен)."""
        tree = []
        for index, (repo_path, disk_path) in enumerate(files, start=1):
            content = base64.b64encode(disk_path.read_bytes()).decode("ascii")
            blob = self.request(
                "POST",
                f"{API}/repos/{owner}/{name}/git/blobs",
                json={"content": content, "encoding": "base64"},
            )
            tree.append(
                {"path": repo_path, "mode": "100644", "type": "blob", "sha": blob["sha"]}
            )
            print(f"    [{index}/{len(files)}] {repo_path}")

        new_tree = self.request(
            "POST", f"{API}/repos/{owner}/{name}/git/trees", json={"tree": tree}
        )
        commit = self.request(
            "POST",
            f"{API}/repos/{owner}/{name}/git/commits",
            json={"message": message, "tree": new_tree["sha"], "parents": []},
        )

        refs = self.session.get(f"{API}/repos/{owner}/{name}/git/refs", timeout=30)
        existing = refs.json() if refs.status_code == 200 else []
        has_main = any(item.get("ref") == "refs/heads/main" for item in existing)

        if has_main:
            self.request(
                "PATCH",
                f"{API}/repos/{owner}/{name}/git/refs/heads/main",
                json={"sha": commit["sha"], "force": True},
            )
        else:
            self.request(
                "POST",
                f"{API}/repos/{owner}/{name}/git/refs",
                json={"ref": "refs/heads/main", "sha": commit["sha"]},
            )
        return commit["sha"]

    def get_release(self, owner: str, name: str, tag: str):
        response = self.session.get(
            f"{API}/repos/{owner}/{name}/releases/tags/{tag}", timeout=30
        )
        if response.status_code == 200:
            return response.json()
        return None

    def create_release(self, owner: str, name: str, tag: str) -> dict:
        return self.request(
            "POST",
            f"{API}/repos/{owner}/{name}/releases",
            json={
                "tag_name": tag,
                "name": f"Anime Viewer {tag.lstrip('v')} — портативная сборка",
                "body": RELEASE_BODY,
                "draft": False,
                "prerelease": False,
            },
        )

    def upload_asset(self, owner: str, name: str, release_id: int, path: Path) -> dict:
        size = path.stat().st_size
        url = f"{UPLOAD_API}/repos/{owner}/{name}/releases/{release_id}/assets"
        headers = {
            "Content-Type": "application/zip",
            "Content-Length": str(size),
        }
        print(f"    загружаю {path.name} ({size / 1024 / 1024:.1f} МБ)…")

        with path.open("rb") as handle:
            response = self.session.post(
                url,
                params={"name": path.name},
                data=handle,
                headers=headers,
                timeout=(60, 7200),
            )
        if response.status_code >= 400:
            raise RuntimeError(
                f"загрузка не удалась: {response.status_code} {response.text[:300]}"
            )
        return response.json()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="Публикация Anime Viewer на GitHub")
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN", ""))
    parser.add_argument("--repo", default="anime-viewer")
    parser.add_argument("--tag", default="v1.5")
    parser.add_argument("--zip", default=str(ROOT / "dist" / "Anime Viewer 1.5 portable.zip"))
    parser.add_argument("--private", action="store_true")
    parser.add_argument("--skip-source", action="store_true")
    parser.add_argument("--skip-release", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    zip_path = Path(args.zip)
    files = collect_source()
    total = sum(path.stat().st_size for _, path in files)

    print("Исходники для репозитория:")
    for repo_path, path in files:
        print(f"  {path.stat().st_size / 1024:9.1f} КБ  {repo_path}")
    print(f"Всего: {len(files)} файлов, {total / 1024 / 1024:.1f} МБ")
    print()
    print(f"Сборка для релиза: {zip_path}")
    if zip_path.exists():
        print(f"  {zip_path.stat().st_size / 1024 / 1024:.1f} МБ, sha256 {sha256(zip_path)[:16]}…")
    else:
        print("  файла нет — релиз без вложения (соберите tools\\build_release.py --zip)")

    if args.dry_run:
        print()
        print("Это примерка (--dry-run): ничего не отправлено.")
        return 0

    if not args.token:
        print()
        print("Нужен токен GitHub. Создайте его:")
        print("  github.com → Settings → Developer settings → Personal access tokens")
        print("  → Tokens (classic) → Generate new token, права: repo")
        print("Затем:")
        print('  $env:GITHUB_TOKEN = "ваш_токен"')
        print("  .\\runtime\\python.exe tools\\publish_github.py")
        return 1

    github = GitHub(args.token)
    user = github.user()
    owner = user["login"]
    print()
    print(f"Вход выполнен: {owner}")

    repo = github.repo(owner, args.repo)
    if repo is None:
        print(f"Создаю публичный репозиторий {owner}/{args.repo}…")
        repo = github.create_repo(
            args.repo,
            "Anime Viewer — десктопное приложение для просмотра аниме и чтения манги "
            "(Windows, портативная сборка, PySide6 + mpv + Shikimori/AniLibria/MangaDex)",
            args.private,
        )
    else:
        print(f"Репозиторий уже есть: {repo['html_url']}")

    if not args.skip_source:
        print("Заливаю исходники:")
        sha = github.put_files(
            owner,
            args.repo,
            files,
            "Anime Viewer 1.5: исходники приложения",
        )
        print(f"  коммит: {sha[:10]}")

    if not args.skip_release and zip_path.exists():
        release = github.get_release(owner, args.repo, args.tag)
        if release is None:
            print(f"Создаю релиз {args.tag}…")
            release = github.create_release(owner, args.repo, args.tag)
        else:
            print(f"Релиз {args.tag} уже есть — прикладываю файл к нему")

        already = [asset["name"] for asset in release.get("assets", [])]
        if zip_path.name in already:
            print(f"  файл {zip_path.name} в релизе уже есть — пропускаю")
        else:
            asset = github.upload_asset(owner, args.repo, release["id"], zip_path)
            print(f"  загружено: {asset['browser_download_url']}")

    print()
    print("Готово:")
    print(f"  репозиторий: https://github.com/{owner}/{args.repo}")
    print(f"  релизы:      https://github.com/{owner}/{args.repo}/releases")
    return 0


if __name__ == "__main__":
    sys.exit(main())
