"""Apply the disqualifiers that a CSV column can answer, before any fetch.

Every row this catches is a domain never crawled and a paid tier never reached,
so this runs before crawl_sites.py. Verdicts are written at ROW level, because
a geography or headcount rule is a property of the row, not of the domain --
the same company can appear twice with different data.

  python scripts/prefilter.py --campaign acme            # dry run
  python scripts/prefilter.py --campaign acme --apply
"""

import argparse
import csv
import json
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fetchers.base import enable_utf8_stdout, jsonl_append, jsonl_read, normalize_domain  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def campaign_dir(slug):
    return os.path.join(ROOT, "campaigns", slug)


def to_number(value):
    m = re.search(r"-?\d[\d,]*\.?\d*", str(value or ""))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def rule_hits(rule, value):
    """Does this row value trip the rule? Returns a reason or empty string."""
    raw = str(value or "").strip()
    low = raw.lower()

    rejects = [str(v).strip().lower() for v in (rule.get("reject_values") or [])]
    if rejects and low in rejects:
        return "%s = %s" % (rule.get("column"), raw)

    accepts = [str(v).strip().lower() for v in (rule.get("accept_values") or [])]
    if accepts and low and low not in accepts:
        return "%s = %s (not in target list)" % (rule.get("column"), raw)

    for frag in (rule.get("reject_contains") or []):
        if str(frag).strip().lower() in low:
            return "%s contains %r" % (rule.get("column"), frag)

    num = to_number(raw)
    if num is not None:
        if rule.get("min") is not None and num < float(rule["min"]):
            return "%s = %s (below %s)" % (rule.get("column"), raw, rule["min"])
        if rule.get("max") is not None and num > float(rule["max"]):
            return "%s = %s (above %s)" % (rule.get("column"), raw, rule["max"])
    return ""


def main():
    enable_utf8_stdout()
    ap = argparse.ArgumentParser()
    ap.add_argument("--campaign", required=True)
    ap.add_argument("--apply", action="store_true",
                    help="write verdicts; without it this is a dry run")
    args = ap.parse_args()

    cdir = campaign_dir(args.campaign)
    cfg_path = os.path.join(cdir, "campaign.json")
    rubric_path = os.path.join(cdir, "rubric.json")
    for p in (cfg_path, rubric_path):
        if not os.path.exists(p):
            sys.exit("missing " + p)

    cfg = json.load(open(cfg_path, encoding="utf-8"))
    rubric = json.load(open(rubric_path, encoding="utf-8"))
    csv_path, url_col = cfg.get("input_csv"), cfg.get("url_column")
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))

    rules = [d for d in (rubric.get("disqualifiers") or [])
             if d.get("checkable_from") == "csv" and d.get("column")]
    if not rules:
        print("no csv-checkable disqualifiers in rubric.json -- nothing to pre-filter")
        return

    print("pre-filter rules:")
    for r in rules:
        print("  %-18s %s" % (r.get("id", "?"), r.get("rule", "")))

    missing = [r["column"] for r in rules if rows and r["column"] not in rows[0]]
    if missing:
        sys.exit("rubric names columns not in the CSV: %s\nCSV columns: %s"
                 % (", ".join(missing), ", ".join(rows[0].keys())))

    already = {a.get("row") for a in jsonl_read(os.path.join(cdir, "assessments.jsonl"))
               if a.get("row") is not None}

    hits, reasons = [], Counter()
    for i, row in enumerate(rows):
        if i in already:
            continue
        for rule in rules:
            why = rule_hits(rule, row.get(rule["column"]))
            if why:
                hits.append((i, rule, why, normalize_domain(row.get(url_col) or "")))
                reasons[rule.get("id", "?")] += 1
                break

    print("\n%d of %d rows disqualified before any fetch" % (len(hits), len(rows)))
    for rid, n in reasons.most_common():
        print("  %-18s %4d" % (rid, n))
    for i, rule, why, _ in hits[:10]:
        print("    row %-5d %s" % (i + 2, why))
    if len(hits) > 10:
        print("    ... and %d more" % (len(hits) - 10))

    if not args.apply:
        print("\nDry run. Re-run with --apply to write these verdicts.")
        return

    out = os.path.join(cdir, "assessments.jsonl")
    for i, rule, why, domain in hits:
        jsonl_append(out, {
            "row": i,
            "domain": domain,
            "fitment": "Unfit",
            "ranking": 3,
            "comments": (rule.get("comment") or why)[:90],
            "evidence": "pre-filter rule %s: %s" % (rule.get("id"), why),
            "crawl_status": "not_crawled",
            "source_tier": "prefilter",
            "judge": "rule",
        })
    print("\nwrote %d verdicts to %s" % (len(hits), out))
    print("These rows are excluded from the crawl -- run crawl_sites.py next.")


if __name__ == "__main__":
    main()
