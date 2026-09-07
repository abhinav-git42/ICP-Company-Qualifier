"""Shared work queue for the free tiers.

The paid tiers run in waves -- every domain through tier N, then the failures
through tier N+1 -- because Apify batches and batching needs the whole set at
once.

The free tiers do not work that way. They cost nothing, so there is no reason
to make a fast one wait for a slow one. Instead every free crawler pulls from
ONE queue: whichever is idle takes the next domain. stdlib is ~1s and Crawl4AI
is ~40s, so stdlib naturally absorbs most of the list while Crawl4AI grinds
through the hard ones in parallel. That is work-stealing, and it means total
wall-clock is set by the slowest *remaining* item rather than by the sum of
per-tier passes.

Retry is folded into the same loop rather than being a second phase. A domain
that fails its crawler's quality gate goes back on the queue tagged with who
has already tried it; the next idle crawler that has NOT tried it picks it up.
A domain leaves the pool when a crawler clears the gate, or when every free
crawler has failed it -- and only then does it become eligible for a paid tier.
"""

import threading
import time

from fetchers.base import apply_gate, SiteResult


class FreePool:
    """Thread-safe queue with per-domain memory of which tiers have tried it."""

    def __init__(self, domains, tier_names, prior_attempts=None):
        prior_attempts = prior_attempts or {}
        self.tier_names = set(tier_names)
        self.lock = threading.Lock()
        self.total = len(domains)

        self.pending = []
        self.attempted = {}
        self.attempts = {}
        self.results = {}      # cleared the gate
        self.best = {}         # least-bad failure, kept for diagnostics
        self.done = set()
        self.inflight = 0

        for d in domains:
            prior = list(prior_attempts.get(d, []))
            self.attempts[d] = prior
            # A cached run already burned these tiers; do not repeat them.
            self.attempted[d] = {a.get("tier") for a in prior
                                 if a.get("tier") in self.tier_names}
            if self.attempted[d] >= self.tier_names:
                self.done.add(d)
            else:
                self.pending.append(d)

    def take(self, tier_name):
        """Next domain this tier has not attempted, or None."""
        with self.lock:
            for i, d in enumerate(self.pending):
                if tier_name not in self.attempted[d]:
                    self.pending.pop(i)
                    self.attempted[d].add(tier_name)
                    self.inflight += 1
                    return d
            return None

    def give_back(self, domain, result, record):
        with self.lock:
            self.inflight -= 1
            self.attempts[domain].append(record)
            if result.gate_ok:
                self.results[domain] = result
                self.done.add(domain)
                return
            if (domain not in self.best
                    or result.body_chars() > self.best[domain].body_chars()):
                self.best[domain] = result
            if self.attempted[domain] >= self.tier_names:
                self.done.add(domain)      # every free tier has now failed it
            else:
                self.pending.append(domain)

    def drained(self):
        """Nothing queued and nothing in flight that could requeue."""
        with self.lock:
            return not self.pending and self.inflight == 0

    def settled(self):
        with self.lock:
            return len(self.done)


def _attempt_record(tier_name, result):
    return {
        "tier": tier_name, "status": result.status, "gate_ok": result.gate_ok,
        "gate_reason": result.gate_reason, "chars": result.body_chars(),
        "pages": len(result.pages), "duration_s": result.duration_s,
        "error": (result.error or "")[:300],
    }


def _worker(pool, tier, max_pages, on_done, stop):
    while not stop.is_set():
        domain = pool.take(tier.name)
        if domain is None:
            # Nothing for THIS tier right now. Another tier may still requeue
            # work, so idle briefly rather than exiting.
            if pool.drained():
                return
            time.sleep(0.2)
            continue
        try:
            result = tier.fetch_site(domain, max_pages=max_pages)
        except Exception as exc:
            result = SiteResult(domain=domain, source_tier=tier.name,
                                status="conn_fail", error=str(exc)[:200])
        if not result.source_tier:
            result.source_tier = tier.name
        apply_gate(result)
        pool.give_back(domain, result, _attempt_record(tier.name, result))
        on_done(tier.name, domain, result)


def run_free_pool(domains, free_tiers, max_pages, workers, prior_attempts=None,
                  progress_every=25, verbose=True):
    """Run every free tier concurrently over one shared queue.

    Returns the FreePool, whose .results / .best / .attempts carry the outcome.
    """
    pool = FreePool(domains, [t.name for t in free_tiers], prior_attempts)
    if not pool.pending:
        return pool

    counter = {"n": 0}
    tally = {t.name: 0 for t in free_tiers}
    clock = threading.Lock()
    stop = threading.Event()
    started = time.time()

    def on_done(tier_name, domain, result):
        with clock:
            counter["n"] += 1
            if result.gate_ok:
                tally[tier_name] += 1
            if verbose and counter["n"] % progress_every == 0:
                print("      %4d fetches | %d/%d domains settled | %.0fs"
                      % (counter["n"], pool.settled(), pool.total,
                         time.time() - started))

    threads = []
    for tier in free_tiers:
        n = max(1, min(workers, getattr(tier, "max_workers", workers) or workers))
        for _ in range(n):
            t = threading.Thread(target=_worker,
                                 args=(pool, tier, max_pages, on_done, stop),
                                 daemon=True)
            t.start()
            threads.append(t)

    if verbose:
        print("      %d threads: %s" % (
            len(threads),
            ", ".join("%s x%d" % (
                t.name, max(1, min(workers, getattr(t, "max_workers", workers) or workers)))
                for t in free_tiers)))

    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        stop.set()
        raise

    if verbose:
        won = ", ".join("%s %d" % (k, v) for k, v in tally.items() if v)
        print("      free pool done in %.0fs -- %d/%d passed the gate%s"
              % (time.time() - started, len(pool.results), pool.total,
                 " (" + won + ")" if won else ""))
    return pool
