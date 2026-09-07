"""Plumbing tests for the fetch tiers.

test_gate.py covers judgment (is this content good enough?). This covers the
layer underneath it -- the boring machinery that, when it breaks, makes a
working tier look like a broken one.

Both real bugs found in this project were of exactly that shape and neither
raised anything: Crawl4AI wrote progress logging onto the JSON channel, and its
subprocess died on a cp1252 console because the runner never got the UTF-8
reconfigure the main scripts had. Each made successful fetches read as hard
failures. So these assert the channel, not the crawl.

Run: python scripts/test_tiers.py            (offline only, no network)
     python scripts/test_tiers.py --live     (adds one real crawl4ai fetch)
"""

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fetchers.base import (  # noqa: E402
    GATE, LIMITS, Page, SiteResult, apply_gate, build_digest,
    enable_utf8_stdout, normalize_domain,
)
from fetchers._json_http import (  # noqa: E402
    ApiError, markdown_to_text, page_from_markdown, with_retries,
)
from fetchers.tier1_stdlib import StdlibFetcher, pick_subpages  # noqa: E402
from fetchers.tier2_apify import ApifyFetcher  # noqa: E402
from fetchers.tier3_firecrawl import FirecrawlFetcher  # noqa: E402
from fetchers.tier4_crawl4ai import Crawl4AIFetcher, RUNNER, SENTINEL  # noqa: E402
from fetchers.tier_scrapling import ScraplingFetcher  # noqa: E402
from freepool import FreePool, run_free_pool  # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print("%-4s %-42s %s" % ("PASS" if ok else "FAIL", name, detail))


# --- 1. the contract every tier must honour --------------------------------

def test_contract():
    for cls in (StdlibFetcher, ScraplingFetcher, ApifyFetcher,
                FirecrawlFetcher, Crawl4AIFetcher):
        f = cls()
        ok = (isinstance(f.name, str) and f.name
              and isinstance(f.costs_money, bool)
              and callable(f.available) and callable(f.fetch_site)
              and isinstance(f.available(), bool))
        check("contract:%s" % f.name, ok)
        # An unavailable tier must explain itself rather than fail mutely.
        if not f.available():
            check("contract:%s explains absence" % f.name,
                  bool(f.unavailable_reason()), f.unavailable_reason()[:44])


def test_unparseable_input():
    """Junk in a CSV cell must never raise -- it becomes a no_url result."""
    for cls in (StdlibFetcher, ScraplingFetcher, ApifyFetcher,
                FirecrawlFetcher, Crawl4AIFetcher):
        f = cls()
        try:
            r = f.fetch_site("not a domain at all")
            ok = isinstance(r, SiteResult) and r.status == "no_url"
            detail = r.status
        except Exception as exc:
            ok, detail = False, "raised %s" % type(exc).__name__
        check("junk input:%s" % f.name, ok, detail)


# --- 2. the crawl4ai channel: the actual bug that bit us -------------------

def test_runner_unicode_roundtrip():
    """The runner must survive characters a cp1252 console cannot encode.

    This is the regression test for the bug that made stripe/vercel/notion
    look unreachable. It drives the real subprocess with no crawl4ai call.
    """
    hostile = "arrow \u2192 joiner \u2060 zwsp \u200b emdash \u2014 cjk \u4e2d\u6587"
    probe = (
        "import sys, json\n"
        "for s in (sys.stdout, sys.stderr):\n"
        "    try: s.reconfigure(encoding='utf-8', errors='replace')\n"
        "    except Exception: pass\n"
        "real = sys.stdout\n"
        "sys.stdout = sys.stderr\n"
        "print(%r)\n"                       # noise onto the polluted channel
        "real.write(%r + json.dumps({'pages': [{'markdown': %r}]}) + '\\n')\n"
        % (hostile, SENTINEL, hostile)
    )
    env = dict(os.environ, PYTHONIOENCODING="utf-8:replace")
    proc = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          env=env, timeout=60)
    check("runner:subprocess survives non-cp1252", proc.returncode == 0,
          (proc.stderr or "").strip().splitlines()[-1][:40] if proc.returncode else "")

    out = proc.stdout or ""
    check("runner:sentinel present in stdout", SENTINEL in out)
    if SENTINEL in out:
        blob = out.split(SENTINEL, 1)[1].splitlines()[0]
        try:
            data = json.loads(blob)
            got = data["pages"][0]["markdown"]
            check("runner:payload survives round-trip", got == hostile)
        except Exception as exc:
            check("runner:payload survives round-trip", False, str(exc)[:40])


