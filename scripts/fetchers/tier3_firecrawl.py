"""Tier 3: Firecrawl.

Strongest anti-bot and proxy rotation of the four tiers. Plain REST, so no pip
install. Costs money per scraped page.

Strategy: one scrape of the homepage asking for markdown AND links, then score
those links with the same rules tier 1 uses and scrape the best few. That gets
subpages without paying for a separate /v1/map call.
"""

import os
import time
from urllib.parse import urlparse

from .base import Fetcher, SiteResult, normalize_domain
from ._json_http import ApiError, page_from_markdown, post_json, with_retries
from .tier1_stdlib import pick_subpages

# v2 is current. Response shape matches v1 for our purposes: data.markdown,
# data.metadata.{title,description,sourceURL,statusCode}, data.links as a flat
# list of URL strings. Override with FIRECRAWL_API_URL if the version moves on.
SCRAPE_URL = os.environ.get("FIRECRAWL_API_URL",
                            "https://api.firecrawl.dev/v2/scrape")


class FirecrawlFetcher(Fetcher):
    name = "firecrawl"
    costs_money = True

    def __init__(self, api_key=None, timeout=120):
        self.api_key = api_key or os.environ.get("FIRECRAWL_API_KEY", "")
        self.timeout = timeout

    def available(self):
        return bool(self.api_key)

    def unavailable_reason(self):
        return "FIRECRAWL_API_KEY not set"

    def _headers(self):
        return {"Authorization": "Bearer " + self.api_key}

    def _scrape(self, url, want_links=False):
        payload = {
            "url": url,
            "formats": ["markdown", "links"] if want_links else ["markdown"],
            "onlyMainContent": True,
            "timeout": 45000,
        }
        # Rate limits and 5xx get backed off and retried; 401/402 do not.
        data = with_retries(lambda: post_json(
            SCRAPE_URL, payload, headers=self._headers(), timeout=self.timeout))
        body = data.get("data") if isinstance(data, dict) else None
        if not isinstance(body, dict):
            return None, []
        meta = body.get("metadata") or {}
        page = page_from_markdown(
            meta.get("sourceURL") or url,
            body.get("markdown") or "",
            title=meta.get("title") or meta.get("ogTitle") or "",
            description=meta.get("description") or meta.get("ogDescription") or "",
            http_status=int(meta.get("statusCode") or 200),
        )
        return page, (body.get("links") or [])

    def fetch_site(self, domain, max_pages=5):
        start = time.time()
        domain = normalize_domain(domain)
        if not domain:
            return SiteResult(domain="", source_tier=self.name, status="no_url",
                              error="unparseable")
        result = SiteResult(domain=domain, source_tier=self.name)

        home_url = "https://" + domain
        try:
            home, links = self._scrape(home_url, want_links=True)
        except ApiError as exc:
            result.status = "blocked" if exc.status in (401, 402) else "conn_fail"
            result.error = "firecrawl:" + str(exc)[:160]
            result.duration_s = round(time.time() - start, 2)
            return result

        if home is None or not home.text:
            result.status = "thin"
            result.error = "firecrawl:no_text"
            result.duration_s = round(time.time() - start, 2)
            return result

        result.pages.append(home)
        base = home.url or home_url
        host = urlparse(base).netloc or domain

        for sub in pick_subpages(links, base, host, limit=max_pages - 1):
            if time.time() - start > self.timeout:
                break
            try:
                page, _ = self._scrape(sub)
            except ApiError:
                continue  # one bad subpage should not sink an otherwise good site
            if page and page.text:
                result.pages.append(page)

        result.status = "ok" if result.body_chars() >= 400 else "thin"
        result.duration_s = round(time.time() - start, 2)
        return result
