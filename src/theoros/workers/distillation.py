"""Theoros distillation worker — Phase 3.

Periodic worker that finds sessions without summaries, gathers their
captured text, sends it to a local LLM for structured summarization,
and writes the result back to sessions.summary.

The worker asks for a JSON summary with main topics, verbatim names, a
brief narrative, and notable patterns — designed so downstream
reflections (Phase 4) have structured evidence to draw on, not
flattened-neutral descriptions.

Distillation tracking is implicit: sessions where summary IS NULL have
not been distilled yet.  Once a session's summary is written, it won't
be reprocessed.

Run:
    python -m theoros.workers.distillation
"""

# Architectural note: distillation calls a local llama-server via a
# `/raw` pass-through route — the spec's "local LLM for evidence"
# shape.  `/raw` accepts {system, prompt, max_tokens, temperature} and
# returns {response: "..."}, side-stepping
# the OpenAI chat-completions envelope.  Local models are less reliable
# at clean JSON output than Claude, so _extract_json_object defensively
# unwraps markdown fences and pre/post commentary before json.loads.

from __future__ import annotations

import json
import logging
import re
import signal
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from threading import Event

from pydantic_settings import BaseSettings

# Reuse database utilities from the capture service.  open_connection
# sets WAL mode and foreign keys; reconstruct_chain_text walks delta
# chains to recover full text at a given position.  Both are
# schema-level concerns shared across the codebase — if this coupling
# becomes awkward later, they can move to a shared theoros.db module.
from theoros.services.capture.db import open_connection, reconstruct_chain_text


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# distillation.py lives at src/theoros/workers/ — three parents up to
# the repo root.
REPO_ROOT = Path(__file__).resolve().parents[3]

log = logging.getLogger("theoros.distillation")


class DistillationSettings(BaseSettings):
    """Settings loaded from environment / .env file."""

    # Path to the local SQLite database.  If relative, resolved against
    # the repo root.
    theoros_db_path: str = "memory/system/theoros.db"

    # URL of the local LLM server.  Shared with the reflection worker
    # — a single GPU means one llama-server at a time.
    theoros_llm_url: str = "http://127.0.0.1:8000"

    # Maximum tokens for the distillation response.
    theoros_distillation_max_tokens: int = 1024

    # Sampling temperature for the local model.  Distillation wants
    # structured, low-variance output, so we run cooler than reflection.
    theoros_distillation_temperature: float = 0.2

    # HTTP timeout for local LLM calls, in seconds.  Higher than the
    # old API default because local inference on a 3080 can take a
    # while on longer prompts.
    theoros_distillation_timeout: int = 120

    # Seconds between distillation passes.
    theoros_distillation_interval: int = 900  # 15 minutes

    # Sessions with fewer events than this are skipped — not enough
    # content for a meaningful summary.  They'll be picked up if more
    # events land in the session later.
    theoros_distillation_min_events: int = 3

    # Maximum characters of session text sent to Claude.  Keeps the
    # prompt within typical context windows.  Content is truncated per
    # chain piece and overall.
    theoros_distillation_max_text_chars: int = 12_000

    # Maximum times a session can fail distillation before being marked
    # with a skip summary.  Prevents a single bad session from blocking
    # the entire pipeline indefinitely.
    theoros_distillation_max_retries: int = 3

    # Path to the operator-identity block loaded into the distillation
    # prompt at runtime.  Kept in the gitignored memory/ tree so real
    # names never enter version control; a template lives at
    # docs/operator_identity.example.txt.  If the file is missing, the
    # worker falls back to a generic first-person rule (see
    # FALLBACK_IDENTITY_BLOCK).
    theoros_operator_identity_path: str = "memory/system/operator_identity.txt"

    model_config = {
        "env_file": str(REPO_ROOT / ".env"),
        "env_file_encoding": "utf-8",
        "case_sensitive": False,
        "extra": "ignore",
    }

    @property
    def db_path(self) -> Path:
        """Return the database path, resolved against repo root if relative."""
        p = Path(self.theoros_db_path)
        if p.is_absolute():
            return p
        return REPO_ROOT / p

    @property
    def operator_identity_path(self) -> Path:
        """Return the identity-block path, resolved against repo root if relative."""
        p = Path(self.theoros_operator_identity_path)
        if p.is_absolute():
            return p
        return REPO_ROOT / p


# ---------------------------------------------------------------------------
# LLM prompt
# ---------------------------------------------------------------------------

