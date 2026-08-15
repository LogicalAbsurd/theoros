// popup.js — Theoros browser extension popup
//
// Communicates with the background script to show/toggle pause state
// and trigger manual captures.

const stateText = document.getElementById("state-text");
const toggleBtn = document.getElementById("toggle-btn");
const excludeBtn = document.getElementById("exclude-btn");
const captureBtn = document.getElementById("capture-btn");
const manageLink = document.getElementById("manage-link");

let paused = false;

function updateUI(isPaused) {
  paused = isPaused;

  if (isPaused) {
    stateText.textContent = "Theoros is paused";
    stateText.className = "state-paused";
    toggleBtn.textContent = "Resume Theoros";
    toggleBtn.className = "btn btn-resume";
    captureBtn.disabled = true;
  } else {
    stateText.textContent = "Theoros is active";
    stateText.className = "state-active";
    toggleBtn.textContent = "Pause Theoros";
    toggleBtn.className = "btn btn-pause";
    captureBtn.disabled = false;
  }
}

// --- Exclude button visibility / disabled state ---
// Hidden for non-HTTP tabs; disabled if the domain is already excluded.

async function setupExcludeButton() {
  let tabs;
  try {
    tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  } catch {
    excludeBtn.style.display = "none";
    return;
  }

  if (tabs.length === 0 || !tabs[0].url) {
    excludeBtn.style.display = "none";
    return;
  }

  const url = tabs[0].url;
  if (!url.startsWith("http://") && !url.startsWith("https://")) {
    excludeBtn.style.display = "none";
    return;
  }

  const hostname = new URL(url).hostname;

  // Read from the server-synced cache background maintains. The exclude
  // button only adds domains, so path_prefixes aren't consulted here —
  // a page hidden by a path_prefix entry can still be domain-excluded
  // separately, and showing the button in that case isn't misleading.
  const result = await chrome.storage.local.get("exclusions_cache");
  const exclusions = result.exclusions_cache || { domains: [], path_prefixes: [] };

  const alreadyExcluded = exclusions.domains.some(
    (d) => hostname === d || hostname.endsWith("." + d)
  );
  if (alreadyExcluded) {
    excludeBtn.disabled = true;
    excludeBtn.textContent = "Site already excluded";
  }
}

setupExcludeButton();

// --- Exclude button click: add domain and close popup ---

excludeBtn.addEventListener("click", async () => {
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  if (tabs.length === 0 || !tabs[0].url) return;

  const hostname = new URL(tabs[0].url).hostname;

  // Route the write through background so the capture service stays
  // authoritative and the cache is refreshed in one round-trip. On
  // failure we keep the popup open so the user can retry — silently
  // closing after a failed write would leave them thinking the site
  // was excluded when it wasn't.
  const response = await chrome.runtime.sendMessage({
    type: "add-exclusion",
    kind: "domain",
    value: hostname,
  });

  if (!response?.ok) {
    console.error(
      `[theoros] popup: add-exclusion failed: ${response?.error || "no response"}`
    );
    excludeBtn.textContent = "Couldn't exclude — try again";
    return;
  }

  window.close();
});

// --- Manage exclusions link ---

manageLink.addEventListener("click", () => {
  chrome.runtime.openOptionsPage();
  window.close();
});

// --- Pause/resume and capture (unchanged logic) ---

// Query background for current state on popup open.
chrome.runtime.sendMessage({ type: "get-state" }, (response) => {
  if (response) updateUI(response.isPaused);
});

toggleBtn.addEventListener("click", () => {
  chrome.runtime.sendMessage(
    { type: "set-paused", paused: !paused },
    (response) => {
      if (response) updateUI(response.isPaused);
    }
  );
});

captureBtn.addEventListener("click", () => {
  chrome.runtime.sendMessage({ type: "capture-now" });
  window.close();
});
