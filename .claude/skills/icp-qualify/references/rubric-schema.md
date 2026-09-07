# Turning a plain-English brief into `rubric.json`

The rubric is what makes lead #400 get the same treatment as lead #10. Everything
downstream is only as good as this file.

## Schema

```json
{
  "campaign": "q1-fintech-ai-agents",
  "offering_summary": "One paragraph. Read before every batch, so make it the thing you would tell a new SDR on day one: what we sell, who it is for, and what problem it removes.",

  "disqualifiers": [
    {
      "id": "dq_geo",
      "rule": "Outside North America",
      "checkable_from": "csv",
      "column": "Country",
      "accept_values": ["United States", "Canada"],
      "comment": "Outside target geography"
    },
    {
      "id": "dq_headcount",
      "rule": "Under 50 employees - cannot fund a build",
      "checkable_from": "csv",
      "column": "Employees",
      "min": 50,
      "comment": "Below minimum headcount"
    },
    {
      "id": "dq_competitor",
      "rule": "Sells AI development services themselves",
      "checkable_from": "crawl",
      "evidence": "services or solutions page offers AI/ML development to clients"
    }
  ],

  "fit_signals": [
    {"id": "f_regulated", "signal": "Operates in a regulated vertical - fintech, insurance, healthcare", "weight": "strong"},
    {"id": "f_own_product", "signal": "Ships its own software product, not just services", "weight": "strong"},
    {"id": "f_data_heavy", "signal": "Describes manual document, claims or compliance workload", "weight": "moderate"}
  ],

  "antifit_signals": [
    {"id": "a_agency", "signal": "Marketing or design agency - no engineering function", "weight": "strong"},
    {"id": "a_hardware", "signal": "Pure hardware or physical products, no software surface", "weight": "moderate"}
  ],

  "momentum_signals": [
    {"id": "m_hiring_eng", "signal": "Careers page lists engineering, data or AI roles", "weight": "strong"},
    {"id": "m_funding", "signal": "Announces a recent raise, acquisition or expansion", "weight": "strong"},
    {"id": "m_ai_page", "signal": "Has a page about AI plans - actively shopping this category", "weight": "strong"},
    {"id": "m_scale", "signal": "Names enterprise customers or publishes volume metrics", "weight": "moderate"}
  ],

  "tier_definitions": {
    "Good": "Regulated-vertical company shipping its own software, with visible engineering capability",
    "Avg": "Software company but wrong vertical, or right vertical with no evidence of build capacity",
    "Unfit": "Agency, competitor, no software surface, or nothing on the site matches"
  }
}
```

## Field rules

**`checkable_from`** decides whether a disqualifier costs a fetch.

- `"csv"` — answerable from a column. Runs in `prefilter.py` before any crawl,
  which is free and instant. Needs `column` plus one of:
  `reject_values` (exact, case-insensitive) · `accept_values` (keep only these)
  · `reject_contains` (substring list) · `min` / `max` (numeric, digits parsed
  out of the cell so "50-200" and "10,000" both work).
- `"crawl"` — needs the website. Needs an `evidence` line saying what on the page
  proves it, so the judgment is repeatable rather than a vibe.

Extract every CSV-checkable rule the brief honestly supports. This is the single
biggest lever on cost and runtime.

**`weight`** is `strong` / `moderate` / `weak`. Strong signals should be things
that alone change the answer.

**The three signal lists are not interchangeable.** This is the most common way
to get the rubric wrong:

| List | Question it answers | Feeds |
|---|---|---|
| `fit_signals` | Are they the right *kind* of company? | Fitment |
| `antifit_signals` | Are they visibly the wrong kind? | Fitment |
| `momentum_signals` | Is *now* the right time to reach them? | Ranking |

A momentum signal must be about timing or buying readiness — hiring, funding,
launches, a page about the problem you solve. "Is a fintech" is a fit signal, not
momentum, no matter how well it matches.

If `momentum_signals` is empty, Ranking degenerates into a copy of Fitment and
the priority column becomes useless. Every brief has an answer to question 5;
if the user's was thin, ask a follow-up rather than leaving this list empty.

## Writing signals that survive contact with a website

Every signal must be **checkable against marketing copy**, because that is all
the crawl returns.

| Not checkable | Checkable |
|---|---|
| "Well-funded" | "Site mentions a funding round, investors, or 'backed by'" |
| "Growing fast" | "Careers page lists 5+ open roles" |
| "Innovative" | "Publishes an engineering blog or product changelog" |
| "Decision-maker is technical" | "Site has developer docs or an API reference" |
| "Good budget" | "Names enterprise logos, or lists pricing above $X" |

Anything requiring data the crawl cannot see — revenue, headcount, tech stack,
intent data — belongs in a `csv` disqualifier if a column carries it, or nowhere.

## Confirming with the user

Present it as prose, not JSON. Name what you inferred:

> Here's how I read the brief:
>
> **Automatic no:** outside US/Canada · under 50 staff · anyone selling AI dev services
> **Good fit:** regulated vertical, ships its own software, visible engineering team
> **Average:** software company in the wrong vertical, or right vertical with no build capacity
> **Call first:** hiring engineers, recently raised, or already has an AI page
>
> Two things I inferred rather than were told: the 50-employee floor (you said
> "not tiny startups"), and treating agencies as unfit rather than average. Both
> right?

Wait for a yes. Then crawl.
