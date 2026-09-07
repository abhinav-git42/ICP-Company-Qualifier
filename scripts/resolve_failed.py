"""Write verdicts for domains that never produced usable content.

This is deliberately a script and not a judgment call. "Never infer fit from a
failed crawl" is the rule most likely to erode over 40 batches, so it is made
mechanical: every gate-failed domain gets Unfit / 3 and a comment naming the
data problem rather than implying anything about the company.

  python scripts/resolve_failed.py --campaign acme            # dry run
  python scripts/resolve_failed.py --campaign acme --apply
"""

import argparse
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fetchers.base import enable_utf8_stdout, jsonl_append, jsonl_read  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# gate_reason prefix -> comment. Each says what went wrong with the DATA, never
# anything about the business, because we learned nothing about the business.
REASONS = [
    ("status:dns_fail", "Domain does not resolve - not verified"),
    ("status:timeout", "Site timed out - not verified"),
    ("status:blocked", "Site blocks automated access - not verified"),
    ("status:http_4xx", "Site returned an error - not verified"),
    ("status:http_5xx", "Site server error - not verified"),
    ("status:conn_fail", "Site unreachable - not verified"),
    ("status:no_url", "No website provided - not verified"),
    ("parked:", "Parked or for-sale domain - no live business site"),
    ("stub:", "Site blocks automated access - not verified"),
    ("thin:", "Site has too little content - not verified"),
    ("single_thin_page", "Site has too little content - not verified"),
    ("not_prose", "Site has no readable description - not verified"),
    ("no_real_title", "Site has no readable description - not verified"),
    ("no_pages", "Site unreachable - not verified"),
]


def comment_for(reason):
    for prefix, text in REASONS:
        if (reason or "").startswith(prefix):
            return text
    return "Site unreachable - not verified"


def main():
    enable_utf8_stdout()
    ap = argparse.ArgumentParser()
    ap.add_argument("--campaign", required=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    cdir = os.path.join(ROOT, "campaigns", args.campaign)
    results = jsonl_read(os.path.join(cdir, "crawl_results.jsonl"))
    assessed = jsonl_read(os.path.join(cdir, "assessments.jsonl"))
    done = {a.get("domain") for a in assessed if a.get("domain")}

    failed = [r for r in results
              if not r.get("gate_ok") and r.get("domain") not in done]
    if not failed:
        print("nothing to resolve -- every gate-failed domain already has a verdict")
        return

    tally = Counter(comment_for(r.get("gate_reason", "")) for r in failed)
    print("%d unverifiable domains:" % len(failed))
    for text, n in tally.most_common():
        print("  %4d  %s" % (n, text))

    if not args.apply:
        print("\nDry run. Re-run with --apply to write these verdicts.")
        return

    out = os.path.join(cdir, "assessments.jsonl")
    for r in failed:
        jsonl_append(out, {
            "domain": r.get("domain"),
            "fitment": "Unfit",
            "ranking": 3,
            "comments": comment_for(r.get("gate_reason", "")),
            "evidence": "gate=%s tier=%s" % (r.get("gate_reason"), r.get("source_tier")),
            "crawl_status": r.get("status"),
            "source_tier": r.get("source_tier") or "none",
        })
    print("\nwrote %d verdicts to %s" % (len(failed), out))


if __name__ == "__main__":
    main()
