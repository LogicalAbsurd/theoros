// background.js — Theoros browser extension background script (Chrome MV3 service worker)
//
// Two capture triggers:
// 1. Toolbar button click — manual capture of the active tab.
// 2. Dwell timer — auto-capture after 5s of sustained focus on a loaded
//    HTTP(S) page.
//
// Each tab gets a tab_session_id (UUID) that persists as long as the tab
// isn't unfocused for more than 10 minutes continuously.  Both triggers
// send the session ID and trigger type in raw_metadata so the capture
// service can dedup within a session while preserving cross-session
// captures of the same URL.
//
// All communication stays on 127.0.0.1.

const CAPTURE_SERVICE_URL = "http://127.0.0.1:8765/capture";
const EXCLUSIONS_URL = "http://127.0.0.1:8765/exclusions/urls";
const BADGE_DURATION_MS = 3_000;
const DWELL_MS = 5_000;
const UNFOCUSED_RESET_MS = 10 * 60 * 1_000; // 10 minutes

const STORAGE_KEY_TAB_SESSIONS = "tab_sessions";

const EXTENSION_VERSION = chrome.runtime.getManifest().version;

// ---------------------------------------------------------------------------
// Pause state — persisted to chrome.storage.local across restarts
// ---------------------------------------------------------------------------
let isPaused = false;

// ---------------------------------------------------------------------------
// Per-tab state
// ---------------------------------------------------------------------------
// Map<tabId, {
//   tab_session_id:    string   — UUID, reset after 10min unfocused
//                                 (reset scheduled via chrome.alarms so
//                                 it survives worker termination — see
//                                 the "Unfocused-reset alarm" section
//                                 below)
//   last_focused_at:   number   — timestamp (ms)
//   last_unfocused_at: number   — timestamp (ms)
//   dwell_timer:       number|null — setTimeout handle (5s; well under
//                                 the service worker's ~30s idle kill
//                                 window, so setTimeout is fine here)
//   last_url_at_load:  string|null — URL when dwell timer was set
// }>
const tabStates = new Map();

function getOrCreateTabState(tabId) {
  let state = tabStates.get(tabId);
  if (!state) {
    state = {
      tab_session_id: crypto.randomUUID(),
      last_focused_at: null,
      last_unfocused_at: null,
      dwell_timer: null,
      last_url_at_load: null,
    };
    tabStates.set(tabId, state);
    console.log(
      `[theoros] tab ${tabId}: created state, session_id ${state.tab_session_id}`
    );
    // Async: may overwrite the fresh UUID with a persisted one if the
    // script was terminated and reloaded within the 10-min window.
    // Completes well before any dwell timer (5s) fires.
    restorePersistedSession(tabId, state);
    // Persist immediately so tabs that stay focused (and never trigger
    // onTabUnfocused) survive script termination.  Brief race with
    // restorePersistedSession is harmless — restore overwrites if it
    // finds a persisted entry; after both settle, storage is correct.
    persistTabSession(tabId);
  }
  return state;
}

function clearTabState(tabId) {
  cancelDwellTimer(tabId);
  cancelUnfocusedResetAlarm(tabId);
  tabStates.delete(tabId);
  removePersistedTabSession(tabId);
}

// ---------------------------------------------------------------------------
// Tab session persistence — survives background script termination
// ---------------------------------------------------------------------------
// Only tab_session_id and last_unfocused_at are persisted; timers and
// transient focus state reinitialize from browser state on script reload.
//
// Read-modify-write on chrome.storage.local is safe here: this extension
// runs in a single background service worker with no concurrent writers.

async function loadPersistedTabSessions() {
  const result = await chrome.storage.local.get(STORAGE_KEY_TAB_SESSIONS);
  return result[STORAGE_KEY_TAB_SESSIONS] || {};
}

async function persistTabSession(tabId) {
  const state = tabStates.get(tabId);
  if (!state) return;
  const stored = await loadPersistedTabSessions();
  stored[tabId] = {
    tab_session_id: state.tab_session_id,
    last_unfocused_at: state.last_unfocused_at,
  };
  await chrome.storage.local.set({ [STORAGE_KEY_TAB_SESSIONS]: stored });
}

