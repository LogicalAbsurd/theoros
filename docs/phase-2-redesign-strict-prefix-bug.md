# Phase 2 redesign: strict-prefix matching is brittle on real pages

## What was built (commits 187db7a, 37ab43c)

Migration 004 added chain columns. The capture service now does:
1. Dedup check (tab_session_id + content_hash exact match) → return existing.
2. Find most recent prior in same (tab_session_id, source_identifier).
3. If new.startswith(prior_full_text) → continuation, store suffix as delta.
4. Else → breakpoint, new chain.

## What broke

Tested on this Claude.ai conversation (rows 12-15). Page tail
contains a "last activity" timestamp ("10:44 PM" → "10:45 PM")
that drifts every minute. Single-character UI mutation breaks
the strict prefix at character 534,199 of 534,203, triggering
a false-positive breakpoint.

This is not specific to Claude.ai. Many real pages have ambient
volatility:
- Relative timestamps ("2 min ago" → "3 min ago")
- Notification counts
- Status indicators
- Sidebar widgets that re-render

## Four candidate fixes

1. **Trailing-volatility tolerance**: strip last N chars of new
   before prefix check. Cheap, handles tail-drift only.
2. **Pattern normalization**: regex out known volatile patterns
   (timestamps etc.) before comparing. Fragile, many patterns.
3. **Longest-common-prefix logic**: drop strict prefix; compute
   LCP, treat as continuation if LCP is ≥ N% of prior length.
   Robust, but delta semantics change (delta is "from LCP
   boundary onward" not just "after prior end"), and
   reconstruct_chain_text no longer works by simple concat —
   each row needs to track the LCP boundary it was computed
   against.
4. **Tab-session+URL identity, LCP-based deltas**: chain identity
   follows tab_session_id + source_identifier alone. Within a
   chain, all captures are continuations. Deltas are computed
   via LCP. Breakpoints only on URL change or tab session
   change. Most robust; significant rewrite.

## Decision

TBD. the operator leaning toward option 4. Open question: is LCP-based
delta storage worth the complexity, or does option 1 handle
80% of real cases?

## Recommended next session plan

1. Apply option 1 first as a quick fix (trailing-volatility,
   strip last 500 chars). Commit.
2. Run Theoros for 24-48 hours of normal use.
3. Inspect what got captured: how many false-positive
   breakpoints occurred despite option 1?
4. Decide based on data whether option 4 is justified, or if
   option 1 is good enough.

## Test fixture

Rows 12-15 in raw_events demonstrate the bug. Row 14's full
reconstructed text should be a prefix of row 15's content_text
modulo the tail timestamp. Any future fix should result in
row 15 being a continuation of chain 41ad1bb2-... rather than
a new chain.
