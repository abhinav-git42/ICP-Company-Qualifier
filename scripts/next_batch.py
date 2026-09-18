"""Print the next digests that have not been assessed yet.

Progress lives in assessments.jsonl rather than in the model's context, so a
long run resumes cleanly across sessions -- which matters at ~40 batches for a
1000-lead list.

  python scripts/next_batch.py --campaign acme --size 25
  python scripts/next_batch.py --campaign acme --status      # just the counts
  python scripts/next_batch.py --campaign acme --comments    # TypeSafe runs: verdict
                                                             # is fixed, write the comment
"""

import argparse
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fetchers.base import enable_utf8_stdout, jsonl_read, normalize_domain  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Row fields worth showing alongside the crawl text. Company name and country
# are real evidence; the rest of the CSV is noise at judgment time.
CONTEXT_HINTS = ("company", "name", "country", "region", "industry", "employee",
                 "headcount", "size", "revenue", "title", "seniority")


def campaign_dir(slug):
    return os.path.join(ROOT, "campaigns", slug)


def read_rows(path):
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def load_context(slug):
    """domain -> a short line of CSV context, plus row indexes."""
    cfg_path = os.path.join(campaign_dir(slug), "campaign.json")
    if not os.path.exists(cfg_path):
        return {}, {}
    with open(cfg_path, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    csv_path, url_col = cfg.get("input_csv"), cfg.get("url_column")
    if not csv_path or not url_col or not os.path.exists(csv_path):
        return {}, {}
    rows = read_rows(csv_path)
    cols = [c for c in (rows[0].keys() if rows else [])
            if any(h in (c or "").lower() for h in CONTEXT_HINTS)]
    ctx, idx = {}, {}
    for i, row in enumerate(rows):
        d = normalize_domain(row.get(url_col) or "")
        if not d:
            continue
        idx.setdefault(d, []).append(i)
        if d in ctx:
            continue
        bits = ["%s=%s" % (c, (row.get(c) or "").strip()[:40])
                for c in cols if (row.get(c) or "").strip()]
        ctx[d] = " | ".join(bits)
    return ctx, idx


def comment_batch(args, cdir, results, done):
    """TypeSafe runs: the verdict is already decided. Show it beside the digest
    so the comment explains it, and never ask for a verdict."""
    from typesafe_judge import COMMENTS, DECISIONS, latest_by_domain

    decisions = latest_by_domain(jsonl_read(os.path.join(cdir, DECISIONS)))
    commented = latest_by_domain(jsonl_read(os.path.join(cdir, COMMENTS)))
    digests = {r.get("domain"): r for r in results}
    pending = [d for d in decisions if d not in commented and d not in done]

    print("campaign %s  --  %d decided by TypeSafe | %d commented | %d pending comment"
          % (args.campaign, len(decisions), len(set(decisions) & set(commented)),
             len(pending)))
    if args.status:
        return
    batch = pending[:args.size]
    if not batch:
        print("\nNothing pending. Next: python scripts/typesafe_judge.py --campaign %s "
              "--finalize" % args.campaign)
        return

    ctx, _ = load_context(args.campaign)
    print("\n" + "#" * 70)
    print("# COMMENT BATCH OF %d  --  one JSON line each to comments.jsonl" % len(batch))
    print("# The verdict is TypeSafe's. Explain it; do not change it.")
    print("#" * 70)
    for i, d in enumerate(batch, 1):
        dec, r = decisions[d], digests.get(d) or {}
        print("\n----- [%d/%d] %s  (tier=%s, %d chars) -----"
              % (i, len(batch), d, r.get("source_tier"), r.get("chars", 0)))
        print("VERDICT: %s / %s   (%s)" % (dec.get("fitment"), dec.get("ranking"),
                                          dec.get("why", "")))
        by_kind = {}
        for s in dec.get("signals_present") or []:
            by_kind.setdefault(s.get("kind"), []).append(s.get("text", ""))
        for kind in ("disqualifier", "fit", "anti-fit", "momentum"):
            if by_kind.get(kind):
                print("  %-12s %s" % (kind + ":", " | ".join(by_kind[kind])))
        if dec.get("needs_review"):
            print("  review:      " + "; ".join(dec["needs_review"]))
        if ctx.get(d):
            print("CSV: " + ctx[d])
        print(r.get("digest") or "(no content)")
    print("\n" + "#" * 70)
    print("# END BATCH -- %d remaining after this one" % max(0, len(pending) - len(batch)))
    print("#" * 70)


def main():
    enable_utf8_stdout()
    ap = argparse.ArgumentParser()
    ap.add_argument("--campaign", required=True)
    ap.add_argument("--size", type=int, default=25)
    ap.add_argument("--status", action="store_true", help="counts only")
    ap.add_argument("--include-failed", action="store_true",
                    help="also emit gate-failed domains (normally auto-resolved)")
    ap.add_argument("--comments", action="store_true",
                    help="TypeSafe runs: decided domains that still need a comment")
    args = ap.parse_args()

    cdir = campaign_dir(args.campaign)
    results = jsonl_read(os.path.join(cdir, "crawl_results.jsonl"))
    assessed = jsonl_read(os.path.join(cdir, "assessments.jsonl"))
    done = {a.get("domain") for a in assessed if a.get("domain")}

    if args.comments:
        comment_batch(args, cdir, results, done)
        return

    cfg_path = os.path.join(cdir, "campaign.json")
    if os.path.exists(cfg_path) and not args.include_failed:
        with open(cfg_path, "r", encoding="utf-8") as fh:
            if json.load(fh).get("judge") == "typesafe":
                print("This campaign's judge is TypeSafe, so Claude does not grade it.\n"
                      "Use: python scripts/next_batch.py --campaign %s --comments"
                      % args.campaign)
                return

    passed = [r for r in results if r.get("gate_ok")]
    failed = [r for r in results if not r.get("gate_ok")]
    pending = [r for r in passed if r.get("domain") not in done]
    pending_failed = [r for r in failed if r.get("domain") not in done]

    print("campaign %s  --  %d crawled | %d gate-pass | %d gate-fail | "
          "%d assessed | %d pending"
          % (args.campaign, len(results), len(passed), len(failed),
             len(done), len(pending)))

    if pending_failed and not args.include_failed:
        print("\n%d gate-failed domains still need a verdict. They are NOT rated on "
              "fit -- resolve them with:\n  python scripts/resolve_failed.py --campaign %s"
              % (len(pending_failed), args.campaign))

    if args.status:
        return

    batch = (pending_failed if args.include_failed else pending)[:args.size]
    if not batch:
        print("\nNothing pending. Next: python scripts/merge_results.py --campaign %s"
              % args.campaign)
        return

    ctx, _ = load_context(args.campaign)
    print("\n" + "#" * 70)
    print("# BATCH OF %d  --  assess every domain below, one JSON line each" % len(batch))
    print("#" * 70)
    for i, r in enumerate(batch, 1):
        d = r.get("domain", "")
        print("\n----- [%d/%d] %s  (tier=%s, status=%s, %d chars) -----"
              % (i, len(batch), d, r.get("source_tier"), r.get("status"), r.get("chars", 0)))
        if ctx.get(d):
            print("CSV: " + ctx[d])
        print(r.get("digest") or "(no content)")
    print("\n" + "#" * 70)
    print("# END BATCH -- %d remaining after this one"
          % max(0, len(pending) - len(batch)))
    print("#" * 70)


if __name__ == "__main__":
    main()