async function removePersistedTabSession(tabId) {
  const stored = await loadPersistedTabSessions();
  if (!(tabId in stored)) return;
  delete stored[tabId];
  await chrome.storage.local.set({ [STORAGE_KEY_TAB_SESSIONS]: stored });
}

/**
 * If a persisted session exists for this tab and the 10-min unfocused
 * window hasn't elapsed, restore it into the in-memory state (overwriting
 * the fresh UUID that getOrCreateTabState minted synchronously).
 */
async function restorePersistedSession(tabId, state) {
  const stored = await loadPersistedTabSessions();
  const persisted = stored[tabId];
  if (!persisted) return;

  if (
    persisted.last_unfocused_at &&
    Date.now() - persisted.last_unfocused_at >= UNFOCUSED_RESET_MS
  ) {
    // Session expired while the script was unloaded — discard stale entry.
    removePersistedTabSession(tabId);
    return;
  }

  state.tab_session_id = persisted.tab_session_id;
  state.last_unfocused_at = persisted.last_unfocused_at;
  console.log(
    `[theoros] tab ${tabId}: restored session ${state.tab_session_id} from storage`
  );
}

// ---------------------------------------------------------------------------
// Focus tracking
// ---------------------------------------------------------------------------
// The "currently focused tab" is the active tab in the focused window,
// provided the browser has OS focus (windowId !== WINDOW_ID_NONE).

let focusedWindowId = chrome.windows.WINDOW_ID_NONE;
let currentFocusedTabId = null;

function isHttpUrl(url) {
  return typeof url === "string" && url.startsWith("http");
}

// ---------------------------------------------------------------------------
// URL exclusion helpers
// ---------------------------------------------------------------------------

async function loadExclusions() {
  // Reads from the server-synced cache (populated by
  // refreshExclusionsFromServer on lifecycle events and after every
  // write-through). Falls back to empty lists if the cache hasn't been
  // populated yet (e.g., capture service was unreachable at startup).
  const result = await chrome.storage.local.get("exclusions_cache");
  return result.exclusions_cache || { domains: [], path_prefixes: [] };
}

/**
 * Check whether a URL matches the operator's exclusion list.
 *
 * Domain matching: hostname is the entry OR ends with "."+entry.
 * Path-prefix matching: domain portion matches as above AND the URL
 * path starts with the prefix's path component.
 */
async function isUrlExcluded(url) {
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    return false;
  }

  const exclusions = await loadExclusions();
  const hostname = parsed.hostname;

  function hostMatches(host, entry) {
    return host === entry || host.endsWith("." + entry);
  }

  for (const domain of exclusions.domains) {
    if (hostMatches(hostname, domain)) return true;
  }

  for (const prefix of exclusions.path_prefixes) {
    const slashIndex = prefix.indexOf("/");
    if (slashIndex === -1) continue;
    const entryHost = prefix.slice(0, slashIndex);
    const entryPath = prefix.slice(slashIndex); // includes leading /
    if (hostMatches(hostname, entryHost) && parsed.pathname.startsWith(entryPath)) {
      return true;
    }
  }

  return false;
}

// ---------------------------------------------------------------------------
// Exclusion migration — one-time push of chrome.storage.local data to server
// ---------------------------------------------------------------------------
// On first run after the Layer 3 update, any exclusions the user configured
// via the options page (stored in chrome.storage.local["exclusions"]) are
// POSTed to the capture service so they become the server-authoritative copy.
// The server's UNIQUE constraint makes each POST idempotent, so partial
// failures retry safely on next extension load.

async function migrateExclusionsToServer() {
  const { exclusions_migrated_v1 } = await chrome.storage.local.get(
    "exclusions_migrated_v1"
  );
  if (exclusions_migrated_v1) return;

  const { exclusions } = await chrome.storage.local.get("exclusions");
  if (!exclusions) {
    // Nothing to migrate — set the flag so we don't check again.
    console.log("[theoros] no local exclusions to migrate");
    await chrome.storage.local.set({ exclusions_migrated_v1: true });
    return;
  }

  const domains = exclusions.domains || [];
  const pathPrefixes = exclusions.pathPrefixes || [];
  console.log(
    `[theoros] migrating ${domains.length} domains and ${pathPrefixes.length} path prefixes to capture service`
  );

  try {
    for (const domain of domains) {
      const resp = await fetch(EXCLUSIONS_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kind: "domain", value: domain }),
      });
      if (!resp.ok) {
        throw new Error(`POST domain "${domain}" failed: ${resp.status}`);
      }
    }

    for (const prefix of pathPrefixes) {
      const resp = await fetch(EXCLUSIONS_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kind: "path_prefix", value: prefix }),
      });
      if (!resp.ok) {
        throw new Error(`POST path_prefix "${prefix}" failed: ${resp.status}`);
      }
    }

    await chrome.storage.local.set({ exclusions_migrated_v1: true });
    console.log("[theoros] migration complete");
  } catch (err) {
    // Leave flag unset so migration retries on next extension load.
    console.error(`[theoros] migration failed: ${err.message}`);
  }
}

