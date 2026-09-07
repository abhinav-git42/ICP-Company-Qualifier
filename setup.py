"""One-command setup. Run from the project root:

    python setup.py

Installs the three FREE crawlers automatically (no account, no card), then
offers to record the two OPTIONAL paid API keys. Everything it does is
idempotent -- re-run it any time to repair an install or add a key later.

    python setup.py --check       verify an existing install, change nothing
    python setup.py --no-keys     install only, never prompt for keys
"""

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(ROOT, ".env")
PY = sys.executable

# Ordered deliberately. camoufox downgrades playwright, which silently breaks
# crawl4ai's browser -- so the playwright browser install MUST come last.
STEPS = [
    ("scrapling", "TLS-fingerprinted HTTP crawler",
     [PY, "-m", "pip", "install", "--disable-pip-version-check", "-q",
      "scrapling[fetchers]"]),
    ("crawl4ai", "browser-based crawler (renders JavaScript)",
     [PY, "-m", "pip", "install", "--disable-pip-version-check", "-q",
      "crawl4ai"]),
    ("chromium", "headless browser for crawl4ai (~120MB, last on purpose)",
     [PY, "-m", "playwright", "install", "chromium"]),
]

KEYS = [
    ("APIFY_TOKEN", "Apify", "https://console.apify.com/settings/integrations",
     "cheap bulk crawling, ~$0.023/domain"),
    ("FIRECRAWL_API_KEY", "Firecrawl", "https://www.firecrawl.dev/app/api-keys",
     "strongest anti-bot; rescues the hard tail"),
]

BAR = "=" * 68


def say(msg=""):
    print(msg, flush=True)


def run(cmd):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=1800)
        return p.returncode == 0, (p.stderr or p.stdout or "").strip()
    except subprocess.TimeoutExpired:
        return False, "timed out after 30 minutes"
    except Exception as exc:
        return False, str(exc)


def installed(module):
    import importlib.util
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def check_python():
    if sys.version_info < (3, 9):
        say("  FAIL  Python %d.%d found; 3.9+ required."
            % sys.version_info[:2])
        return False
    say("  ok    Python %d.%d.%d" % sys.version_info[:3])
    return True


def probe():
    """What actually works right now, tier by tier."""
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    try:
        from crawl_sites import TIER_ORDER, TIER_CLASSES
    except Exception as exc:
        say("  FAIL  cannot import the pipeline: %s" % str(exc)[:120])
        return False
    say()
    say("  TIER STATUS")
    all_free_ok = True
    for name in TIER_ORDER:
        f = TIER_CLASSES[name]()
        ok = f.available()
        kind = "paid" if f.costs_money else "free"
        if ok:
            say("    %-10s %-5s  ready" % (name, kind))
        else:
            say("    %-10s %-5s  not configured -- %s"
                % (name, kind, f.unavailable_reason()))
            if not f.costs_money:
                all_free_ok = False
    return all_free_ok


def install():
    say(BAR)
    say("INSTALLING THE FREE CRAWLERS")
    say(BAR)
    say("  No account or payment needed for any of these.")
    say()
    failures = []
    for module, what, cmd in STEPS:
        if module != "chromium" and installed(module):
            say("  ok    %-10s already installed (%s)" % (module, what))
            continue
        say("  ...   %-10s installing -- %s" % (module, what))
        ok, err = run(cmd)
        if ok:
            say("  ok    %-10s done" % module)
        else:
            failures.append((module, err))
            say("  FAIL  %-10s %s" % (module, err.splitlines()[-1][:90] if err else ""))
    return failures


def read_env():
    values = {}
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    values[k.strip()] = v.strip()
    return values


def write_env(values):
    lines = [
        "# Written by setup.py. This file is gitignored -- never commit it.",
        "# Both keys are OPTIONAL; the three free crawlers work without them.",
        "",
    ]
    for key, label, url, _ in KEYS:
        lines.append("# %s -- %s" % (label, url))
        lines.append("%s=%s" % (key, values.get(key, "")))
        lines.append("")
    with open(ENV_PATH, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    # Best-effort permission tightening; harmless where unsupported.
    try:
        os.chmod(ENV_PATH, 0o600)
    except Exception:
        pass


def prompt_keys():
    say()
    say(BAR)
    say("OPTIONAL PAID CRAWLERS")
    say(BAR)
    say("  The free crawlers handle most sites. These two only get used for")
    say("  what the free ones cannot fetch, and never without confirmation.")
    say("  Press ENTER to skip either one -- you can re-run setup.py later.")
    say()

    values = read_env()
    for key, label, url, why in KEYS:
        existing = values.get(key, "")
        if existing:
            say("  %s: already set (%s...), ENTER keeps it." % (label, existing[:7]))
        else:
            say("  %s -- %s" % (label, why))
            say("    get one at: %s" % url)
        try:
            entered = input("    %s: " % key).strip()
        except (EOFError, KeyboardInterrupt):
            say()
            say("  (skipped -- no input available)")
            return values
        if entered:
            values[key] = entered
            say("    saved.")
        elif existing:
            say("    kept existing.")
        else:
            say("    skipped -- %s stays disabled." % label)
        say()

    write_env(values)
    say("  Keys written to .env (gitignored).")
    return values


def main():
    args = set(sys.argv[1:])
    say(BAR)
    say("LEAD GEN -- ICP QUALIFICATION SETUP")
    say(BAR)
    if not check_python():
        return 1

    failures = []
    if "--check" not in args:
        failures = install()

    if "--check" not in args and "--no-keys" not in args:
        prompt_keys()

    free_ok = probe()

    say()
    say(BAR)
    if failures:
        say("SETUP INCOMPLETE -- %d component(s) failed" % len(failures))
        for module, err in failures:
            say("  %s: %s" % (module, (err or "").splitlines()[-1][:100] if err else "?"))
        say()
        say("  Re-run 'python setup.py' to retry just the failures.")
    elif free_ok:
        say("READY")
        say()
        say("  Verify:   python scripts/test_gate.py && python scripts/test_tiers.py")
        say("  Then:     open this folder in Claude Code and run  /new")
    else:
        say("PARTIAL -- a free crawler is still unavailable (see above)")
    say(BAR)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