# The system prompt is assembled at runtime from three parts: a fixed
# preamble, an operator-identity block (loaded from a gitignored local
# file so real names never enter the repo), and a fixed body describing
# the JSON contract and rules.  Splitting it this way — rather than
# using str.format — keeps the JSON example's literal curly braces from
# needing to be escaped.

_PROMPT_PREAMBLE = """\
You are a summarization engine for Theoros, a personal memory system that \
captures what its operator reads, writes, and attends to on screen. Your \
primary responsibility is faithful compression, not interpretation. Every \
statement in your output must be directly supported by the supplied text. \
When uncertain, choose the less specific interpretation. Omit unsupported \
details rather than infer them."""

# Generic fallback used when the operator-identity file is absent (fresh
# clone, template deploy, etc).  Weaker than a fully populated block —
# it lacks the specific-names enumeration that catches confabulations
# where a recurring handle gets misread as an external entity — but
# strong enough to hold the first-person-is-the-operator invariant.
FALLBACK_IDENTITY_BLOCK = """\
Operator identity — this rule overrides all other pattern-matching. The \
person whose screen this is — the operator — is the author of any \
first-person text captured. Handles, usernames, folder names, filenames, \
and project names that recur in first-person contexts should be treated \
as the operator's own, not as external entities. Do not characterize the \
operator's own names as groups, communities, cults, projects, or things \
the operator is engaging with, leaving, joining, recovering from, or \
standing across from. When uncertain whether a name refers to the \
operator or to a third party, prefer the interpretation that keeps \
first-person material attributed to the operator."""

_PROMPT_BODY = """\
You receive the text content from one computer session and produce a \
structured summary. Output valid JSON with exactly these fields:

{"topics":[],"names":[],"narrative":"","patterns":[]}

Your summary should capture substance, not speculation:
- What was explicitly read, viewed, discussed, or written.
- The observable character of the session.
- Major themes and attention shifts directly evident.
- Named people, projects, tools, concepts, and communities.
- Only patterns directly supported by repeated or observable evidence.

Field requirements:
- topics: 3-8 explicit themes.
- names: names exactly as they appear in the text — people, handles, \
projects, tools, titles, files — listed verbatim, without asserting what \
kind of thing each one is.
- narrative: 2-4 sentences describing only observable content.
- patterns: 0-5 directly observable repetitions or transitions.
- Empty arrays are preferred over speculative entries.

Rules:
- Never invent missing context, motivations, beliefs, goals, emotions, or \
relationships.
- Never combine separate observations into a new conclusion unless that \
conclusion is explicitly supported.
- Describe what was encountered, not what probably happened.
- Most captured material was READ, not authored by the operator. Do not \
attribute AI responses or third-party text to them.
- OCR establishes only screen presence. Prefer "appeared on screen" or \
"screenshots showed" rather than engagement verbs.
- Do not assume material was recent unless a date explicitly appears.
- Before emitting each statement, ensure it is supported by supplied text. \
If no direct evidence exists, omit it.
- No topic-bridging. State only topics whose words actually appear in the \
captured text. Do not infer an adjacent or implied topic from one that is \
present. A video about Solomon is a video about Solomon — not about \
Masonry, not about the Templars, unless those words appear. A page about \
the Rosicrucian Order is about the Rosicrucian Order — not about \
Freemasonry, unless the text says so. If two concepts commonly co-occur \
in the wider world but only one is on the screen, only the one on the \
screen belongs in the summary.
- No spatial-adjacency attribution. Never attribute textual content to a \
named source (channel, author, publisher, application, video, post) \
unless the captured text itself explicitly ties them together — a byline, \
a "posted by X" marker, an in-text signature, an app-tag directly \
preceding the content. Nearby-on-screen is not tied. Screens routinely \
render content from multiple sources side by side (a video plus an \
unrelated community post, a page plus a sidebar recommendation, an \
overlay from a different app). When distinct blocks of content interleave \
in the captured text, describe each block without assigning authorship, \
or omit the attribution entirely. "The page contained a description \
mentioning X" is safer than "channel Y's video was about X" when Y's tag \
is only nearby, not tied to X.

Respond with only the JSON object."""


