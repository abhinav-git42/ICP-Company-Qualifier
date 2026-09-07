"""Scrapling: free, no account, TLS-fingerprinted HTTP.

https://github.com/D4Vinci/Scrapling

Its role in the free pool is narrow and deliberate: `Fetcher.get` performs a
real browser TLS handshake via curl_cffi. That defeats fingerprint-based
blocking which rejects tier 1's plain urllib handshake -- at roughly tier 1's
speed and with no browser process.

BROWSER MODES ARE NOT USED. Scrapling's StealthyFetcher (Camoufox) and
DynamicFetcher both fail on Windows with
`BrowserType.launch_persistent_context: spawn UNKNOWN`, even though patchright
launches fine directly -- an issue in how Scrapling spawns a persistent
context. Nothing is lost by leaving them out: Crawl4AI already covers browser
rendering in the same free pool, so enabling them here would add a slow
duplicate rather than a new capability. `--scrapling-browser` re-enables the
attempt if a future version fixes it.

Parsed with tier 1's extractor so a Scrapling digest is byte-comparable with a
stdlib one. Different fetchers, one extraction path -- otherwise the quality
gate would be measuring the parser rather than the page.
"""

import importlib.util
import time
from urllib.parse import urlparse

from .base import (
    LIMITS, Fetcher, SiteResult, candidate_urls, normalize_domain, same_site,
)
from .tier1_stdlib import parse_page, pick_subpages

_QUIETED = False


def _quiet():
    """Scrapling logs an INFO line per fetch; at pool scale that is thousands
    of lines interleaved across threads."""
    global _QUIETED
    if _QUIETED:
        return
    try:
        import logging
        for name in ("scrapling", "scrapling.fetchers", "camoufox", "patchright"):
            logging.getLogger(name).setLevel(logging.WARNING)
    except Exception:
        pass
    _QUIETED = True


class ScraplingFetcher(Fetcher):
    name = "scrapling"
    costs_money = False
    # No browser process, so it can run about as wide as stdlib. Held slightly
    # lower because curl_cffi holds a real TLS session per request.
    max_workers = 6

    def __init__(self, timeout=30, use_browser=False):
        self.timeout = timeout
        self.use_browser = use_browser        # see module docstring

    def available(self):
        try:
            return importlib.util.find_spec("scrapling") is not None
        except (ImportError, ValueError):
            return False

    def unavailable_reason(self):
        return 'scrapling not installed (pip install "scrapling[fetchers]")'

    # --- fetching ----------------------------------------------------------

    def _get(self, url):
        """-> (html, final_url, status_code, error)."""
        from scrapling.fetchers import Fetcher as SFetcher
        try:
            p = SFetcher.get(url, timeout=self.timeout, stealthy_headers=True)
            return (p.html_content or ""), (p.url or url), int(p.status or 0), ""
        except Exception as exc:
            if not self.use_browser:
                return "", url, 0, str(exc)[:160]

        # Opt-in only, and currently broken on Windows -- see module docstring.
        try:
            from scrapling.fetchers import StealthyFetcher
            p = StealthyFetcher.fetch(url, headless=True, network_idle=True,
                                      timeout=self.timeout * 1000)
            return (p.html_content or ""), (p.url or url), int(p.status or 0), ""
        except Exception as exc:
            return "", url, 0, str(exc)[:160]

    @staticmethod
    def _status_for(code, err):
        if code in (401, 403, 429):
            return "blocked"
        if code and 400 <= code < 500:
            return "http_4xx"
        if code and code >= 500:
            return "http_5xx"
        low = (err or "").lower()
        if "timed out" in low or "timeout" in low:
            return "timeout"
        if "resolve" in low or "getaddrinfo" in low or "name or service" in low:
            return "dns_fail"
        return "conn_fail"

    # --- contract ----------------------------------------------------------

    def fetch_site(self, domain, max_pages=5):
        _quiet()
        start = time.time()
        domain = normalize_domain(domain)
        if not domain:
            return SiteResult(domain="", source_tier=self.name, status="no_url",
                              error="unparseable")
        result = SiteResult(domain=domain, source_tier=self.name)

        html = final_url = err = ""
        code = 0
        for url in candidate_urls(domain):
            html, final_url, code, err = self._get(url)
            if html:
                break

        if not html:
            result.status = self._status_for(code, err)
            result.error = ("scrapling:" + (err or "HTTP %s" % code))[:200]
            result.duration_s = round(time.time() - start, 2)
            return result

        home, links = parse_page(final_url, html, code or 200)
        result.pages.append(home)

        parsed = urlparse(final_url)
        if not same_site(parsed.netloc, domain):
            result.error = "redirect_offdomain:" + parsed.netloc

        for sub in pick_subpages(links, final_url, parsed.netloc,
                                 limit=max_pages - 1):
            if time.time() - start > self.timeout * 2:
                break
            time.sleep(LIMITS["per_domain_delay_s"])
            s_html, s_final, s_code, _ = self._get(sub)
            if s_html:
                page, _ = parse_page(s_final, s_html, s_code or 200)
                if page.text:
                    result.pages.append(page)

        result.status = "ok" if result.body_chars() >= 400 else "thin"
        result.duration_s = round(time.time() - start, 2)
        return result
