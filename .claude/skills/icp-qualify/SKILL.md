---
name: icp-qualify
description: Qualify and rank a lead-gen CSV against a campaign ICP. Captures the campaign in plain English, builds an auditable rubric, crawls every lead's website for evidence, and appends Fitment / Ranking / Comments columns to the original CSV. Use when the user runs /new, uploads a lead list to score, or asks to qualify, rank, score, or filter leads for a campaign.
---

# ICP Qualification & Ranking

Turn a raw lead CSV into a ranked, evidence-backed list. Scripts do the fetching;
you do the judging -- unless the campaign's judge is TypeSafe, in which case
TypeSafe decides Fitment and Ranking and you write every comment.

**Working directory is the project root** (the folder containing `scripts/` and
`campaigns/`). All commands below are run from there. Quote every path — the
project root contains a space.

---

## Step 1 — Capture the campaign

First, silently check whether a TypeSafe key is set on this machine — it
changes the wording of question 7:

```bash
python scripts/typesafe_judge.py --status
```

Then ask all seven questions in **one message**. Take free prose; do not force
choices. Question 7 is asked for **every** campaign: the judge is a
per-campaign choice, never a machine-wide default.

> Before I build the qualification rubric, tell me about this campaign — plain
> English is fine, answer in one go:
>
> 1. **What kind of campaign is this?** (cold email, LinkedIn, ABM, event follow-up, paid retargeting…)
> 2. **What are we offering?** What it is, what makes it different, and rough price point if that shapes who can buy.
> 3. **Who do we want to reach?** Industry, company size, geography, maturity, and the roles you'd be talking to.
> 4. **Anything that's an automatic no?** Hard disqualifiers — competitors, wrong regions, company types you never sell to.
> 5. **What separates a lead you'd call first from one you'd call last?** This drives ranking, and it's a different question from whether they fit.
> 6. **Is time a constraint on this run?** By default the crawler exhausts the free tiers first, which is slower but costs nothing.
> 7. **Who should decide Fitment and Ranking for this campaign — Claude or TypeSafe?** Claude is the default: it reads every site and writes the whole verdict. TypeSafe decides the grades with calibrated probabilities, and I still write every comment.

If the status said the key is **not** set, add this sentence to question 7:
*"TypeSafe needs a `TYPESAFE_API_KEY`, which isn't set up on this machine yet."*

If an answer is vague ("SaaS companies"), ask one targeted follow-up. A vague
rubric produces generic comments, and that shows up in every row.

Then:
- Derive a short kebab-case `<slug>` from the campaign.
- `mkdir -p "campaigns/<slug>/input" "campaigns/<slug>/output"`
- Write the answers **verbatim** into `campaigns/<slug>/brief.md` under the
  question headings. Verbatim matters — the rubric is your interpretation, and
  the original wording is what you check it against later.
- Record the judge (see question 7 below):
  `python scripts/typesafe_judge.py --campaign <slug> --set-judge <claude|typesafe>`

### Handling question 6 — the speed/cost tradeoff

**Default is free-first. Do not change it unless the user explicitly agrees
after being told the cost.**

If they say time is *not* a constraint, or don't mention it: say nothing further
and use the default order (`stdlib → crawl4ai → apify → firecrawl`).

If they say time **is** a constraint, do not just switch. Explain the trade and
ask for a decision:

> Noted. I can prioritise speed, but it's a real trade so I'd rather you decide.
>
> **Default (free-first):** all three free crawlers — stdlib, Crawl4AI and
> Scrapling — work a shared queue concurrently before anything bills. Crawl4AI
> is the slow one (~40s/site) but also the richest extractor we have, and it
> only ever gets the domains the fast crawlers couldn't do. $0.
>
> **Speed-first:** Apify goes ahead of Crawl4AI. Much faster, but it **spends
> credits** — about $0.023 per domain, so ~$4.60 for those same 200. Firecrawl
> still backstops the hard tail either way.
>
> Want me to prioritise speed and spend the credits?

Only on a clear yes, pass `--speed` on the first crawl. It persists the choice
to `campaign.json` (`speed_priority: true`) so every later command in the run
follows it without asking again:

```bash
python scripts/crawl_sites.py --campaign <slug> --url-column "<column>" --speed
```

On anything other than a clear yes — hesitation, "maybe", a question back —
stay on the default. Free-and-slow is recoverable; spent credits are not.

### Handling question 7 — the judge

The choice belongs to this campaign only and is saved to its `campaign.json`.
The next `/new` asks again.

- **Claude, no answer, or unclear** → `--set-judge claude`. Say nothing further.
- **TypeSafe, and the key is set** → `--set-judge typesafe`.
- **TypeSafe, but no key** → tell the user they can add it themselves by running
  `python setup.py` (or putting `TYPESAFE_API_KEY=` in `.env`), and never to
  paste it into chat. Record `claude` for now and carry on: the judge only
  matters at step 8, so steps 2–7 run the same either way. Before step 8, check
  `--status` again; if the key is there by then, run `--set-judge typesafe`.