def _load_identity_block(path: Path) -> str:
    """Read the operator-identity block from disk, or return the fallback.

    The identity block enumerates the operator's real names and handles;
    keeping it in a gitignored file (memory/system/operator_identity.txt
    by default) means the enumeration never enters version control.  If
    the file is missing or unreadable we fall back to a generic
    first-person rule — weaker but still coherent.
    """
    try:
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        log.warning(
            "operator-identity file not found at %s; "
            "using generic first-person fallback",
            path,
        )
        return FALLBACK_IDENTITY_BLOCK
    except OSError as exc:
        log.warning(
            "could not read operator-identity file at %s (%s); "
            "using generic first-person fallback",
            path,
            exc,
        )
        return FALLBACK_IDENTITY_BLOCK

    if not text:
        log.warning(
            "operator-identity file at %s is empty; "
            "using generic first-person fallback",
            path,
        )
        return FALLBACK_IDENTITY_BLOCK

    return text


def _build_system_prompt(identity_block: str) -> str:
    """Assemble the full system prompt from its three parts."""
    return f"{_PROMPT_PREAMBLE}\n\n{identity_block}\n\n{_PROMPT_BODY}"


# ---------------------------------------------------------------------------
# Database queries
# ---------------------------------------------------------------------------

def _check_sessions_table(db_path: Path) -> None:
    """Verify that the sessions table exists, raising if it doesn't."""
    conn = open_connection(db_path)
    try:
        cursor = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='sessions'"
        )
        if cursor.fetchone() is None:
            raise RuntimeError(
                f"sessions table not found in {db_path}.  "
                f"Run `python -m theoros.db.migrate` to create the schema."
            )
    finally:
        conn.close()


def _find_undistilled_sessions(
    conn: sqlite3.Connection, min_events: int
) -> list[dict]:
    """Find sessions without summaries that have enough events to distill."""
    cursor = conn.execute(
        "SELECT id, started_at, ended_at, event_count, dominant_source_app "
        "FROM sessions "
        "WHERE summary IS NULL AND event_count >= ? "
        "ORDER BY started_at",
        (min_events,),
    )
    return [
        {
            "id": row[0],
            "started_at": row[1],
            "ended_at": row[2],
            "event_count": row[3],
            "dominant_source_app": row[4],
        }
        for row in cursor.fetchall()
    ]


def _gather_session_text(
    conn: sqlite3.Connection, session_id: int, max_chars: int
) -> str:
    """Reconstruct the textual content of a session for the LLM prompt.

    Groups events by chain_id and reconstructs the final state of each
    chain — the most complete version of each piece of content, without
    redundant intermediate deltas.  Includes title and source context so
    the LLM knows what it's looking at.
    """
    cursor = conn.execute(
        "SELECT id, chain_id, chain_position, is_chain_root, "
        "  content_text, title, source_identifier, source_tier, source_app "
        "FROM raw_events "
        "WHERE session_id = ? "
        "ORDER BY captured_at",
        (session_id,),
    )
    rows = cursor.fetchall()

    # Track the highest chain position seen per chain, along with
    # metadata from the most informative row.
    chains: dict[str, dict] = {}
    for (event_id, chain_id, chain_pos, is_root,
         content_text, title, source_id, source_tier, source_app) in rows:
        if chain_id is None:
            continue

        prev = chains.get(chain_id)
        if prev is None or chain_pos > prev["max_position"]:
            chains[chain_id] = {
                "max_position": chain_pos,
                "title": title,
                "source_identifier": source_id,
                "source_tier": source_tier,
                "source_app": source_app,
                "is_root_with_text": is_root == 1 and content_text is not None,
                "content_text": content_text,
            }
        # Prefer a non-null title from any row in the chain.
        if title and prev is not None and not prev.get("title"):
            chains[chain_id]["title"] = title

    # Reconstruct text for each chain and build prompt pieces.
    pieces: list[str] = []
    total_chars = 0

    for chain_id, info in chains.items():
        if total_chars >= max_chars:
            break

        # Fast path: single-row chain with content_text available.
        if info["is_root_with_text"] and info["max_position"] == 1:
            text = info["content_text"] or ""
        else:
            text = reconstruct_chain_text(conn, chain_id, info["max_position"])

        if not text.strip():
            continue

        # Build a header line for context.
        header_parts = []
        if info.get("title"):
            header_parts.append(info["title"])
        if info.get("source_app"):
            header_parts.append(f"[{info['source_app']}]")
        elif info.get("source_tier"):
            header_parts.append(f"[{info['source_tier']}]")
        header = " — ".join(header_parts) if header_parts else "[untitled]"

        # Truncate this piece if we're running out of budget.
        remaining = max_chars - total_chars
        if len(text) > remaining:
            text = text[:remaining] + "\n[truncated]"

        pieces.append(f"--- {header} ---\n{text}")
        total_chars += len(text)

    return "\n\n".join(pieces)


