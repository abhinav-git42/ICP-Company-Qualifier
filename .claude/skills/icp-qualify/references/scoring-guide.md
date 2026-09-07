# Scoring guide — Fitment, Ranking, Comments

One JSON line per domain, appended to `campaigns/<slug>/assessments.jsonl`:

```json
{"domain":"acme.com","fitment":"Good","ranking":1,"comments":"Builds LLM apps for insurers - direct match","evidence":"/services: 'AI implementation for insurance carriers'","crawl_status":"ok","source_tier":"stdlib"}
```

`evidence` and `source_tier` stay internal — they exist for the QA pass and are
not written to the output CSV.

---

## Order of operations

**1. Disqualifiers first.** If any `checkable_from: "crawl"` rule fires, the
answer is `Unfit` / `3` and the comment names the rule. Stop — no further
reasoning, no partial credit for being otherwise interesting.

**2. Fitment** — are they the right *kind* of company?

| | Meaning |
|---|---|
| `Good` | Clear match on one or more **strong** fit signals, visible on the site |
| `Avg` | Partial match, or plausible but unconfirmed by the content |
| `Unfit` | An anti-fit signal fires, or nothing on the site matches |

Fitment is about category only. Never let excitement about timing pull a company
into `Good` — that belongs in Ranking.

**3. Ranking** — should we contact them *first*?

| | Rule |
|---|---|
| `1` | `Good` fit **and** at least one momentum signal |
| `2` | `Good` fit with no momentum signal, **or** `Avg` fit with a **strong** momentum signal |
| `3` | `Avg` fit with no momentum, plus everything `Unfit`, disqualified, or unverifiable |

**Ranking is allowed to diverge from Fitment, and should.** A perfect-fit company
with a static brochure site is a `Good`/`2`. An average-fit company hiring three
ML engineers is an `Avg`/`2`. If a whole run produces zero divergences, the two
axes have collapsed and the priority column is worthless — go back to
`momentum_signals` in the rubric.

---

## Never rate fit from a failed crawl

If `crawl_status` is anything other than `ok`, or the content is a cookie banner,
a parking page, or a bot wall, you learned **nothing about the company**. That is
a data-quality fact, not evidence of poor fit, and the comment must say which.

`resolve_failed.py` handles these automatically. If you meet one anyway:

```json
{"domain":"acme.com","fitment":"Unfit","ranking":3,"comments":"Site unreachable - not verified","crawl_status":"dns_fail"}
```

Never write "no clear fit" for a site you could not read. That sentence claims
you looked.

---

## Comments

One phrase. **12 words or fewer.** Grounded in something actually on the site.
This is the column a human reads before deciding to send, so it has to carry
information a glance at the company name would not.

The test: *could this comment be copy-pasted onto a different lead?* If yes,
rewrite it.

**Banned — true of anything, so worth nothing:**

- "Good fit for our services"
- "Matches ICP"
- "Potential customer"
- "Company looks relevant"
- "Could benefit from our offering"

**Good — each names the specific thing that decided it:**

| Verdict | Comment |
|---|---|
| Good / 1 | `Claims automation for health insurers; hiring 4 ML engineers` |
| Good / 1 | `Series B fintech, dedicated AI roadmap page` |
| Good / 2 | `Regulated-vertical SaaS, but no hiring or launch signal` |
| Avg / 2 | `Logistics not fintech, but actively hiring AI engineers` |
| Avg / 3 | `Ships software, vertical unclear from site copy` |
| Unfit / 3 | `Design agency - no engineering function` |
| Unfit / 3 | `Sells AI development services - direct competitor` |
| Unfit / 3 | `Domain does not resolve - not verified` |

Notice the `Good`/`2` and `Avg`/`2` rows. A comment on a divergent row should
make the divergence obvious — say what fit, and what was missing or made up for it.

Use plain ASCII hyphens, not em-dashes: these land in a CSV that gets opened in
Excel and imported into sending tools.

---

## Judgment calls

**Holding companies and multi-brand sites.** Judge the entity the domain
represents. If the site is a portfolio page for eight unrelated businesses, that
is `Avg` at best — you cannot tell what you would be selling into.

**Thin but real sites.** A genuine company with a sparse site is `Avg`, not
`Unfit`, and the comment should say the site was thin. Distinguish this from
`resolve_failed.py` territory: here you *did* read the site and it was genuinely
uninformative, which is different from not reaching it.

**Non-English sites.** Judge them normally if the digest is intelligible. If not,
treat as unverifiable rather than guessing from the domain name.

**The CSV context line** (`CSV: Company=... | Country=...`) is evidence too. If
the site is ambiguous but the row says `Industry=Insurance`, that is a legitimate
tiebreaker — but say so in the comment (`CSV lists insurance; site copy generic`)
so a reviewer knows the call rested on the row rather than the crawl.

**When genuinely torn between two grades,** take the lower one and let the
comment explain. An over-graded list wastes send volume; an under-graded lead
still sits in the file with a comment saying why, and is easy to recover.
