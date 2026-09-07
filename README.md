# Lead Gen — ICP Qualification & Ranking

Turn a raw lead CSV into a ranked, evidence-backed list. Every lead's website is
crawled for real content, judged against a campaign rubric, and the verdict is
appended to your original file.

```
Company, Website, Country, ...   →   Company, Website, Country, ..., Fitment, Ranking, Comments, Crawl_Status
```

| Column | Values | Means |
|---|---|---|
| `Fitment` | `Good` · `Avg` · `Unfit` | Are they the right kind of company? |
| `Ranking` | `1` · `2` · `3` | Which wave do we contact them in? |
| `Comments` | one short phrase | The specific thing on their site that decided it |
| `Crawl_Status` | `ok`, `dns_fail`, `blocked`… | Whether we actually read the site |

`Ranking` is a **priority wave, not a copy of Fitment**. A perfect-fit company
with a static brochure site ranks below an average-fit company hiring three ML
engineers. Fit answers *are they right*; ranking answers *is now the time*.

---

## Getting started (new machine)

```bash
git clone <your-repo-url>
cd "Lead Gen"
python setup.py
```

`setup.py` installs the three free crawlers automatically — no account, no card
— then offers to record the two optional paid keys. Press ENTER to skip either;
they can be added later by re-running it. Re-running is always safe.

```bash
python setup.py --check      # verify an install, change nothing
python setup.py --no-keys    # install only, never prompt
```

Verify, then run:

```bash
python scripts/test_gate.py && python scripts/test_tiers.py
```

Open the folder in Claude Code and type `/new`.

### What you need

| | Required? | Notes |
|---|---|---|
| Python 3.9+ | **yes** | 3.12 recommended |
| Apify token | no | cheap bulk crawling; skip and the tier stays off |
| Firecrawl key | no | rescues the hard tail; skip and the tier stays off |

**Without any keys the pipeline still works** — the three free crawlers clear
most of a typical list. Missing keys disable a tier with a note, never an error.

### Keys

`setup.py` writes them to `.env`, which is gitignored. Real environment
variables take precedence if you prefer those. **Never commit `.env`.**

If a key leaks (pasted into chat, committed, shared in a ticket), rotate it at
the provider — both are one-click regenerate.

### What is NOT in this repo, by design

Campaign folders are client data and stay local. Only the synthetic `_test`
fixture (public domains, no customer information) is tracked. Clone the repo,
run your own campaigns, and nothing you crawl or score is ever committed.

---

## Run it

```
/new
```

That starts the `icp-qualify` skill, which walks the whole thing: five plain-English
questions, a rubric you confirm, your CSV, the crawl, the evaluation, the output.

---

## How it works

```
/new
  1. INTAKE      five plain-English questions      → brief.md
  2. RUBRIC      interpreted, then you confirm     → rubric.json
  3. CSV         column detection + data health    → inspect_csv.py
  4. PRE-FILTER  column-only rules, before any fetch
  5. CRAWL       4-tier cascade, cached, resumable
  6. EVALUATE    25 digests at a time, checkpointed
  7. MERGE       original CSV + 4 new columns
  8. QA          distribution, edge cases, per-tier audit
```

### The fetch cascade

Escalation is driven by a **content-quality gate**, not by HTTP status. A site
that returns `200 OK` with nothing but a cookie banner escalates exactly like one
that times out — that is what stops thin content from quietly becoming an `Avg`
rating.

**The three free tiers run concurrently over one shared queue. The two paid
tiers run in strict order afterwards, only on what the free pool could not
satisfy.**

| Tier | Cost | Speed | Strength |
|---|---|---|---|
| stdlib | free | ~1s | plain HTTP; clears most of a typical list |
| Crawl4AI | free | ~40s | real browser; renders JS |
| Scrapling | free | ~2s | TLS fingerprinting; beats handshake-based blocks |
| Apify | $0.023 batched | ~157s/run | renders JS at volume, cheaply |
| Firecrawl | per page | ~8s | strongest anti-bot; the hard-tail backstop |

### The free pool

There is no reason to make a fast free crawler wait for a slow one, so they
share a queue: whichever is idle takes the next domain. stdlib naturally
absorbs most of the list while Crawl4AI grinds the hard ones in parallel.

Retry is folded into the same loop rather than being a second phase. A domain
that fails its crawler's quality gate goes back on the queue tagged with who
has tried it; the next idle crawler that has **not** tried it picks it up. A
domain leaves the pool when one crawler clears the gate, or when all three have
failed — and only then is it eligible to cost money.

Observed on the fixture (18 threads: stdlib x8, crawl4ai x4, scrapling x6):

```
stripe.com     stdlib/ok/OK                                          <- one attempt
vercel.com     crawl4ai/ok/OK                                        <- one attempt
g2.com         stdlib/blocked/X -> scrapling/thin/X -> crawl4ai/conn_fail/X
example.com    crawl4ai/thin/X  -> scrapling/thin/X  -> stdlib/thin/X
```