# ---------------------------------------------------------------------------
# LLM interaction
# ---------------------------------------------------------------------------

def _extract_json_object(text: str) -> dict | None:
    """Pull a JSON object out of an LLM response that may include
    surrounding prose or markdown fences.

    Local models often add a preamble ("Here is the summary:") or sign
    off after the JSON, and sometimes wrap the object in ```json
    fences.  We strip a fenced block if present, then scan from the
    first `{` to the last `}` to recover the object.  Returns None if
    nothing parseable is found.
    """
    text = text.strip()
    if not text:
        return None

    # If the model wrapped the response in a fenced block, pull out the
    # body.  Handles both ```json ... ``` and bare ``` ... ```.  Only
    # the first fence is honored — distillation responses shouldn't
    # contain multiple JSON blocks.
    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()

    first = text.find("{")
    last = text.rfind("}")
    if first == -1 or last == -1 or last < first:
        return None

    try:
        return json.loads(text[first : last + 1])
    except json.JSONDecodeError:
        return None


def _call_local_llm(
    settings: DistillationSettings,
    session_text: str,
    session_meta: dict,
    system_prompt: str,
) -> dict | None:
    """Send session text to the local LLM via logos_gate's /raw route
    and parse the structured summary.

    /raw is a thin pass-through that accepts {system, prompt,
    max_tokens, temperature} and returns {response: "..."}.  We then
    defensively extract the JSON object from the response — local
    models are less reliable about clean JSON than Claude is.

    Returns the parsed dict on success, or None on failure.
    """
    user_content = (
        f"Session from {session_meta['started_at']} to "
        f"{session_meta['ended_at']}, "
        f"{session_meta['event_count']} events"
    )
    if session_meta.get("dominant_source_app"):
        user_content += f", primarily in {session_meta['dominant_source_app']}"
    user_content += f".\n\nCaptured content:\n\n{session_text}"

    payload = {
        "system": system_prompt,
        "prompt": user_content,
        "max_tokens": settings.theoros_distillation_max_tokens,
        "temperature": settings.theoros_distillation_temperature,
    }

    url = f"{settings.theoros_llm_url.rstrip('/')}/raw"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(
            req, timeout=settings.theoros_distillation_timeout
        ) as resp:
            body = json.loads(resp.read().decode("utf-8"))

        content = body.get("response", "")
        if not content or not content.strip():
            log.warning("local LLM response had empty content")
            return None

        summary = _extract_json_object(content)
        if summary is None:
            # Log a snippet of what the model actually returned so a
            # human can tell whether the prompt drifted or the model is
            # malfunctioning.
            log.warning(
                "failed to extract JSON from local LLM response: %r",
                content[:300],
            )
            return None

        # Validate expected fields exist (permissive — don't fail on
        # extra fields, just ensure the ones we need are present).
        for key in ("topics", "names", "narrative", "patterns"):
            if key not in summary:
                log.warning("local LLM response missing '%s' field", key)
                summary.setdefault(key, [] if key != "narrative" else "")

        return summary

    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        log.warning("local LLM HTTP %d: %s", exc.code, error_body)
        return None
    except urllib.error.URLError as exc:
        log.warning("local LLM unreachable (%s): %s", url, exc)
        return None
    except (json.JSONDecodeError, KeyError, IndexError) as exc:
        log.warning("failed to parse local LLM envelope: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Summary write-back
# ---------------------------------------------------------------------------

def _write_summary(
    conn: sqlite3.Connection, session_id: int, summary: dict
) -> None:
    """Write a structured summary to the sessions.summary column as JSON.

    The schema defines summary as TEXT.  Storing JSON preserves the
    structured fields (topics, names, narrative, patterns) so the
    Phase 4 reflection pipeline can parse them directly rather than
    re-extracting from prose.
    """
    conn.execute(
        "UPDATE sessions SET summary = ? WHERE id = ?",
        (json.dumps(summary, indent=2), session_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Distillation cycle
# ---------------------------------------------------------------------------

def _make_skip_summary(session: dict, attempts: int) -> dict:
    """Build a placeholder summary for a session that exhausted retries.

    Written to sessions.summary so the session won't be retried on
    subsequent cycles.  The 'skipped' flag lets downstream consumers
    know this isn't a real distillation.
    """
    return {
        "topics": [],
        "names": [],
        "narrative": (
            f"Distillation skipped after {attempts} failed attempts.  "
            f"Session had {session['event_count']} events from "
            f"{session['started_at']} to {session['ended_at']}."
        ),
        "patterns": [],
        "skipped": True,
    }


def _distillation_cycle(
    settings: DistillationSettings,
    retry_counts: dict[int, int],
) -> int:
    """Run one distillation pass.  Returns the number of sessions distilled.

    retry_counts maps session IDs to their cumulative failure count
    across cycles within this worker run.  When a session exceeds
    max_retries, it's marked with a skip summary so it stops blocking
    the pipeline.
    """
    # Reload the identity block each cycle so edits to
    # memory/system/operator_identity.txt take effect without a worker
    # restart.  Cheap — a small text file, once per 15-minute cycle.
    identity_block = _load_identity_block(settings.operator_identity_path)
    system_prompt = _build_system_prompt(identity_block)

    conn = open_connection(settings.db_path)
    try:
        sessions = _find_undistilled_sessions(
            conn, settings.theoros_distillation_min_events
        )
        if not sessions:
            log.debug("no undistilled sessions found")
            return 0

        log.info("found %d undistilled session(s)", len(sessions))
        distilled = 0

        for session in sessions:
            sid = session["id"]
            log.info(
                "distilling session %d (%s to %s, %d events)",
                sid,
                session["started_at"],
                session["ended_at"],
                session["event_count"],
            )

            text = _gather_session_text(
                conn, sid, settings.theoros_distillation_max_text_chars
            )
            if not text.strip():
                log.warning("session %d has no text content, skipping", sid)
                continue

            summary = _call_local_llm(settings, text, session, system_prompt)
            if summary is None:
                retry_counts[sid] = retry_counts.get(sid, 0) + 1
                attempts = retry_counts[sid]

                if attempts >= settings.theoros_distillation_max_retries:
                    log.warning(
                        "session %d failed %d times, marking as skipped",
                        sid,
                        attempts,
                    )
                    _write_summary(conn, sid, _make_skip_summary(session, attempts))
                    continue

                log.warning(
                    "no result for session %d (attempt %d/%d), "
                    "will retry next cycle",
                    sid,
                    attempts,
                    settings.theoros_distillation_max_retries,
                )
                # Stop processing further sessions this cycle — the LLM
                # is likely down and retrying immediately wastes time.
                break

            _write_summary(conn, sid, summary)
            distilled += 1
            log.info(
                "session %d distilled: %d topics, %d names",
                sid,
                len(summary.get("topics", [])),
                len(summary.get("names", [])),
            )

        return distilled
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    settings = DistillationSettings()

    log.info("starting theoros distillation worker")
    log.info("database: %s", settings.db_path)
    log.info("LLM endpoint: %s", settings.theoros_llm_url)
    log.info("temperature: %.2f", settings.theoros_distillation_temperature)
    log.info(
        "interval: %ds, min events per session: %d",
        settings.theoros_distillation_interval,
        settings.theoros_distillation_min_events,
    )

    # Surface which identity source is in effect at startup so a missing
    # or misnamed operator_identity.txt is obvious in the logs rather
    # than only showing up as weaker distillations.
    if settings.operator_identity_path.exists():
        log.info(
            "operator identity: loaded from %s",
            settings.operator_identity_path,
        )
    else:
        log.warning(
            "operator identity: file missing at %s — using generic fallback",
            settings.operator_identity_path,
        )

    _check_sessions_table(settings.db_path)

    shutdown = Event()

    def _handle_signal(signum: int, _frame: object) -> None:
        log.info("received signal %d, shutting down", signum)
        shutdown.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    # Track per-session failure counts across cycles within this worker
    # run.  Resets on restart — no schema changes needed.
    retry_counts: dict[int, int] = {}

    # Run an initial cycle immediately on startup so we don't wait a
    # full interval before processing anything that's already queued.
    try:
        distilled = _distillation_cycle(settings, retry_counts)
        if distilled:
            log.info("initial pass: distilled %d session(s)", distilled)
    except Exception:
        log.exception("error in initial distillation cycle")

    # Periodic loop.  Event.wait() with a timeout is interruptible by
    # signals — cleaner than time.sleep() which can block through
    # SIGTERM on some platforms.
    while not shutdown.is_set():
        shutdown.wait(timeout=settings.theoros_distillation_interval)
        if shutdown.is_set():
            break
        try:
            _distillation_cycle(settings, retry_counts)
        except Exception:
            log.exception("error in distillation cycle")

    log.info("theoros distillation worker stopped")


if __name__ == "__main__":
    main()
