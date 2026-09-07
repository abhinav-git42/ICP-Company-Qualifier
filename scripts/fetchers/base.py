"""Shared contract for every fetch tier.

Defines the SiteResult all tiers return, the single quality gate they are
judged by, and the digest builder that caps what the evaluator ever reads.

Keeping the gate here -- rather than in each tier -- is the whole point: a
tier cannot grade its own homework, so tier 1 and tier 4 output are held to
byte-identical standards.
"""

import json
import os
import re
import sys
from dataclasses import dataclass, field, asdict

# --- gate thresholds -------------------------------------------------------
# Tunable in one place. Raise min_body_chars / min_content_words to escalate
# more aggressively (better digests, more spend); lower to escalate less.
GATE = {
    "min_body_chars": 400,        # total extracted text across all pages
    "min_home_chars": 1200,       # homepage alone, if no subpage was retrieved
    "min_content_words": 25,      # distinct meaningful words -- proves prose, not chrome
}

# --- safety limits --------------------------------------------------------
# One place for every "don't get blocked, don't overspend" knob. Deliberately
# conservative: a crawl that gets an IP banned costs far more than one that
# takes longer.
LIMITS = {
    # Politeness toward the sites we read.
    "per_domain_delay_s": 0.8,    # pause between pages on the SAME host
    "max_workers": 8,             # distinct hosts in flight at once
    # Transient-failure handling. Applies to every network tier.
    "http_retries": 2,            # attempts after the first
    "backoff_base_s": 2.0,        # doubles each retry
    "backoff_max_s": 30.0,
    "retry_after_cap_s": 60.0,    # honour Retry-After, but never sleep forever
    # Spend guards.
    "apify_budget_headroom_usd": 5.0,   # refuse to start a run inside this
    "apify_max_concurrent_runs": 4,     # account allows 32; stay well under
}

DIGEST_MAX_CHARS = 2500
DIGEST_HOME_CHARS = 1200
DIGEST_SUBPAGE_CHARS = 260

OK_STATUSES = {"ok", "thin"}

# Phrases that mean "this is not a company website".
PARKED_PATTERNS = [
    "domain is for sale", "domain for sale", "buy this domain",
    "this domain may be for sale", "parked free", "parked domain",
    "coming soon", "under construction", "site is being updated",
    "default web site page", "welcome to nginx", "apache2 ubuntu default",
    "future home of", "hugedomains", "sedoparking",
]

# Pages that returned 200 but only told us to turn on a browser feature.
STUB_PATTERNS = [
    "please enable javascript", "enable javascript to", "javascript is required",
    "javascript is disabled", "requires javascript", "enable cookies",
    "checking your browser", "verify you are human", "attention required",
    "access denied", "403 forbidden", "detected unusual activity",
]

# Words that appear on every site's chrome and prove nothing about the business.
NAV_STOPWORDS = {
    "home", "about", "contact", "services", "products", "blog", "news", "login",
    "signup", "sign", "register", "search", "menu", "close", "more", "read",
    "learn", "click", "here", "page", "site", "website", "welcome", "privacy",
    "policy", "terms", "conditions", "cookie", "cookies", "copyright", "rights",
    "reserved", "email", "phone", "address", "follow", "share", "twitter",
    "facebook", "linkedin", "instagram", "youtube", "subscribe", "newsletter",
    "submit", "send", "next", "previous", "back", "toggle", "navigation",
    "skip", "content", "main", "footer", "header", "with", "this", "that",
    "have", "from", "your", "our", "you", "the", "and", "for", "are", "was",
    "were", "will", "can", "all", "any", "get", "new", "now", "how", "what",
    "when", "where", "who", "why", "their", "has", "not", "but", "its",
}


@dataclass
class Page:
    url: str
    title: str = ""
    description: str = ""
    headings: list = field(default_factory=list)
    text: str = ""
    http_status: int = 0


@dataclass
class SiteResult:
    domain: str
    source_tier: str = ""
    status: str = "no_url"      # ok|thin|dns_fail|timeout|http_4xx|http_5xx|blocked|parked|no_url
    pages: list = field(default_factory=list)
    error: str = ""
    gate_ok: bool = False
    gate_reason: str = ""
    duration_s: float = 0.0

    def body_chars(self):
        return sum(len(p.text) for p in self.pages)

    def home(self):
        return self.pages[0] if self.pages else None

    def to_dict(self):
        return asdict(self)

    @staticmethod
    def from_dict(d):
        d = dict(d)
        d["pages"] = [Page(**p) for p in d.get("pages", [])]
        return SiteResult(**d)


