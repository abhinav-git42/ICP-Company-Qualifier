"""Read-only report on a lead CSV. Modifies nothing.

Answers the questions that have to be settled before a crawl: which column
holds the website, how healthy the URLs are, how many rows are duplicates, and
which columns can carry a disqualifier that never costs a fetch.

  python scripts/inspect_csv.py --campaign acme
  python scripts/inspect_csv.py --file "C:/path/to/leads.csv"
"""

import argparse
import csv
import os
import re
import sys
from collections import Counter, OrderedDict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fetchers.base import enable_utf8_stdout, normalize_domain  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

HEADER_HINTS = [
    (re.compile(r"^(website|web ?site|url|domain|company ?(website|url|domain|site))$", re.I), 50),
    (re.compile(r"(website|domain)", re.I), 30),
    (re.compile(r"\burl\b", re.I), 25),
    (re.compile(r"(site|web)", re.I), 10),
]

# Columns whose values are worth checking a disqualifier against, pre-crawl.
DQ_HINTS = re.compile(
    r"(country|region|geo|location|state|city|industry|sector|vertical|"
    r"employee|headcount|size|staff|revenue|funding|type|category|seniority|"
    r"title|role|department|technolog|stack)", re.I)


def sniff(path):
    with open(path, "rb") as fh:
        head = fh.read(4)
    encoding = "utf-8-sig" if head.startswith(b"\xef\xbb\xbf") else "utf-8"
    with open(path, "r", encoding=encoding, newline="", errors="replace") as fh:
        sample = fh.read(64 * 1024)
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        delim = dialect.delimiter
    except csv.Error:
        dialect, delim = csv.excel, ","
    return encoding, dialect, delim


def read_rows(path):
    encoding, dialect, delim = sniff(path)
    with open(path, "r", encoding=encoding, newline="", errors="replace") as fh:
        rows = list(csv.DictReader(fh, dialect=dialect))
    return rows, encoding, delim


def score_column(name, values):
    """Header hint plus value shape. Value shape wins: a column whose cells
    look like hosts beats one that merely has a promising name."""
    score = 0
    for pattern, weight in HEADER_HINTS:
        if pattern.search(name or ""):
            score += weight
            break
    sample = [v for v in values if str(v).strip()][:200]
    if not sample:
        return score, 0.0
    hits = sum(1 for v in sample if normalize_domain(v))
    ratio = hits / len(sample)
    return score + int(ratio * 60), ratio


def main():
    enable_utf8_stdout()
    ap = argparse.ArgumentParser()
    ap.add_argument("--campaign", default="")
    ap.add_argument("--file", default="")
    ap.add_argument("--samples", type=int, default=3)
    args = ap.parse_args()

    path = args.file
    if not path and args.campaign:
        d = os.path.join(ROOT, "campaigns", args.campaign, "input")
        files = sorted(f for f in os.listdir(d) if f.lower().endswith(".csv")) \
            if os.path.isdir(d) else []
        if not files:
            sys.exit("no CSV under " + d)
        path = os.path.join(d, files[0])
    if not path or not os.path.exists(path):
        sys.exit("need --file or --campaign with a CSV in its input/ folder")

    rows, encoding, delim = read_rows(path)
    if not rows:
        sys.exit("CSV has a header but no data rows")
    columns = list(rows[0].keys())

    print("=" * 70)
    print("CSV INSPECTION  --  %s" % os.path.basename(path))
    print("=" * 70)
    print("  path      : %s" % path)
    print("  rows      : %d" % len(rows))
    print("  columns   : %d" % len(columns))
    print("  encoding  : %s" % encoding)
    print("  delimiter : %r" % delim)

    scores = OrderedDict()
    print("\n  COLUMNS")
    for col in columns:
        values = [r.get(col) or "" for r in rows]
        score, ratio = score_column(col, values)
        scores[col] = (score, ratio)
        samples = [str(v).strip() for v in values if str(v).strip()][:args.samples]
        filled = sum(1 for v in values if str(v).strip())
        print("    %-28s %3d%% filled  e.g. %s" % (
            (col or "(unnamed)")[:28], 100 * filled // max(1, len(rows)),
            " | ".join(s[:28] for s in samples) or "(all blank)"))

    best = max(scores.items(), key=lambda kv: kv[1][0])
    print("\n  WEBSITE COLUMN (best guess)")
    ranked = sorted(scores.items(), key=lambda kv: -kv[1][0])[:3]
    for col, (score, ratio) in ranked:
        mark = "->" if col == best[0] else "  "
        print("    %s %-28s score=%-4d %.0f%% of values parse as a host"
              % (mark, col[:28], score, 100 * ratio))
    url_col = best[0]

    values = [r.get(url_col) or "" for r in rows]
    domains, blank, malformed = [], 0, []
    for i, v in enumerate(values):
        if not str(v).strip():
            blank += 1
            continue
        d = normalize_domain(v)
        if d:
            domains.append(d)
        else:
            malformed.append((i + 2, str(v)[:50]))   # +2 = 1-indexed with header

    counts = Counter(domains)
    dupes = [(d, n) for d, n in counts.most_common() if n > 1]

    print("\n  URL HEALTH  (column: %s)" % url_col)
    print("    usable domains  : %d (%d unique)" % (len(domains), len(counts)))
    print("    blank cells     : %d" % blank)
    print("    unparseable     : %d" % len(malformed))
    for line, val in malformed[:8]:
        print("        row %-5d %r" % (line, val))
    if len(malformed) > 8:
        print("        ... and %d more" % (len(malformed) - 8))
    print("    duplicate domains: %d domains covering %d rows"
          % (len(dupes), sum(n for _, n in dupes)))
    for d, n in dupes[:8]:
        print("        %-40s x%d" % (d, n))
    print("    (duplicates are crawled once and share a verdict -- no rows are dropped)")

    print("\n  COLUMNS USABLE FOR PRE-CRAWL DISQUALIFIERS")
    found = False
    for col in columns:
        if not DQ_HINTS.search(col or ""):
            continue
        vals = [str(r.get(col) or "").strip() for r in rows]
        vals = [v for v in vals if v]
        if not vals:
            continue
        uniq = Counter(vals)
        found = True
        top = " | ".join("%s (%d)" % (v[:22], n) for v, n in uniq.most_common(4))
        print("    %-28s %3d distinct   %s" % (col[:28], len(uniq), top))
    if not found:
        print("    none detected -- every disqualifier will need a crawl")

    print("\n  NEXT")
    print('    python scripts/crawl_sites.py --campaign <slug> --url-column "%s"' % url_col)
    print("=" * 70)


if __name__ == "__main__":
    main()