/**
 * Fetch the current exclusion list from the capture service and store it
 * in chrome.storage.local["exclusions_cache"] — the single key the
 * capture-time check, the options page, and the popup all read from.
 *
 * Called from runtime.onInstalled / onStartup (so a fresh install and
 * every browser launch hydrate from the server) and after every
 * write-through. Returns the fetched { domains, path_prefixes } object
 * so callers can echo the fresh list back without a second fetch;
 * returns null on failure and intentionally leaves the existing cache
 * untouched — fail-open so capture keeps working from the last-known
 * list when the service is briefly unreachable.
 */
async function refreshExclusionsFromServer() {
  try {
    const resp = await fetch(EXCLUSIONS_URL);
    if (!resp.ok) {
      console.error(
        `[theoros] exclusions refresh failed: HTTP ${resp.status} — cache left as-is`
      );
      return null;
    }
    // Server returns { domains: [...], path_prefixes: [...] }.
    const data = await resp.json();
    await chrome.storage.local.set({ exclusions_cache: data });
    console.log(
      `[theoros] exclusions refreshed: ${data.domains.length} domains, ${data.path_prefixes.length} path_prefixes`
    );
    return data;
  } catch (err) {
    console.error(
      `[theoros] exclusions refresh failed: ${err.message} — cache left as-is`
    );
    return null;
  }
}

/**
 * Transition the focused-tab state.  Calls onTabUnfocused for the old
 * tab synchronously, then queries the new tab async (we need its URL
 * and load status) before calling onTabFocused.
 */
function setFocusedTab(tabId) {
  if (currentFocusedTabId === tabId) return;

  const oldTabId = currentFocusedTabId;
  currentFocusedTabId = tabId;

  if (oldTabId !== null) {
    onTabUnfocused(oldTabId);
  }

  if (tabId !== null) {
    chrome.tabs.get(tabId).then((tab) => {
      // Focus may have moved while the query was in flight.
      if (currentFocusedTabId !== tabId) return;
      onTabFocused(tabId, tab);
    }).catch((err) => {
      // Tab may have closed between the event and the query.
      console.warn(`[theoros] tab ${tabId}: query failed — ${err.message}`);
    });
  }
}

// ---------------------------------------------------------------------------
// Focus / unfocus handlers
// ---------------------------------------------------------------------------

async function onTabFocused(tabId, tab) {
  console.log(`[theoros] tab ${tabId} became focused (${tab.url})`);

  if (!isHttpUrl(tab.url)) {
    console.log(`[theoros] tab ${tabId}: non-HTTP URL, not tracking`);
    return;
  }

  if (await isUrlExcluded(tab.url)) {
    console.log(`[theoros] tab ${tabId}: URL excluded, not tracking (${tab.url})`);
    return;
  }

  const state = getOrCreateTabState(tabId);

  // Cancel the 10-minute unfocused clock — session continues.
  cancelUnfocusedResetAlarm(tabId);

  state.last_focused_at = Date.now();

  // If the page is already loaded, start the dwell timer now.
  // Otherwise tabs.onUpdated with status='complete' will start it.
  if (tab.status === "complete") {
    startDwellTimer(tabId, tab.url);
  }
}

