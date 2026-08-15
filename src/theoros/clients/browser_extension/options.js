// options.js — Theoros exclusion list management page
//
// The capture service holds the source of truth for URL exclusions
// (table url_exclusions, served at /exclusions/urls). The options page
// reads from chrome.storage.local["exclusions_cache"] — the snapshot
// background maintains via refreshExclusionsFromServer — and routes all
// writes through background's "add-exclusion" / "remove-exclusion"
// runtime messages so the server and the local cache stay in sync.
//
// Shape (matches the service response): { domains: string[], path_prefixes: string[] }

const domainList = document.getElementById("domain-list");
const domainInput = document.getElementById("domain-input");
const domainAddBtn = document.getElementById("domain-add-btn");
const domainError = document.getElementById("domain-error");

const prefixList = document.getElementById("prefix-list");
const prefixInput = document.getElementById("prefix-input");
const prefixAddBtn = document.getElementById("prefix-add-btn");
const prefixError = document.getElementById("prefix-error");

async function loadExclusions() {
  const result = await chrome.storage.local.get("exclusions_cache");
  return result.exclusions_cache || { domains: [], path_prefixes: [] };
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

function renderDomainList(domains) {
  domainList.innerHTML = "";
  if (domains.length === 0) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "No excluded domains.";
    domainList.appendChild(empty);
    return;
  }
  for (const domain of domains) {
    const li = document.createElement("li");
    const span = document.createElement("span");
    span.textContent = domain;
    const btn = document.createElement("button");
    btn.className = "remove-btn";
    btn.textContent = "Remove";
    btn.addEventListener("click", () => removeDomain(domain));
    li.appendChild(span);
    li.appendChild(btn);
    domainList.appendChild(li);
  }
}

function renderPrefixList(prefixes) {
  prefixList.innerHTML = "";
  if (prefixes.length === 0) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "No excluded path prefixes.";
    prefixList.appendChild(empty);
    return;
  }
  for (const prefix of prefixes) {
    const li = document.createElement("li");
    const span = document.createElement("span");
    span.textContent = prefix;
    const btn = document.createElement("button");
    btn.className = "remove-btn";
    btn.textContent = "Remove";
    btn.addEventListener("click", () => removePrefix(prefix));
    li.appendChild(span);
    li.appendChild(btn);
    prefixList.appendChild(li);
  }
}

// Accepts an optional exclusions object so a successful write can
// re-render directly from background's response without a second
// storage read. Falls back to loadExclusions() on initial page load.
async function render(exclusions) {
  if (!exclusions) {
    exclusions = await loadExclusions();
  }
  renderDomainList(exclusions.domains);
  renderPrefixList(exclusions.path_prefixes);
}

// ---------------------------------------------------------------------------
// Validation
// ---------------------------------------------------------------------------

// Valid hostname: no scheme, no path, no trailing slash, at least one dot,
// only alphanumeric, hyphens, and dots.
function isValidHostname(value) {
  if (!value || value.includes("/") || value.includes(":") || value.endsWith(".")) {
    return false;
  }
  // Must look like a domain — use URL constructor to verify.
  try {
    const url = new URL("https://" + value);
    return url.hostname === value;
  } catch {
    return false;
  }
}

// Valid path prefix: "host/path", host portion valid, path starts with /.
function isValidPathPrefix(value) {
  const slashIndex = value.indexOf("/");
  if (slashIndex === -1) return false;
  const host = value.slice(0, slashIndex);
  const path = value.slice(slashIndex); // includes the leading /
  if (!isValidHostname(host)) return false;
  // Path must start with / and not contain scheme-like patterns.
  if (!path.startsWith("/")) return false;
  // Reject trailing slash on bare path (e.g. "example.com/") — that's
  // effectively a domain exclusion, not a path prefix.
  // But "example.com/admin/" is fine.
  if (path === "/") return false;
  return true;
}

// ---------------------------------------------------------------------------
// Add / remove
// ---------------------------------------------------------------------------
// Client-side validation runs first for instant feedback (avoids a round
// trip when the input is obviously wrong); the dup check after it is
// best-effort against the cached snapshot — the server's UNIQUE constraint
// is the real guard, and background returns the unchanged list either way.

async function addDomain() {
  domainError.textContent = "";
  const value = domainInput.value.trim().toLowerCase();
  if (!isValidHostname(value)) {
    domainError.textContent =
      "Enter a valid hostname (no scheme, no path, no trailing slash).";
    return;
  }
  const exclusions = await loadExclusions();
  if (exclusions.domains.includes(value)) {
    domainError.textContent = "Already excluded.";
    return;
  }
  const response = await chrome.runtime.sendMessage({
    type: "add-exclusion",
    kind: "domain",
    value,
  });
  if (!response?.ok) {
    domainError.textContent = response?.error || "Add failed.";
    return;
  }
  domainInput.value = "";
  render(response.exclusions);
}

async function removeDomain(domain) {
  domainError.textContent = "";
  const response = await chrome.runtime.sendMessage({
    type: "remove-exclusion",
    kind: "domain",
    value: domain,
  });
  if (!response?.ok) {
    domainError.textContent = response?.error || "Remove failed.";
    return;
  }
  render(response.exclusions);
}

async function addPrefix() {
  prefixError.textContent = "";
  const value = prefixInput.value.trim().toLowerCase();
  if (!isValidPathPrefix(value)) {
    prefixError.textContent =
      'Enter "hostname/path" (e.g. example.com/admin). No scheme.';
    return;
  }
  const exclusions = await loadExclusions();
  if (exclusions.path_prefixes.includes(value)) {
    prefixError.textContent = "Already excluded.";
    return;
  }
  const response = await chrome.runtime.sendMessage({
    type: "add-exclusion",
    kind: "path_prefix",
    value,
  });
  if (!response?.ok) {
    prefixError.textContent = response?.error || "Add failed.";
    return;
  }
  prefixInput.value = "";
  render(response.exclusions);
}

async function removePrefix(prefix) {
  prefixError.textContent = "";
  const response = await chrome.runtime.sendMessage({
    type: "remove-exclusion",
    kind: "path_prefix",
    value: prefix,
  });
  if (!response?.ok) {
    prefixError.textContent = response?.error || "Remove failed.";
    return;
  }
  render(response.exclusions);
}

// ---------------------------------------------------------------------------
// Event listeners
// ---------------------------------------------------------------------------

domainAddBtn.addEventListener("click", addDomain);
domainInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") addDomain();
});

prefixAddBtn.addEventListener("click", addPrefix);
prefixInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") addPrefix();
});

// Initial render. Ask background to pull a fresh list from the capture
// service before painting — a fresh extension install seeds an empty
// cache, so without this the page would show "No excluded domains" even
// when the server has entries. Background's handleRefreshExclusions
// echoes the cached list back on network failure, so we still render
// something useful offline rather than blank.
async function initialRender() {
  try {
    const response = await chrome.runtime.sendMessage({ type: "refresh-exclusions" });
    if (response?.exclusions) {
      render(response.exclusions);
      if (!response.ok) {
        domainError.textContent = `Showing cached list — refresh failed: ${response.error}`;
      }
      return;
    }
  } catch (err) {
    console.error(`[theoros] options: refresh-exclusions failed: ${err.message}`);
  }
  // Background unreachable entirely — fall back to whatever we can read
  // directly from storage.
  render();
}

initialRender();
