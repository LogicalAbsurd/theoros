# Theoros — Project Specification

## What this is

Theoros is a local-first personal AI witness system. Its purpose is to
observe the meaningful text its operator reads, writes, and attends to
on one specific computer, store that material as structured long-term
memory, and reflect patterns, tensions, and themes back to the operator
over days, weeks, and months.

The name is Theoros (Greek θεωρός) — "one who observes and contemplates,"
historically an envoy sent to witness sacred events and return with the
report. That is the register. Theoros is a witness: not a therapist,
not a diagnostic tool, not an assistant, not an oracle. It observes,
remembers, and reflects — with restraint as a first-class design
constraint, built into schema and prompt scaffolding, not left to model
behavior.

This is a months-long build, architected as a house rather than a shack:
proper schemas and a coherent memory model from the beginning, with
pragmatic iteration on the capture layer as experience reveals what
actually produces signal.

## What Theoros is not

Theoros is not an AI companion. It has no identity, no voice, no
drafting capability, no persona. It is explicitly the opposite
architectural move from a companion system: no synthesis that isn't
grounded in retrieved evidence, no first-person register, no
pronouncements on who the operator is. If a companion-style system
happens to share the same machine, the two do not share databases,
services, or identity — infrastructure patterns can be reused (systemd
service layout, Python package structure) but only as patterns, not as
shared state.

## Hardware and environment

- Fedora 44 on LUKS-encrypted `/home`
- NVIDIA RTX 3080, Intel i7-12700K, 16 GB DDR4, ~500 GB free SSD
- Local LLM available via llama.cpp-style server on localhost
- Supabase project with a Theoros-scoped schema (no sharing with
  unrelated systems on the same account)
- systemd user services for persistent processes

## Architectural decisions already made

These are settled. Revisiting them requires a concrete reason.

1. **No Cloudflare Worker in the pipeline.** Local writes go directly
   to Supabase via the `supabase-py` client. Fewer moving parts for a
   single-machine system.

2. **Capture is full-content, tiered, and present from the start.**
   Theoros captures the actual text content of what the operator reads
   and writes, not just window metadata. OCR is not deferred; it is a
   third-tier catchall. The full strategy is described in the Capture
   section below.

3. **Two-tier cognition, with the division visible in the schema:**
   - **Local LLM** (ARC or equivalent): high-frequency, cheap work.
     Per-session summaries, chunk classification, topic tagging, dedup
     judgments, entity extraction. Produces *evidence*.
   - **Claude API**: lower-frequency, more expensive work. Daily
     summaries, weekly and monthly reflections, cross-theme synthesis,
     answering pointed reflective queries. Produces *interpretation*.
   - Evidence and interpretation are separate record types in the
     schema, not just different prompts.

4. **Sessions are activity-based, not time-based.** A session is a
   coherent focus block, detected from the raw event stream using
   idle-threshold and dominance-threshold heuristics. Raw events are
   logged losslessly so the session detection algorithm can be revised
   later without losing history.

5. **Local stays local; only distilled outputs go to Supabase.** Raw
   captures and normalized records live only on this machine. Session
   summaries, daily/weekly/monthly reflections, and embeddings go to
   Supabase — where the distilled outputs can be read from the
   operator's phone and the embeddings power retrieval.

6. **Project layout uses the `src/` Python package pattern**
   (`src/theoros/`) to prevent import bugs when running under systemd.

## Capture — the ingestion layer

Full-content capture across three tiers. Each tier has a specific
strength; they cooperate rather than compete.

### Tier 1: Browser extension

Primary capture for web content. The extension reads the DOM via
Readability.js to extract clean article text, captures URLs, captures
typed content in textareas and chat interfaces, and captures page
structure (headings, links). This covers the bulk of the operator's
day — reading, social media comments and replies, chats with frontier
LLMs, webmail, web dev work.

The extension communicates with a local capture service, not Supabase
directly. The service writes to the local SQLite event log.

### Tier 2: Accessibility API (AT-SPI)

Secondary capture for native applications that expose their text
content through Linux's accessibility layer. Obsidian, native email
clients, VS Code, terminal emulators, and many GTK/Qt applications
support AT-SPI. A Python daemon (`pyatspi` or equivalent) reads
focused-window text content and feeds it into the same event log as
Tier 1.

AT-SPI coverage on Wayland is imperfect; where it fails, Tier 3
catches the residual.

### Tier 3: OCR catchall

Periodic screenshot capture with OCR extraction for anything the
higher tiers miss. Runs on a configurable interval during active use,
extracts text via tesseract or a local vision model, deduplicates
against what Tiers 1 and 2 already captured, retains extracted text
permanently and screenshots for a bounded retention window
(configurable, default 14 days) before discarding the image.

### Capture control and exclusion

Treated as a first-class subsystem, not an afterthought.

- **Pause capture toggle:** system tray icon or hotkey that halts all
  three tiers simultaneously. State visible to the operator at all
  times.
- **App-level exclusions:** specific applications and browser
  extensions never have their content captured. Bitwarden (and
  equivalent password managers) are excluded by default. Banking sites
  and other operator-specified domains are excluded per-URL.
- **Field-level exclusions in browser:** the extension detects
  password fields and fields tagged as sensitive and skips them,
  regardless of surrounding page capture.
- **Credential pattern redaction:** before anything reaches the event
  log, a pass scans for likely credential patterns (API keys, bearer
  tokens, credit card numbers, SSNs) via regex and redacts them,
  replacing with a placeholder that notes "[redacted: pattern type]".
  This runs on all three tiers' output.