function onTabUnfocused(tabId) {
  const state = tabStates.get(tabId);
  if (!state) return; // never fully tracked (non-HTTP, or async query didn't complete)

  console.log(`[theoros] tab ${tabId} became unfocused`);

  cancelDwellTimer(tabId);

  state.last_unfocused_at = Date.now();
  persistTabSession(tabId);

  // Schedule the 10-minute unfocused clock via chrome.alarms (not
  // setTimeout) so it survives service-worker termination — see the
  // "Unfocused-reset alarm" section below for the full rationale.  If
  // the alarm fires before the tab refocuses, handleUnfocusedReset
  // mints a fresh session_id; any subsequent capture is a new session.
  chrome.alarms.create(unfocusedResetAlarmName(tabId), {
    delayInMinutes: UNFOCUSED_RESET_MS / 60_000,
  });
  console.log(`[theoros] tab ${tabId}: unfocused reset alarm scheduled (10min)`);
}

// ---------------------------------------------------------------------------
// Dwell timer
// ---------------------------------------------------------------------------

function startDwellTimer(tabId, url) {
  const state = tabStates.get(tabId);
  if (!state) return;

  // Cancel any existing dwell timer (e.g., page reloaded while focused).
  cancelDwellTimer(tabId);

  state.last_url_at_load = url;
  console.log(`[theoros] tab ${tabId}: dwell timer set for ${url}`);

  state.dwell_timer = setTimeout(() => {
    state.dwell_timer = null;

    // Belt-and-suspenders: verify the tab is still focused.  The timer
    // should have been cancelled on unfocus, but guard anyway.
    if (currentFocusedTabId !== tabId) {
      console.log(
        `[theoros] tab ${tabId}: dwell elapsed but tab no longer focused, skipping`
      );
      return;
    }

    console.log(`[theoros] tab ${tabId}: dwell elapsed, capturing`);
    captureTab(tabId, "dwell");
  }, DWELL_MS);
}

function cancelDwellTimer(tabId) {
  const state = tabStates.get(tabId);
  if (!state?.dwell_timer) return;
  clearTimeout(state.dwell_timer);
  state.dwell_timer = null;
  console.log(`[theoros] tab ${tabId}: dwell timer cancelled`);
}

// ---------------------------------------------------------------------------
// Unfocused-reset alarm
// ---------------------------------------------------------------------------
// Why alarms here and setTimeout for dwell:
//   - The dwell timer is 5s, which is well under the Chrome MV3 service
//     worker's ~30s idle kill window.  The worker is guaranteed alive
//     for the timer's lifetime, so plain setTimeout is fine and saves
//     the round-trip through chrome.alarms.
//   - The unfocused reset is 10 minutes.  A setTimeout would be silently
//     discarded the first time the worker sleeps, so the session would
//     never reset and stale sessions would accumulate.  chrome.alarms is
//     persistent across worker terminations and wakes the worker to fire
//     its onAlarm listener, which is what we need.
// Alarms are keyed by name; we encode the tabId in the name so the
// listener can route the fire back to the right tab without holding
// per-tab handles in memory.

const UNFOCUSED_RESET_ALARM_PREFIX = "unfocused-reset-";

function unfocusedResetAlarmName(tabId) {
  return `${UNFOCUSED_RESET_ALARM_PREFIX}${tabId}`;
}

/**
 * Runs when an unfocused-reset alarm fires.  The worker may have been
 * killed between scheduling and firing, so the in-memory tabStates
 * entry may be absent — in that case we patch the persisted record
 * directly so the next captureTab picks up the fresh session_id via
 * restorePersistedSession.
 */
async function handleUnfocusedReset(tabId) {
  const newSessionId = crypto.randomUUID();
  const state = tabStates.get(tabId);
  if (state) {
    state.tab_session_id = newSessionId;
    await persistTabSession(tabId);
  } else {
    const stored = await loadPersistedTabSessions();
    if (!stored[tabId]) return; // tab gone or never tracked — nothing to reset
    stored[tabId].tab_session_id = newSessionId;
    await chrome.storage.local.set({ [STORAGE_KEY_TAB_SESSIONS]: stored });
  }
  console.log(
    `[theoros] tab ${tabId}: 10min unfocused, new session_id ${newSessionId}`
  );
}

chrome.alarms.onAlarm.addListener((alarm) => {
  if (!alarm.name.startsWith(UNFOCUSED_RESET_ALARM_PREFIX)) return;
  const tabId = Number(alarm.name.slice(UNFOCUSED_RESET_ALARM_PREFIX.length));
  if (!Number.isInteger(tabId)) return;
  handleUnfocusedReset(tabId);
});

