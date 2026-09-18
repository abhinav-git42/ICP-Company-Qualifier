"""Compare TypeSafe's decisions with Claude's verdicts on the same domains.

Use it before trusting TypeSafe on a live campaign: take a campaign Claude has
already judged, let TypeSafe decide the same domains alongside it, and see
where they differ. Read-only; nothing here changes a verdict.

  python scripts/typesafe_judge.py --campaign acme --all    # decide next to Claude
  python scripts/compare_judges.py --campaign acme
  python scripts/compare_judges.py --campaign acme --show 30
"""

import argparse
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fetchers.base import enable_utf8_stdout, jsonl_read  # noqa: E402
from merge_results import judge_of  # noqa: E402
from typesafe_judge import DECISIONS, GRADES, campaign_dir, latest_by_domain  # noqa: E402


def matrix(pairs, labels, title):
    """Rows are Claude, columns are TypeSafe."""
    grid = Counter(pairs)
    agree = sum(grid[(k, k)] for k in labels)
    total = max(1, len(pairs))
    print("\n  %s  --  agree on %d of %d (%.0f%%)" % (title, agree, len(pairs),
                                                    100.0 * agree / total))
    print("    %-14s" % "Claude \\ TS" + "".join("%8s" % k for k in labels))
    for row in labels:
        print("    %-14s" % row + "".join("%8d" % grid[(row, col)] for col in labels))


def main():
    enable_utf8_stdout()
    ap = argparse.ArgumentParser()
    ap.add_argument("--campaign", required=True)
    ap.add_argument("--show", type=int, default=15, help="disagreements to list")
    args = ap.parse_args()

    cdir = campaign_dir(args.campaign)
    decisions = latest_by_domain(jsonl_read(os.path.join(cdir, DECISIONS)))
    claude = latest_by_domain([a for a in jsonl_read(os.path.join(cdir, "assessments.jsonl"))
                               if a.get("row") is None and judge_of(a) == "claude"])
    both = sorted(set(decisions) & set(claude))
    if not both:
        sys.exit("No domain has both a Claude verdict and a TypeSafe decision.\n"
                 "Run: python scripts/typesafe_judge.py --campaign %s --all"
                 % args.campaign)

    print("=" * 68)
    print("CLAUDE vs TYPESAFE  --  %d domains judged by both" % len(both))
    print("=" * 68)
    fit_pairs = [(str(claude[d].get("fitment")).title(), decisions[d]["fitment"])
                 for d in both]
    rank_pairs = [(str(claude[d].get("ranking")), str(decisions[d]["ranking"]))
                  for d in both]
    matrix(fit_pairs, list(GRADES), "FITMENT")
    matrix(rank_pairs, ["1", "2", "3"], "RANKING")

    # Do TypeSafe's probabilities order leads the way Claude's grades do? Clear
    # separation here means the probability column is usable for ranking.
    print("\n  Average TypeSafe P(Good), grouped by Claude's grade")
    for g in GRADES:
        ps = [decisions[d]["fitment_probabilities"].get("Good", 0.0)
              for d in both if str(claude[d].get("fitment")).title() == g]
        if ps:
            print("    Claude %-6s %5d leads   P(Good) %.2f" % (g, len(ps), sum(ps) / len(ps)))

    flagged = [d for d in both if decisions[d].get("needs_review")]
    flagged_off = [d for d in flagged
                   if str(claude[d].get("fitment")).title() != decisions[d]["fitment"]]
    print("\n  TypeSafe flagged %d for review; %d of those disagree with Claude"
          % (len(flagged), len(flagged_off)))

    diffs = [d for d in both
             if str(claude[d].get("fitment")).title() != decisions[d]["fitment"]
             or str(claude[d].get("ranking")) != str(decisions[d]["ranking"])]
    if diffs:
        print("\n  DISAGREEMENTS (first %d of %d)" % (min(args.show, len(diffs)), len(diffs)))
        for d in diffs[:args.show]:
            c, t = claude[d], decisions[d]
            print("\n    %s" % d)
            print("      Claude   %s/%s  %s" % (c.get("fitment"), c.get("ranking"),
                                              str(c.get("comments") or "")[:70]))
            print("      TypeSafe %s/%s  %s" % (t["fitment"], t["ranking"], t.get("why", "")[:70]))
    print("=" * 68)


if __name__ == "__main__":
    main()
