#!/usr/bin/env python3
"""
Feed watchdog notifier for ragingcunt.com.

Reads data/feed-health.json (written by build.py) and keeps ONE GitHub issue in
sync so a broken feed pings you by email — and nothing else does:

  * a feed dark >= STALE_AFTER_MISSES weekly runs  -> open/update the issue
  * everything healthy again                        -> close the issue

Silence is the healthy state. You only hear from it when a feed genuinely
stops pulling live data, and you get a "recovered" note when it heals.

Run in CI with GH_TOKEN set (GitHub Actions provides secrets.GITHUB_TOKEN).
Locally:  python scripts/feed_health_alert.py --dry-run   # prints, touches nothing
"""
from __future__ import annotations
import argparse, json, os, pathlib, subprocess, sys, tempfile

# Windows consoles default to cp1252 and choke on the emoji in our output;
# force UTF-8 so --dry-run stays legible everywhere (CI is already UTF-8).
for _stream in (sys.stdout, sys.stderr):
    try: _stream.reconfigure(encoding="utf-8")
    except Exception: pass

ROOT   = pathlib.Path(__file__).resolve().parent.parent
HEALTH = ROOT / "data" / "feed-health.json"

LABEL   = "feed-health"                       # marks the one issue we manage
TITLE   = "🩺 Feed watchdog: a feed stopped pulling"
MENTION = "@sean-vc"                           # who to ping (GitHub username)
REPO    = os.environ.get("GITHUB_REPOSITORY", "whycantwe/ragingcunt")


def gh(*args, check=True):
    """Invoke the gh CLI, returning stdout. --repo is injected for every call."""
    proc = subprocess.run(["gh", *args, "--repo", REPO],
                          capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def find_open_issue():
    """Return the number of our open watchdog issue, or None."""
    out = gh("issue", "list", "--label", LABEL, "--state", "open",
             "--json", "number", "--limit", "1")
    arr = json.loads(out or "[]")
    return arr[0]["number"] if arr else None


def ensure_label():
    """Create the marker label if it doesn't exist yet (idempotent)."""
    gh("label", "create", LABEL, "--color", "B60205",
       "--description", "Automated: a data feed stopped pulling", check=False)


def build_body(broken, generated, threshold):
    rows = []
    for r in broken:
        last_ok = r.get("last_ok") or "never — has never pulled a live event"
        rows.append(f"| {r['city']} | {r['label']} | `{r.get('status','?')}` | "
                    f"{r['misses']} | {last_ok} |")
    table = ("| City | Feed | Last status | Failed runs | Last had events |\n"
             "|---|---|---|---|---|\n" + "\n".join(rows))
    errs = [f"- **{r['label']}** — `{(r.get('error') or '').strip()}`"
            for r in broken if r.get("error")]
    err_block = ("\n\n**Latest errors**\n" + "\n".join(errs)) if errs else ""
    return (
        f"{MENTION} — one or more feeds have **errored on every pull for "
        f"{threshold}+ weekly runs in a row**. They're likely dead (URL moved, "
        f"host now blocking, org retired the calendar) and need a look.\n\n"
        f"{table}{err_block}\n\n"
        f"**What to do:** check each feed's URL in `data/sources.yml`, fetch it "
        f"by hand, and repair or replace it. This issue **closes itself** on the "
        f"next run once every feed is pulling again.\n\n"
        f"<sub>Auto-managed by scripts/feed_health_alert.py · snapshot "
        f"{generated}. Edits here are overwritten each run.</sub>"
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="print the intended action without calling GitHub")
    args = ap.parse_args()

    if not HEALTH.exists():
        print(f"[alert] no {HEALTH} — run build.py first; nothing to do")
        return
    data = json.loads(HEALTH.read_text(encoding="utf-8"))
    threshold = int(data.get("stale_after_misses", 2))
    sources = data.get("sources", {})
    broken = sorted((r for r in sources.values() if r.get("misses", 0) >= threshold),
                    key=lambda r: (-r["misses"], r["city"]))

    print(f"[alert] {len(sources)} sources, {len(broken)} dark >= {threshold} runs")

    if args.dry_run:
        if broken:
            print(f"[alert] DRY-RUN would OPEN/UPDATE issue titled {TITLE!r}:\n")
            print(build_body(broken, data.get("generated", "?"), threshold))
        else:
            print("[alert] DRY-RUN all feeds healthy — would CLOSE any open issue")
        return

    existing = find_open_issue()

    if broken:
        body = build_body(broken, data.get("generated", "?"), threshold)
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False,
                                         encoding="utf-8") as f:
            f.write(body); body_file = f.name
        try:
            if existing:
                gh("issue", "edit", str(existing), "--body-file", body_file)
                print(f"[alert] updated issue #{existing} ({len(broken)} broken)")
            else:
                ensure_label()
                gh("issue", "create", "--title", TITLE, "--label", LABEL,
                   "--body-file", body_file)
                print(f"[alert] opened watchdog issue ({len(broken)} broken)")
        finally:
            os.unlink(body_file)
    else:
        if existing:
            gh("issue", "close", str(existing),
               "--comment", f"✅ All feeds pulling again as of {data.get('generated','?')}. "
                            f"Auto-closed by the watchdog.")
            print(f"[alert] closed issue #{existing} — all healthy")
        else:
            print("[alert] all healthy, no open issue — nothing to do")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:      # never let a gh hiccup fail the weekly job
        print(f"[alert] non-fatal: {e}", file=sys.stderr)
        sys.exit(0)
