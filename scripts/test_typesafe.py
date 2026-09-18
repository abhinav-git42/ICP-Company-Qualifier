"""Offline tests for the TypeSafe judge.

No network and no key: the API is replaced with canned answers in TypeSafe's
documented response shape. These assert the parts we own -- the questions built
from a rubric, the scoring-guide rules in decide(), the request we send, and the
decide -> comment -> finalize -> merge round trip. Whether TypeSafe answers well
is a different question, and compare_judges.py is how you check it.

Run: python scripts/test_typesafe.py
"""

import contextlib
import csv
import io
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import merge_results  # noqa: E402
import next_batch  # noqa: E402
import typesafe_judge as tj  # noqa: E402
from fetchers._json_http import ApiError  # noqa: E402
from fetchers.base import enable_utf8_stdout, jsonl_append, jsonl_read  # noqa: E402

RESULTS = []

RUBRIC = {
    "campaign": "selftest",
    "offering_summary": "We build AI automation for regulated software companies.",
    "disqualifiers": [
        {"id": "dq_geo", "rule": "Outside North America", "checkable_from": "csv",
         "column": "Country", "accept_values": ["United States", "Canada"]},
        {"id": "dq_competitor", "rule": "Sells AI development services themselves",
         "checkable_from": "crawl",
         "evidence": "services page offers AI/ML development to clients"},
    ],
    "fit_signals": [
        {"id": "f_regulated", "signal": "Operates in fintech, insurance or healthcare",
         "weight": "strong"},
        {"id": "f_data_heavy", "signal": "Describes manual document workload",
         "weight": "moderate"},
    ],
    "antifit_signals": [
        {"id": "a_agency", "signal": "Marketing or design agency", "weight": "strong"},
        {"id": "a_hardware", "signal": "Pure hardware products", "weight": "moderate"},
    ],
    "momentum_signals": [
        {"id": "m_hiring", "signal": "Careers page lists engineering roles",
         "weight": "strong"},
        {"id": "m_scale", "signal": "Names enterprise customers", "weight": "moderate"},
    ],
    "tier_definitions": {"Good": "Regulated software company",
                         "Avg": "Software, wrong vertical",
                         "Unfit": "Agency or no software"},
}