function cancelUnfocusedResetAlarm(tabId) {
  // chrome.alarms.clear is a no-op if no alarm by that name exists,
  // so unconditional cancellation is safe.  Fire-and-forget — we
  // don't need to await the boolean "was-cleared" result.
  chrome.alarms.clear(unfocusedResetAlarmName(tabId));
  console.log(`[theoros] tab ${tabId}: unfocused reset alarm cancelled`);
}

// ---------------------------------------------------------------------------
// Core capture function
// ---------------------------------------------------------------------------
// Both the click handler and the dwell timer call this.  The `trigger`
// parameter ("click" or "dwell") goes into raw_metadata so the capture
// service can distinguish the two.

async function captureTab(tabId, trigger) {
  console.log(`[theoros] captureTab(${tabId}, ${trigger})`);

  try {
    const tab = await chrome.tabs.get(tabId);

    if (!isHttpUrl(tab.url)) {
      console.log(
        `[theoros] tab ${tabId}: non-HTTP URL (${tab.url}), skipping capture`
      );
      return;
    }

    // Ensure state exists (click captures can fire before dwell tracking
    // creates state for this tab).
    const state = getOrCreateTabState(tabId);

    // Register the message listener *before* injecting so we never miss
    // a fast response from the content script.
    const extractionPromise = waitForContentScript(tabId, 10_000);

    await chrome.scripting.executeScript({
      target: { tabId },
      files: ["vendor/Readability.js", "content_script.js"],
    });

    const data = await extractionPromise;
    console.log(`[theoros] tab ${tabId}: extraction received`, {
      title: data.title,
      url: data.url,
      textLength: data.content_text?.length,
      fallback: data.fallback,
    });

    const payload = {
      captured_at: new Date().toISOString(),
      source_tier: "browser",
      source_app: "chrome",
      source_identifier: data.url,
      title: data.title,
      content_text: data.content_text,
      raw_metadata: {
        extension_version: EXTENSION_VERSION,
        readability_excerpt_length: data.content_text?.length ?? 0,
        readability_fallback: data.fallback,
        trigger,
        tab_session_id: state.tab_session_id,
      },
    };

    if (data.error) {
      payload.raw_metadata.extraction_error = data.error;
    }

    console.log(
      `[theoros] tab ${tabId}: POSTing to capture service (trigger=${trigger})...`
    );
    const response = await fetch(CAPTURE_SERVICE_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

    console.log(
      `[theoros] tab ${tabId}: capture service responded ${response.status}`
    );

    if (response.status === 200 || response.status === 201) {
      const body = await response.json();
      if (body.duplicate) {
        console.log(`[theoros] tab ${tabId}: capture deduped`, body);
      } else {
        console.log(`[theoros] tab ${tabId}: capture stored`, body);
      }
      showBadge(tabId, "\u2713", "#2e7d32", BADGE_DURATION_MS);
    } else {
      const text = await response.text();
      console.error(
        `[theoros] tab ${tabId}: capture service error (${response.status}):`,
        text
      );
      showBadge(tabId, "!", "#c62828");
    }
  } catch (err) {
    console.error(`[theoros] tab ${tabId}: capture failed:`, err);
    showBadge(tabId, "!", "#c62828");
  }
}

// ---------------------------------------------------------------------------
// Event listeners
// ---------------------------------------------------------------------------

// Exclusion write-through helpers used by the message listener.
//
// These run async work (network + cache refresh) and MUST resolve to a
// response object — never throw. The listener returns the promise so
// Firefox keeps the event page alive until it settles and forwards the
// resolved value as the page's `await sendMessage(...)` result.
// (The sendResponse + `return true` style works for the simple branches
// below but races on non-persistent event pages: the page can be torn
// down before sendResponse fires, dropping the reply. Returning a
// promise from the listener avoids that.)

async function handleAddExclusion({ kind, value }) {
  let resp;
  try {
    resp = await fetch(EXCLUSIONS_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind, value }),
    });
  } catch (err) {
    return { ok: false, error: err.message };
  }
  if (!resp.ok) {
    const text = await resp.text().catch(() => "");
    return { ok: false, error: `POST ${resp.status}: ${text}` };
  }
  const data = await refreshExclusionsFromServer();
  if (!data) return { ok: false, error: "cache refresh failed" };
  return { ok: true, exclusions: data };
}