If `--set-judge typesafe` prints a fallback warning, tell the user in one line
that TypeSafe is unavailable and Claude will judge this campaign, then carry on.

The judge only changes step 8. Every other step is identical.

---

## Step 2 — Build the rubric, and get it confirmed

Read `references/rubric-schema.md`, then write `campaigns/<slug>/rubric.json`.

Mark each disqualifier `checkable_from: "csv"` (answerable from a column) or
`"crawl"` (needs the website). CSV-checkable rules cost no fetch and no API
spend, so extract as many as the brief honestly supports.

**Show the rubric back in plain English and wait for confirmation.** Say which
parts you inferred rather than were told. A wrong rubric caught here costs a
minute; caught after the crawl it costs the whole run and real money.

---

## Step 3 — Take the CSV and confirm the mapping

Ask the user to drop the file into `campaigns/<slug>/input/`. Then:

```bash
python scripts/inspect_csv.py --campaign <slug>
```

Report back: row count, the detected website column, URL health, duplicates, and
which columns can carry a pre-crawl disqualifier. **Confirm the website column
with the user** rather than assuming — the report ranks candidates but a list
with both `Website` and `Company Domain` is a genuine coin-flip.

Duplicates are never dropped. They are crawled once and share a verdict.

---

## Step 4 — Pre-filter (free disqualifications)

```bash
python scripts/prefilter.py --campaign <slug>            # dry run, always first
python scripts/prefilter.py --campaign <slug> --apply
```

Report how many rows this removed and why. On a list with a geography or
headcount column this often removes a large share before a single request.

---

## Step 5 — Crawl, tier 1 (free)

```bash
python scripts/crawl_sites.py --campaign <slug> --url-column "<column>"
```

Runs the three FREE crawlers — stdlib, Crawl4AI and Scrapling — concurrently
over one shared queue. Whichever is idle takes the next domain; anything that
fails one crawler's quality gate is automatically retried by a crawler that has
not tried it. Nothing here costs money.

Caches each result and prints a report: pass rate, failure reasons, per-tier
breakdown. Expect **75–88% to pass**, and note which crawler won what — that is
the per-tier audit in step 10.

Interrupting is safe. Re-running is near-instant and re-fetches nothing.

---

## Step 6 — Escalate, only if the numbers justify it

Read the failure report. Then **tell the user the count and ask before spending**:

> 112 of 480 domains failed the quality gate — mostly JS-rendered sites and bot
> walls. Escalation tries Crawl4AI first, which is free but slow (~40s each, so
> maybe 20 minutes), then bills only for whatever it can't get: Apify at
> ~$0.023/domain, Firecrawl for the hard tail. Want me to?

Escalate only on a clear yes:

```bash
python scripts/crawl_sites.py --campaign <slug> --escalate --confirm-spend
```

The script refuses to run a paid tier without `--confirm-spend`, and caps
attempts at 30% of the list unless `--max-escalations N` says otherwise. Tiers
with no key configured are skipped with a note, not an error.

Only domains that ALL THREE free crawlers failed reach a paid tier. Escalation
then runs **tier by tier in waves**, not domain by domain, so Apify can put up
to 20 domains in one actor run (`--batch-size`).

Order is `stdlib -> crawl4ai -> apify -> firecrawl`: both free tiers are
exhausted before anything bills. Crawl4AI is slow (~40s/domain, 4 workers) but
free and, measured, the richest extractor of the four. If you would rather
spend money than wall-clock on a large list, skip it for this run:

```bash
python scripts/crawl_sites.py --campaign <slug> --escalate --confirm-spend --tiers apify,firecrawl
```

Skipping escalation entirely is a legitimate choice — those rows simply end as
unverifiable, which is honest.

---

## Step 7 — Resolve what could not be verified

```bash
python scripts/resolve_failed.py --campaign <slug>            # dry run
python scripts/resolve_failed.py --campaign <slug> --apply
```

This writes `Unfit` / `3` for every domain with no usable content, with a comment
naming the **data** problem ("Domain does not resolve — not verified"), never
implying anything about the company. Do this with the script, not by hand — it is
the rule most likely to erode over a long run.

---

## Step 8 — Evaluate

