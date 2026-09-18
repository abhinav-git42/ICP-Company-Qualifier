"""Step 8B: TypeSafe decides Fitment and Ranking; Claude writes the comments.

Used only when a campaign's judge is "typesafe". The judge is chosen per
campaign at /new and saved to campaign.json; anything else means Claude, which
reads each digest and writes the whole verdict itself (SKILL.md step 8A).

  1. decide    One TypeSafe request per domain. Every rubric signal becomes a
               yes/no question and the grade a Good/Avg/Unfit choice. decide()
               turns the answers into Fitment and Ranking using the rules in
               references/scoring-guide.md.        -> typesafe_decisions.jsonl
  2. comment   Claude reads `next_batch.py --comments` and writes one comment
               per domain. It never edits a decision.        -> comments.jsonl
  3. finalize  Joins decision + comment.                    -> assessments.jsonl
               merge_results.py then works exactly as it does for a Claude run.

  python scripts/typesafe_judge.py --status                          is a key set?
  python scripts/typesafe_judge.py --campaign acme --set-judge typesafe
  python scripts/typesafe_judge.py --campaign acme --dry-run         questions + cost, no calls
  python scripts/typesafe_judge.py --campaign acme                   decide every pending domain
  python scripts/typesafe_judge.py --campaign acme --finalize        once the comments exist
  python scripts/typesafe_judge.py --campaign acme --all             also decide domains Claude
                                                                     already judged, for
                                                                     compare_judges.py
"""

import argparse
import hashlib
import json
import os
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fetchers.base import enable_utf8_stdout, jsonl_append, jsonl_read  # noqa: E402
from fetchers._json_http import ApiError, post_json, with_retries  # noqa: E402
from next_batch import load_context  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# --- decision thresholds ---------------------------------------------------
# Starting points, not truths. TypeSafe's probabilities are calibrated, but
# where to draw each line depends on your leads and on what a wrong call costs.
# Tune them against a campaign you have already judged (compare_judges.py).
TYPESAFE = {
    "model": "jev-latest",        # pin e.g. "jev-1.13.0" once thresholds are tuned
    "signal_present": 0.5,        # a fit / anti-fit / momentum answer at or above this counts
    "disqualifier_fires": 0.7,    # higher bar: a false positive throws away a good lead
    "torn_margin": 0.15,          # top grade beats the next-lower one by less -> take the lower
    "review_confidence": 0.5,     # fitment confidence below this is flagged for QA
    "workers": 4,                 # requests in flight; the account allows 1,200/min
    "usd_per_mtok": 0.042,        # jev-1.13 list price, input tokens only
    "max_spend_usd": 5.0,         # refuse a bigger run without --confirm-spend
}

JUDGES = ("claude", "typesafe")
GRADES = ("Good", "Avg", "Unfit")       # best first; "lower" means later in this tuple
DECISIONS = "typesafe_decisions.jsonl"
COMMENTS = "comments.jsonl"

# A bad key, no credit, or a malformed question fails every request the same
# way. Stop at the first one instead of burning through the whole list.
FATAL_STATUSES = {401, 402, 403, 422}

NOT_SHOWN = "The website does not show this, or does not say"


# --- environment -----------------------------------------------------------
# fetchers.base loads .env into os.environ at import, so these see it too.

def api_key():
    return os.environ.get("TYPESAFE_API_KEY", "").strip()


def model_name():
    return os.environ.get("TYPESAFE_DEFAULT_MODEL", "").strip() or TYPESAFE["model"]


def api_url():
    base = os.environ.get("TYPESAFE_BASE_URL", "").strip() or "https://api.typesafe.ai"
    return base.rstrip("/") + "/v1/systemone"


# --- campaign files --------------------------------------------------------

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


def latest_by_domain(rows):
    """Last line wins, so a re-decided or re-commented domain needs no cleanup."""
    out = {}
    for r in rows:
        if r.get("domain"):
            out[r["domain"]] = r
    return out