def check(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print("%-4s %-46s %s" % ("PASS" if ok else "FAIL", name, detail))


def answers(nouls=None, probs=None, confidence=0.8):
    """Every question answered: nouls default to a clear no."""
    out = {}
    for qid in tj.build_questions(RUBRIC):
        if qid != "fitment":
            out[qid] = {"type": "noul", "noul": (nouls or {}).get(qid, 0.05)}
    probs = probs or {"Good": 0.8, "Avg": 0.15, "Unfit": 0.05}
    out["fitment"] = {"type": "choice", "choice": max(probs, key=probs.get),
                      "probabilities": probs, "confidence": confidence}
    return out


# --- 1. questions ----------------------------------------------------------

def test_questions():
    q = tj.build_questions(RUBRIC)
    check("questions:csv disqualifier not asked", "dq__dq_geo" not in q)
    check("questions:crawl disqualifier asked", q.get("dq__dq_competitor", {}).get("type") == "noul")
    want = {"fit__f_regulated", "fit__f_data_heavy", "anti__a_agency",
            "anti__a_hardware", "mom__m_hiring", "mom__m_scale", "fitment"}
    check("questions:every signal has a question", want <= set(q), "%d total" % len(q))
    nouls = [v for v in q.values() if v["type"] == "noul"]
    check("questions:nouls carry true/false criteria",
          all(set(v["criteria"]) == {"true", "false"} for v in nouls))
    check("questions:nouls name the website state",
          all("`website`" in v["instructions"] for v in nouls))
    fit = q["fitment"]
    check("questions:fitment is a Good/Avg/Unfit choice",
          fit["type"] == "choice" and list(fit["criteria"]) == list(tj.GRADES))
    check("questions:fitment carries the offering",
          "regulated software" in fit["instructions"]["what_we_sell"])
    check("questions:anti-fit listed under Unfit",
          "Marketing or design agency" in fit["criteria"]["Unfit"].get("look_for", []))
    check("questions:tier definition is the Good rule",
          fit["criteria"]["Good"]["definition"] == "Regulated software company")
    empty = [k for k, v in fit["criteria"].items() for kk, vv in v.items() if not vv]
    check("questions:no empty criteria fields", not empty, str(empty))
    bare = dict(RUBRIC, antifit_signals=[], disqualifiers=[], tier_definitions={})
    qb = tj.build_questions(bare)
    check("questions:no anti-fit -> no Unfit look_for",
          "look_for" not in qb["fitment"]["criteria"]["Unfit"])
    check("questions:missing tiers use scoring-guide wording",
          qb["fitment"]["criteria"]["Avg"]["definition"] == tj.DEFAULT_TIERS["Avg"])


# --- 2. the decision rules -------------------------------------------------

def test_decide():
    d = tj.decide(RUBRIC, answers({"dq__dq_competitor": 0.9, "mom__m_hiring": 0.9}))
    check("decide:disqualifier -> Unfit/3",
          (d["fitment"], d["ranking"], d["disqualified_by"]) == ("Unfit", 3, "dq_competitor"))
    d = tj.decide(RUBRIC, answers({"dq__dq_competitor": 0.6}))
    check("decide:disqualifier below its bar is ignored", d["fitment"] == "Good")

    d = tj.decide(RUBRIC, answers({"fit__f_regulated": 0.9, "mom__m_scale": 0.7}))
    check("decide:Good + momentum -> 1", (d["fitment"], d["ranking"]) == ("Good", 1))
    d = tj.decide(RUBRIC, answers({"fit__f_regulated": 0.9}))
    check("decide:Good, no momentum -> 2", (d["fitment"], d["ranking"]) == ("Good", 2))

    avg = {"Good": 0.1, "Avg": 0.8, "Unfit": 0.1}
    d = tj.decide(RUBRIC, answers({"mom__m_hiring": 0.8}, avg))
    check("decide:Avg + strong momentum -> 2", (d["fitment"], d["ranking"]) == ("Avg", 2))
    d = tj.decide(RUBRIC, answers({"mom__m_scale": 0.8}, avg))
    check("decide:Avg + moderate momentum -> 3", (d["fitment"], d["ranking"]) == ("Avg", 3))

    torn = {"Good": 0.45, "Avg": 0.40, "Unfit": 0.15}
    d = tj.decide(RUBRIC, answers({"fit__f_regulated": 0.9}, torn))
    check("decide:torn Good/Avg takes Avg", d["fitment"] == "Avg", d["why"])
    clear = {"Good": 0.6, "Avg": 0.3, "Unfit": 0.1}
    d = tj.decide(RUBRIC, answers({"fit__f_regulated": 0.9}, clear))
    check("decide:clear Good stays Good", d["fitment"] == "Good")
    torn_low = {"Good": 0.1, "Avg": 0.46, "Unfit": 0.44}
    d = tj.decide(RUBRIC, answers(None, torn_low))
    check("decide:torn Avg/Unfit takes Unfit", (d["fitment"], d["ranking"]) == ("Unfit", 3))

    d = tj.decide(RUBRIC, answers({"fit__f_regulated": 0.9, "anti__a_agency": 0.8,
                                   "mom__m_hiring": 0.9}))
    check("decide:strong anti-fit -> Unfit/3", (d["fitment"], d["ranking"]) == ("Unfit", 3))
    d = tj.decide(RUBRIC, answers({"fit__f_regulated": 0.9, "anti__a_hardware": 0.8}))
    check("decide:moderate anti-fit alone does not override", d["fitment"] == "Good")

    d = tj.decide(RUBRIC, answers({"fit__f_regulated": 0.9}, confidence=0.3))
    check("decide:low confidence flagged",
          any("confidence" in r for r in d["needs_review"]), str(d["needs_review"]))
    d = tj.decide(RUBRIC, answers())
    check("decide:Good with no fit signal flagged",
          any("no fit signal" in r for r in d["needs_review"]))
    d = tj.decide(RUBRIC, answers({"fit__f_regulated": 0.9}))
    check("decide:confident Good with signal not flagged", not d["needs_review"])

    a = answers({"fit__f_regulated": 0.9})
    a["fitment"]["choice"] = "something-else"
    d = tj.decide(RUBRIC, a)
    check("decide:unknown choice falls back to argmax", d["fitment"] == "Good")

    a = answers()
    del a["mom__m_hiring"]
    try:
        tj.decide(RUBRIC, a)
        check("decide:missing answer raises", False)
    except KeyError:
        check("decide:missing answer raises", True)

    d = tj.decide(RUBRIC, answers({"fit__f_regulated": 0.93, "mom__m_hiring": 0.81}))
    kinds = {(s["kind"], s["id"]) for s in d["signals_present"]}
    check("decide:signals_present lists what was found",
          kinds == {("fit", "f_regulated"), ("momentum", "m_hiring")}, str(kinds))
    check("decide:every noul probability kept", len(d["signals"]) == len(answers()) - 1)


# --- 3. the request --------------------------------------------------------

def test_request():
    seen = {}

    def fake_post(url, payload, headers=None, timeout=0):
        seen.update(url=url, payload=payload, headers=headers)
        return {"model": "jev-1.13.0", "answers": answers(), "usage": {"input_tokens": 900}}

    real = tj.post_json
    tj.post_json = fake_post
    try:
        q = tj.build_questions(RUBRIC)
        state = tj.build_state("DOMAIN: acme.com\nHOME: claims software", "Company=Acme")
        resp = tj.ask(state, q, "k-123", "jev-latest")
    finally:
        tj.post_json = real
    check("request:endpoint", seen["url"] == "https://api.typesafe.ai/v1/systemone", seen["url"])
    check("request:bearer auth", seen["headers"].get("Authorization") == "Bearer k-123")
    p = seen["payload"]
    check("request:state/model/questions", set(p) == {"state", "model", "questions"})
    check("request:state names website + lead_record",
          set(p["state"]) == {"website", "lead_record"})
    check("request:no lead_record when CSV has none",
          set(tj.build_state("x", "")) == {"website"})
    check("request:response returned", resp["model"] == "jev-1.13.0")

    def partial_post(url, payload, headers=None, timeout=0):
        a = answers()
        a.pop("fitment")
        return {"answers": a}

    tj.post_json = partial_post
    try:
        tj.ask({"website": "x"}, tj.build_questions(RUBRIC), "k", "m")
        check("request:missing answers raise", False)
    except ApiError as exc:
        check("request:missing answers raise", "missing" in str(exc))
    finally:
        tj.post_json = real

    os.environ["TYPESAFE_BASE_URL"] = "https://example.test/"
    check("request:TYPESAFE_BASE_URL honoured",
          tj.api_url() == "https://example.test/v1/systemone")
    os.environ.pop("TYPESAFE_BASE_URL")


# --- 4. the round trip -----------------------------------------------------

SLUG = "_typesafe_selftest"


def run(fn, argv):
    old = sys.argv
    sys.argv = ["x"] + argv
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            try:
                code = fn()
            except SystemExit as exc:
                code = exc.code
    finally:
        sys.argv = old
    return code, buf.getvalue()


def make_campaign():
    cdir = tj.campaign_dir(SLUG)
    shutil.rmtree(cdir, ignore_errors=True)
    os.makedirs(os.path.join(cdir, "input"))
    csv_path = os.path.join(cdir, "input", "leads.csv")
    with open(csv_path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["Company", "Website", "Country"])
        w.writerow(["Acme Claims", "acme.com", "United States"])
        w.writerow(["Brandly", "brandly.com", "Canada"])
        w.writerow(["Gone", "gone.com", "Canada"])
    with open(os.path.join(cdir, "rubric.json"), "w", encoding="utf-8") as fh:
        json.dump(RUBRIC, fh)
    with open(os.path.join(cdir, "campaign.json"), "w", encoding="utf-8") as fh:
        json.dump({"input_csv": csv_path, "url_column": "Website"}, fh)
    for dom, ok in (("acme.com", True), ("brandly.com", True), ("gone.com", False)):
        jsonl_append(os.path.join(cdir, "crawl_results.jsonl"), {
            "domain": dom, "gate_ok": ok, "status": "ok" if ok else "dns_fail",
            "gate_reason": "pass" if ok else "status:dns_fail",
            "source_tier": "stdlib", "chars": 900,
            "digest": "DOMAIN: %s\nHOME: sample copy" % dom})
    return cdir


def fake_api(url, payload, headers=None, timeout=0):
    agency = "brandly" in payload["state"]["website"]
    a = answers({"anti__a_agency": 0.9} if agency else
                {"fit__f_regulated": 0.9, "mom__m_hiring": 0.85})
    return {"model": "jev-1.13.0", "answers": a, "usage": {"input_tokens": 1000}}


def test_round_trip():
    cdir = make_campaign()
    real = tj.post_json
    key = os.environ.pop("TYPESAFE_API_KEY", None)
    try:
        code, out = run(tj.main, ["--campaign", SLUG, "--set-judge", "typesafe"])
        cfg = tj.read_config(SLUG)
        check("trip:set-judge without key falls back", code == 2 and cfg["judge"] == "claude")

        code, out = run(tj.main, ["--campaign", SLUG])
        check("trip:decide refuses a claude campaign", "not typesafe" in str(code))

        os.environ["TYPESAFE_API_KEY"] = "k-test"
        code, out = run(tj.main, ["--campaign", SLUG, "--set-judge", "typesafe"])
        check("trip:set-judge with key", code == 0 and tj.read_config(SLUG)["judge"] == "typesafe")

        code, out = run(tj.main, ["--campaign", SLUG, "--dry-run"])
        check("trip:dry run sends nothing",
              code == 0 and not os.path.exists(os.path.join(cdir, tj.DECISIONS)))
        check("trip:dry run shows estimate", "estimate" in out and "$" in out)

        def unauthorized(url, payload, headers=None, timeout=0):
            raise ApiError("HTTP 401 bad key", status=401)

        tj.post_json = unauthorized
        code, out = run(tj.main, ["--campaign", SLUG, "--workers", "1"])
        check("trip:401 stops the run", code == 1 and "rejected" in out)
        check("trip:401 writes nothing", not jsonl_read(os.path.join(cdir, tj.DECISIONS)))

        tj.post_json = fake_api
        code, out = run(tj.main, ["--campaign", SLUG])
        dec = tj.latest_by_domain(jsonl_read(os.path.join(cdir, tj.DECISIONS)))
        check("trip:decides gate-pass domains only", set(dec) == {"acme.com", "brandly.com"})
        check("trip:acme Good/1", (dec["acme.com"]["fitment"], dec["acme.com"]["ranking"])
              == ("Good", 1))
        check("trip:agency Unfit/3", (dec["brandly.com"]["fitment"],
                                      dec["brandly.com"]["ranking"]) == ("Unfit", 3))
        check("trip:decision records model + tokens",
              dec["acme.com"]["model"] == "jev-1.13.0" and dec["acme.com"]["input_tokens"] == 1000)

        code, out = run(tj.main, ["--campaign", SLUG])
        check("trip:re-run decides nothing new", "0 pending" in out)

        code, out = run(next_batch.main, ["--campaign", SLUG])
        check("trip:claude batch refused on typesafe campaign", "--comments" in out)

        code, out = run(next_batch.main, ["--campaign", SLUG, "--comments"])
        check("trip:comment batch shows verdict", "VERDICT: Good / 1" in out
              and "VERDICT: Unfit / 3" in out)
        check("trip:comment batch shows signals", "Operates in fintech" in out)

        jsonl_append(os.path.join(cdir, tj.COMMENTS),
                     {"domain": "acme.com", "comments": "Claims software for insurers",
                      "evidence": "HOME: claims", "fitment": "Unfit", "ranking": 3})
        code, out = run(tj.main, ["--campaign", SLUG, "--finalize"])
        check("trip:finalize waits for missing comments", code == 1 and "1 decided" in out)

        jsonl_append(os.path.join(cdir, tj.COMMENTS),
                     {"domain": "brandly.com", "comments": "Brand studio - no software",
                      "disagree": True})
        code, out = run(tj.main, ["--campaign", SLUG, "--finalize"])
        assessed = tj.latest_by_domain(jsonl_read(os.path.join(cdir, "assessments.jsonl")))
        check("trip:finalize writes the rest", code == 0 and "wrote 1" in out)
        check("trip:comment cannot move a grade",
              (assessed["acme.com"]["fitment"], assessed["acme.com"]["ranking"]) == ("Good", 1))
        check("trip:judge + disagree recorded", assessed["acme.com"]["judge"] == "typesafe"
              and assessed["brandly.com"]["disagree"] is True)

        run(__import__("resolve_failed").main, ["--campaign", SLUG, "--apply"])
        code, out = run(merge_results.main, ["--campaign", SLUG])
        out_csv = os.path.join(cdir, "output", "leads_qualified.csv")
        with open(out_csv, encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        check("trip:merge keeps every row", len(rows) == 3)
        check("trip:merge columns unchanged", list(rows[0])[-4:] ==
              ["Fitment", "Ranking", "Comments", "Crawl_Status"])
        check("trip:merge carries TypeSafe grades",
              (rows[0]["Fitment"], rows[0]["Ranking"]) == ("Good", "1"))
        check("trip:merge summary names judges", "typesafe" in out and "rule" in out
              and "disagreeing with TypeSafe   : 1" in out)
    finally:
        tj.post_json = real
        os.environ.pop("TYPESAFE_API_KEY", None)
        if key is not None:
            os.environ["TYPESAFE_API_KEY"] = key
        shutil.rmtree(cdir, ignore_errors=True)


def main():
    enable_utf8_stdout()
    for test in (test_questions, test_decide, test_request, test_round_trip):
        print("\n--- %s" % test.__name__)
        test()
    failed = [r for r in RESULTS if not r[1]]
    print("\n%d/%d passed" % (len(RESULTS) - len(failed), len(RESULTS)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