Check the campaign's judge with `python scripts/typesafe_judge.py --status
--campaign <slug>`, then follow **8A** for `claude` or **8B** for `typesafe`.

### 8A — Claude judges, one batch at a time

Read `references/scoring-guide.md` once, and `campaigns/<slug>/rubric.json` once.
Then loop:

```bash
python scripts/next_batch.py --campaign <slug> --size 25
```

For each domain in the batch, append one JSON line to
`campaigns/<slug>/assessments.jsonl`:

```json
{"domain":"acme.com","fitment":"Good","ranking":1,"comments":"Builds LLM apps for insurers - direct match","evidence":"/services: 'AI implementation for insurance carriers'","crawl_status":"ok","source_tier":"stdlib","judge":"claude"}
```

Write the batch with a heredoc, then call `next_batch.py` again. Repeat until it
reports `0 pending`. Check progress any time with `--status`.

Because progress lives in the file, a long run resumes cleanly across sessions —
which matters at ~40 batches for a 1000-lead list. Tell the user if a list is
large enough to need more than one sitting.

### 8B — TypeSafe decides, Claude comments

**1. Decide.** Preview first, then run:

```bash
python scripts/typesafe_judge.py --campaign <slug> --dry-run
python scripts/typesafe_judge.py --campaign <slug>
```

The dry run prints the questions built from `rubric.json` and a cost estimate
(typically well under $1 per 1,000 leads). Tell the user the estimate in one
line and run it; they already chose TypeSafe at intake. The script refuses runs
estimated above $5 without `--confirm-spend` — ask before passing it.

Every rubric signal becomes a yes/no question and the grade a Good/Avg/Unfit
choice, all in one request per domain. The script applies the scoring-guide
rules in code (disqualifiers first, "torn → lower grade", strong anti-fit →
Unfit, the Ranking table) and writes `typesafe_decisions.jsonl`. Interrupting is
safe; re-running resumes.

If it stops on a rejected key (401) or says the key is missing, tell the user,
run `--set-judge claude`, and continue with 8A. Never ask for a key in chat.

**2. Comment.** Read `references/scoring-guide.md` once (especially *Comments on
TypeSafe verdicts*), then loop:

```bash
python scripts/next_batch.py --campaign <slug> --comments --size 25
```

Each domain shows TypeSafe's verdict, the signals it found, and the digest. For
each one, append one JSON line to `campaigns/<slug>/comments.jsonl`:

```json
{"domain":"acme.com","comments":"Builds LLM apps for insurers - direct match","evidence":"/services: 'AI implementation for insurance carriers'"}
```

**Do not write `fitment` or `ranking` — they are TypeSafe's.** If the site
plainly contradicts the verdict, still write the most accurate comment you can
ground in the site and add `"disagree": true`. QA surfaces those; you never
override the decision.

**3. Finalize.** When `--comments` reports `0 pending`:

```bash
python scripts/typesafe_judge.py --campaign <slug> --finalize
```

This joins each decision with its comment into `assessments.jsonl`, taking the
grades from the decision file only.

---

## Step 9 — Merge

```bash
python scripts/merge_results.py --campaign <slug>
```

Writes `campaigns/<slug>/output/<name>_qualified.csv` — every original column and
row preserved in order, with `Fitment`, `Ranking`, `Comments`, `Crawl_Status`
appended. Add `--no-status` if the destination tool rejects unknown columns.

**Any warning about unassessed rows means the run is not finished.** Close the
gap before handing the file over.

---

## Step 10 — QA before delivering

Do all four (five on a TypeSafe run):

1. **Distribution sanity.** A list coming back 80–90% `Good` almost always means
   a loose rubric, not a great list. Say so.
2. **Edge cases.** Re-read every rank-1, every row where Fitment and Ranking
   diverge, and a random 5. Do the comments hold up against the digests?
3. **Cascade audit.** Compare the `Good`/`Avg`/`Unfit` split and average digest
   length grouped by `source_tier` in the merge summary. If stdlib rows skew
   noticeably more `Avg` than Apify rows, tier 1 is passing thin content it
   should be escalating — raise `GATE["min_body_chars"]` in
   `scripts/fetchers/base.py` and re-run the failures.
4. **Comment quality.** Skim 20. If they read interchangeably, the rubric was too
   vague — fix the rubric, delete `assessments.jsonl`, and re-evaluate. The
   crawl cache is untouched, so this costs nothing but time.
5. **TypeSafe runs only.** The merge summary counts verdicts flagged for review
   (low confidence, or `Good` with no fit signal found) and comments marked
   `disagree`. Re-read every one of them and report the count and a few
   examples. Do not change the grades — if a pattern shows up, the fix is the
   rubric or the thresholds in `TYPESAFE` at the top of
   `scripts/typesafe_judge.py`, and that is the user's call.

Then hand over the CSV path, the tier distribution, the top disqualification
reasons, and any targeting pattern worth feeding back into the next list.

---

## Notes

- **Never rate fit from a failed crawl.** No content means no evidence. Step 7
  exists so this is mechanical.
- **Ranking is a priority wave, not a restatement of Fitment.** If no row
  diverges between them, you have collapsed the two axes — re-read
  `references/scoring-guide.md`.
- **API keys come from environment variables only** (`APIFY_TOKEN`,
  `FIRECRAWL_API_KEY`, `TYPESAFE_API_KEY`). Never ask for a key in chat and never
  write one to a file.
- **Re-running a campaign** is cheap: the crawl cache persists per domain, so
  only the evaluation repeats. For a TypeSafe campaign, also delete
  `typesafe_decisions.jsonl` and `comments.jsonl` so the new rubric is used.
- **Comparing judges.** On a campaign Claude already judged,
  `python scripts/typesafe_judge.py --campaign <slug> --all` decides every domain
  alongside Claude's verdicts without touching them, and
  `python scripts/compare_judges.py --campaign <slug>` shows where they differ.
