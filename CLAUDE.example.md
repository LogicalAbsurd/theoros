# CLAUDE.example.md — Working-context template for Claude Code

This is a template. Copy it to `CLAUDE.md` (which is gitignored in this
repo — see `.gitignore`) and edit for your own operator identity and
working preferences.

## What this file is

Standing context for Claude Code sessions in this repo. It is read at
the start of every session. Its job is to ensure Claude Code
(a) understands what Theoros is before touching anything, (b) respects
the architectural decisions already made rather than relitigating them,
(c) works in the operator's preferred register, and (d) knows where to
find the full specification.

Claude Code should read this file and then `docs/spec.md` before
proposing any substantive work.

## What Theoros is (short form)

A local-first personal AI witness system for one operator on one
machine. It captures the meaningful text that crosses the operator's
screen, stores it as structured long-term memory, and reflects patterns
back over days, weeks, and months. It is a witness, not an assistant:
no voice of its own, no synthesis ungrounded in retrieved evidence, no
pronouncement on identity.

Full architecture, phases, and design decisions: `docs/spec.md`. That
file is authoritative. This file is for *how to work*, not *what to
build*.

## Operator

Fill in your own context here — how you'd like Claude Code to address
you, what you know and don't know, and how you prefer to work.
Examples of the kinds of things worth stating:

- Name or handle to use.
- Programming background and comfort level.
- Preferred pace and register (e.g., one decision at a time with
  what+why; suggestions over clarifying questions; direct engagement
  over cushioned framing).
- Any other projects or identities on the same machine that Claude
  Code should be aware of but must not conflate with Theoros.

## Working register

This is a months-long build. Deliberate, not plodding. Prefer legible,
well-chosen implementations over clever or compact ones — but don't
avoid the right tool for a job because a worse tool would feel simpler.
Over-engineering and under-engineering both slow the build down; the
goal is chosen fit.

Explanations accompany code, not decorate it. When Claude Code adds a
function or a migration or a config choice, it should briefly say why
in a comment or commit message. "Because it's idiomatic" is not a
reason; "because systemd starts the service before the network is up,
so we need a retry here" is.

Flag tradeoffs explicitly. If two approaches exist and Claude Code
picks one, it names the other in passing and says why it chose as it
did. Don't hide tradeoffs behind "best practices."

## Architectural decisions that are settled

These are in `docs/spec.md` in full. In brief, don't relitigate:

- Local-first. `supabase-py` writes directly; no intermediate worker.
- Full-content capture across three tiers (browser extension, AT-SPI,
  OCR) — all present from Phase 2, not deferred.
- Two-tier cognition: local LLM for evidence (high-frequency, cheap),
  Claude for interpretation (lower-frequency, expensive). Separate
  record types in schema.
- Activity-based sessions from raw event log. Raw events never
  discarded.
- Raw captures and normalized records stay local. Only summaries,
  reflections, and embeddings go to Supabase.
- `src/theoros/` Python package layout. Not flat.
- Python 3.13+ in a `.venv/` at the repo root. Dependencies in
  `requirements.txt`.

If Claude Code believes a settled decision should change, the right
move is to raise it with the operator (in a response, not a commit)
and wait for direction. Do not refactor past a settled decision
silently.

## Capture defaults and privacy

The capture layer sees sensitive content by design, and that's the
point — Theoros's value depends on it capturing the full texture of
the operator's screen life, including deep-introspective writing that
in other contexts might be considered sensitive. Processing stays local
precisely so capture can be generous.

**Default on ambiguity: capture.** False absences (expected content
missing) are worse than over-captures, because the operator can see
what was captured and purge what shouldn't have been; they cannot
recover what was silently skipped. If Claude Code is unsure whether a
given surface should be captured, the answer is: capture it, and make
the behavior visible/configurable.

**Categorical exclusions that apply regardless of defaults:**
- Password fields in the browser (detected by input type).
- Password-manager extensions — their UI is never captured.
- Credential-pattern regex scan (API keys, bearer tokens, credit
  cards, SSNs) runs on all captured text before storage; matches are
  redacted in place.
- Any app or URL the operator has added to the explicit exclusion
  list.

**Operator-initiated controls:**
- Pause-capture toggle (tray icon + hotkey) halts all three tiers.
- Operator-initiated purge by time range, with transitive deletion of
  derived records.

Any change to the capture layer must preserve the categorical
exclusions and the pause/purge controls. A change that adds capture
coverage but weakens those is rejected.

## The reflection register

Claude Code will eventually write prompt templates for Claude's
reflection layer (Phase 4). When it does, the register matters:

- Distinguish observed facts from inferred tendencies in output.
- Name evidence explicitly with references to the underlying records.
- Prefer *"I notice X, Y, Z — here is a frame that might fit; does
  it?"* over assertions of what is happening.
- Interpretive vocabulary is permitted. Mythic, archetypal,
  alchemical, symbolic framings are valid lenses when a pattern
  genuinely resembles one the operator works with. Offered as
  possibilities, not declarations.
- Never pronounce on identity, pathologize, or declare what the
  operator is. This cuts in all directions — no clinical framing, no
  mythologizing framing, no productivity framing.
- The operator brings the interpretive frame. Theoros supplies the
  texture and names patterns.

## Repository conventions

- **Branch:** `main` during early phases; feature branches as the
  project grows.
- **Commits:** Short summary line in the imperative ("add SQLite
  schema for raw events," not "added" or "adds"). Body when the change
  needs context. Reference phases in commit messages where relevant
  ("Phase 2a: browser extension skeleton").
- **Tests:** Tests live in `tests/` mirroring the `src/theoros/`
  structure. Not every function needs a test; integration tests at
  the boundaries (capture → SQLite, normalize → Supabase, retrieval
  → prompt) matter more than unit tests on pure functions.
- **Secrets:** Never commit. `.env.example` is the template; real
  `.env` is gitignored. Supabase URL and service key, Claude API key,
  anything else sensitive goes in `.env` only.
- **Memory data is not code:** `memory/` directory structure is
  tracked via `.gitkeep` files; contents are gitignored. Never commit
  anything from `memory/*` except the skeleton.

## What to do first in a new session

1. Read this file.
2. Read `docs/spec.md`.
3. Check `git log --oneline -20` for recent work context.
4. Check `git status` for uncommitted work.
5. Ask the operator what's up before proposing anything substantive.

## What not to do

- Do not write code that silently sends content outside the local
  machine, except through the explicit Supabase promotion paths
  defined in the spec.
- Do not add features to the capture layer that bypass the categorical
  exclusions or the pause/purge controls.
- Do not write reflection prompts that allow Claude to pronounce on
  identity. "You are X" is always wrong; "this resembles X" is
  acceptable.
- Do not add dependencies without noting why in the commit and in
  `requirements.txt`.
- Do not run database migrations without the operator's explicit
  confirmation.
- Do not commit to `main` without the operator's explicit confirmation
  during early phases.
