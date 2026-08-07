# ragingcunt.com — local rage, issue by issue

A riot-grrrl / xerox-zine wall of **real** local organizing — protests, meetings,
mutual aid, shows, tenant fights, know-your-rights — sorted by **city** and drilled
down to the **neighborhood** ("issue"). Static site, no server. It refreshes itself
every Sunday via a GitHub Action.

> **Core rule, enforced in code:** the site never fabricates events. It only shows
> what a real, configured feed returned. Empty city = honest empty issue.

---

## How it works (no backend)

1. `index.html` is a static page. On load it reads **one** file: `data/events.json`.
2. A GitHub Action (`.github/workflows/weekly.yml`) runs every Sunday: it executes
   `scripts/build.py`, which pulls from the feeds in `data/sources.yml`, normalizes +
   buckets + dedupes them, and writes a fresh `data/events.json`.
3. The Action commits that file. GitHub Pages redeploys on the push. Done — no server
   ever "runs"; a fresh JSON is dropped in weekly.

## File tree

```
index.html                        # riot-grrrl front end (reads data/events.json)
CNAME                             # custom domain for Pages (delete if not using ragingcunt.com)
data/
  events.json                     # DATA. Ships as a demo seed; the Sunday job overwrites it.
  sources.yml                     # ← the ONE file you maintain: feeds per city
  neighborhoods/
    sacramento.geojson            # PLACEHOLDER polygons — replace with real boundaries
scripts/
  build.py                        # fetch → normalize → bucket → dedupe → keep-last-good → write
  requirements.txt
.github/workflows/
  weekly.yml                      # Sunday cron + manual trigger + auto-commit
```

## The data contract (what `build.py` emits, what `index.html` consumes)

```jsonc
{
  "generated": "2026-07-12T14:00:00Z",   // ISO, stamped each run
  "demo": false,                          // true only for the shipped seed
  "cities": {
    "sacramento": {
      "label": "Sacramento, CA",
      "events": [
        {
          "id": "sha1-12char",            // hash(title|start|venue) — dedupe key
          "title": "Rent Is Theft: Rally at City Hall",
          "type": "action",               // action|meeting|mutualaid|show|teachin|tenant|kyr
          "org": "Capitol Tenants Union",
          "start": "2026-07-18T17:30:00-07:00",
          "end": null,
          "venue": "City Hall Steps",
          "neighborhood": "Downtown",     // or "Citywide"
          "lat": null, "lng": null,
          "url": "https://…",
          "blurb": "…",
          "source": "mobilize"            // provenance
        }
      ]
    }
  }
}
```
The front end is **done** — don't change it to fit new data; make the build emit this shape.

---

## Go live: the one-time setup (≈4 clicks)

Push this folder to a repo, then:

1. **Pages:** Settings ▸ Pages ▸ Source = your default branch, root. (Custom domain:
   set `ragingcunt.com` here; the `CNAME` file backs it. Delete `CNAME` if not using it.)
2. **Let the Action write:** Settings ▸ Actions ▸ General ▸ Workflow permissions ▸
   **Read and write**. Without this the Sunday job can't commit and the site never updates.
3. **First run:** Actions ▸ *weekly-build* ▸ **Run workflow**. (Cron won't fire until
   Sunday; this proves the chain end-to-end now.)
4. **Secrets (only if a source needs a key):** Settings ▸ Secrets ▸ Actions. Public
   ICS/Mobilize feeds need none.

After the first successful run it self-sustains: the weekly auto-commit also keeps
GitHub's scheduler from going dormant (scheduled workflows disable after 60 days of *no*
repo activity — the commit counts).

On first push, before any feeds are wired, the site renders the **demo seed** (banner up,
cards stamped `sample`). The moment `build.py` writes real data (`demo:false`), the banner
disappears on its own.

## Updating / resilience (already built in)

- **Keep-last-good:** if a feed errors, its city keeps last week's events instead of blanking.
  (The demo seed is explicitly excluded — it can never leak into real output.)
- **Dedupe:** on `id = hash(title|start|venue)` — the same protest on three feeds collapses to one.
- **Expire:** anything before now (−6h grace) is dropped in the build; the front end also
  filters by date as a backstop.

## Add a city

1. Add real feeds under that city in `data/sources.yml` (see the comments there for how to
   find ICS / Mobilize feeds).
2. (Optional) Drop `data/neighborhoods/<city-key>.geojson` with real polygons to get
   neighborhood buckets; without it, that city's events land in "Citywide" (fine for
   smaller cities).
3. Run the workflow (or wait for Sunday). That's the whole per-city recipe.

## Local dev

```bash
pip install -r scripts/requirements.txt
python scripts/build.py            # writes data/events.json from sources.yml
python -m http.server              # then open http://localhost:8000
```

---

## Known limits (so nothing surprises you)

- **No national firehose of "leftist events" exists.** "Auto-updating" = you curate the
  *sources* once per city; the job refreshes from them. Sourcing is the real work.
- **Instagram-only orgs are invisible** — no usable feed. Those events won't appear unless
  someone submits them (see the submit-form idea below).
- **Neighborhood polygons are placeholders.** Replace `sacramento.geojson` with real
  boundaries; reserve neighborhood granularity for the metros where it matters.
- **Cron can lag** minutes-to-an-hour under GitHub load (irrelevant weekly).

## Build it out in Claude Code — copy-paste prompts

Open this repo in Claude Code and work these in order:

1. **Verify the Mobilize fetcher.**
   > "Open `scripts/build.py`. Check `fetch_mobilize` against the current Mobilize API
   > (https://github.com/mobilizeamerica/api): confirm the endpoint, params, pagination,
   > and response fields (timeslots, location, browser_url). Fix anything stale and add
   > pagination + a per-request timeout/retry. Don't change the output schema."

2. **Wire real Sacramento feeds.**
   > "Help me populate `data/sources.yml` for Sacramento. For each org I name, find its
   > public ICS feed or Mobilize org_id, verify the URL actually returns events, and add a
   > correctly-typed source line. Only add feeds you've confirmed resolve."

3. **Real neighborhood boundaries.**
   > "Replace `data/neighborhoods/sacramento.geojson` with real Sacramento neighborhood
   > polygons from an open-data source. Keep each feature's `properties.name`. Then run
   > `build.py` and show me which sample events bucket into which neighborhood."

4. **Harden the build.**
   > "Add structured logging, a `--dry-run` flag that prints results without writing, and a
   > summary line per city (counts by type). Make one failing feed never abort the run."

5. **(Optional) Submit-form fallback** for Instagram-only / uncovered blocks.
   > "Add a 'submit an event' path: a static form that emails submissions to
   > holler@ragingcunt.com (or opens a Google Form), plus a documented manual step to add
   > vetted submissions into a `data/manual/<city>.json` that `build.py` merges in."

6. **(Optional) Action Network source.**
   > "Add a `fetch_action_network` fetcher using the AN API with a token from
   > `os.environ['ACTION_NETWORK_TOKEN']`, mapping to the same event schema, and register
   > it in `FETCHERS`."
