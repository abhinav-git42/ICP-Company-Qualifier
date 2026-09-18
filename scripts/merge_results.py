"""Append the verdict columns to the original CSV.

Every original column and every original row survives, in the original order.
This file goes straight back into a sending tool, so that is non-negotiable.

  python scripts/merge_results.py --campaign acme
  python scripts/merge_results.py --campaign acme --no-status
"""

import argparse
import csv
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fetchers.base import enable_utf8_stdout, jsonl_read, normalize_domain  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

NEW_COLUMNS = ["Fitment", "Ranking", "Comments", "Crawl_Status"]
VALID_FITMENT = {"Good", "Avg", "Unfit"}


def campaign_dir(slug):
    return os.path.join(ROOT, "campaigns", slug)


def judge_of(a):
    """Who produced a verdict. Lines written before the judge field existed
    were Claude's, except pre-filter rows."""
    return a.get("judge") or ("rule" if a.get("source_tier") == "prefilter" else "claude")


def unique_header(name, existing):
    """Never silently clobber a column the source file already had."""
    if name not in existing:
        return name
    n = 2
    while "%s_%d" % (name, n) in existing:
        n += 1
    return "%s_%d" % (name, n)


def main():
    enable_utf8_stdout()
    ap = argparse.ArgumentParser()
    ap.add_argument("--campaign", required=True)
    ap.add_argument("--no-status", action="store_true",
                    help="omit Crawl_Status if the destination tool is strict")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    cdir = campaign_dir(args.campaign)
    cfg_path = os.path.join(cdir, "campaign.json")
    if not os.path.exists(cfg_path):
        sys.exit("missing " + cfg_path + " -- run crawl_sites.py first")
    cfg = json.load(open(cfg_path, encoding="utf-8"))
    csv_path, url_col = cfg.get("input_csv"), cfg.get("url_column")

    with open(csv_path, "r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        original_fields = list(reader.fieldnames or [])
        rows = list(reader)

    assessments = jsonl_read(os.path.join(cdir, "assessments.jsonl"))
    crawl = {r.get("domain"): r for r in
             jsonl_read(os.path.join(cdir, "crawl_results.jsonl"))}

    # Row-level verdicts (pre-filter) beat domain-level ones, because a row rule
    # can disqualify one row of a duplicated domain but not the other.
    by_domain, by_row = {}, {}
    for a in assessments:
        if a.get("row") is not None:
            by_row[int(a["row"])] = a
        elif a.get("domain"):
            by_domain[a["domain"]] = a

    cols = NEW_COLUMNS[:-1] if args.no_status else NEW_COLUMNS
    header_map = {}
    fields = list(original_fields)
    for c in cols:
        actual = unique_header(c, fields)
        header_map[c] = actual
        fields.append(actual)

    out_path = args.out or os.path.join(
        cdir, "output",
        os.path.splitext(os.path.basename(csv_path))[0] + "_qualified.csv")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    stats = Counter()
    rank_stats = Counter()
    tier_stats = Counter()
    judge_stats = Counter()
    dq_reasons = Counter()
    flagged = 0
    disagreed = 0
    divergent = 0
    unassessed = 0
    bad_values = []

    # utf-8-sig so Excel does not mangle non-ASCII company names.
    with open(out_path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for i, row in enumerate(rows):
            domain = normalize_domain(row.get(url_col) or "")
            a = by_row.get(i) or by_domain.get(domain)

            if a is None:
                # A blank here would read as "fine". Silent gaps are the failure
                # mode that matters, so unassessed rows are explicit.
                a = {"fitment": "Unfit", "ranking": 3, "comments": "Not assessed",
                     "crawl_status": (crawl.get(domain) or {}).get("status") or "not_crawled",
                     "source_tier": "none", "judge": "none"}
                unassessed += 1

            fitment = str(a.get("fitment") or "Unfit").strip().title()
            if fitment not in VALID_FITMENT:
                bad_values.append((i + 2, "Fitment", a.get("fitment")))
                fitment = "Unfit"
            try:
                ranking = int(a.get("ranking") or 3)
            except (TypeError, ValueError):
                ranking = 3
            if ranking not in (1, 2, 3):
                bad_values.append((i + 2, "Ranking", a.get("ranking")))
                ranking = 3

            comments = str(a.get("comments") or "").strip()[:120]
            status = a.get("crawl_status") or (crawl.get(domain) or {}).get("status") or ""

            out = dict(row)
            out[header_map["Fitment"]] = fitment
            out[header_map["Ranking"]] = ranking
            out[header_map["Comments"]] = comments
            if not args.no_status:
                out[header_map["Crawl_Status"]] = status
            writer.writerow(out)

            stats[fitment] += 1
            rank_stats[ranking] += 1
            tier_stats[a.get("source_tier") or "none"] += 1
            judge_stats[judge_of(a)] += 1
            flagged += 1 if a.get("needs_review") else 0
            disagreed += 1 if a.get("disagree") else 0
            if (fitment == "Good" and ranking != 1) or (fitment == "Unfit" and ranking != 3):
                divergent += 1
            if a.get("source_tier") == "prefilter":
                dq_reasons[comments] += 1

    total = len(rows)
    print("=" * 68)
    print("MERGE COMPLETE  --  %d rows" % total)
    print("=" * 68)
    print("  output   : %s" % out_path)
    print("  columns  : %d original + %d appended" % (len(original_fields), len(cols)))
    print("\n  FITMENT")
    for k in ("Good", "Avg", "Unfit"):
        print("    %-6s %5d  (%.0f%%)" % (k, stats[k], 100.0 * stats[k] / max(1, total)))
    print("\n  RANKING (priority wave)")
    for k in (1, 2, 3):
        print("    %-6d %5d  (%.0f%%)" % (k, rank_stats[k],
                                          100.0 * rank_stats[k] / max(1, total)))
    print("\n  rows where Ranking diverges from Fitment: %d" % divergent)
    print("\n  EVIDENCE SOURCE")
    for tier, n in tier_stats.most_common():
        print("    %-12s %5d" % (tier, n))
    print("\n  VERDICT FROM")
    for judge, n in judge_stats.most_common():
        print("    %-12s %5d" % (judge, n))
    if judge_stats.get("typesafe"):
        print("    TypeSafe verdicts flagged for review : %d" % flagged)
        print("    comments disagreeing with TypeSafe   : %d" % disagreed)
    if dq_reasons:
        print("\n  PRE-FILTER DISQUALIFICATIONS")
        for reason, n in dq_reasons.most_common(8):
            print("    %5d  %s" % (n, reason[:60]))
    if unassessed:
        print("\n  WARNING: %d rows had no verdict and were defaulted to Unfit/3."
              % unassessed)
        print("           Run next_batch.py / resolve_failed.py to close the gap.")
    if bad_values:
        print("\n  WARNING: %d malformed verdict values were coerced:" % len(bad_values))
        for line, field, val in bad_values[:5]:
            print("      row %-5d %s=%r" % (line, field, val))
    print("=" * 68)


if __name__ == "__main__":
    main()