Successes cost exactly one fetch. Failures get all three, and **the order
differs per domain** — that is work-stealing, not a fixed sequence.

### Why Apify is batched

Not an optimisation — the difference between **$5.81 and $25.55** per
1000-lead campaign. Every actor run pays ~60s of container startup, so one
domain per run costs $0.102 while four in one run cost $0.093 total ($0.023
each). Both measured on this account. Apify crawls up to 20 domains per run
(`--batch-size`), mapped back by registrable domain.

### Does a weaker tier winning early hurt quality?

No, and the reason is structural: **the digest is capped at 2,500 characters.**
Once a result clears the gate with more than that, extra characters change
nothing the evaluator ever sees. So a thin-but-passing result from an early
free tier produces the same judgment input as a fat one from a paid tier.
Anything that fails the gate still falls through to the tiers behind it.

### What the tiers actually beat

Measured on `g2.com`, which turned out to be a useful adversary — it is
DataDome-protected, and only one of the five gets through:

| Tier | Result |
|---|---|
| stdlib | `HTTP 403` |
| Scrapling | `thin` — TLS fingerprinting is not enough here |
| Crawl4AI | `Blocked by anti-bot protection: DataDome captcha` (fails fast, 3.2s) |
| Apify | `403` four times, rotated sessions, escalated to its UNBLOCKER proxy, still `403` |
| **Firecrawl** | **85,600 chars across 5 pages, 8s** |

That is why Firecrawl stays last rather than being dropped: it is the only
thing that rescues the hard tail, and it only ever sees the tail.

Tiers 1–3 need **no pip install** — Apify and Firecrawl are plain HTTPS+JSON, so
`urllib` covers them. Tiers with no key are skipped with a note, not an error.

Only escalated domains cost money, so a 1000-lead list typically means 150–250
paid fetches rather than 1000. **A paid tier will not run without
`--confirm-spend`**, and results are cached per domain so a paid fetch is never
paid for twice.

### Speed vs cost

Onboarding asks whether time is a constraint. **The default is free-first and
does not change unless you explicitly agree to spend**, after being told what it
costs:

| | Order | 200 escalated domains |
|---|---|---|
| **Default** | free pool (stdlib + crawl4ai + scrapling) → apify → firecrawl | slower, **$0** for whatever the pool catches |
| `--speed` | stdlib + scrapling → apify → firecrawl → crawl4ai | faster, spends credits sooner |

The choice persists to `campaign.json` (`speed_priority`), so the rest of the
run follows it without asking again. Override per-invocation with `--tiers`.

### Safety limits

All in one place — `LIMITS` in `scripts/fetchers/base.py`. Deliberately
conservative: a crawl that gets an IP banned costs far more than a slow one.

| Guard | Value | Why |
|---|---|---|
| per-domain delay | 0.8s | pages of one host are fetched in series with a pause; different hosts still run in parallel, so it costs almost no wall-clock |
| max workers | 8 | distinct hosts in flight |
| crawl4ai workers | 4 | each is a separate headless Chromium; more would exhaust RAM |
| scrapling workers | 6 | no browser process, but each holds a real TLS session |
| retries | 2, exponential backoff | honours `Retry-After` when the server sends one |
| Apify concurrency | 4 | the account allows 32; staying well under |
| Apify budget headroom | $5 | refuses to start a run near the monthly cap |

**`429` and `503` are retried; `401`, `402` and `403` are not.** A 403 is a
considered refusal — hammering it is what turns a soft block into a hard one,
and retrying an auth failure just burns quota and looks like abuse.

Crawling stays on the target site (`registrable` domain match) and skips
legal/login/cart paths, so a run never wanders off into unrelated hosts.

### The quality gate

A crawl result is only good enough to judge a lead on if **all** hold:

- ≥ 400 chars of body text after nav/footer stripping
- a real title, meta description, or H1 — not the bare domain echoed back
- not a parking page (`domain is for sale`, `coming soon`)
- not a JS or bot-wall stub (`please enable JavaScript`, `checking your browser`)
- homepage > 1200 chars, or at least one subpage retrieved
- ≥ 25 distinct content words beyond generic nav vocabulary

Thresholds live in one place — `GATE` in `scripts/fetchers/base.py`. Raise them to
escalate harder; lower them to spend less.

---

## Setup

Python 3.12. Tiers 1–3 need **no pip install** — Apify and Firecrawl are plain
HTTPS+JSON, so `urllib` covers them.

Keys are read from the environment only, never stored in the repo:

```bash
[Environment]::SetEnvironmentVariable('APIFY_TOKEN','apify_api_...','User')
```

The two free browser/fingerprint tiers, if you want them:

