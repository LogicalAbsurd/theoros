# Theoros — Mobile Capture Design

Date: 2026-05-04
Status: Design (pre-implementation)

## Why this exists

Theoros currently observes one machine: the operator's Fedora desktop.
Estimated >50% of the operator's actual reading life happens on a
Samsung Galaxy S25 Ultra — AI chats in apps, social media in apps,
Firefox browsing, Discord. Every reflection Theoros writes today is
structurally biased toward the desktop slice. Embeddings, theme files,
and long-arc synthesis built on this archive will inherit and entrench
that bias.

Mobile capture closes the gap before Phase 5 (Supabase promotion +
reading surface) commits the desktop bias to derived storage.

## Architectural fit

Mobile capture is a new *Tier 4* in the existing capture model. It
behaves like a fourth source feeding the same capture service, the
same SQLite event log, the same downstream pipeline (session
detection → distillation → reflection). No changes to the core six-
layer architecture; only new ingestion.

The "local-first" rule from the spec is preserved: raw mobile captures
travel from the phone to the desktop's capture service over an
authenticated tunnel and land in the same local SQLite event log as
desktop captures. Only distilled outputs continue to flow to Supabase.

## Transport

Cloudflare Tunnel + Zero Trust service token.

The operator already runs a Cloudflare Tunnel exposing a local LLM web
UI behind Zero Trust on a personal domain. The same tunnel
infrastructure adds a new hostname route for the capture service —
e.g., `theoros-capture.<domain>` → `127.0.0.1:8765`. Zero Trust
service tokens (Cloudflare's machine-to-machine auth pattern) provide
non-interactive authentication: the Android app stores a service token
in encrypted storage and includes it in request headers. No login
screen, no session management, no manual re-auth.

Trade-offs accepted:
- Capture service is now reachable from the internet behind Zero Trust
  auth. Attack surface exists but is bounded by Cloudflare's edge auth
  and the service token requirement.
- Desktop must be on for live capture. The operator runs the desktop
  24/7 when possible. Offline queueing is deferred to a later
  iteration; v1 drops captures that occur during desktop downtime.

Rejected alternatives:
- **Tailscale**: conflicts with the operator's Mullvad VPN (Android
  permits one VPN at a time).
- **Direct-to-Supabase**: violates the architectural rule that raw
  captures stay local.

## Capture mechanism

Android Accessibility Service.

A custom Android app registers as an accessibility service. The OS
delivers events to the service whenever window content changes,
focus shifts, or text updates occur in any app on the device. The
service reads the relevant text content from the AccessibilityNodeInfo
tree, applies exclusion logic, redacts credential patterns, and
dispatches a capture record to the desktop tunnel.

This is the same architectural role AT-SPI plays on Linux — read
focused-window text from a system accessibility layer — adapted to
Android's API.

Rejected alternatives:
- **Share-sheet target**: requires manual operator action per capture.
  Misses ambient reading.
- **Firefox-only mobile extension**: covers <20% of mobile activity.
  Misses every native app.
- **Clipboard monitor**: captures only what is explicitly copied.
- **Background screenshot + OCR**: Android restricts background
  screenshotting outside accessibility services. Battery-expensive.
  Accessibility service text extraction is strictly better when
  available.

## Exclusion model — five layers

Privacy controls are load-bearing architecture, not a feature. Layered
defense, each layer independent of the others.

### Layer 1: App-level exclusion list

Operator-maintained config inside the app. List of Android package
names (e.g., `com.example.app`). When an excluded package is
foregrounded, the accessibility service halts capture for that focus
context entirely. No text reads, no metadata, nothing.

Config is local to the device. Editable through the app's settings UI.
Never transmitted off-device, never logged, never enters version
control.

### Layer 2: Browser private-mode exclusion

Firefox on Android exposes private-tab state via accessibility node
info flags. The service inspects this on every Firefox capture and
skips when private mode is detected. Operator confirmation that this
flag is reliably exposed will be verified during early implementation.

### Layer 3: URL exclusion

The desktop's existing URL exclusion list syncs to the phone (read-
only mirror, fetched periodically from the capture service). Firefox
captures check the URL against the list before transmission.

### Layer 4: Manual pause toggle

Quick-settings tile and persistent notification action. One tap halts
all capture. State is visible to the operator at all times via the
tile/notification badge. Resumes only on explicit operator action; no
auto-resume.

### Layer 5: Credential pattern redaction

Reuses the seven regex patterns from the desktop pipeline (API keys,
bearer tokens, credit cards with Luhn validation, SSNs, etc.). Runs
on every captured text payload before transmission. Replaces matches
with `[redacted: pattern type]` placeholders.

### Combinatorial behavior

A capture proceeds only if: app is not excluded **and** (if browser)
private mode is not active **and** (if URL is present) URL is not
excluded **and** pause is not engaged. Credential redaction runs on
the payload regardless. Failure of any check halts capture for that
event.

## Data model

