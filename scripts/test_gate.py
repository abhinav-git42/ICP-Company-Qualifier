"""Deterministic tests for the quality gate.

Parked pages and JS stubs are the cases that matter most and the ones you
cannot reliably source from live domains -- a domain that is parked today may
not be next month. So they are asserted here against synthetic content.

Run: python scripts/test_gate.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fetchers.base import enable_utf8_stdout, Page, SiteResult, build_digest, quality_gate  # noqa: E402

REAL = (
    "We build custom logistics software for mid-market freight brokers. Our "
    "platform handles load matching, carrier onboarding, settlement and "
    "compliance reporting across North America. Founded in 2014, we serve "
    "over two hundred brokerages and integrate with major transport management "
    "systems. Our engineering team specialises in EDI translation, rate "
    "benchmarking and predictive capacity planning for regional carriers."
)

CASES = [
    ("real_multipage", SiteResult(
        domain="acme.com", status="ok",
        pages=[Page(url="https://acme.com/", title="Acme Logistics Software",
                    description="Freight broker platform", headings=["Load matching"],
                    text=REAL),
               Page(url="https://acme.com/about", title="About Acme",
                    text="Founded 2014 in Chicago by supply chain engineers.")]),
     True, "pass"),

    ("parked_for_sale", SiteResult(
        domain="acme.com", status="ok",
        pages=[Page(url="https://acme.com/", title="acme.com",
                    text="This domain is for sale. Buy this domain today. "
                         "Inquire now to purchase acme.com from our brokerage "
                         "team and start using it for your own business site.")]),
     False, "parked"),

    ("js_stub", SiteResult(
        domain="acme.com", status="ok",
        pages=[Page(url="https://acme.com/", title="Acme",
                    text="Please enable JavaScript to run this app. This "
                         "application requires JavaScript to be enabled in your "
                         "browser settings before the content can be displayed.")]),
     False, "stub"),

    ("cloudflare_wall", SiteResult(
        domain="acme.com", status="ok",
        pages=[Page(url="https://acme.com/", title="Attention Required",
                    text="Checking your browser before accessing acme.com. "
                         "Please verify you are human to continue browsing this "
                         "website. Ray ID shown below for reference purposes.")]),
     False, "stub"),

    ("too_thin", SiteResult(
        domain="acme.com", status="ok",
        pages=[Page(url="https://acme.com/", title="Acme Corporation Limited",
                    text="Acme Corporation. Contact us.")]),
     False, "thin"),

    ("title_is_just_domain", SiteResult(
        domain="acme.com", status="ok",
        pages=[Page(url="https://acme.com/", title="acme", text=REAL)]),
     False, "no_real_title"),

    ("single_page_under_home_floor", SiteResult(
        domain="acme.com", status="ok",
        pages=[Page(url="https://acme.com/", title="Acme Logistics Software",
                    description="Freight platform", text=REAL)]),
     False, "single_thin_page"),

    ("nav_chrome_only", SiteResult(
        domain="acme.com", status="ok",
        pages=[Page(url="https://acme.com/", title="Acme Corporation Home",
                    description="Welcome to Acme",
                    text=" ".join(["Home About Contact Services Products Blog News "
                                   "Login Privacy Policy Terms Cookie Search Menu"] * 12)),
            Page(url="https://acme.com/about", title="About", text="Home About Contact")]),
     False, "not_prose"),

    ("dead_domain", SiteResult(domain="acme.com", status="dns_fail"),
     False, "status:dns_fail"),

    ("blocked_403", SiteResult(domain="acme.com", status="blocked"),
     False, "status:blocked"),

    ("parked_but_long_real_page", SiteResult(
        domain="acme.com", status="ok",
        pages=[Page(url="https://acme.com/", title="Acme Logistics Software",
                    description="Freight platform",
                    # mentions "coming soon" but is a genuine site -- must NOT trip
                    text=REAL * 5 + " Our new carrier portal is coming soon."),
               Page(url="https://acme.com/about", title="About", text=REAL)]),
     True, "pass"),
]


def main():
    enable_utf8_stdout()
    failures = 0
    for name, result, want_ok, want_reason in CASES:
        ok, reason = quality_gate(result)
        good = (ok == want_ok) and reason.startswith(want_reason)
        if not good:
            failures += 1
        print("%-4s %-32s ok=%-5s reason=%-24s (wanted ok=%s ~%s)" % (
            "PASS" if good else "FAIL", name, ok, reason, want_ok, want_reason))

    # Digest must never exceed the cap, whatever it is fed.
    big = SiteResult(domain="acme.com", status="ok",
                     pages=[Page(url="https://acme.com/", title="T" * 500,
                                 description="D" * 900, headings=["H" * 300] * 30,
                                 text=REAL * 200)] +
                          [Page(url="https://acme.com/p%d" % i, title="P",
                                text=REAL * 50) for i in range(8)])
    d = build_digest(big)
    ok = len(d) <= 2500
    failures += 0 if ok else 1
    print("%-4s %-32s len=%d (cap 2500)" % ("PASS" if ok else "FAIL",
                                            "digest_respects_cap", len(d)))

    print("\n%d/%d passed" % (len(CASES) + 1 - failures, len(CASES) + 1))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
