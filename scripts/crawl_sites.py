"""Cascade driver: stdlib -> Crawl4AI -> Apify -> Firecrawl.

Free tiers are exhausted before anything bills.

Escalation is driven by the shared quality gate, not by HTTP status. A 200 OK
carrying nothing but a cookie banner escalates exactly like a timeout.

Runs TIER BY TIER in waves rather than domain by domain. Each tier gets every
domain that the previous tier could not satisfy, which lets a batching tier
(Apify) put many domains in one actor run -- measured 4.4x cheaper than one run
per domain, because each run pays ~60s of container startup regardless.

Spend is deliberately a SEPARATE command, not an interactive prompt. Phase one
runs the free tier across the whole list and reports what failed; escalating to
a paid tier requires re-invoking with --escalate --confirm-spend.

  # phase 1 -- free
  python scripts/crawl_sites.py --campaign acme --url-column Website

  # phase 2 -- paid, only after reading the phase-1 report
  python scripts/crawl_sites.py --campaign acme --escalate --confirm-spend
"""

import argparse
import csv
import json
import os
import sys
import time
from collections import Counter, OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fetchers.base import (  # noqa: E402
    LIMITS, SiteResult, apply_gate, build_digest, enable_utf8_stdout, jsonl_append,
    jsonl_read, normalize_domain,
)
from fetchers.tier1_stdlib import StdlibFetcher  # noqa: E402
from fetchers.tier2_apify import ApifyFetcher  # noqa: E402
from fetchers.tier3_firecrawl import FirecrawlFetcher  # noqa: E402
from fetchers.tier4_crawl4ai import Crawl4AIFetcher  # noqa: E402
from fetchers.tier_scrapling import ScraplingFetcher  # noqa: E402
from freepool import run_free_pool  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Free tiers first, then paid. Crawl4AI sits ahead of the paid tiers because
# measurement put it AHEAD of stdlib on content richness (5 of 6 domains, +32%
# total chars) at a cost of only local wall-clock. Anything it cannot fetch
# still falls through to Apify and Firecrawl.
# The three free tiers run CONCURRENTLY over a shared queue (see freepool.py),
# not in sequence -- order among them is therefore informational. The paid
# tiers run in strict order afterwards, and only on what the free pool could
# not satisfy.
TIER_ORDER = ["stdlib", "crawl4ai", "scrapling", "apify", "firecrawl"]

# Chosen at onboarding when the user says time IS a constraint. Puts the paid,
# faster tier ahead of the slow free ones -- so it must be an explicit choice,
# never a default, because it spends credits to buy wall-clock.
SPEED_ORDER = ["stdlib", "scrapling", "apify", "firecrawl", "crawl4ai"]
TIER_CLASSES = {
    "stdlib": StdlibFetcher,
    "crawl4ai": Crawl4AIFetcher,
    "scrapling": ScraplingFetcher,
    "apify": ApifyFetcher,
    "firecrawl": FirecrawlFetcher,
}


def campaign_dir(slug):
    return os.path.join(ROOT, "campaigns", slug)


def read_config(slug):
    path = os.path.join(campaign_dir(slug), "campaign.json")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    return {}


def write_config(slug, cfg):
    with open(os.path.join(campaign_dir(slug), "campaign.json"), "w",
              encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)


def find_input_csv(slug):
    d = os.path.join(campaign_dir(slug), "input")
    if not os.path.isdir(d):
        return ""
    files = sorted(f for f in os.listdir(d) if f.lower().endswith(".csv"))
    return os.path.join(d, files[0]) if files else ""


def read_rows(path):
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        sample = fh.read(64 * 1024)
        fh.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        return list(csv.DictReader(fh, dialect=dialect))


def cache_path(slug, domain):
    safe = domain.replace("/", "_").replace(":", "_")
    return os.path.join(campaign_dir(slug), "cache", safe + ".json")


def load_cache(slug, domain):
    path = cache_path(slug, domain)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def save_cache(slug, domain, result, attempts):
    path = cache_path(slug, domain)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"accepted": result.to_dict(), "attempts": attempts}, fh,
                  ensure_ascii=False)