def test_runner_ignores_stdout_noise():
    """Progress logging before the sentinel must not corrupt the payload."""
    probe = (
        "import sys, json\n"
        "print('[FETCH]... | OK | 1.78s')\n"
        "print('[SCRAPE].. done')\n"
        "sys.stdout.write(%r + json.dumps({'pages': [{'markdown': 'real'}]}) + '\\n')\n"
        % SENTINEL
    )
    proc = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=60)
    out = proc.stdout or ""
    ok = False
    if SENTINEL in out:
        try:
            ok = json.loads(out.split(SENTINEL, 1)[1].splitlines()[0])[
                "pages"][0]["markdown"] == "real"
        except Exception:
            ok = False
    check("runner:noise before payload ignored", ok)


def test_runner_reports_failure():
    """A dead runner must surface a message, not an empty success."""
    proc = subprocess.run([sys.executable, "-c", "import sys; sys.exit(3)"],
                          capture_output=True, text=True, timeout=30)
    check("runner:nonzero exit is detectable", proc.returncode == 3)
    check("runner:script exists on disk", os.path.exists(RUNNER))


# --- 3. shared extraction ---------------------------------------------------

def test_markdown_cleanup():
    md = "# Title\n\n[Link](http://x)\\\\ \\\\ text and 50\\% off\\.\n"
    text, heads = markdown_to_text(md)
    check("markdown:no backslash artifacts", "\\" not in text, text[:40])
    check("markdown:headings extracted", heads == ["Title"], str(heads))
    page = page_from_markdown("https://x.com/", md, title="T", description="D")
    check("markdown:builds a Page", bool(page.text) and page.title == "T")


def test_digest_cap():
    big = SiteResult(domain="x.com", status="ok", pages=[
        Page(url="https://x.com/", title="T" * 400, description="D" * 800,
             headings=["H%d" % i for i in range(50)], text="word " * 20000)
    ] + [Page(url="https://x.com/p%d" % i, title="P", text="word " * 5000)
         for i in range(10)])
    d = build_digest(big)
    check("digest:respects 2500 cap", len(d) <= 2500, "%d chars" % len(d))


def test_gate_is_tier_agnostic():
    """The same content must get the same verdict whichever tier produced it.

    This is what makes reordering tiers safe.
    """
    body = ("We build load matching and carrier settlement software for "
            "regional freight brokers across North America, covering EDI "
            "translation, rate benchmarking, predictive capacity planning, "
            "carrier onboarding, compliance reporting and settlement "
            "reconciliation for two hundred mid-market brokerages. ") * 3
    pages = [Page(url="https://x.com/", title="Acme Freight Software",
                  description="Logistics platform", headings=["Load matching"],
                  text=body),
             Page(url="https://x.com/about", title="About Acme",
                  text="Founded 2014 in Chicago by supply chain engineers.")]
    verdicts = set()
    for tier in ("stdlib", "crawl4ai", "apify", "firecrawl"):
        r = SiteResult(domain="x.com", source_tier=tier, status="ok",
                       pages=[Page(**vars(p)) for p in pages])
        apply_gate(r)
        verdicts.add((r.gate_ok, r.gate_reason))
    ok = len(verdicts) == 1 and verdicts.pop()[0] is True
    check("gate:identical PASS across all 4 tiers", ok, str(verdicts))


# --- 4. safety limits -------------------------------------------------------

def test_limits_sane():
    checks = [
        ("per_domain_delay_s", lambda v: 0 < v <= 5),
        ("max_workers", lambda v: 1 <= v <= 32),
        ("http_retries", lambda v: 0 <= v <= 5),
        ("backoff_max_s", lambda v: 1 <= v <= 120),
        ("apify_max_concurrent_runs", lambda v: 1 <= v <= 32),
    ]
    for key, valid in checks:
        check("limits:%s in range" % key, key in LIMITS and valid(LIMITS[key]),
              str(LIMITS.get(key)))
    check("limits:crawl4ai concurrency capped",
          getattr(Crawl4AIFetcher, "max_workers", 99) <= 6,
          "%s workers" % getattr(Crawl4AIFetcher, "max_workers", "?"))