class Fetcher:
    """Contract every tier implements. Order is a config line, not a rewrite."""

    name = "base"
    costs_money = False

    def available(self):
        raise NotImplementedError

    def unavailable_reason(self):
        return ""

    def fetch_site(self, domain, max_pages=5):
        raise NotImplementedError


# --- helpers ---------------------------------------------------------------

_WS = re.compile(r"\s+")

# Zero-width and format characters. They carry no meaning, bloat the digest,
# and crash a cp1252 console on Windows. Stripped at the source.
_INVISIBLE = re.compile(
    "[\\u00ad\\u180e\\u200b-\\u200f\\u202a-\\u202e\\u2060-\\u2064"
    "\\u2066-\\u206f\\ufeff\\ufff9-\\ufffb]")
_WORD = re.compile(r"[a-z][a-z-]{2,}")

MULTI_TLDS = {
    "co.uk", "org.uk", "ac.uk", "gov.uk", "co.in", "net.in", "org.in",
    "com.au", "net.au", "org.au", "co.nz", "co.za", "com.br", "com.mx",
    "com.sg", "com.hk", "co.jp", "or.jp", "com.cn", "com.tr", "co.il",
    "com.ar", "com.my", "co.id", "com.ph", "com.vn", "co.th", "com.pk",
    "com.sa", "com.eg", "co.ke", "com.ng", "com.tw", "co.kr", "com.pe",
    "com.co", "com.ua", "com.pl", "com.es", "com.pt", "com.ru",
}


def collapse(text):
    return _WS.sub(" ", _INVISIBLE.sub("", text or "")).strip()


def load_env(path=None):
    """Read the project's .env into os.environ, without overriding real env vars.

    Keys can live in either place. .env is preferred for a shared setup because
    on Windows a registry-level variable only reaches NEWLY started processes,
    so an already-open terminal keeps reading stale values -- a confusing
    failure for anyone who just ran setup.

    Never logs a value. Safe to call repeatedly.
    """
    if path is None:
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)))), ".env")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except (OSError, UnicodeDecodeError):
        return {}
    loaded = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key or not value:
            continue
        loaded[key] = value
        os.environ.setdefault(key, value)   # a real env var always wins
    return loaded


