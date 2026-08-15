# Theoros — Mobile Capture Layer 3 (URL Exclusion Sync) Design

Date: 2026-05-15
Status: Design (pre-implementation)
Supersedes: the "Layer 3" section of `phase-mobile-capture-design.md`,
which assumed the desktop's URL exclusion list lived in a location
reachable by the capture service. It does not.

## Why this exists

The mobile capture design specified Layer 3 as a read-only mirror on
the phone of the desktop's URL exclusion list, fetched periodically
from the capture service. Implementation revealed the assumption
underneath was wrong: the desktop's URL exclusion list lives entirely
in the browser extension's `chrome.storage.local`, scoped to the
extension and unreachable from anything else, including the capture
service it runs on top of.

Two cleanish ways out and one that pretends to be clean:

- **Move the source of truth to the capture service** (Option 1).
  Single store, single API, every client reads from there.
- **Push from the extension on every change** (Option 2).
  Two stores pretending to be one. Conflict resolution becomes a
  problem the moment the extension is closed when an edit needs to
  happen on the phone, and the moment a manual SQLite edit conflicts
  with the extension's view of itself. Don't.
- **Maintain two independent lists** (Option 3).
  Cheap. Loses single-source elegance. Fine as a stopgap, wrong as a
  destination.

This document specifies Option 1. The single-source-of-truth move
also lines up with Phase 5's reading surface, which will want to query
exclusion state when rendering captured records.

## Target architecture

```
                        +-----------------------+
                        |   capture service     |
                        |   (FastAPI, SQLite)   |
                        |                       |
                        |  url_exclusions table |
                        +-----------+-----------+
                                    |
              GET /exclusions/urls  |  POST/DELETE /exclusions/urls
                                    |
               +--------------------+--------------------+
               |                                         |
       +-------+--------+                       +--------+--------+
       |   browser ext  |                       |   Android app   |
       |  (Firefox)     |                       |  (a11y service) |
       |                |                       |                 |
       |  local cache   |                       |  local cache    |
       |  in storage    |                       |  in shared prefs|
       +----------------+                       +-----------------+
```

The extension and the Android app are both clients of the same API.
Each keeps a local cache for fast checks during capture; cache refresh
happens on a schedule (every hour or on user action) and at startup.

## Schema (SQLite, migration 007)

One table, kind-discriminated:

```sql
CREATE TABLE url_exclusions (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  kind        TEXT NOT NULL CHECK (kind IN ('domain', 'path_prefix')),
  value       TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  UNIQUE (kind, value)
);

CREATE INDEX idx_url_exclusions_kind ON url_exclusions(kind);
```

Notes:
- `kind` matches the extension's existing two categories. Adding a
  third match type later (e.g., regex) is one row with no schema
  change.
- `UNIQUE (kind, value)` makes duplicate inserts idempotent — relevant
  for the migration flow below, which may re-import the same list
  more than once during testing.
- `created_at` is a TEXT (ISO 8601) for consistency with the rest of
  the Theoros schema, which uses TEXT timestamps throughout.

The M6 widening of `source_tier` for mobile captures will become
migration 008.

## API contract

All endpoints live on the capture service, under `/exclusions/urls`.
Authentication uses the same `require_cf_access` dependency as
`/capture`: localhost-bypassed for the extension and any local
tooling, service-token-gated for the Android app coming through the
tunnel.

### `GET /exclusions/urls`

List the current exclusions.

Response (200):
```json
{
  "domains": ["example.com", "private-thing.com"],
  "path_prefixes": ["github.com/sensitive-org/"]
}
```

The response shape matches the extension's existing in-memory model
(`{domains, pathPrefixes}`) so the extension can drop the response
straight into its check function without restructuring.

### `POST /exclusions/urls`

Add an exclusion.

Request:
```json
{ "kind": "domain", "value": "example.com" }
```

Responses:
- 201 — created
- 200 — already exists (idempotent; `UNIQUE` constraint hits, we
  swallow it and return the existing row)
- 400 — validation error (kind not in enum, empty value, etc.)

### `DELETE /exclusions/urls`

Remove an exclusion.

Request:
```json
{ "kind": "domain", "value": "example.com" }
```

Responses:
- 200 — deleted (or never existed; idempotent)
- 400 — validation error

Body-bearing DELETE is mildly unidiomatic; the alternative is
`DELETE /exclusions/urls/{kind}/{value}` with URL-encoded value, which
gets ugly fast for domains containing dots and slashes. Body wins on
ergonomics; FastAPI handles it cleanly.

## Migration flow

The extension currently holds the entire list in
`chrome.storage.local`. On the first run of the new extension code,
we need to move that data into the capture service without losing it.

Sequence in the new `background.js`:

```
on extension load:
  if chrome.storage.local has "exclusions_migrated_v1" flag:
    skip migration; fetch fresh list from service, populate local cache
  else:
    read existing chrome.storage.local["exclusions"]
    for each domain in list:
      POST /exclusions/urls { kind: "domain", value: domain }
    for each prefix in list:
      POST /exclusions/urls { kind: "path_prefix", value: prefix }
    set chrome.storage.local["exclusions_migrated_v1"] = true
    fetch fresh list from service, populate local cache
```

The POSTs are idempotent (per the `UNIQUE` constraint), so retrying
the migration if it fails halfway through is safe. The flag prevents
re-running the migration on every extension reload after the first
success.

If the capture service is unreachable on first load, the extension
falls back to its existing `chrome.storage.local` list for the
current session and retries the migration on the next load. Capture
keeps working from the local list throughout — Layer 3 must never
silently weaken exclusion checks.

## Implementation order

Six pieces, sequenced so each ends at a natural seam where the system
is in a valid state and can stop there if needed:

1. **Migration 007.** Schema only. Run, verify table is empty, done.
2. **Capture service endpoints.** `GET`, `POST`, `DELETE` on
   `/exclusions/urls`. Auth via the existing dependency. Test with
   curl: localhost reads/writes, mobile token reads/writes through
   the tunnel, no-token requests 403. At this point the system is
   queryable but no client uses it.
3. **Extension migration of existing data.** Run the migration flow
   above against the live extension. After this, the SQLite table is
   the source of truth and the extension reads from cache populated
   from the service. Roll back is straightforward — `chrome.storage.local`
   still has its copy; we don't clear it for some weeks just in case.
4. **Extension write-through.** Options page POSTs and DELETEs hit
   the service; `chrome.storage.local` cache updates from the
   service's response, not directly. After this, the extension fully
   participates in the new model.
5. **Android settings UI.** Settings screen showing the current
   exclusion list, with add/remove. Mirrors the M3 Layer 1 app
   exclusion UI in structure (`SharedPreferences`-backed cache, list
   view, dialog for add). Pulls from `GET /exclusions/urls` at
   startup and on manual refresh.
6. **Android capture-time enforcement.** In the accessibility service,
   when the foregrounded app is Firefox, extract the URL from the
   node tree (M2 already has the extraction path; we add a URL pull
   to it), match against the cached exclusion list, halt capture on
   match. After this, all five privacy layers are active on mobile.

Each piece is its own commit. Pieces 1+2 form a self-contained unit
(API exists, nobody uses it) that's safe to ship without doing 3-6.

## Fail-open vs fail-closed on fetch failure

When a client (extension or Android) can't reach the capture service
to refresh its cache, behavior is:

- **Extension:** use the last-cached list. Capture continues.
- **Android:** use the last-cached list, up to seven days stale.
  Beyond seven days, halt Firefox capture entirely until the next
  successful refresh.

The seven-day ceiling on Android exists because mobile is the higher-
risk surface: it can leave the desktop's network entirely and be in
unpredictable contexts (other Wi-Fi, cellular, offline travel). The
exclusion list is privacy-protective; running for weeks with a
possibly-stale list while the user adds new exclusions they think are
active is the bad mode. Halting Firefox capture is recoverable on
the next desktop sync; missing exclusions during that window is not.

Other capture (non-Firefox apps) continues regardless, since those
are governed by Layer 1 (app exclusion list) which is fully local to
the phone.

## Auth posture

Both clients authenticate the same way they already do for `/capture`:

- **Extension:** localhost-bypass. The extension already POSTs to
  `127.0.0.1:8765` without any token; the `/exclusions/urls`
  endpoints inherit the same bypass.
- **Android:** Cloudflare Access service token via the tunnel,
  validated by the existing `require_cf_access` dependency.

No new auth surface is introduced.

## Non-goals

- Multi-device exclusion lists (e.g., one phone's exclusions
  diverging from another phone's). One operator, one list.
- Regex exclusions. Possible future addition via a new `kind`; not
  in scope here.
- Per-app exclusion overrides for non-Firefox apps. Those use Layer 1
  (the device-local app exclusion list); URLs aren't extractable
  from arbitrary Android apps' accessibility trees in a reliable
  way.
- UI for editing exclusions from the phone before piece 5. Piece 5
  is when the phone becomes a real client of the list.

## Open questions

1. **URL extraction reliability on Firefox for Android.** M2 found
   Firefox node trees expose useful text but the structure of the
   URL location bar varies by app version. Need to verify URL
   extraction at piece 6, possibly with a marker-string scan similar
   to the private-mode detection in M3 Layer 2.

2. **Cache refresh cadence on Android.** Hourly is the starting
   number; might need to be more frequent if list churn is high in
   practice. Configurable in settings from the start to avoid
   re-shipping the app to tune it.

3. **What happens to the old chrome.storage.local data.** The
   migration flag prevents re-import, but the original data sits
   there indefinitely as backup. After some weeks of confidence in
   the new system, we could prune it; not urgent.