async function handleRemoveExclusion({ kind, value }) {
  // Body-bearing DELETE — contract documented in
  // docs/phase-mobile-capture-layer3-design.md (chosen over
  // /exclusions/urls/{kind}/{value} to avoid URL-encoding domains
  // and path prefixes that contain dots and slashes).
  let resp;
  try {
    resp = await fetch(EXCLUSIONS_URL, {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind, value }),
    });
  } catch (err) {
    return { ok: false, error: err.message };
  }
  if (!resp.ok) {
    const text = await resp.text().catch(() => "");
    return { ok: false, error: `DELETE ${resp.status}: ${text}` };
  }
  const data = await refreshExclusionsFromServer();
  if (!data) return { ok: false, error: "cache refresh failed" };
  return { ok: true, exclusions: data };
}

// Pure-refresh handler for "refresh-exclusions" messages (options page
// on open). Re-pulls the server list, persists it, returns it. Mirrors
// the add/remove response shape so the caller can render directly from
// `response.exclusions`. On failure we still return the cached list so
// the options page renders something useful offline rather than blank.
async function handleRefreshExclusions() {
  const data = await refreshExclusionsFromServer();
  if (data) return { ok: true, exclusions: data };
  const cached = await loadExclusions();
  return { ok: false, error: "refresh failed", exclusions: cached };
}

// Messages from the popup (get-state, set-paused, capture-now) and
// from the options/popup pages (add-exclusion, remove-exclusion).
// Content-script messages (extraction-result) are handled by the
// per-capture listeners in waitForContentScript — they coexist fine.
//
// Two response styles in one listener — both are valid, each branch
// hits one or the other:
//   * Synchronous-ish branches that finish in one tick (get-state,
//     set-paused) call sendResponse and return.
//   * capture-now starts async work but only needs sendResponse to
//     fire on the *next* turn, so sendResponse + `return true` is fine
//     (the keep-alive there isn't load-bearing for correctness).
//   * add-exclusion / remove-exclusion do network work whose reply IS
//     load-bearing for the UI. For those we RETURN A PROMISE — Firefox
//     keeps the event page alive until it settles and forwards the
//     resolved value to the page's `await sendMessage(...)`. The old
//     sendResponse style raced and dropped replies (see helpers above).
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  // Ignore messages from content scripts in web pages. Can't filter on
  // sender.tab: the options page is tab-hosted (open_in_tab) and carries one
  // too. Extension pages have a moz-extension:// URL; content scripts carry
  // the web page's http(s) URL.
  if (sender.url && !sender.url.startsWith(chrome.runtime.getURL("/"))) return;

  if (message.type === "get-state") {
    sendResponse({ isPaused });
    return;
  }

  if (message.type === "set-paused") {
    const wasPaused = isPaused;
    isPaused = message.paused;
    chrome.storage.local.set({ isPaused });

    if (isPaused && !wasPaused) onPause();
    else if (!isPaused && wasPaused) onUnpause();

    sendResponse({ isPaused });
    return;
  }

  if (message.type === "capture-now") {
    if (isPaused) {
      sendResponse({ error: "paused" });
      return;
    }
    chrome.tabs.query({ active: true, currentWindow: true }).then(async (tabs) => {
      if (tabs.length > 0) {
        const tab = tabs[0];
        if (await isUrlExcluded(tab.url)) {
          console.log(`[theoros] tab ${tab.id}: URL excluded, not capturing (${tab.url})`);
          sendResponse({ error: "excluded" });
          return;
        }
        captureTab(tab.id, "click");
      }
      sendResponse({ ok: true });
    });
    return true; // keep channel open for async sendResponse
  }

  if (message.type === "add-exclusion") return handleAddExclusion(message);
  if (message.type === "remove-exclusion") return handleRemoveExclusion(message);
  if (message.type === "refresh-exclusions") return handleRefreshExclusions();
});

// Tab activated (user switched tabs within a window).
chrome.tabs.onActivated.addListener(({ tabId, windowId }) => {
  if (isPaused) return;
  // Only matters if this is the focused window.
  if (windowId === focusedWindowId) {
    setFocusedTab(tabId);
  }
});