def enable_utf8_stdout():
    """Windows consoles default to cp1252 and raise on any non-Latin-1 char.
    Every CLI here prints crawled copy, so this is not optional."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def normalize_domain(raw):
    """A messy CSV cell -> a bare lowercase host, or empty string if unusable."""
    if not raw:
        return ""
    s = str(raw).strip().strip('"').strip("'")
    if not s or s.lower() in {"n/a", "na", "none", "null", "-", "--"}:
        return ""
    s = re.sub(r"^\s*(https?:)?//", "", s, flags=re.I)
    s = s.split("/")[0].split("?")[0].split("#")[0]
    s = s.split("@")[-1]           # somebody pasted an email address
    s = s.strip().strip(".").lower()
    s = re.sub(r":\d+$", "", s)    # strip port
    if not s or "." not in s or " " in s:
        return ""
    if not re.match(r"^[a-z0-9.-]+$", s):
        try:
            s = s.encode("idna").decode("ascii")
        except Exception:
            return ""
    if not re.match(r"^[a-z0-9.-]+\.[a-z]{2,}$", s):
        return ""
    return s


def candidate_urls(domain):
    """Ordered URL attempts for a bare host."""
    if not domain:
        return []
    bare = domain[4:] if domain.startswith("www.") else domain
    urls = ["https://" + domain]
    alt = "https://" + bare if domain.startswith("www.") else "https://www." + bare
    if alt not in urls:
        urls.append(alt)
    urls.append("http://" + domain)
    return urls


def registrable(host):
    """Rough eTLD+1. Good enough to keep a crawl on the same company."""
    host = (host or "").lower().lstrip(".")
    parts = [p for p in host.split(".") if p]
    if len(parts) <= 2:
        return ".".join(parts)
    if ".".join(parts[-2:]) in MULTI_TLDS and len(parts) >= 3:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def same_site(host, domain):
    return bool(host) and registrable(host) == registrable(domain)


def distinct_content_words(text):
    words = {w for w in _WORD.findall((text or "").lower()) if w not in NAV_STOPWORDS}
    return len(words)


def _matches(text, patterns):
    low = (text or "").lower()
    for p in patterns:
        if p in low:
            return p
    return ""


# --- the gate --------------------------------------------------------------

def quality_gate(result):
    """(ok, reason). The one definition of "good enough to judge a lead on".

    Reachability is only the first question. A 200 OK carrying nothing but a
    cookie banner fails exactly like a timeout -- that is what stops thin
    content from quietly becoming an "Avg" rating downstream.
    """
    if result.status not in OK_STATUSES:
        return False, "status:" + result.status
    if not result.pages:
        return False, "no_pages"

    home = result.home()
    all_text = " ".join(p.text for p in result.pages)
    all_meta = " ".join(
        (p.title or "") + " " + (p.description or "") + " " + " ".join(p.headings or [])
        for p in result.pages
    )
    haystack = (all_text + " " + all_meta)[:4000]
    body = result.body_chars()

    hit = _matches(haystack, PARKED_PATTERNS)
    if hit and body < 2000:
        return False, "parked:" + hit.replace(" ", "_")

    hit = _matches(haystack, STUB_PATTERNS)
    if hit and body < 2000:
        return False, "stub:" + hit.replace(" ", "_")

    if body < GATE["min_body_chars"]:
        return False, "thin:%d_chars" % body

    # A real title/description/H1 -- not just the domain echoed back.
    label = collapse(" ".join(filter(None, [
        home.title if home else "",
        home.description if home else "",
        " ".join(home.headings) if home and home.headings else "",
    ])))
    stripped = re.sub(r"[^a-z]", "", label.lower())
    domain_key = re.sub(r"[^a-z]", "", registrable(result.domain).split(".")[0].lower())
    if len(stripped) < 8 or (domain_key and stripped == domain_key):
        return False, "no_real_title"

    home_chars = len(home.text) if home else 0
    if len(result.pages) < 2 and home_chars < GATE["min_home_chars"]:
        return False, "single_thin_page:%d" % home_chars

    n_words = distinct_content_words(all_text)
    if n_words < GATE["min_content_words"]:
        return False, "not_prose:%d_words" % n_words

    return True, "pass"


def apply_gate(result):
    ok, reason = quality_gate(result)
    result.gate_ok = ok
    result.gate_reason = reason
    return result


# --- digest ----------------------------------------------------------------

def build_digest(result, max_chars=DIGEST_MAX_CHARS):
    """Compact, uniform text block the evaluator reads. Capped here, in code,
    so batch size stays predictable regardless of which tier won."""
    home = result.home()
    out = ["DOMAIN: " + result.domain]
    if home:
        if home.title:
            out.append("TITLE: " + collapse(home.title)[:200])
        if home.description:
            out.append("META: " + collapse(home.description)[:300])
        # Dedupe while preserving order. Repeated section headers are common
        # and each copy costs budget that a real subpage could have used.
        heads, seen = [], set()
        for h in (home.headings or []):
            h = collapse(h)
            key = h.lower()
            if h and key not in seen:
                seen.add(key)
                heads.append(h)
        if heads:
            out.append("HEADINGS: " + " | ".join(heads[:12])[:400])
        out.append("HOME: " + collapse(home.text)[:DIGEST_HOME_CHARS])

    for p in result.pages[1:]:
        path = re.sub(r"^https?://[^/]+", "", p.url) or "/"
        chunk = collapse(" ".join(filter(None, [
            p.title or "", " ".join(p.headings or [])[:200], p.text
        ])))
        if chunk:
            out.append("PAGE %s: %s" % (path[:60], chunk[:DIGEST_SUBPAGE_CHARS]))

    digest = "\n".join(out)
    if len(digest) > max_chars:
        digest = digest[:max_chars - 3] + "..."
    return digest


def jsonl_append(path, obj):
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, ensure_ascii=False) + "\n")


def jsonl_read(path):
    rows = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        pass
    return rows


# Populate os.environ from .env at import time, so every tier's available()
# check sees the same keys regardless of which script is running.
load_env()
