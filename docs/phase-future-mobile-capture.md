# Future consideration: mobile capture

## The problem

Theoros currently captures only from the operator's Fedora
desktop. the operator reports that >50% of his browsing happens on
phone. Without mobile capture, Theoros's witness is
systematically biased toward desktop-time activity, which has
a different texture than phone-time activity.

This is a structural gap, not a missing feature: the
reflections Theoros produces will under-represent phone-time
patterns of attention, mood, social engagement, and reading.

## Out of scope for current phases

Adding mobile capture is at minimum a phase as large as Phase 2
(capture). Considerations:

- iOS vs Android, very different capture surfaces
- No AT-SPI equivalent
- App-store distribution and sandboxing
- Battery / privacy implications of always-on capture
- Cross-device sync model (does mobile write directly to
  Supabase? to a relay running on the desktop? to a separate
  local store on the phone?)

## What this changes about current decisions

Schema-level:
- chain_id and tab_session_id assume "tab" as a unit. Mobile
  has no tabs in the desktop sense. A future schema may need
  a more abstract "session" identifier.
- source_identifier assumes URL. Mobile content may not have
  URLs (in-app reading, native composers, etc.).

The good news: the current schema has TEXT columns for these
identifiers, not enforced foreign keys. Mobile capture can
populate them with mobile-shaped values without a migration.

## When to revisit

Before Phase 5 (reading surface) at minimum. Possibly before
Phase 4 (Claude reflections), because the reflections will
visibly bias toward desktop activity if mobile is missing.
