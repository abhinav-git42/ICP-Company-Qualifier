"""Tier 2: Apify website-content-crawler (actor id aYG0l9s7dbB7j3gbS).

Purpose-built for this job -- crawls a site, renders JS, strips boilerplate,
returns clean markdown. Plain REST, so no pip install.

BATCHED BY DESIGN. Measured on this account, one domain per actor run costs
~$0.102 because every run pays ~60s of container startup before it fetches
anything. Four domains in a single run cost $0.093 total -- $0.023 each, 4.4x
cheaper -- and took the same wall time. So this tier crawls many domains per
run and maps the dataset back by registrable domain.

Uses the async pattern (POST /runs, poll, read dataset) rather than
run-sync-get-dataset-items, whose timeout ceiling a large batch would exceed.
"""

import json
import os
import time
from urllib.parse import urlparse

from .base import (
    LIMITS, Fetcher, SiteResult, normalize_domain, registrable,
)
from ._json_http import (
    ApiError, get_json, page_from_markdown, post_json, with_retries)

ACTOR = "apify~website-content-crawler"
BASE = "https://api.apify.com/v2"

DEFAULT_BATCH = 20          # domains per actor run
POLL_INTERVAL = 5           # seconds between status checks
TERMINAL = {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT", "TIMING-OUT"}


class ApifyFetcher(Fetcher):
    name = "apify"
    costs_money = True
    supports_batch = True

    def __init__(self, token=None, batch_size=DEFAULT_BATCH, run_timeout=900):
        self.token = token or os.environ.get("APIFY_TOKEN", "")
        self.batch_size = batch_size
        self.run_timeout = run_timeout

    def available(self):
        return bool(self.token)

    def unavailable_reason(self):
        return "APIFY_TOKEN not set"

    # --- single domain: just a batch of one -------------------------------

    def fetch_site(self, domain, max_pages=5):
        # Unparseable input is a no_url, not thin content. fetch_batch keys
        # those by the ORIGINAL string (there is no domain to key them by), so
        # resolve it here rather than falling through to the default below.
        nd = normalize_domain(domain)
        if not nd:
            return SiteResult(domain="", source_tier=self.name,
                              status="no_url", error="unparseable")
        results = self.fetch_batch([domain], max_pages=max_pages)
        return results.get(nd) or SiteResult(
            domain=nd, source_tier=self.name,
            status="thin", error="apify:no_result")

    # --- the real entry point ---------------------------------------------

    def fetch_batch(self, domains, max_pages=5):
        """{domain: SiteResult} for every domain passed in."""
        clean = []
        out = {}
        for d in domains:
            nd = normalize_domain(d)
            if nd and nd not in clean:
                clean.append(nd)
            elif not nd:
                out[d] = SiteResult(domain="", source_tier=self.name,
                                    status="no_url", error="unparseable")
        if clean:
            ok, msg = self.check_budget()
            if not ok:
                return dict(out, **{d: SiteResult(
                    domain=d, source_tier=self.name, status="blocked",
                    error="apify:budget guard -- " + msg) for d in clean})

        for i in range(0, len(clean), self.batch_size):
            chunk = clean[i:i + self.batch_size]
            out.update(self._run_chunk(chunk, max_pages))
        return out

    def _run_chunk(self, domains, max_pages):
        start = time.time()
        payload = {
            "startUrls": [{"url": "https://" + d} for d in domains],
            "crawlerType": "playwright:adaptive",
            # Depth 1 plus a global page budget. WCC enqueues every startUrl
            # before following links, so if the budget runs short the homepages
            # are already in -- the worst case degrades to homepage-only rather
            # than starving whole domains.
            "maxCrawlDepth": 1,
            "maxCrawlPages": len(domains) * max_pages,
            "maxResults": len(domains) * max_pages,
            "removeCookieWarnings": True,
            "removeElementsCssSelector":
                "nav, footer, script, style, noscript, svg, "
                "[role=\"navigation\"], [role=\"banner\"]",
            "saveMarkdown": True,
            "saveHtml": False,
            "proxyConfiguration": {"useApifyProxy": True},
            "requestTimeoutSecs": 40,
        }

        try:
            run = self._start(payload)
            run = self._wait(run["id"], deadline=start + self.run_timeout)
            items = self._dataset(run.get("defaultDatasetId"))
        except ApiError as exc:
            status = "blocked" if exc.status in (401, 402) else "conn_fail"
            return {d: SiteResult(domain=d, source_tier=self.name, status=status,
                                  error="apify:" + str(exc)[:160],
                                  duration_s=round(time.time() - start, 2))
                    for d in domains}
        except Exception as exc:
            return {d: SiteResult(domain=d, source_tier=self.name,
                                  status="conn_fail",
                                  error="apify:" + str(exc)[:160],
                                  duration_s=round(time.time() - start, 2))
                    for d in domains}

        return self._map_items(items, domains, max_pages, start)

    # --- api plumbing ------------------------------------------------------

    def check_budget(self):
        """(ok, message). Refuse to start a run that could push the account
        into its monthly cap -- a hard stop mid-campaign is far worse than a
        clear refusal before it starts."""
        try:
            data = get_json("%s/users/me/limits?token=%s" % (BASE, self.token),
                            timeout=30)
        except ApiError as exc:
            return True, "budget unknown (%s)" % str(exc)[:60]
        d = (data or {}).get("data") or {}
        used = (d.get("current") or {}).get("monthlyUsageUsd")
        cap = (d.get("limits") or {}).get("maxMonthlyUsageUsd")
        if used is None or not cap:
            return True, "budget unknown"
        left = float(cap) - float(used)
        msg = "$%.2f of $%.2f used, $%.2f left" % (used, cap, left)
        if left <= LIMITS["apify_budget_headroom_usd"]:
            return False, msg + " -- inside the $%.0f safety headroom" % (
                LIMITS["apify_budget_headroom_usd"])
        return True, msg

    def _start(self, payload):
        url = "%s/acts/%s/runs?token=%s&timeout=%d" % (
            BASE, ACTOR, self.token, self.run_timeout)
        data = with_retries(lambda: post_json(url, payload, timeout=60))
        run = (data or {}).get("data") or {}
        if not run.get("id"):
            raise ApiError("no run id in response")
        return run

    def _wait(self, run_id, deadline):
        url = "%s/actor-runs/%s?token=%s" % (BASE, run_id, self.token)
        while True:
            data = with_retries(lambda: get_json(url, timeout=45))
            run = (data or {}).get("data") or {}
            if run.get("status") in TERMINAL:
                return run
            if time.time() > deadline:
                # Leave the run alone; it may still finish and be readable on a
                # later pass. We just stop paying wall-clock for it here.
                raise ApiError("run %s still %s after %ds"
                               % (run_id, run.get("status"), self.run_timeout))
            time.sleep(POLL_INTERVAL)

    def _dataset(self, dataset_id):
        if not dataset_id:
            return []
        url = "%s/datasets/%s/items?token=%s&clean=true" % (
            BASE, dataset_id, self.token)
        items = with_retries(lambda: get_json(url, timeout=120))
        return items if isinstance(items, list) else []

    # --- mapping -----------------------------------------------------------

    def _map_items(self, items, domains, max_pages, start):
        buckets = {d: [] for d in domains}
        index = {registrable(d): d for d in domains}

        for item in items:
            if not isinstance(item, dict):
                continue
            page_url = item.get("url") or ""
            host = urlparse(page_url).netloc
            owner = index.get(registrable(host))
            if not owner:
                continue
            md = item.get("markdown") or item.get("text") or ""
            if not md:
                continue
            meta = item.get("metadata") or {}
            page = page_from_markdown(
                page_url, md,
                title=meta.get("title") or item.get("title") or "",
                description=meta.get("description") or "",
                http_status=int(item.get("httpStatusCode") or 200),
            )
            if page.text:
                buckets[owner].append(page)

        elapsed = round(time.time() - start, 2)
        results = {}
        for d, pages in buckets.items():
            r = SiteResult(domain=d, source_tier=self.name, duration_s=elapsed)
            if not pages:
                r.status = "thin"
                r.error = "apify:no_content (blocked or empty)"
                results[d] = r
                continue
            # Shallowest path first so page 0 is the homepage, matching every
            # other tier's convention for the gate and the digest.
            pages.sort(key=lambda p: len(
                [s for s in urlparse(p.url).path.split("/") if s]))
            r.pages = pages[:max_pages]
            r.status = "ok" if r.body_chars() >= 400 else "thin"
            results[d] = r
        return results
