"""Minimal JSON-over-HTTPS client shared by the Apify and Firecrawl tiers, and
by the optional TypeSafe judge.

Both vendors are plain REST, so urllib covers them and neither tier needs a
pip install -- which is why three of the four tiers are dependency-free.
"""

import json
import re
import time
import urllib.error
import urllib.request

from .base import LIMITS, Page, collapse


# Rate limits and 5xx are transient. 529 is TypeSafe's "overloaded".
RETRYABLE_STATUSES = (429, 500, 502, 503, 504, 529)


class ApiError(Exception):
    def __init__(self, message, status=0, retryable=False, retry_after=0.0):
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.retry_after = retry_after


def _retry_after_of(exc):
    """Seconds the server asked us to wait, clamped so a bad header cannot
    stall the whole run."""
    try:
        raw = exc.headers.get("Retry-After") if exc.headers else None
    except Exception:
        raw = None
    if not raw:
        return 0.0
    try:
        return min(float(raw), LIMITS["retry_after_cap_s"])
    except (TypeError, ValueError):
        return 0.0          # HTTP-date form; fall back to our own backoff


def with_retries(call, retries=None, label=""):
    """Run `call`, retrying transient failures with exponential backoff.

    Rate limits and 5xx are transient; 401/402/403 are not, and retrying them
    just burns quota and looks like abuse to the provider.
    """
    attempts = LIMITS["http_retries"] if retries is None else retries
    delay = LIMITS["backoff_base_s"]
    last = None
    for i in range(attempts + 1):
        try:
            return call()
        except ApiError as exc:
            last = exc
            if not exc.retryable or i == attempts:
                raise
            wait = exc.retry_after or delay
            time.sleep(min(wait, LIMITS["backoff_max_s"]))
            delay = min(delay * 2, LIMITS["backoff_max_s"])
    raise last


def post_json(url, payload, headers=None, timeout=180):
    body = json.dumps(payload).encode("utf-8")
    hdrs = {"Content-Type": "application/json", "Accept": "application/json"}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw.decode("utf-8", "replace")) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        # 402 = out of credits, 401 = bad key: both are worth surfacing loudly
        # rather than silently degrading to the next tier.
        raise ApiError("HTTP %d %s" % (exc.code, detail), status=exc.code,
                       retryable=exc.code in RETRYABLE_STATUSES,
                       retry_after=_retry_after_of(exc))
    except ApiError:
        raise
    except Exception as exc:
        raise ApiError(str(exc)[:200], retryable=True)


def get_json(url, headers=None, timeout=60):
    hdrs = {"Accept": "application/json"}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, headers=hdrs, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw.decode("utf-8", "replace")) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        raise ApiError("HTTP %d %s" % (exc.code, detail), status=exc.code,
                       retryable=exc.code in RETRYABLE_STATUSES,
                       retry_after=_retry_after_of(exc))
    except ApiError:
        raise
    except Exception as exc:
        raise ApiError(str(exc)[:200], retryable=True)


_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MD_IMG = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_CHROME = re.compile(r"[*_`>#]+")
# Markdown hard-breaks and escaped punctuation leave stray backslashes that
# survive collapse() as "\\ \\" noise and eat the digest budget.
_MD_ESCAPE = re.compile(r"\\+(?=[\s\W]|$)")


def markdown_to_text(md):
    """Flatten markdown to the same shape tier 1 produces, so digests from
    different tiers read identically to the evaluator."""
    if not md:
        return "", []
    headings = []
    for line in md.splitlines():
        m = re.match(r"^\s{0,3}(#{1,3})\s+(.*)", line)
        if m:
            h = collapse(_MD_LINK.sub(r"\1", m.group(2)))
            h = _MD_ESCAPE.sub("", _MD_CHROME.sub("", h)).strip()
            if h and len(h) < 200:
                headings.append(h)
    text = _MD_IMG.sub(" ", md)
    text = _MD_LINK.sub(r"\1", text)
    text = re.sub(r"^\s*\|.*\|\s*$", " ", text, flags=re.M)   # tables
    text = re.sub(r"^[-=]{3,}$", " ", text, flags=re.M)       # rules
    text = _MD_CHROME.sub(" ", text)
    # Markdown hard-breaks and escapes leave stray backslashes that survive as
    # "\\ \\" noise in the digest and eat the character budget.
    text = _MD_ESCAPE.sub("", text)
    return collapse(text), headings[:20]


def page_from_markdown(url, md, title="", description="", http_status=200):
    text, headings = markdown_to_text(md)
    return Page(
        url=url,
        title=collapse(title)[:300],
        description=collapse(description)[:500],
        headings=headings,
        text=text,
        http_status=http_status,
    )
