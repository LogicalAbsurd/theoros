# Theoros

### Concept

Theoros (Greek θεωρός) — "one who observes and contemplates."  

Many contemplative, meditative or spiritual traditions speak of human consciousness  as layered, or perhaps multi-elemental.  In the developer's personal experience, conscious awareness sits in an observational shell zone or architecture surrounding deeper, more primordial elements of self.  Theoros is conceived as a digital approximation of this fundamental element (or "sphere") of consciousness, the "witness," digitized and tasked with pattern-recognition and reflective insight. 

Theoros watches user activity on two devices; a desktop PC running KDE Plasma Fedora 44, and a smartphone running Android 16.  From this activity, any text Theoros sees is processed by a local AI model, which produces distillations of activity sessions.  Each day, a local AI model processes captured text into session distillations that remain 100% local and private on the user's machine.  These distilled summaries are then sent to a frontier AI service for synthesis and analysis, producing a thoughtful report of where the user's attention was spent for that day, including inference into the user's interests and a suggested conceptual framing.  After one week of daily reflections has been generated, Theoros sends the seven daily reports again to the frontier service for a weekly report.

This makes Theoros at once an archival system and a meta-awareness system.  It acts as an extension of the living human witness function into digital space.  In essence, it expands user awareness into their digital life.

Theoros is not a therapist, a diagnostic tool, or an assistant. It simply observes, remembers, reflects, and offers insight.

### Design principles

- **Capture generously, interpret downstream.** The record errs toward completeness; meaning-making happens in later layers where it can be corrected.
- **Evidence and interpretation are separate record types.** A local LLM produces evidence (session summaries) at high frequency; a frontier model produces interpretation (reflections) at low frequency, grounded only in that evidence.
- **Honest absence over confident invention.** The distillation layer is prompted to describe what crossed the screen without inferring authorship, attribution, or identity that the text doesn't establish.
- **The witness never pronounces on identity.** Reflections name patterns and offer frames; the operator supplies the interpretation.
- **Privacy is load-bearing architecture, not a feature.** Pause toggle, app/URL exclusions, credential-pattern redaction, and operator-initiated purge with transitive deletion of derived records.

## Status

Fully operational for its single operator: all capture tiers live (desktop + mobile), daily and weekly reflections generating on timers with self-healing catch-up on boot, encrypted nightly backups to two destinations, and a phone-readable reflection surface behind Cloudflare Access. Current work: embeddings-backed retrieval and long-arc theme synthesis.

**This is a personal system, not a packaged product.** It's published as a reference architecture — it assumes one operator, one Fedora/KDE machine, and existing Supabase/Cloudflare/local-LLM infrastructure. Porting notes and setup docs are in `docs/`.

## Architecture

Six layers: ingestion, extraction, normalization, memory, retrieval,
reflection. Two storage tiers: local SQLite for raw and normalized
captures, Supabase for distilled outputs and embeddings. Two cognition
tiers: local LLM for evidence, Claude API for interpretation.

Full architecture and design decisions: [`docs/spec.md`](docs/spec.md).

## Repository layout

```
src/theoros/
  clients/
    browser_extension/   Firefox extension (Manifest V3)
    kwin_script/         KWin script for window metadata via D-Bus
    window_meta_daemon.py  D-Bus listener → capture service
    ocr_daemon.py          Periodic screenshot + tesseract → capture service
  services/
    capture/             FastAPI capture service (app, db, redact, models)
  cli/
    commands/purge.py    Operator-initiated time-range purge
  db/
    migrate.py           SQLite migration runner
    migrations/          001–006, applied in order
    supabase_migrations/ Remote schema (applied via SQL Editor)
  workers/
    session_detector.py    Idle-threshold session grouping
    distillation.py        Local LLM session summaries
    reflection.py          Daily Claude reflection synthesis
deploy/
  systemd/               Unit files for all services and timers
docs/
  spec.md                Authoritative specification
  phase-2b-kwin-architecture.md
  phase-2a-mutating-pages-decisions.md
```

## Running

Prerequisites: Python 3.14, Fedora with KDE Plasma/Wayland, system
`python3-gobject` package for D-Bus daemons, `tesseract` and
`tesseract-langpack-eng` for OCR.

```sh
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in Supabase URL/key, Claude API key

# apply migrations
python -m theoros.db.migrate

# start capture service
python -m theoros.services.capture

# start window-meta daemon (requires KWin script installed)
python -m theoros.clients.window_meta_daemon

# start OCR daemon (requires tesseract + spectacle)
python -m theoros.clients.ocr_daemon
```

Systemd user units in `deploy/systemd/`:

- `theoros-capture.service` — FastAPI capture service (127.0.0.1:8765)
- `theoros-window-meta.service` — KWin D-Bus window-meta daemon
- `theoros-ocr.service` — periodic screenshot + tesseract daemon
- `theoros-session-detector.service` — idle-threshold session grouping
- `theoros-distillation.service` — local LLM session summaries
- `theoros-reflection.service` + `theoros-reflection.timer` — daily Claude reflection (04:00)

##### Copyright © 2026 LogicalAbsurd. All rights reserved — contact for licensing