```bash
python -m pip install crawl4ai "scrapling[fetchers]"
python -m playwright install chromium
```

> **Install-order trap, learned the hard way.** If you also install `camoufox`
> (for Scrapling's stealth mode), it **downgrades Playwright** — which silently
> breaks Crawl4AI, because the older Playwright looks for a different Chromium
> build number. The symptom is `Executable doesn't exist at ...chromium_headless_shell-<n>`.
> The fix is to re-run `python -m playwright install chromium` afterwards.
> Always install camoufox *before* Crawl4AI, or re-run the browser install last.

Scrapling's browser modes (`StealthyFetcher`, `DynamicFetcher`) fail on Windows
with `launch_persistent_context: spawn UNKNOWN` even when patchright launches
fine directly. They are therefore **off by default** — nothing is lost, since
Crawl4AI already covers browser rendering in the same pool. Scrapling is used
purely for its TLS-fingerprinted HTTP, which is fast and fails differently from
stdlib. `--scrapling-browser` re-enables the attempt if a future version fixes it.

Tiers with no key configured are skipped with a note, not an error. See
[config/providers.example.json](config/providers.example.json).

> **Windows gotcha:** `SetEnvironmentVariable(...,'User')` writes to the
> registry, and only *newly started* processes inherit it. Terminals and editors
> already open keep their stale environment block, so a tier can read as
> unavailable until you restart them. Check what a new process will actually
> see with:
> ```bash
> [Environment]::GetEnvironmentVariable('APIFY_TOKEN','User')
> ```

---

## Scripts

Every script takes `--campaign <slug>` and is safe to re-run.

| Script | Does |
|---|---|
| `inspect_csv.py` | Column detection, URL health, duplicates. Read-only. |
| `prefilter.py` | Applies CSV-column disqualifiers before any fetch. `--apply` to write. |
| `crawl_sites.py` | The cascade. `--escalate --confirm-spend` for paid tiers. |
| `resolve_failed.py` | Writes `Unfit`/`3` for unverifiable domains. `--apply` to write. |
| `next_batch.py` | Prints the next 25 unassessed digests. `--status` for counts. |
| `merge_results.py` | Appends the columns to the original CSV. `--no-status` to drop it. |
| `test_gate.py` | Quality-gate judgment tests (12). Run after touching `GATE`. |
| `test_tiers.py` | Tier + pool tests (42). Run after touching any fetcher. |
| `freepool.py` | Shared work queue for the free tiers (not a CLI). |

`prefilter.py` and `resolve_failed.py` are dry-run by default.

---

## Guarantees

- **Your original file is never modified.** Output is a new file in `output/`.
- **Every original column and row survives, in order.** It goes back into a
  sending tool, so this is non-negotiable.
- **Duplicate domains are never dropped** — crawled once, verdict shared across
  every row.
- **No row is ever left blank.** Anything unassessed becomes `Unfit`/`3`/`"Not
  assessed"` and the merge prints a warning, because silent gaps read as "fine".
- **Fit is never inferred from a failed crawl.** A dead domain gets
  `"Domain does not resolve - not verified"`, never "no clear fit".
- **UTF-8 with BOM** on output, so Excel doesn't mangle non-ASCII company names.

---

## Re-running

The crawl cache is per-domain and persists. So:

- **Re-run the crawl** — near-instant, fetches nothing, costs nothing.
- **Fix the rubric and re-judge** — delete `campaigns/<slug>/assessments.jsonl`
  and re-run from step 8. The crawl is untouched, so this is free.
- **Interrupt anything** — nothing is lost. Progress lives in files, not memory,
  which is why a 1000-lead run can span two sessions.

---

## Layout

```
campaigns/<slug>/
  brief.md              your answers, verbatim
  rubric.json           the interpreted criteria
  campaign.json         resolved CSV path + website column
  input/leads.csv       your original file, untouched
  cache/<domain>.json   per-domain crawl cache, records the winning tier
  crawl_results.jsonl   digests + gate outcome, rebuilt each run
  crawl_log.jsonl       every tier attempt, appended forever
  assessments.jsonl     one verdict per lead
  output/leads_qualified.csv
```

---

## Known limits

- **~40 evaluation batches** for a 1000-lead list, which may span two sessions.
  Resumability is designed for it, but it is not one-click at that size.
- **Some sites defeat all four tiers** — hard bot walls, login-gated content.
  These end unverifiable and are never guessed at.
- **Apify is slow even batched** — ~157s for a run, since container startup
  dominates. It is the cheap tier, not the fast one. Firecrawl answers in ~8s.
- **Some sites defeat everything but Firecrawl** (DataDome, hard Cloudflare).
  Budget for tier 3 actually being reached on a small tail.
- **The rubric is the whole ballgame.** Output quality tracks rubric specificity
  almost exactly, which is why step 2 has a confirmation gate.