def rubric_sha(rubric):
    blob = json.dumps(rubric, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha1(blob).hexdigest()[:12]


# --- questions -------------------------------------------------------------

def crawl_disqualifiers(rubric):
    return [d for d in (rubric.get("disqualifiers") or [])
            if d.get("checkable_from") == "crawl" and d.get("id")]


def signals(rubric, key):
    return [s for s in (rubric.get(key) or []) if s.get("id") and s.get("signal")]


def _noul(instructions, yes):
    return {"type": "noul", "instructions": instructions,
            "criteria": {"true": yes, "false": NOT_SHOWN}}


# Used only when a rubric leaves a tier undefined. Worded as scoring-guide.md.
DEFAULT_TIERS = {
    "Good": "A clear match on one or more strong fit signals, visible on the site",
    "Avg": "A partial match, or plausible but not confirmed by the site",
    "Unfit": "An anti-fit sign applies, or nothing on the site matches",
}


def fitment_question(rubric):
    tiers = rubric.get("tier_definitions") or {}
    fit = signals(rubric, "fit_signals")
    strong = [s["signal"] for s in fit if s.get("weight") == "strong"] or \
             [s["signal"] for s in fit]
    anti = [s["signal"] for s in signals(rubric, "antifit_signals")]

    # The definition is the rule. Signals are listed as things to look for, not
    # as "any one is enough" -- a definition that needs two of them together
    # would otherwise contradict its own criteria.
    good = {"definition": tiers.get("Good") or DEFAULT_TIERS["Good"],
            "look_for": strong}
    avg = {"definition": tiers.get("Avg") or DEFAULT_TIERS["Avg"],
           "also": "A real company whose site says too little to tell"}
    unfit = {"definition": tiers.get("Unfit") or DEFAULT_TIERS["Unfit"],
             "look_for": anti,
             "also": "Nothing on the site matches the kind of customer we sell to"}

    def clean(d):
        return {k: v for k, v in d.items() if v}

    return {
        "type": "choice",
        "instructions": clean({
            "question": "Which grade best describes how well this company fits "
                        "the kind of customer we sell to?",
            "what_we_sell": rubric.get("offering_summary", ""),
            "judge_on": "What kind of company this is, as shown in `website`. "
                        "Hiring, funding and other timing signs do not change the grade.",
            "tiebreaker": "If present, `lead_record` may settle a case the "
                          "website leaves unclear.",
        }),
        "criteria": {"Good": clean(good), "Avg": clean(avg), "Unfit": clean(unfit)},
    }


def build_questions(rubric):
    """rubric.json -> TypeSafe questions, all asked in one request per domain.

    Question ids never reach the model, so each question carries its full
    meaning in its own text. Every signal is its own yes/no question: one
    narrow judgment each, combined in decide() rather than in a prompt.
    """
    q = {}
    for d in crawl_disqualifiers(rubric):
        proof = d.get("evidence") or "the website clearly shows it"
        q["dq__" + d["id"]] = _noul(
            'Does `website` show that this is true of the company: "%s"?' % d.get("rule", ""),
            "The website shows it. What counts as proof: %s" % proof)
    for s in signals(rubric, "fit_signals"):
        q["fit__" + s["id"]] = _noul(
            'Does `website` show that this is true of the company: "%s"?' % s["signal"],
            "The website clearly shows this")
    for s in signals(rubric, "antifit_signals"):
        q["anti__" + s["id"]] = _noul(
            'Does `website` show that this is true of the company: "%s"?' % s["signal"],
            "The website clearly shows this")
    for s in signals(rubric, "momentum_signals"):
        q["mom__" + s["id"]] = _noul(
            'Does `website` show this sign that now is a good time to reach the '
            'company: "%s"?' % s["signal"],
            "The website clearly shows this sign")
    q["fitment"] = fitment_question(rubric)
    return q


def build_state(digest, context=""):
    state = {"website": digest or ""}
    if context:
        state["lead_record"] = context
    return state


def estimate_tokens(state, questions):
    """Rough input-token count (~4 chars per token). The state is read once per
    request however many questions ride on it."""
    return (len(json.dumps(state, ensure_ascii=False)) +
            len(json.dumps(questions, ensure_ascii=False))) // 4


# --- the decision ----------------------------------------------------------

def decide(rubric, answers, t=None):
    """TypeSafe's answers -> Fitment and Ranking.

    This is references/scoring-guide.md in code, so the rules stay explicit and
    tunable instead of living in a prompt. Raises KeyError if an answer the
    rubric needs is missing.
    """
    t = t or TYPESAFE
    seen = []

    def hits(kind, items, prefix, threshold):
        found = []
        for s in items:
            p = float(answers[prefix + s["id"]]["noul"])
            if p >= threshold:
                found.append(s)
                seen.append({"kind": kind, "id": s["id"],
                             "text": s.get("signal") or s.get("rule") or "",
                             "p": round(p, 3)})
        return found

    dq_hits = hits("disqualifier", crawl_disqualifiers(rubric), "dq__",
                   t["disqualifier_fires"])
    fit_hits = hits("fit", signals(rubric, "fit_signals"), "fit__", t["signal_present"])
    anti_hits = hits("anti-fit", signals(rubric, "antifit_signals"), "anti__",
                     t["signal_present"])
    mom_hits = hits("momentum", signals(rubric, "momentum_signals"), "mom__",
                    t["signal_present"])

    fa = answers["fitment"]
    probs = {g: float((fa.get("probabilities") or {}).get(g, 0.0)) for g in GRADES}
    top = fa.get("choice") if fa.get("choice") in GRADES else max(GRADES, key=probs.get)
    confidence = float(fa.get("confidence") or 0.0)
    grade = top
    why = ["fitment %s p=%.2f" % (top, probs[top])]

    # "When genuinely torn between two grades, take the lower one."
    i = GRADES.index(top)
    if i < len(GRADES) - 1:
        lower = GRADES[i + 1]
        if probs[top] - probs[lower] < t["torn_margin"]:
            grade = lower
            why.append("torn with %s p=%.2f, took the lower grade" % (lower, probs[lower]))

    # A strong anti-fit signal alone changes the answer.
    strong_anti = [s for s in anti_hits if s.get("weight") == "strong"]
    if strong_anti and grade != "Unfit":
        grade = "Unfit"
        why.append("strong anti-fit %s" % strong_anti[0]["id"])

    if dq_hits:
        # Disqualifiers first: no partial credit for being otherwise interesting.
        grade, ranking = "Unfit", 3
        why = ["disqualified by %s" % dq_hits[0]["id"]]
    elif grade == "Unfit":
        ranking = 3
    elif grade == "Good":
        ranking = 1 if mom_hits else 2
    else:
        ranking = 2 if any(s.get("weight") == "strong" for s in mom_hits) else 3
    if not dq_hits and grade != "Unfit":
        why.append("momentum " + ", ".join(s["id"] for s in mom_hits)
                   if mom_hits else "no momentum signal")

    review = []
    if not dq_hits:
        if confidence < t["review_confidence"]:
            review.append("low fitment confidence %.2f" % confidence)
        if grade == "Good" and not fit_hits:
            review.append("Good with no fit signal found on the site")

    return {
        "fitment": grade,
        "ranking": ranking,
        "why": "; ".join(why),
        "disqualified_by": dq_hits[0]["id"] if dq_hits else None,
        "fitment_probabilities": {g: round(probs[g], 3) for g in GRADES},
        "fitment_confidence": round(confidence, 3),
        "signals_present": seen,
        "signals": {qid: round(float(a["noul"]), 3) for qid, a in answers.items()
                    if isinstance(a, dict) and a.get("type") == "noul"},
        "needs_review": review,
    }


# --- the API call ----------------------------------------------------------

def ask(state, questions, key, model):
    payload = {"state": state, "model": model, "questions": questions}

    def call():
        resp = post_json(api_url(), payload,
                         headers={"Authorization": "Bearer " + key}, timeout=60)
        missing = [q for q in questions if q not in (resp.get("answers") or {})]
        if missing:
            raise ApiError("response missing answers: %s" % ", ".join(missing[:3]),
                           retryable=True)
        return resp

    return with_retries(call, label="typesafe")


# --- commands --------------------------------------------------------------

def cmd_status(slug):
    key = api_key()
    print("TypeSafe key  : %s" % ("set" if key else
                                  "not set -- add TYPESAFE_API_KEY to .env (python setup.py)"))
    print("model         : %s" % model_name())
    if not slug:
        return 0
    cdir = campaign_dir(slug)
    cfg = read_config(slug)
    decided = latest_by_domain(jsonl_read(os.path.join(cdir, DECISIONS)))
    commented = latest_by_domain(jsonl_read(os.path.join(cdir, COMMENTS)))
    done = {a.get("domain") for a in jsonl_read(os.path.join(cdir, "assessments.jsonl"))
            if a.get("domain")}
    print("campaign judge: %s" % cfg.get("judge", "claude (not set)"))
    print("decisions     : %d decided | %d commented | %d finalized"
          % (len(decided), len(set(decided) & set(commented)), len(set(decided) & done)))
    return 0


def cmd_set_judge(slug, judge):
    cdir = campaign_dir(slug)
    if not os.path.isdir(cdir):
        sys.exit("no campaign folder at %s" % cdir)
    cfg = read_config(slug)
    note = ""
    if judge == "typesafe" and not api_key():
        judge = "claude"
        note = "TYPESAFE_API_KEY is not set -- falling back to Claude as judge."
    cfg["judge"] = judge
    write_config(slug, cfg)
    if note:
        print("WARNING: " + note)
    print("campaign %s judge: %s" % (slug, judge))
    return 2 if note else 0


def cmd_decide(args):
    slug = args.campaign
    cdir = campaign_dir(slug)
    cfg = read_config(slug)
    rubric_path = os.path.join(cdir, "rubric.json")
    if not os.path.exists(rubric_path):
        sys.exit("missing %s -- build the rubric first (SKILL.md step 2)" % rubric_path)
    with open(rubric_path, "r", encoding="utf-8") as fh:
        rubric = json.load(fh)

    if cfg.get("judge") != "typesafe" and not args.all:
        sys.exit("campaign judge is %s, not typesafe. Set it with --set-judge typesafe,\n"
                 "or pass --all to decide alongside Claude for compare_judges.py."
                 % cfg.get("judge", "claude"))

    results = jsonl_read(os.path.join(cdir, "crawl_results.jsonl"))
    passed = latest_by_domain([r for r in results if r.get("gate_ok")])
    done = {a.get("domain") for a in jsonl_read(os.path.join(cdir, "assessments.jsonl"))
            if a.get("domain")}
    out = os.path.join(cdir, DECISIONS)
    decided = latest_by_domain(jsonl_read(out))
    sha = rubric_sha(rubric)
    stale = sum(1 for r in decided.values() if r.get("rubric_sha") != sha)

    pending = [r for d, r in passed.items()
               if d not in decided and (args.all or d not in done)]
    if args.limit:
        pending = pending[:args.limit]

    questions = build_questions(rubric)
    ctx, _ = load_context(slug)
    states = {r["domain"]: build_state(r.get("digest"), ctx.get(r["domain"]))
              for r in pending}
    est_tokens = sum(estimate_tokens(s, questions) for s in states.values())
    est_usd = est_tokens * TYPESAFE["usd_per_mtok"] / 1e6
    model = model_name()

    print("campaign   : %s" % slug)
    print("judge      : TypeSafe (%s) decides, Claude comments" % model)
    print("questions  : %d per domain (%d disqualifier, %d fit, %d anti-fit, "
          "%d momentum, 1 fitment)"
          % (len(questions), len(crawl_disqualifiers(rubric)),
             len(signals(rubric, "fit_signals")), len(signals(rubric, "antifit_signals")),
             len(signals(rubric, "momentum_signals"))))
    print("domains    : %d gate-pass | %d already decided | %d pending"
          % (len(passed), len(decided), len(pending)))
    print("estimate   : ~%s input tokens, ~$%.2f" % (format(est_tokens, ","), est_usd))
    if stale:
        print("WARNING    : %d decisions were made with an older rubric. Delete %s and\n"
              "             %s to re-decide them." % (stale, DECISIONS, COMMENTS))
    if not signals(rubric, "momentum_signals"):
        print("WARNING    : no momentum_signals -- Ranking will copy Fitment.")

    if args.dry_run:
        print("\n--- questions sent with every domain ---")
        print(json.dumps(questions, indent=2, ensure_ascii=False))
        print("\nDry run. Nothing was sent.")
        return 0
    if not pending:
        print("\nNothing pending. Next: python scripts/next_batch.py --campaign %s --comments"
              % slug)
        return 0

    key = api_key()
    if not key:
        print("\nTYPESAFE_API_KEY is not set. Add it with 'python setup.py', or run\n"
              "  python scripts/typesafe_judge.py --campaign %s --set-judge claude\n"
              "and continue with Claude as judge (SKILL.md step 8A)." % slug)
        return 2
    if est_usd > TYPESAFE["max_spend_usd"] and not args.confirm_spend:
        print("\nEstimate is above $%.2f. Re-run with --confirm-spend to proceed."
              % TYPESAFE["max_spend_usd"])
        return 2

    by_domain = {r["domain"]: r for r in pending}
    written, failed, tokens = 0, 0, 0
    fitment, ranking, review = Counter(), Counter(), 0
    fatal = None
    t0 = time.time()
    print()
    pool = ThreadPoolExecutor(max_workers=max(1, args.workers))
    try:
        futures = {pool.submit(ask, states[d], questions, key, model): d for d in by_domain}
        for fut in as_completed(futures):
            d = futures[fut]
            try:
                resp = fut.result()
                verdict = decide(rubric, resp["answers"])
            except ApiError as exc:
                if exc.status in FATAL_STATUSES:
                    fatal = exc
                    break
                failed += 1
                print("  FAIL  %-32s %s" % (d, str(exc)[:80]))
                continue
            except (KeyError, TypeError, ValueError) as exc:
                failed += 1
                print("  FAIL  %-32s malformed answer: %s" % (d, str(exc)[:60]))
                continue
            r = by_domain[d]
            used = int((resp.get("usage") or {}).get("input_tokens") or 0)
            tokens += used
            rec = {"domain": d}
            rec.update(verdict)
            rec.update({
                "model": resp.get("model") or model,
                "input_tokens": used,
                "crawl_status": r.get("status"),
                "source_tier": r.get("source_tier"),
                "rubric_sha": sha,
                "decided_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            })
            jsonl_append(out, rec)
            written += 1
            fitment[verdict["fitment"]] += 1
            ranking[verdict["ranking"]] += 1
            review += 1 if verdict["needs_review"] else 0
            if written % 25 == 0:
                print("  ... %d/%d decided" % (written, len(pending)))
    finally:
        pool.shutdown(wait=True, cancel_futures=True)

    print("=" * 68)
    print("TYPESAFE DECISIONS  --  %d written, %d failed, %.0fs"
          % (written, failed, time.time() - t0))
    print("=" * 68)
    if written:
        print("  tokens   : %s input, ~$%.3f"
              % (format(tokens, ","), tokens * TYPESAFE["usd_per_mtok"] / 1e6))
        print("  fitment  : " + "  ".join("%s %d" % (g, fitment[g]) for g in GRADES))
        print("  ranking  : " + "  ".join("%d: %d" % (k, ranking[k]) for k in (1, 2, 3)))
        print("  flagged for review: %d" % review)
    if fatal:
        print("\nSTOPPED: TypeSafe returned %s" % str(fatal)[:200])
        if fatal.status == 401:
            print("The key was rejected. Check TYPESAFE_API_KEY in .env.")
        elif fatal.status == 422:
            print("A question failed validation -- check rubric.json with --dry-run.")
        print("Decisions already written are kept; re-running resumes.")
        return 1
    if failed:
        print("\n%d domains failed and stay pending. Re-run to retry them." % failed)
    print("\nNext: python scripts/next_batch.py --campaign %s --comments --size 25" % slug)
    return 1 if failed and not written else 0


def cmd_finalize(slug):
    cdir = campaign_dir(slug)
    decisions = latest_by_domain(jsonl_read(os.path.join(cdir, DECISIONS)))
    comments = latest_by_domain(jsonl_read(os.path.join(cdir, COMMENTS)))
    out = os.path.join(cdir, "assessments.jsonl")
    done = {a.get("domain") for a in jsonl_read(out) if a.get("domain")}

    written, missing = 0, []
    for d, dec in decisions.items():
        if d in done:
            continue
        c = comments.get(d)
        if not c or not str(c.get("comments") or "").strip():
            missing.append(d)
            continue
        # Fitment and Ranking come from the decision, never from the comment
        # file, so a comment cannot move a verdict.
        jsonl_append(out, {
            "domain": d,
            "fitment": dec["fitment"],
            "ranking": dec["ranking"],
            "comments": str(c["comments"]).strip(),
            "evidence": c.get("evidence", ""),
            "crawl_status": dec.get("crawl_status"),
            "source_tier": dec.get("source_tier"),
            "judge": "typesafe",
            "model": dec.get("model"),
            "needs_review": dec.get("needs_review") or [],
            "disagree": bool(c.get("disagree")),
        })
        written += 1

    orphans = [d for d in comments if d not in decisions]
    print("wrote %d TypeSafe verdicts to %s" % (written, out))
    if missing:
        print("%d decided domains still need a comment -- run:\n"
              "  python scripts/next_batch.py --campaign %s --comments"
              % (len(missing), slug))
    if orphans:
        print("ignored %d comments with no TypeSafe decision (e.g. %s)"
              % (len(orphans), orphans[0]))
    return 1 if missing else 0


def main():
    enable_utf8_stdout()
    ap = argparse.ArgumentParser()
    ap.add_argument("--campaign")
    ap.add_argument("--status", action="store_true", help="key, campaign judge, progress")
    ap.add_argument("--set-judge", choices=JUDGES, help="record the campaign's judge")
    ap.add_argument("--dry-run", action="store_true", help="print questions + estimate only")
    ap.add_argument("--finalize", action="store_true",
                    help="join decisions and comments into assessments.jsonl")
    ap.add_argument("--all", action="store_true",
                    help="also decide domains that already have a verdict (comparison)")
    ap.add_argument("--limit", type=int, default=0, help="decide at most N domains")
    ap.add_argument("--workers", type=int, default=TYPESAFE["workers"])
    ap.add_argument("--confirm-spend", action="store_true")
    args = ap.parse_args()

    if args.status:
        return cmd_status(args.campaign)
    if not args.campaign:
        ap.error("--campaign is required")
    if args.set_judge:
        return cmd_set_judge(args.campaign, args.set_judge)
    if args.finalize:
        return cmd_finalize(args.campaign)
    return cmd_decide(args)


if __name__ == "__main__":
    sys.exit(main())
