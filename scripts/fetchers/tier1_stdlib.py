"""Tier 1: stdlib fetcher. Free, fast, no dependencies, no API key.

Handles the things that actually decide whether a plain-HTTP crawler works on
real B2B sites: a browser User-Agent, manual gzip/deflate decompression
(urllib does not do it for you), charset sniffing, and a retry on broken TLS.
"""

import gzip
import re
import socket
import ssl
import time
import zlib
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse
import urllib.error
import urllib.request

from .base import (
    LIMITS, Fetcher, Page, SiteResult, candidate_urls, collapse,
    normalize_domain, same_site,
)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    # Deliberately no "br" -- brotli is not in the stdlib and we cannot decode it.
    "Accept-Encoding": "gzip, deflate",
    "Connection": "close",
}

MAX_BYTES = 1_500_000
SKIP_CONTENT = ("<script", "<style", "<noscript", "<svg")

# Path fragments worth a subpage fetch, best first. Careers is deliberately
# included -- hiring is the strongest momentum signal a public site offers.
PATH_SIGNALS = [
    (("what-we-do", "what_we_do"), 10),
    (("services", "service"), 9),
    (("solutions", "solution"), 9),
    (("products", "product"), 8),
    (("about", "about-us", "company", "who-we-are"), 8),
    (("industries", "industr", "verticals"), 7),
    (("capabilities", "capabilit", "expertise"), 7),
    (("case-studies", "case-study", "casestud"), 6),
    (("customers", "clients", "client"), 6),
    (("pricing", "plans"), 5),
    (("careers", "career", "jobs", "join-us", "we-are-hiring"), 5),
    (("platform", "technology"), 4),
]

SKIP_PATH = re.compile(
    r"(\.(pdf|jpg|jpeg|png|gif|svg|zip|mp4|mp3|doc|docx|xls|xlsx|ppt|pptx|webp|ico)$"
    r"|/wp-content/|/wp-admin/|/wp-json/|/feed/?$|/tag/|/category/|/author/"
    r"|/privacy|/terms|/cookie|/legal|/sitemap|/login|/signin|/sign-in|/cart"
    r"|/account|/search|/rss|mailto:|tel:|javascript:|#)",
    re.I,
)

BLOCK_TAGS = {"script", "style", "noscript", "svg", "head", "nav", "footer",
              "form", "iframe", "template", "select", "aside"}
HEADING_TAGS = {"h1", "h2", "h3"}