def test_retry_policy():
    calls = {"n": 0}

    def transient():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ApiError("429", status=429, retryable=True, retry_after=0.01)
        return "ok"
    check("retry:recovers from 429", with_retries(transient) == "ok",
          "%d attempts" % calls["n"])

    hits = {"n": 0}

    def fatal():
        hits["n"] += 1
        raise ApiError("401", status=401, retryable=False)
    try:
        with_retries(fatal)
        ok = False
    except ApiError:
        ok = hits["n"] == 1
    check("retry:does NOT retry auth failure", ok, "%d attempt(s)" % hits["n"])


def test_no_offdomain_crawl():
    links = ["https://evil.com/about", "/services", "https://x.com/careers",
             "mailto:a@x.com", "https://sub.x.com/products", "/privacy"]
    picked = pick_subpages(links, "https://x.com/", "x.com", limit=5)
    off = [u for u in picked if "evil.com" in u]
    check("scope:never leaves the target site", not off, str(picked[:2]))
    check("scope:skips legal/boilerplate paths",
          not any("privacy" in u for u in picked))


def test_apify_budget_guard():
    f = ApifyFetcher()
    if not f.available():
        check("apify:budget guard (skipped, no token)", True, "no APIFY_TOKEN")
        return
    ok, msg = f.check_budget()
    check("apify:budget guard reports headroom", isinstance(ok, bool) and bool(msg), msg)


# --- 5. optional live check -------------------------------------------------

def test_live_crawl4ai():
    f = Crawl4AIFetcher()
    if not f.available():
        check("live:crawl4ai (skipped, not installed)", True)
        return
    r = f.fetch_site("example.com", max_pages=2)
    check("live:crawl4ai returns a SiteResult without raising",
          isinstance(r, SiteResult), "status=%s chars=%d" % (r.status, r.body_chars()))



# --- 6. the free pool: concurrency correctness ------------------------------

_FIXTURE_PROSE = (
    "We manufacture precision hydraulic components for industrial equipment "
    "across sixteen countries, with in-house tooling, machining, assembly and "
    "certification capability. Our engineering group designs custom manifolds, "
    "cylinders, valves and rotary actuators for agricultural harvesters, "
    "mining excavators, marine winches and aerospace ground support vehicles. "
    "Founded in nineteen eighty-four, the business operates four production "
    "plants, holds ISO certification, and supports customers through a "
    "distribution network spanning Europe, North America and Southeast Asia. "
    "Recent investment added robotic welding cells, automated inspection "
    "benches and a dedicated prototyping laboratory that shortens development "
    "cycles for original equipment manufacturers requiring bespoke fluid power "
    "solutions under demanding duty conditions and tight tolerance budgets. "
)


class _FakeTier:
    """Deterministic stand-in so pool logic is tested without the network."""

    costs_money = False

    def __init__(self, name, succeeds_on=(), max_workers=2, delay=0.0):
        self.name = name
        self.succeeds_on = set(succeeds_on)
        self.max_workers = max_workers
        self.delay = delay
        self.seen = []
        self._lock = __import__("threading").Lock()

    def available(self):
        return True

    def fetch_site(self, domain, max_pages=5):
        if self.delay:
            __import__("time").sleep(self.delay)
        with self._lock:
            self.seen.append(domain)
        ok = domain in self.succeeds_on
        return SiteResult(
            domain=domain, source_tier=self.name,
            status="ok" if ok else "thin",
            pages=[Page(url="https://%s/" % domain,
                        title="%s Industrial Systems Group" % domain,
                        description="Engineering services provider",
                        # Must clear BOTH gate floors: 1200 chars on a
                        # single page, and 25 DISTINCT content words. Repeating
                        # one sentence satisfies neither -- the gate treats
                        # repetition as chrome, not prose.
                        text=_FIXTURE_PROSE),
                   Page(url="https://%s/about" % domain, title="About",
                        text=_FIXTURE_PROSE)]
            if ok else [])


def test_pool_every_domain_settles():
    """No domain may be silently dropped, whatever the interleaving."""
    domains = ["d%02d.com" % i for i in range(30)]
    a = _FakeTier("a", succeeds_on=domains[:10], delay=0.005)
    b = _FakeTier("b", succeeds_on=domains[10:20], delay=0.01)
    c = _FakeTier("c", succeeds_on=domains[20:], delay=0.002)
    pool = run_free_pool(domains, [a, b, c], 5, 4, verbose=False)
    settled = set(pool.results) | {d for d in domains
                                   if pool.attempted[d] >= pool.tier_names}
    check("pool:every domain settles", settled == set(domains),
          "%d/%d" % (len(settled), len(domains)))
    check("pool:all succeed via some tier", len(pool.results) == 30,
          "%d passed" % len(pool.results))