// Window focus changed (includes OS-level focus loss via WINDOW_ID_NONE).
chrome.windows.onFocusChanged.addListener((windowId) => {
  if (isPaused) return;
  focusedWindowId = windowId;

  if (windowId === chrome.windows.WINDOW_ID_NONE) {
    console.log("[theoros] browser lost OS focus");
    setFocusedTab(null);
    return;
  }

  // Find the active tab in the newly-focused window.
  chrome.tabs.query({ active: true, windowId }).then((tabs) => {
    // Guard: window focus may have moved while the query was in flight.
    if (tabs.length > 0 && focusedWindowId === windowId) {
      setFocusedTab(tabs[0].id);
    }
  });
});

// Tab updated — detect page-load completion and URL changes.
chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (isPaused) return;
  const state = tabStates.get(tabId);

  // URL changed while a dwell timer is pending — cancel it.
  if (
    changeInfo.url &&
    state?.dwell_timer &&
    changeInfo.url !== state.last_url_at_load
  ) {
    console.log(
      `[theoros] tab ${tabId}: URL changed to ${changeInfo.url}, dwell cancelled`
    );
    cancelDwellTimer(tabId);
  }

  // Page finished loading in the currently-focused tab — start dwell.
  if (changeInfo.status === "complete" && tabId === currentFocusedTabId) {
    if (isHttpUrl(tab.url)) {
      isUrlExcluded(tab.url).then((excluded) => {
        if (excluded) {
          console.log(`[theoros] tab ${tabId}: URL excluded, not tracking (${tab.url})`);
          return;
        }
        if (currentFocusedTabId !== tabId) return;
        getOrCreateTabState(tabId);
        startDwellTimer(tabId, tab.url);
      });
    }
  }
});

// Tab closed — clean up all state and timers.
chrome.tabs.onRemoved.addListener((tabId) => {
  console.log(`[theoros] tab ${tabId} closed, cleaning up`);
  clearTabState(tabId);
  if (currentFocusedTabId === tabId) {
    currentFocusedTabId = null;
  }
});

// ---------------------------------------------------------------------------
// Pause / unpause transitions
// ---------------------------------------------------------------------------

function onPause() {
  console.log("[theoros] paused — stopping all tracking");

  // Cancel all timers and alarms across tracked tabs.
  for (const [tabId, state] of tabStates) {
    if (state.dwell_timer) {
      clearTimeout(state.dwell_timer);
      state.dwell_timer = null;
    }
    chrome.alarms.clear(unfocusedResetAlarmName(tabId));
  }

  currentFocusedTabId = null;
  setPauseBadge();
}

function onUnpause() {
  console.log("[theoros] resumed — reinitializing focus tracking");

  clearPauseBadge();
  // Clear stale state so everything starts fresh.
  tabStates.clear();
  initFocus();
}

function setPauseBadge() {
  chrome.action.setBadgeText({ text: "\u23F8" });
  chrome.action.setBadgeBackgroundColor({ color: "#757575" });
  // Override any per-tab badges so the pause indicator wins everywhere.
  for (const tabId of tabStates.keys()) {
    chrome.action.setBadgeText({ text: "\u23F8", tabId });
    chrome.action.setBadgeBackgroundColor({ color: "#757575", tabId });
  }
}

function clearPauseBadge() {
  chrome.action.setBadgeText({ text: "" });
  // Clear per-tab pause badges. Query all tabs rather than relying on
  // tabStates (which onUnpause clears before calling us).
  chrome.tabs.query({}).then((tabs) => {
    for (const tab of tabs) {
      chrome.action.setBadgeText({ text: "", tabId: tab.id });
    }
  });
}

// ---------------------------------------------------------------------------
// Initialization
// ---------------------------------------------------------------------------
// On extension load (browser start or extension reload), determine the
// currently-focused tab so dwell capture works immediately without
// waiting for a user-generated focus event.
// If paused (persisted from a previous session), skip focus tracking and
// restore the pause badge.

function initFocus() {
  chrome.windows.getLastFocused({ populate: true }).then((win) => {
    if (!win || win.id === chrome.windows.WINDOW_ID_NONE) return;
    focusedWindowId = win.id;
    const activeTab = win.tabs?.find((t) => t.active);
    if (activeTab) {
      console.log(
        `[theoros] init: focused window ${win.id}, active tab ${activeTab.id}`
      );
      setFocusedTab(activeTab.id);
    }
  });
}

