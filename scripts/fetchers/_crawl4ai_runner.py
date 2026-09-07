"""Subprocess runner for Crawl4AI.

Lives in its own process so Playwright, asyncio and a bundled Chromium never
touch the main scripts -- the other three tiers stay importable with nothing
installed.

Protocol: JSON on stdin {"urls": [...]}, JSON on stdout {"pages": [...]}.
"""

import asyncio
import json
import sys


def _markdown_of(result):
    md = getattr(result, "markdown", "") or ""
    for attr in ("fit_markdown", "raw_markdown"):
        inner = getattr(md, attr, None)
        if inner:
            return str(inner)
    return str(md)


async def _run(urls):
    from crawl4ai import AsyncWebCrawler

    try:
        from crawl4ai import BrowserConfig
        crawler = AsyncWebCrawler(config=BrowserConfig(headless=True, verbose=False))
    except Exception:
        crawler = AsyncWebCrawler(verbose=False)

    pages = []
    async with crawler:
        for url in urls:
            try:
                res = await crawler.arun(url=url)
            except Exception as exc:
                pages.append({"url": url, "error": str(exc)[:200]})
                continue
            if not getattr(res, "success", True):
                pages.append({"url": url,
                              "error": str(getattr(res, "error_message", "failed"))[:200]})
                continue
            meta = getattr(res, "metadata", None) or {}
            links = getattr(res, "links", None) or {}
            internal = links.get("internal") if isinstance(links, dict) else []
            hrefs = []
            for item in (internal or [])[:400]:
                href = item.get("href") if isinstance(item, dict) else item
                if href:
                    hrefs.append(href)
            pages.append({
                "url": getattr(res, "url", url) or url,
                "title": meta.get("title") or "",
                "description": meta.get("description") or "",
                "markdown": _markdown_of(res),
                "links": hrefs,
            })
    return pages


SENTINEL = "<<<C4A_JSON>>>"


def _emit(real_stdout, obj):
    real_stdout.write(SENTINEL + json.dumps(obj, ensure_ascii=False) + "\n")
    real_stdout.flush()


def main():
    # Windows consoles default to cp1252 and raise on any non-Latin-1 char.
    # crawl4ai's own progress output contains arrows and word-joiners, so
    # without this the child dies with UnicodeEncodeError and the domain looks
    # like a fetch failure. This runner is standalone and cannot import base.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    # crawl4ai logs fetch progress to stdout unconditionally, which corrupts
    # the JSON channel. Point stdout at stderr for the duration of the crawl
    # and emit the payload behind a sentinel on the real stdout.
    real_stdout = sys.stdout
    sys.stdout = sys.stderr
    try:
        try:
            payload = json.loads(sys.stdin.read() or "{}")
        except Exception as exc:
            _emit(real_stdout, {"error": "bad_input: " + str(exc)})
            return 1
        urls = payload.get("urls") or []
        if not urls:
            _emit(real_stdout, {"error": "no_urls"})
            return 1
        try:
            pages = asyncio.run(_run(urls))
        except Exception as exc:
            _emit(real_stdout, {"error": str(exc)[:300]})
            return 1
        _emit(real_stdout, {"pages": pages})
        return 0
    finally:
        sys.stdout = real_stdout


if __name__ == "__main__":
    sys.exit(main())
