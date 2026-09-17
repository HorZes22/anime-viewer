# -*- coding: utf-8 -*-
"""
Разведка сайта источника: сохраняет страницу, её скрипты и отчёт по API-эндпоинтам.

Зачем: чтобы подключить сайт (например, студию озвучки) как источник видео, нужно
увидеть его разметку и запросы. Из «песочницы» ассистента сайт может быть недоступен
(блокировка по SNI), поэтому этот скрипт запускается у вас и складывает всё в папку tools/,
откуда данные можно забрать для анализа.

ЗАПУСК (из папки проекта):

    .\\runtime\\python.exe tools\\site_probe.py https://dreamerscast.com

Полезные ключи:

    --search "мастера меча"   попробовать типовые страницы поиска и сохранить ответы
    --deep                    пройти по найденным ссылкам на аниме (до 5 страниц)
    --dir out                 куда складывать (по умолчанию tools/probe)

Результат: tools/probe/report.txt (краткий отчёт) + сохранённые HTML/JS/JSON.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}

# типовые пути API у аниме-сайтов и CMS
API_PATHS = [
    "/api/", "/api/v1/", "/api/v2/", "/api/anime", "/api/search", "/api/v1/anime",
    "/wp-json/", "/wp-json/wp/v2/", "/graphql", "/_next/data/", "/api/player",
    "/api/translations", "/api/serial", "/search", "/anime", "/catalog",
]

# на что смотреть в разметке и скриптах
INTERESTING = [
    r"\.m3u8", r"\.mp4", r"kodik", r"aniboom", r"alloha", r"sibnet", r"vk\.com/video",
    r"youtube", r"rutube", r"player", r"iframe", r"api_key", r"token", r"bearer",
    r"__NEXT_DATA__", r"window\.__NUXT__", r"wp-json", r"graphql", r"/api/",
]


def safe_name(url: str) -> str:
    parsed = urlparse(url)
    name = (parsed.path.strip("/") or "index").replace("/", "_")
    name = re.sub(r"[^0-9A-Za-zА-Яа-я_.-]", "_", name)[:80]
    return f"{parsed.netloc.replace('.', '_')}_{name}"


def fetch(session: requests.Session, url: str, out_dir: Path, log: list[str], save_html=True):
    started = time.time()
    try:
        resp = session.get(url, headers=HEADERS, timeout=30, allow_redirects=True)
    except Exception as exc:
        log.append(f"[FAIL] {url} — {type(exc).__name__}: {exc}")
        return None
    took = time.time() - started
    ctype = resp.headers.get("Content-Type", "")
    log.append(
        f"[{'OK' if resp.status_code < 400 else 'HTTP ' + str(resp.status_code)}] {url} "
        f"({took:.1f}с, {ctype}, {len(resp.content)} байт, final={resp.url[:100]})"
    )
    if resp.status_code >= 400:
        return resp

    text = resp.text
    if save_html:
        if "html" in ctype:
            (out_dir / f"{safe_name(url)}.html").write_text(text, encoding="utf-8")
        elif "json" in ctype:
            (out_dir / f"{safe_name(url)}.json").write_text(text, encoding="utf-8")
    return resp


def scan_html(html: str, base: str, log: list[str]) -> list[str]:
    """Ищет в разметке скрипты, ссылки на аниме и упоминания плееров."""
    found_links: list[str] = []

    scripts = re.findall(r'<script[^>]+src="([^"]+)"', html)
    scripts = [urljoin(base, s) for s in scripts]
    if scripts:
        log.append("  скрипты: " + ", ".join(s[:90] for s in scripts[:10]))

    for pattern in INTERESTING:
        hits = re.findall(pattern, html, re.IGNORECASE)
        if hits:
            log.append(f"  упоминается {pattern!r}: {len(hits)} раз")

    inline_json = re.findall(
        r'(window\.__NUXT__\s*=|window\.__NEXT_DATA__\s*=|<script[^>]*application/json[^>]*>)(.{0,300})',
        html, re.S,
    )
    for marker, chunk in inline_json[:3]:
        log.append(f"  встроенные данные рядом с {marker[:30]!r}: {chunk[:160]!r}")

    hrefs = re.findall(r'href="([^"]+)"', html)
    for href in hrefs:
        full = urljoin(base, href)
        if re.search(r"/(anime|serial|release|title|catalog|watch|video)/", full, re.I):
            if full not in found_links and urlparse(full).netloc == urlparse(base).netloc:
                found_links.append(full)

    direct = re.findall(r'https?://[^\s"\'<>]+\.(?:m3u8|mp4)', html)
    for url in direct[:5]:
        log.append(f"  прямая ссылка на поток: {url[:120]}")

    if found_links:
        log.append("  страницы аниме: " + ", ".join(found_links[:6]))
    return found_links


def probe_api(session: requests.Session, base: str, out_dir: Path, log: list[str]) -> None:
    log.append("")
    log.append("=== типовые API-пути ===")
    for path in API_PATHS:
        url = urljoin(base, path)
        try:
            resp = session.get(url, headers=HEADERS, timeout=15, allow_redirects=False)
        except Exception as exc:
            log.append(f"  {path}: FAIL {type(exc).__name__}")
            continue
        marker = "JSON" if "json" in resp.headers.get("Content-Type", "") else "html/other"
        log.append(f"  {path}: HTTP {resp.status_code} ({marker}, {len(resp.content)} байт)")
        if resp.status_code == 200 and "json" in resp.headers.get("Content-Type", ""):
            (out_dir / f"api_{path.strip('/').replace('/', '_') or 'root'}.json").write_text(
                resp.text[:200000], encoding="utf-8"
            )


def probe_search(session: requests.Session, base: str, query: str, out_dir: Path, log: list[str]) -> None:
    log.append("")
    log.append(f"=== поиск «{query}» типовыми путями ===")
    candidates = [
        f"/search?q={query}", f"/search?query={query}", f"/?s={query}",
        f"/api/search?q={query}", f"/api/anime?search={query}", f"/anime?search={query}",
        f"/catalog?search={query}",
    ]
    from urllib.parse import quote

    for path in candidates:
        url = urljoin(base, quote(path, safe="/?=&"))
        try:
            resp = session.get(url, headers=HEADERS, timeout=20, allow_redirects=True)
        except Exception as exc:
            log.append(f"  {path}: FAIL {type(exc).__name__}")
            continue
        ctype = resp.headers.get("Content-Type", "")
        log.append(f"  {path}: HTTP {resp.status_code} ({ctype}, {len(resp.content)} байт)")
        if resp.status_code == 200:
            suffix = "json" if "json" in ctype else "html"
            (out_dir / f"search_{safe_name(url.replace('/', '_'))}.{suffix}").write_text(
                resp.text[:300000], encoding="utf-8"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description="Разведка сайта источника видео")
    parser.add_argument("url", help="адрес сайта, например https://dreamerscast.com")
    parser.add_argument("--search", default="", help="что поискать на сайте")
    parser.add_argument("--deep", action="store_true", help="зайти на страницы аниме")
    parser.add_argument("--dir", default="", help="папка для результатов")
    args = parser.parse_args()

    base = args.url.rstrip("/") + "/"
    out_dir = Path(args.dir) if args.dir else Path(__file__).resolve().parent / "probe"
    out_dir.mkdir(parents=True, exist_ok=True)

    log: list[str] = [f"Разведка {base}", f"Результаты: {out_dir}", ""]
    session = requests.Session()

    resp = fetch(session, base, out_dir, log)
    if resp is None:
        log.append("")
        log.append("Сайт недоступен из этого окружения. Попробуйте: включить VPN/обход "
                   "блокировок, проверить адрес, или запустить скрипт в браузере-ориентированной сети.")
    else:
        html = resp.text
        links = scan_html(html, base, log)

        # сохраняем JS-бандлы: в них обычно лежат адреса API
        scripts = [urljoin(base, s) for s in re.findall(r'<script[^>]+src="([^"]+)"', html)]
        js_dir = out_dir / "js"
        js_dir.mkdir(exist_ok=True)
        total = 0
        for script in scripts[:10]:
            try:
                r = session.get(script, headers=HEADERS, timeout=25)
                if r.status_code == 200 and total < 8_000_000:
                    name = safe_name(script)[:60] + ".js"
                    (js_dir / name).write_bytes(r.content)
                    total += len(r.content)
                    log.append(f"  скрипт сохранён: {name} ({len(r.content)} байт)")
            except Exception as exc:
                log.append(f"  скрипт {script[:60]} не скачался: {type(exc).__name__}")

        probe_api(session, base, out_dir, log)
        if args.search:
            probe_search(session, base, args.search, out_dir, log)

        if args.deep and links:
            log.append("")
            log.append("=== страницы аниме ===")
            for url in links[:5]:
                deep = fetch(session, url, out_dir, log)
                if deep is not None and "html" in deep.headers.get("Content-Type", ""):
                    scan_html(deep.text, url, log)

    report = out_dir / "report.txt"
    report.write_text("\n".join(log), encoding="utf-8")
    print("\n".join(log))
    print(f"\nОтчёт сохранён: {report}")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    raise SystemExit(main())