- **Operator-initiated purge:** a simple CLI command allows the
  operator to delete all captures from a specified time range (e.g.,
  "delete everything from the last hour"), with transitive deletion
  of any derived summaries or embeddings that reference them.

## Six-layer architecture

### 1. Ingestion

The capture tiers described above. Outputs raw events into the SQLite
event log.

### 2. Extraction

Raw captures normalize into records with: `timestamp`, `source_tier`
(browser/atspi/ocr), `source_app`, `title`, `url_or_path`, `raw_text`,
`text_length`, `confidence`, `session_id`.

### 3. Normalization

Dedup across tiers (the same content captured via browser and OCR
should become one record). Chunk long text into manageable pieces.
Group into sessions using the activity-based heuristics. Score for
relevance. Local LLM performs topic and entity extraction at this
stage.

### 4. Memory — tiered storage

- **Raw captures** — append-only event log. Local SQLite only.
- **Normalized records** — local SQLite only.
- **Session summaries** — local LLM output. Markdown locally; also
  promoted to Supabase for phone-readable access.
- **Daily summaries** — Claude-generated, synthesized from session
  summaries plus retrieved prior context. Markdown locally; Supabase
  for phone access.
- **Weekly reflections** — Claude-generated, synthesized from the
  week's daily summaries and relevant theme evidence. Local +
  Supabase.
- **Monthly reflections** — Claude-generated, longer-arc synthesis.
  Local + Supabase.
- **Observations** — short, structured, evidence-linked notes. Can be
  operator-authored or generated. Local + Supabase.
- **Theme files** — long-lived, topic-scoped markdown.
  Operator-maintained with system assistance. Local + Supabase.
- **Embeddings** — Supabase pgvector for retrieval.

On-disk structure under `memory/` in the project root (gitignored):
### 5. Retrieval

Semantic similarity (via pgvector) combined with time-range, tag, and
source-type filters. Every Claude-generated output — daily, weekly,
monthly, or on-demand — must be grounded in retrieved evidence.
Nothing synthesizes from a blank prompt.

### 6. Reflection

Claude synthesizes from retrieved evidence. Epistemic discipline is
structural, enforced by prompt scaffolding and schema:

- Observed facts and inferred tendencies are distinguished in the
  output.
- Evidence is named explicitly, with references to the underlying
  records.
- The preferred mode is *"I notice X, Y, Z — here is a frame that
  might fit; does it?"* rather than "here is what is happening."
- **Interpretive vocabulary is permitted** — mythic, archetypal,
  alchemical, symbolic framings are valid lenses when a pattern
  genuinely resembles one the operator works with. Such framings are
  offered as possibilities (*"this resembles the alchemical arc of
  instinct-inquiry-insight-reflection; does that read true to you?"*)
  rather than declarations (*"you are in the nigredo"*).
- Theoros does not pronounce on identity, pathologize, or declare what
  the operator is. This cuts in all directions: no clinical framing,
  no mythologizing framing, no productivity framing. Theoros names
  patterns and asks questions; the operator brings the interpretive
  frame.
- A system that is mostly right accrues authority on the occasions it
  is wrong. Restraint is the structural counterweight.

## Reflection cadence

- **Daily:** Claude synthesizes from the day's session summaries plus
  retrieved context. Short — not a diary entry, a noticing.
- **Weekly:** Claude synthesizes from the week's dailies plus theme
  evidence. Longer, pattern-oriented.
- **Monthly:** Claude synthesizes from the month's weeklies plus
  long-arc theme evidence. Longer still, attends to drift and
  development.
- **On-demand:** the operator can ask Theoros pointed reflective
  questions at any time, answered from retrieved evidence.

All reflections are written to `memory/reflections/` locally and to
Supabase for phone access.

## Phased build

- **Phase 0 — Repo skeleton** *(in progress, nearly complete)*: Python
  package layout, virtualenv, `memory/` tree, `.gitignore`,
  `README.md`, `CLAUDE.md`, `docs/spec.md`, git initialized, remote
  wired.

- **Phase 1 — Schemas**: Local SQLite schema for raw events,
  normalized records, session boundaries. Supabase tables for
  summaries, reflections, observations, theme files, embeddings.
  Schemas reviewed before any migrations run.

- **Phase 2 — Capture layer**. The large phase. Divided:
  - **2a**: Browser extension + local capture service + SQLite
    writes. First tier end-to-end.
  - **2b**: AT-SPI daemon for native app capture.
  - **2c**: OCR catchall with retention policy.
  - **2d**: Capture-control subsystem: pause toggle, app/URL
    exclusions, credential redaction, operator-initiated purge.

- **Phase 3 — Local LLM distillation**: Session summaries, topic/
  entity extraction. Prompts designed to produce content that
  downstream Claude reflections can draw on meaningfully (not
  flattened-neutral; it should notice things in terms the operator's
  inner life uses).

- **Phase 4 — Claude daily summaries and weekly/monthly reflection
  pipeline**: The witness begins speaking. Retrieval integration,
  prompt scaffolding, epistemic discipline enforced at the prompt and
  schema level, promotion of outputs to Supabase.

- **Phase 5 — Reading surface**: Phone-accessible view of reflections
  (simple web page querying Supabase). On-demand reflective query
  interface.

- **Phase 6 — Theme files, cross-theme synthesis, long-arc
  observations**.

## Working principles

- Boring and reliable beats clever. This is a long build.
- Architectural decisions documented here are settled unless revisited
  explicitly.
- Data is not code. Memory contents never enter version control; the
  schemas that produce memory do.
- One step at a time, for both human and Claude Code. Pauses are
  features.
- The capture layer's privacy mechanisms are not optional features;
  they are load-bearing architecture.
