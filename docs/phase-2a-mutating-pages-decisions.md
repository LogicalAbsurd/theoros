# Phase 2a → Phase 2 redesign — mutating-pages decisions

These are the operator's answers from the late-night discussion that
closed Phase 2a. They are fixed points for next session's
redesign, not points to relitigate.

## 1. Unit of capture for mutating pages
Deltas. Continuation chain reconstructible into the page state
at any moment.

## 2. Handling text removal
The captured record reflects the current state. Removed text is
deleted from capture. Reading: the live state is the truth;
revision history of in-progress drafts is not retained.
(NOTE: Consider whether this conflicts with "raw captures are
the source of truth" — possibly OK, possibly worth revisiting.)

## 3. Engagement-shape patterns
The capture/normalization layer faithfully records focus
rhythms (tab focus events, session boundaries, dwell intervals).
Interpretation of those rhythms ("got distracted by something
shiny", "active for 45 min") happens in Phase 4 reflection,
not at capture time.

## 4. What graduates to Supabase
NOT the raw chat or the full delta chain.
DOES go: Theoros-generated detailed summaries of what was
discussed, how it was discussed, and the engagement shape.

Example target voice and detail level:
"He spent 45 minutes coding a new project called XXXXX with
Claude. They decided on a schema for it, made directory
structure, and wrote YYYY and ZZZZ, pushed to git and stopped
for the night."

"He talked to Gemini about the possibility of metaphysical
intelligences for 30 minutes on and off. They explored
Gnostic 'evil archons,' the theological possibilities of the
ground of existence, then pivoted into Kierkegaard's relational
consciousness theory and how it applies to the AI-human
interface contained within his own omnisyncretic framework.
This discussion occurred in two sections, one an active
engagement of 12 minutes and the other 20 minutes on and off,
with a 7-minute break between them, presumably to get a snack
because that's what he told Gemini he was doing. He may have
lied. idk I just work here."

Implications for Phase 3 (local LLM distillation):
- Preserve specialized vocabulary verbatim (don't flatten
  "evil archons" to "metaphysical concepts").
- Voice can be dry/honest — system has limits and doesn't
  pretend omniscience.
- Summaries reference engagement shape, not just content.