def resolve_order(cfg, speed_flag):
    """Which tiers, in what order. Precedence: --speed > campaign.json > default.

    Any unknown name is dropped rather than crashing a long run, and reported.
    """
    if speed_flag:
        order, why = SPEED_ORDER, "--speed (paid tiers first)"
    elif cfg.get("tier_order"):
        order, why = list(cfg["tier_order"]), "campaign.json"
    else:
        order, why = TIER_ORDER, "default (free tiers first)"
    clean = [t for t in order if t in TIER_CLASSES]
    dropped = [t for t in order if t not in TIER_CLASSES]
    if dropped:
        print("  ignoring unknown tier(s) in %s: %s" % (why, ", ".join(dropped)))
    return (clean or TIER_ORDER), why


def build_tiers(names, verbose=True):
    tiers, skipped = [], []
    for name in names:
        cls = TIER_CLASSES.get(name)
        if not cls:
            continue
        inst = cls()
        if inst.available():
            tiers.append(inst)
        else:
            skipped.append((name, inst.unavailable_reason()))
    if verbose:
        for name, reason in skipped:
            print("  tier %-10s unavailable -- %s" % (name, reason))
    return tiers


def _failed(domain, tier_name, exc):
    return SiteResult(domain=domain, source_tier=tier_name, status="conn_fail",
                      error=str(exc)[:200])


def run_wave(tier, domains, max_pages, workers):
    """One tier across many domains -> {domain: SiteResult}.

    A batching tier decides its own chunking and concurrency; everything else
    is fanned out across the thread pool.
    """
    results = {}
    if getattr(tier, "supports_batch", False):
        try:
            results = tier.fetch_batch(domains, max_pages=max_pages) or {}
        except Exception as exc:
            results = {d: _failed(d, tier.name, exc) for d in domains}
    else:
        # A tier may cap its own concurrency: crawl4ai spawns a Chromium per
        # worker, so 10 at once would eat several GB.
        limit = min(workers, getattr(tier, "max_workers", workers) or workers)
        with ThreadPoolExecutor(max_workers=limit) as pool:
            futures = {pool.submit(tier.fetch_site, d, max_pages): d
                       for d in domains}
            done = 0
            for fut in as_completed(futures):
                d = futures[fut]
                try:
                    results[d] = fut.result()
                except Exception as exc:
                    results[d] = _failed(d, tier.name, exc)
                done += 1
                if done % 25 == 0 or done == len(domains):
                    print("      %4d/%d" % (done, len(domains)))
    for d in domains:
        results.setdefault(d, SiteResult(domain=d, source_tier=tier.name,
                                         status="thin", error="no result"))
    return results


def run_cascade(domains, tiers, max_pages, workers, prior_attempts=None):
    """Free tiers share one queue; paid tiers then run in strict order.

    Returns ({domain: result}, {domain: attempts}).
    """
    prior_attempts = prior_attempts or {}
    free = [t for t in tiers if not t.costs_money]
    paid = [t for t in tiers if t.costs_money]

    accepted, best = {}, {}
    attempts = {d: list(prior_attempts.get(d, [])) for d in domains}
    pending = list(domains)

    if free and pending:
        print("    free pool  %4d domains  <-  %s  (concurrent, shared queue)"
              % (len(pending), ", ".join(t.name for t in free)))
        pool = run_free_pool(pending, free, max_pages, workers, prior_attempts)
        accepted.update(pool.results)
        best.update(pool.best)
        attempts.update(pool.attempts)
        pending = [d for d in pending if d not in pool.results]

    for tier in paid:
        if not pending:
            break
        label = "batched" if getattr(tier, "supports_batch", False) else (
            "parallel x%d" % min(workers, getattr(tier, "max_workers", workers) or workers))
        print("    tier %-10s %4d domains (%s)" % (tier.name, len(pending), label))
        started = time.time()
        results = run_wave(tier, pending, max_pages, workers)

        still = []
        for d in pending:
            r = results[d]
            apply_gate(r)
            attempts.setdefault(d, []).append({
                "tier": tier.name, "status": r.status, "gate_ok": r.gate_ok,
                "gate_reason": r.gate_reason, "chars": r.body_chars(),
                "pages": len(r.pages), "duration_s": r.duration_s,
                # Without the error text a failed cascade is undiagnosable:
                # the "least-bad" result that survives is often another tier's.
                "error": (r.error or "")[:300],
            })
            if r.gate_ok:
                accepted[d] = r
            else:
                if d not in best or r.body_chars() > best[d].body_chars():
                    best[d] = r
                still.append(d)

        print("    tier %-10s %4d passed, %4d still failing  (%.0fs)"
              % (tier.name, len(pending) - len(still), len(still),
                 time.time() - started))
        pending = still

    for d in pending:
        accepted[d] = best.get(d) or SiteResult(
            domain=d, status="no_url", error="no tiers ran")
    return accepted, attempts