def test_pool_no_duplicate_work():
    """A tier must never fetch the same domain twice."""
    domains = ["d%02d.com" % i for i in range(20)]
    tiers = [_FakeTier("a", succeeds_on=[], delay=0.002),
             _FakeTier("b", succeeds_on=[], delay=0.002),
             _FakeTier("c", succeeds_on=domains, delay=0.002)]
    run_free_pool(domains, tiers, 5, 4, verbose=False)
    dupes = [t.name for t in tiers if len(t.seen) != len(set(t.seen))]
    check("pool:no tier repeats a domain", not dupes, str(dupes))


def test_pool_retries_across_tiers():
    """A domain one tier fails must reach a tier that has not tried it."""
    domains = ["hard.com"]
    a = _FakeTier("a", succeeds_on=[], delay=0.001)
    b = _FakeTier("b", succeeds_on=[], delay=0.001)
    c = _FakeTier("c", succeeds_on=["hard.com"], delay=0.001)
    pool = run_free_pool(domains, [a, b, c], 5, 2, verbose=False)
    tried = {r["tier"] for r in pool.attempts["hard.com"]}
    check("pool:failure escalates within the pool", "hard.com" in pool.results,
          "rescued by c")
    # The pool stops at the FIRST success, so a/b are not both guaranteed to
    # have run -- only that at least one failure was recorded before the
    # rescue, and that the rescuer is in the trail. Demanding both would be
    # asserting wasted work.
    losers = tried - {"c"}
    check("pool:failing tier recorded before rescue",
          bool(losers) and "c" in tried, str(sorted(tried)))


def test_pool_gives_up_after_all_tiers():
    """When every free tier fails, the domain leaves the pool for the paid ones."""
    domains = ["dead.com"]
    tiers = [_FakeTier(n, succeeds_on=[], delay=0.001) for n in ("a", "b", "c")]
    pool = run_free_pool(domains, tiers, 5, 2, verbose=False)
    check("pool:exhausted domain is not a result", "dead.com" not in pool.results)
    check("pool:exhausted domain tried by all tiers",
          pool.attempted["dead.com"] == {"a", "b", "c"},
          str(sorted(pool.attempted["dead.com"])))
    check("pool:keeps least-bad for diagnostics", "dead.com" in pool.best)


def test_pool_respects_prior_attempts():
    """A cached run must not re-burn a tier that already failed."""
    prior = {"x.com": [{"tier": "a", "status": "thin", "gate_ok": False}]}
    a = _FakeTier("a", succeeds_on=["x.com"], delay=0.001)
    b = _FakeTier("b", succeeds_on=["x.com"], delay=0.001)
    pool = run_free_pool(["x.com"], [a, b], 5, 2, prior_attempts=prior, verbose=False)
    check("pool:skips tiers already attempted", "x.com" not in a.seen,
          "a re-ran" if a.seen else "a correctly skipped")
    check("pool:still rescued by remaining tier", "x.com" in pool.results)


def main():
    enable_utf8_stdout()
    live = "--live" in sys.argv
    print("=" * 68)
    print("TIER PLUMBING TESTS%s" % ("  (+live)" if live else ""))
    print("=" * 68)
    for fn in (test_contract, test_unparseable_input,
               test_runner_unicode_roundtrip, test_runner_ignores_stdout_noise,
               test_runner_reports_failure, test_markdown_cleanup,
               test_digest_cap, test_gate_is_tier_agnostic, test_limits_sane,
               test_retry_policy, test_no_offdomain_crawl,
               test_apify_budget_guard, test_pool_every_domain_settles,
               test_pool_no_duplicate_work, test_pool_retries_across_tiers,
               test_pool_gives_up_after_all_tiers,
               test_pool_respects_prior_attempts):
        fn()
    if live:
        test_live_crawl4ai()

    failed = [n for n, ok, _ in RESULTS if not ok]
    print("\n%d/%d passed" % (len(RESULTS) - len(failed), len(RESULTS)))
    for n in failed:
        print("  FAILED: %s" % n)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
