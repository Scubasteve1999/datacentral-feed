# datacentral-feed

The Buildout Pulse feed for the [DataCentral iOS app](https://github.com/Scubasteve1999/DataCentral).
The app fetches `pulse.json` from:

```
https://raw.githubusercontent.com/Scubasteve1999/datacentral-feed/main/pulse.json
```

Editing `pulse.json` on `main` updates every install — no App Store release needed.

## How it stays fresh (automated)

A GitHub Action ([update-feed.yml](.github/workflows/update-feed.yml)) runs
**Monday and Thursday** (and on demand via the Actions tab → "Run workflow"). It:

1. Pulls RSS from Google News queries + trade press (Data Center Knowledge, DCD)
2. Keeps only US data-center buildout stories (keyword filters, spam exclusions)
3. Classifies each into `announcement | milestone | grid | policy`, extracts
   builders, state, and MW figures
4. Dedupes against existing entries (by URL hash and normalized title)
5. Adds at most 6 entries per run, keeps the newest 120, commits the result

No secrets are required. Optionally add an `ANTHROPIC_API_KEY` repo secret
(Settings → Secrets and variables → Actions) to have new entries rewritten into
cleaner headlines with better classification; without it, heuristics are used.

## Event schema

```json
{
  "id": "16-char stable hash",
  "date": "2026-07-13",
  "kind": "announcement | milestone | grid | policy",
  "title": "OpenAI breaks ground on 500 MW Stargate campus",
  "detail": "Optional one-sentence context.",
  "builders": ["OpenAI", "Oracle"],
  "stateCode": "TX",
  "mw": 500,
  "facilityId": 1001,
  "sourceName": "Data Center Dynamics",
  "sourceURL": "https://..."
}
```

`facilityId` links an event to a facility in the app's registry
(`USFacilityRegistry.swift`) — the bot leaves it `null`; set it by hand when an
event is about a known facility to make the event tappable in the app.

## Manual entries

Edit `pulse.json` directly (GitHub web editor works fine) — add your object to
the top of `events` with a unique `id`. Manual entries are never touched by the
bot (it only prepends and prunes past 120).

## Switching to review mode

If you'd rather approve entries before they go live: in the workflow, replace
the "Commit if changed" step with a PR action (e.g. `peter-evans/create-pull-request`)
so each run opens a PR instead of committing to `main`.