def collect_domains(rows, url_column):
    """domain -> row indexes. Duplicates crawl once and share a verdict."""
    mapping, unusable = OrderedDict(), []
    for i, row in enumerate(rows):
        domain = normalize_domain((row.get(url_column) or "").strip())
        if not domain:
            unusable.append((i, (row.get(url_column) or "").strip()))
            continue
        mapping.setdefault(domain, []).append(i)
    return mapping, unusable


def emit_result(slug, result, attempts, row_idx):
    record = {
        "domain": result.domain,
        "status": result.status,
        "gate_ok": result.gate_ok,
        "gate_reason": result.gate_reason,
        "source_tier": result.source_tier,
        "pages": len(result.pages),
        "chars": result.body_chars(),
        "error": result.error,
        "row_indexes": row_idx,
        "digest": build_digest(result) if result.pages else "",
    }
    jsonl_append(os.path.join(campaign_dir(slug), "crawl_results.jsonl"), record)
    for a in attempts:
        jsonl_append(os.path.join(campaign_dir(slug), "crawl_log.jsonl"),
                     dict(a, domain=result.domain))
    return record


def report(records, unusable, elapsed, escalatable):
    total = len(records)
    passed = [r for r in records if r["gate_ok"]]
    failed = [r for r in records if not r["gate_ok"]]
    print("\n" + "=" * 68)
    print("CRAWL REPORT  --  %d domains in %.1fs" % (total, elapsed))
    print("=" * 68)
    print("  passed gate : %d (%.0f%%)" % (len(passed), 100.0 * len(passed) / max(1, total)))
    print("  failed gate : %d (%.0f%%)" % (len(failed), 100.0 * len(failed) / max(1, total)))
    if unusable:
        print("  unusable url: %d rows" % len(unusable))
    if passed:
        by_tier = Counter(r["source_tier"] for r in passed)
        print("\n  accepted by tier:")
        for tier, n in by_tier.most_common():
            avg = sum(r["chars"] for r in passed if r["source_tier"] == tier) / n
            print("    %-10s %4d   avg %6.0f chars" % (tier, n, avg))
    if failed:
        print("\n  failure reasons:")
        for reason, n in Counter(
                r["gate_reason"].split(":")[0] for r in failed).most_common(12):
            print("    %-22s %4d" % (reason, n))
    if failed and escalatable:
        print("\n  %d domains can still be escalated. To spend on them:" % len(failed))
        print("    python scripts/crawl_sites.py --campaign <slug> --escalate --confirm-spend")
    print("=" * 68)