/**
 * Worker init: restore persisted pause state, kick off focus tracking
 * (when not paused), and log persisted session count.
 *
 * Called from chrome.runtime.onStartup (browser cold start),
 * chrome.runtime.onInstalled (install / update / unpacked-reload), AND
 * at top-level eval — the top-level call covers the developer-reload
 * case where, depending on Chrome version, neither lifecycle event
 * fires reliably for unpacked extensions.
 *
 * MV3 service workers are killed after ~30s idle and re-spawned on
 * events; each spawn re-evaluates this file.  initWorker must
 * therefore be idempotent, and it is:
 *   - tabStates keys by tabId, so repeat calls reuse the same state
 *     object rather than leaking duplicates.
 *   - initFocus → setFocusedTab early-returns when the focused tab is
 *     already current, so a second pass is a no-op.
 *   - Dwell timers are cancelled before being (re)set in
 *     startDwellTimer, so an extra init can't leak setTimeouts.
 */
async function initWorker() {
  const { isPaused: storedPaused } = await chrome.storage.local.get("isPaused");
  isPaused = storedPaused || false;
  if (isPaused) {
    console.log("[theoros] init: paused (restored from storage)");
    setPauseBadge();
  } else {
    initFocus();
  }
  const stored = await loadPersistedTabSessions();
  console.log(
    `[theoros] init: ${Object.keys(stored).length} persisted tab sessions in storage`
  );
}

// Migrate any pre-Layer-3 local exclusions to the capture service, then
// hydrate the cache from the server. Both events fire when the service
// worker genuinely (re)loads after a lifecycle transition: onInstalled
// on install / update / browser update / unpacked reload, onStartup on
// browser cold start. We deliberately don't run this at top-level eval
// — on an MV3 service worker that races on fresh install (the worker
// can wake before the capture service is reachable, with no retry) and
// also fires on unrelated event-driven wake-ups, which would beat on
// /exclusions/urls for no reason. Migration is gated by
// exclusions_migrated_v1 so the extra call from onStartup is a no-op
// once it's run once.
function hydrateExclusions() {
  migrateExclusionsToServer().then(() => refreshExclusionsFromServer());
}

chrome.runtime.onInstalled.addListener(hydrateExclusions);
chrome.runtime.onInstalled.addListener(initWorker);
chrome.runtime.onStartup.addListener(hydrateExclusions);
chrome.runtime.onStartup.addListener(initWorker);

// Top-level init call covers developer reload of the unpacked
// extension.  Safe to call alongside the lifecycle listeners above —
// initWorker is idempotent (see its docstring).
initWorker();

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------

/**
 * Returns a promise that resolves with the first "extraction-result"
 * message from the given tab.  Rejects if nothing arrives within
 * timeoutMs — which likely means the content script failed to load
 * (missing Readability.js, restricted page, etc.).
 */
function waitForContentScript(tabId, timeoutMs) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      chrome.runtime.onMessage.removeListener(listener);
      reject(
        new Error(
          `Content script in tab ${tabId} did not respond within ${timeoutMs}ms`
        )
      );
    }, timeoutMs);

    function listener(message, sender) {
      if (sender.tab?.id === tabId && message.type === "extraction-result") {
        chrome.runtime.onMessage.removeListener(listener);
        clearTimeout(timer);
        resolve(message.data);
      }
    }

    chrome.runtime.onMessage.addListener(listener);
  });
}

/**
 * Show a short text badge on the toolbar icon for visual feedback.
 * If durationMs is provided, the badge clears automatically.
 * If omitted (failure case), the badge stays until the next attempt.
 * Pause badge takes precedence — per-tab badges are suppressed while paused.
 */
function showBadge(tabId, text, color, durationMs) {
  if (isPaused) return;

  chrome.action.setBadgeText({ text, tabId });
  chrome.action.setBadgeBackgroundColor({ color, tabId });

  if (durationMs) {
    setTimeout(() => {
      if (isPaused) return;
      chrome.action.setBadgeText({ text: "", tabId });
    }, durationMs);
  }
}
