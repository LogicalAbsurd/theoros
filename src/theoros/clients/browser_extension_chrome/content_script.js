// content_script.js — Theoros browser extension content script
//
// Injected into the active tab by the background script on toolbar click.
// Extracts clean article text via Mozilla's Readability library, falling
// back to raw innerText if Readability can't parse the page.
//
// Communicates the result back to background.js via chrome.runtime.sendMessage.
// This script runs once per click; it is not a persistent content script.

(function () {
  "use strict";

  let title, url, contentText, fallback, error;

  try {
    url = location.href;
    title = document.title;

    // Readability mutates the document it receives, so we clone the entire
    // DOM first.  This is the library's documented usage pattern — it needs
    // a real Document object, not a fragment.
    const docClone = document.cloneNode(true);
    const reader = new Readability(docClone);
    const article = reader.parse();

    if (article && article.textContent) {
      contentText = article.textContent;
      // Readability sometimes extracts a better title than document.title
      // (e.g. stripping the site name suffix).
      title = article.title || title;
      fallback = false;
    } else {
      // Readability returned null or empty textContent — the page is probably
      // not article-shaped (web app, login page, SPA shell, etc.).  Capture
      // the raw body text so we get something rather than nothing.  The
      // fallback flag lets the capture service and downstream processing
      // know that this wasn't a clean extraction.
      contentText = document.body.innerText;
      fallback = true;
    }
  } catch (err) {
    // Readability itself threw — missing vendor file, malformed DOM, etc.
    // Still capture what we can; the error surfaces in raw_metadata so the
    // issue is visible without losing the event entirely.
    contentText = document.body?.innerText ?? "";
    title = document.title;
    url = location.href;
    fallback = true;
    error = err.message;
    console.error("[theoros] Readability extraction failed:", err);
  }

  chrome.runtime.sendMessage({
    type: "extraction-result",
    data: { title, url, content_text: contentText, fallback, error },
  });
})();