def main():
    enable_utf8_stdout()
    ap = argparse.ArgumentParser()
    ap.add_argument("--campaign", required=True)
    ap.add_argument("--url-column", default="")
    ap.add_argument("--tiers", default="", help="comma list; default stdlib for phase 1")
    ap.add_argument("--escalate", action="store_true",
                    help="only re-try domains that failed the gate")
    ap.add_argument("--confirm-spend", action="store_true",
                    help="required before any paid tier runs")
    ap.add_argument("--max-escalations", type=int, default=0,
                    help="cap paid attempts (default 30%% of list)")
    ap.add_argument("--workers", type=int, default=LIMITS["max_workers"],
                    help="hosts in flight at once (default from LIMITS)")
    ap.add_argument("--batch-size", type=int, default=0,
                    help="domains per batched-tier run (default 20)")
    ap.add_argument("--max-pages", type=int, default=5)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--refresh", action="store_true", help="ignore cache")
    ap.add_argument("--speed", action="store_true",
                    help="prioritise speed: paid Apify ahead of free Crawl4AI. "
                         "Costs credits to buy wall-clock -- only set this when "
                         "the user has explicitly agreed to it.")
    args = ap.parse_args()

    slug = args.campaign
    cdir = campaign_dir(slug)
    if not os.path.isdir(cdir):
        sys.exit("no campaign folder: " + cdir)
    os.makedirs(os.path.join(cdir, "cache"), exist_ok=True)

    cfg = read_config(slug)
    url_column = args.url_column or cfg.get("url_column") or ""
    csv_path = cfg.get("input_csv") or find_input_csv(slug)
    if not csv_path or not os.path.exists(csv_path):
        sys.exit("no input CSV under " + os.path.join(cdir, "input"))
    rows = read_rows(csv_path)
    if not rows:
        sys.exit("input CSV has no rows")
    if not url_column:
        sys.exit("need --url-column (one of: %s)" % ", ".join(rows[0].keys()))
    if url_column not in rows[0]:
        sys.exit("column %r not in CSV. Columns: %s"
                 % (url_column, ", ".join(rows[0].keys())))
    cfg.update({"url_column": url_column, "input_csv": csv_path,
                "row_count": len(rows)})
    write_config(slug, cfg)

    # crawl_results.jsonl always reflects the current invocation in full, so it
    # is rebuilt from cache + fresh fetches rather than appended to forever.
    open(os.path.join(cdir, "crawl_results.jsonl"), "w", encoding="utf-8").close()

    mapping, unusable = collect_domains(rows, url_column)

    # Rows already disqualified by prefilter.py must not cost a fetch. A domain
    # is skipped only when EVERY row carrying it was disqualified -- the same
    # company can appear twice with different data.
    prefiltered = {a["row"] for a in jsonl_read(os.path.join(cdir, "assessments.jsonl"))
                   if a.get("row") is not None and a.get("source_tier") == "prefilter"}
    skipped_dq = 0
    if prefiltered:
        for d in list(mapping.keys()):
            if all(i in prefiltered for i in mapping[d]):
                del mapping[d]
                skipped_dq += 1

    domains = list(mapping.keys())
    if args.limit:
        domains = domains[:args.limit]

    order, order_why = resolve_order(cfg, args.speed)
    if args.speed and cfg.get("speed_priority") is not True:
        cfg["speed_priority"] = True
        cfg["tier_order"] = order
        write_config(slug, cfg)

    if args.tiers:
        names = [t.strip() for t in args.tiers.split(",") if t.strip()]
        order_why = "--tiers"
    elif args.escalate:
        names = [t for t in order if t != "stdlib"]
    else:
        names = ["stdlib"]

    print("campaign   : %s" % slug)
    print("input      : %s (%d rows, %d unique domains)"
          % (os.path.basename(csv_path), len(rows), len(mapping)))
    print("url column : %s" % url_column)
    print("tiers      : %s   [%s]" % (", ".join(names), order_why))
    if skipped_dq:
        print("prefiltered: %d domains skipped (disqualified from CSV columns)" % skipped_dq)

    tiers = build_tiers(names)
    if not tiers:
        sys.exit("no usable tiers -- set APIFY_TOKEN / FIRECRAWL_API_KEY or install crawl4ai")
    if args.batch_size:
        for t in tiers:
            if getattr(t, "supports_batch", False):
                t.batch_size = args.batch_size

    paid = [t for t in tiers if t.costs_money]
    if paid and not args.confirm_spend:
        sys.exit("\nRefusing to run paid tiers (%s) without --confirm-spend.\n"
                 "Re-run with --confirm-spend once the cost is agreed."
                 % ", ".join(t.name for t in paid))

    todo, priors, cached_records = [], {}, []
    for domain in domains:
        blob = None if args.refresh else load_cache(slug, domain)
        if blob:
            accepted = SiteResult.from_dict(blob["accepted"])
            if accepted.gate_ok or not args.escalate:
                cached_records.append(emit_result(slug, accepted, [], mapping[domain]))
                continue
            todo.append(domain)
            priors[domain] = blob.get("attempts", [])
            continue
        if args.escalate:
            continue  # nothing cached means phase 1 never ran for it
        todo.append(domain)

    if paid:
        cap = args.max_escalations or max(1, int(0.30 * len(domains)))
        if len(todo) > cap:
            print("\n  capping paid attempts at %d of %d (--max-escalations)"
                  % (cap, len(todo)))
            todo = todo[:cap]

    print("to fetch   : %d (%d served from cache)\n" % (len(todo), len(cached_records)))
    if not todo:
        report(cached_records, unusable, 0.0, escalatable=False)
        return

    start = time.time()
    accepted, attempts = run_cascade(todo, tiers, args.max_pages, args.workers, priors)

    records = list(cached_records)
    for domain in todo:
        result = accepted[domain]
        save_cache(slug, domain, result, attempts[domain])
        records.append(emit_result(slug, result, attempts[domain],
                                   mapping.get(domain, [])))

    escalatable = any(t not in names for t in order if t != "stdlib")
    report(records, unusable, time.time() - start, escalatable and not args.escalate)


if __name__ == "__main__":
    main()