Tier 4 captures fit the existing `raw_events` schema with one new
`source_tier` value: `mobile`. Migration 007 widens the
`source_tier` CHECK constraint.

Additional metadata fields useful for mobile that map cleanly into
existing columns:
- `source_app` → Android package name
- `title` → window/activity title from accessibility tree
- `url_or_path` → URL when extractable (Firefox), null otherwise
- `raw_text` → extracted text content
- `confidence` → set per-extraction based on node-tree completeness

No schema redesign required.

## Throttling and dedup

The accessibility service receives events at high frequency (every
text change, every focus shift). Naive forwarding would flood the
capture service and waste battery on transmission.

Approach:
- **Event coalescing**: buffer events per focus context, flush after
  an idle threshold (~3-5 seconds of no further events) or focus
  change.
- **Content-hash dedup**: hash the extracted text per focus context;
  skip if identical to the last hash for that context. Same pattern
  as the desktop browser tier.
- **Idle suppression**: if the device screen is off, suspend capture
  entirely (the accessibility service can register for screen state).

These are battery and signal optimizations, not privacy controls. The
exclusion layers above are the privacy controls.

## Local queueing — deferred

V1 assumes desktop reachability. Captures that fail to transmit
(network error, desktop offline, tunnel down) are dropped with a log
entry. Acceptable for v1 given the operator's 24/7 desktop pattern.

V2 (post-mobile-MVP) adds a local SQLite queue on the phone with
retry logic and bounded retention. Marked as a known follow-up, not
shipped initially.

## Phased build

- **M0 — Onboarding and proof of plumbing.**
  Install Android Studio. Build and sideload a hello-world Kotlin app
  on the S25 Ultra. Confirm the build/sign/deploy loop works on
  Fedora. No Theoros code yet.

- **M1 — Minimal accessibility service.**
  App that registers as an accessibility service and logs (locally,
  to logcat) the package name and title of every focused window. No
  text extraction, no network. Goal: prove the accessibility service
  lifecycle works on Samsung One UI, prove battery cost is
  acceptable, prove the service stays alive across reboots and
  Samsung's aggressive background killing.

- **M2 — Text extraction.**
  Add accessibility node tree text extraction. Still local-only,
  still logging to logcat. Validate that extraction produces useful
  text from target apps (Firefox, AI chat apps, Discord, social).

- **M3 — Exclusion layers.**
  Implement all five exclusion layers. Test each independently. App
  is now safe to leave running on real device with real activity.

- **M4 — Cloudflare Tunnel route + auth.**
  Add the capture-service hostname to the existing Cloudflare Tunnel.
  Generate a Zero Trust service token. Configure the desktop capture
  service to validate the token. Test from the phone with curl-
  equivalent (or a desktop test client) before involving the app.

- **M5 — Network transmission.**
  App POSTs captures to the tunnel endpoint. Throttling, dedup, and
  idle suppression in place. End-to-end mobile → desktop capture
  working.

- **M6 — Migration 007 and integration.**
  Widen `source_tier` CHECK to include `mobile`. Capture service
  validates and writes mobile records to the existing `raw_events`
  table. Session detector, distillation, and reflection consume them
  alongside desktop records with no further changes.

Each milestone ends with the operator able to verify the work
independently. No milestone introduces both new capture surface and
new transmission surface in the same step.

## Open questions

These are deferred to implementation but flagged now so they don't
ambush the build:

1. **Samsung One UI background killing.** Samsung is aggressive about
   killing background services. The accessibility service has more
   protection than a normal background service, but One UI's battery
   optimization may still interfere. May require operator-side
   configuration (excluding the app from battery optimization) and
   we'll know how bad it is after M1.

2. **Firefox private mode flag reliability.** Layer 2 depends on
   Firefox exposing `isPrivate` via accessibility node info
   consistently. To be verified during M2.

3. **Accessibility service permission UX.** Granting an accessibility
   service is a multi-step process and Android shows scary warnings
   (rightly so — the permission is powerful). One-time onboarding
   pain, not ongoing.

4. **AI chat app coverage.** Some apps render text in custom views
   that don't fully expose content to accessibility services. Will
   need to verify each target app during M2 and document gaps.

5. **App distribution.** Sideload only? F-Droid (would require open-
   sourcing)? Self-hosted update mechanism? Deferred until app
   exists.

## Non-goals for v1

- Offline queueing (deferred to v2)
- iOS support (operator has Android only)
- Cross-device synchronization between multiple phones
- Web-based admin UI for the mobile app (settings live in the app)
- Tablet support (deferred until a tablet enters the picture)

## Success criteria

V1 is complete when:
- Capture runs on the S25 Ultra for a full day without crashing or
  draining battery noticeably.
- Captures from at least Firefox, one AI chat app, and Discord land
  in the desktop's `raw_events` table with correct `source_tier`,
  `source_app`, and `raw_text`.
- All five exclusion layers verifiably block capture in their
  respective conditions.
- The downstream pipeline (session detection, distillation,
  reflection) processes mobile records without modification beyond
  Migration 007.
