# Theoros Browser Extension (Firefox, click-to-capture)

Click the toolbar button on any page to extract its text via
Readability.js and send it to the local Theoros capture service.

Automatic capture on page load is a future enhancement (Phase 2a
continued).


## Prerequisites

The capture service must be running on `127.0.0.1:8765`.  Verify:

```bash
curl http://127.0.0.1:8765/health
# should return {"status":"ok","error":null}
```

If the service isn't running, see the capture service README or start
it with `systemctl --user start theoros-capture`.


## One-time setup: download Readability.js

The `vendor/Readability.js` file in this directory is a placeholder.
Replace it with the real library:

```bash
# From the repo root:
curl -L -o src/theoros/clients/browser_extension/vendor/Readability.js \
  https://raw.githubusercontent.com/mozilla/readability/main/Readability.js
```

Then commit the file — vendoring means committing the dependency.

**Compatibility note:** the extension injects Readability.js as a
classic script (not an ES module).  It needs `Readability` defined as
a global.  The upstream file uses `function Readability(...)` at the
top level, which works.  If your downloaded version uses
`export default`, strip that line.


## Loading the extension in Firefox

1. Open `about:debugging` in Firefox.
2. Click **This Firefox** in the left sidebar.
3. Click **Load Temporary Add-on...**
4. Navigate to this directory and select `manifest.json`.

The extension loads for the current Firefox session only.  It unloads
on restart; repeat the steps above to reload.  Changes to extension
files take effect after clicking **Reload** next to the extension in
`about:debugging`.


## Usage

1. Navigate to any page you want to capture.
2. Click the Theoros toolbar button (puzzle-piece icon until custom
   icons are added).
3. Watch the badge on the icon:
   - **Green check** — capture succeeded, event stored.
   - **Red !** — capture failed (service down, network error, etc.).
     The badge stays until the next attempt so you notice.


## Debugging

1. In `about:debugging`, click **Inspect** next to the Theoros
   extension to open its devtools.
2. The **Console** tab shows the full request/response cycle:
   - Extraction results (title, URL, text length, fallback flag)
   - The payload sent to the capture service
   - The service's response status and body
3. Content-script errors (e.g. Readability parse failures) also
   appear in the extension's console, not the page's console.


## Files

| File | Purpose |
|------|---------|
| `manifest.json` | Extension manifest (MV3, Firefox) |
| `background.js` | Handles toolbar click, orchestrates injection and capture |
| `content_script.js` | Runs in the page, extracts text via Readability |
| `vendor/Readability.js` | Mozilla Readability library (vendored) |
| `icons/` | Toolbar icons (placeholders for now) |