class Extractor(HTMLParser):
    """Pulls title, meta description, headings, visible text and links."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.description = ""
        self.headings = []
        self.links = []
        self._chunks = []
        self._block_depth = 0
        self._in_title = False
        self._heading = None
        self._heading_buf = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in BLOCK_TAGS:
            self._block_depth += 1
            return
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            name = (a.get("name") or a.get("property") or "").lower()
            if name in ("description", "og:description") and not self.description:
                self.description = collapse(a.get("content") or "")
        elif tag == "a":
            href = a.get("href")
            if href:
                self.links.append(href)
        elif tag in HEADING_TAGS and self._block_depth == 0:
            self._heading = tag
            self._heading_buf = []

    def handle_endtag(self, tag):
        if tag in BLOCK_TAGS:
            self._block_depth = max(0, self._block_depth - 1)
            return
        if tag == "title":
            self._in_title = False
        elif tag in HEADING_TAGS and self._heading == tag:
            h = collapse(" ".join(self._heading_buf))
            if h and len(h) < 200:
                self.headings.append(h)
            self._heading = None
            self._heading_buf = []

    def handle_data(self, data):
        if self._in_title:
            self.title += data
            return
        if self._block_depth:
            return
        if self._heading:
            self._heading_buf.append(data)
        text = data.strip()
        if text:
            self._chunks.append(text)

    def text(self):
        return collapse(" ".join(self._chunks))


def _decode_body(raw, headers):
    enc = (headers.get("Content-Encoding") or "").lower()
    try:
        if "gzip" in enc:
            raw = gzip.decompress(raw)
        elif "deflate" in enc:
            try:
                raw = zlib.decompress(raw)
            except zlib.error:
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
    except Exception:
        pass  # served a bad encoding header; fall through and try as-is

    charset = ""
    ctype = headers.get("Content-Type") or ""
    m = re.search(r"charset=([\w-]+)", ctype, re.I)
    if m:
        charset = m.group(1)
    if not charset:
        m = re.search(rb'charset=["\']?([\w-]+)', raw[:4096], re.I)
        if m:
            charset = m.group(1).decode("ascii", "ignore")
    for cs in (charset, "utf-8", "cp1252"):
        if not cs:
            continue
        try:
            return raw.decode(cs)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", "replace")


def _classify_error(exc):
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in (401, 403, 429):
            return "blocked", "HTTP %d" % exc.code
        if 400 <= exc.code < 500:
            return "http_4xx", "HTTP %d" % exc.code
        return "http_5xx", "HTTP %d" % exc.code
    if isinstance(exc, socket.timeout):
        return "timeout", "timeout"
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        text = str(reason)
        if isinstance(reason, socket.timeout) or "timed out" in text.lower():
            return "timeout", "timeout"
        if isinstance(reason, socket.gaierror) or "getaddrinfo" in text.lower() \
                or "name or service not known" in text.lower() \
                or "nodename nor servname" in text.lower():
            return "dns_fail", "dns"
        if "certificate" in text.lower() or "ssl" in text.lower():
            return "tls_fail", text[:120]
        return "conn_fail", text[:120]
    return "conn_fail", str(exc)[:120]


_LAX_CTX = ssl.create_default_context()
_LAX_CTX.check_hostname = False
_LAX_CTX.verify_mode = ssl.CERT_NONE


def _retry_after_seconds(exc):
    try:
        raw = exc.headers.get("Retry-After") if exc.headers else None
        return min(float(raw), LIMITS["retry_after_cap_s"]) if raw else 0.0
    except Exception:
        return 0.0


def fetch_url(url, timeout=12, allow_insecure_retry=True, _attempt=0):
    """-> (html, final_url, status, error). status is 'ok' or a failure code.

    Backs off and retries when a host says it is rate limited or briefly
    unavailable. A 403 is NOT retried: that is a considered refusal, and
    hammering it is what turns a soft block into a hard one.
    """
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(MAX_BYTES)
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if ctype and "html" not in ctype and "xml" not in ctype and "text" not in ctype:
                return "", resp.geturl(), "not_html", ctype[:60]
            return _decode_body(raw, resp.headers), resp.geturl(), "ok", ""
    except Exception as exc:
        status, err = _classify_error(exc)

        # 429/503 are "come back later", not "go away". Honour Retry-After if
        # the host sent one, else exponential backoff.
        transient = (isinstance(exc, urllib.error.HTTPError)
                     and exc.code in (429, 503))
        if transient and _attempt < LIMITS["http_retries"]:
            wait = _retry_after_seconds(exc) or (
                LIMITS["backoff_base_s"] * (2 ** _attempt))
            time.sleep(min(wait, LIMITS["backoff_max_s"]))
            return fetch_url(url, timeout, allow_insecure_retry, _attempt + 1)

        # Small-business certs are broken constantly. Discarding a real lead
        # over an expired cert is the worse error; we are reading public
        # marketing copy, not transacting.
        if status == "tls_fail" and allow_insecure_retry:
            try:
                with urllib.request.urlopen(req, timeout=timeout, context=_LAX_CTX) as resp:
                    raw = resp.read(MAX_BYTES)
                    return _decode_body(raw, resp.headers), resp.geturl(), "ok", "insecure_tls"
            except Exception as exc2:
                status, err = _classify_error(exc2)
        return "", url, status, err


def score_link(path):
    score = 0
    low = path.lower()
    for fragments, weight in PATH_SIGNALS:
        if any(f in low for f in fragments):
            score = max(score, weight)
    depth = len([p for p in low.split("/") if p])
    return score - max(0, depth - 2)


def pick_subpages(links, base_url, domain, limit=4):
    seen, scored = set(), []
    for href in links:
        if not href or SKIP_PATH.search(href):
            continue
        try:
            full = urljoin(base_url, href)
        except ValueError:
            continue
        parsed = urlparse(full)
        if parsed.scheme not in ("http", "https"):
            continue
        if not same_site(parsed.netloc, domain):
            continue
        clean = parsed.scheme + "://" + parsed.netloc + parsed.path.rstrip("/")
        if clean in seen or clean.rstrip("/") == base_url.rstrip("/"):
            continue
        s = score_link(parsed.path)
        if s <= 0:
            continue
        seen.add(clean)
        scored.append((s, clean))
    scored.sort(key=lambda x: (-x[0], len(x[1])))
    return [u for _, u in scored[:limit]]


def parse_page(url, html, http_status=200):
    ex = Extractor()
    try:
        ex.feed(html)
    except Exception:
        pass  # malformed markup: keep whatever was parsed before the break
    return Page(
        url=url,
        title=collapse(ex.title)[:300],
        description=ex.description[:500],
        headings=ex.headings[:20],
        text=ex.text(),
        http_status=http_status,
    ), ex.links


class StdlibFetcher(Fetcher):
    name = "stdlib"
    costs_money = False

    def __init__(self, timeout=12, site_budget=45):
        self.timeout = timeout
        self.site_budget = site_budget

    def available(self):
        return True

    def fetch_site(self, domain, max_pages=5):
        start = time.time()
        domain = normalize_domain(domain)
        if not domain:
            return SiteResult(domain="", source_tier=self.name, status="no_url",
                              error="unparseable")

        result = SiteResult(domain=domain, source_tier=self.name)
        html, final_url, status, err = "", "", "", ""
        for url in candidate_urls(domain):
            html, final_url, status, err = fetch_url(url, self.timeout)
            if status == "ok" and html:
                break

        if status != "ok" or not html:
            result.status = status if status in (
                "dns_fail", "timeout", "blocked", "http_4xx", "http_5xx") else "conn_fail"
            result.error = err
            result.duration_s = round(time.time() - start, 2)
            return result

        home, links = parse_page(final_url, html)
        result.pages.append(home)

        parsed = urlparse(final_url)
        if not same_site(parsed.netloc, domain):
            result.error = "redirect_offdomain:" + parsed.netloc

        for sub in pick_subpages(links, final_url, parsed.netloc, limit=max_pages - 1):
            if time.time() - start > self.site_budget:
                break
            # Pages of one host are fetched in series with a pause. Different
            # hosts run in parallel, so this costs almost no wall-clock while
            # keeping us well under anything that looks like an attack.
            time.sleep(LIMITS["per_domain_delay_s"])
            sub_html, sub_final, sub_status, _ = fetch_url(sub, self.timeout)
            if sub_status == "ok" and sub_html:
                page, _ = parse_page(sub_final, sub_html)
                if page.text:
                    result.pages.append(page)

        result.status = "ok" if result.body_chars() >= 400 else "thin"
        result.duration_s = round(time.time() - start, 2)
        return result
