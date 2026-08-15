package com.theoros.capture

import android.accessibilityservice.AccessibilityService
import android.util.Log
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch

class TheorosAccessibilityService : AccessibilityService() {

    companion object {
        private const val TAG = "TheorosA11y"
        // Throttle: don't walk the tree more than once per this interval, per package.
        private const val MIN_WALK_INTERVAL_MS = 2000L

        // System surfaces that aren't "things the user is reading."
        // These get skipped at extraction time. Distinct from the M3 user-facing
        // exclusion list — those are operator choices; this is structural noise.
        private val SYSTEM_NOISE_PACKAGES = setOf(
            "com.android.systemui",
            "com.android.settings",
            "com.android.settings.intelligence",
            "com.sec.android.app.launcher",
            "com.sec.android.app.clockpackage",
            "com.samsung.android.app.aodservice",
            "com.samsung.android.biometrics.app.setting",
            "com.google.android.inputmethod.latin",
        )

        // Recognized Firefox package names. Stable only for now; Beta/Nightly
        // can be added here when we need to support them. Kept as a set so the
        // private-mode check and URL extraction both target the same identity.
        private val FIREFOX_PACKAGES = setOf(
            "org.mozilla.firefox",
        )

        // Firefox's address-bar node uses a semantic accessibility id. Its
        // contentDescription is of the form "<host/path>. Search or enter address".
        private const val FIREFOX_URL_VIEW_ID = "ADDRESSBAR_URL_BOX"
        private const val FIREFOX_URL_DESC_SUFFIX = ". Search or enter address"

        // Marker substrings that indicate a Firefox window is in private mode.
        // Firefox doesn't expose a clean isPrivate accessibility flag, so we
        // detect via content of the rendered toolbar and tab strip.
        private val FIREFOX_PRIVATE_MARKERS = listOf(
            "Private browsing",
            "Private tab",
            "private browsing mode",
        )
    }

    private val lastWalkByPackage = mutableMapOf<String, Long>()
    private val lastSentByPackage = mutableMapOf<String, String>()

    // SupervisorJob so one failed post doesn't cancel the scope; IO dispatcher
    // because ApiClient.postCapture already uses withContext(IO) but launching
    // here keeps the a11y callback off any blocking path.
    private val sendScope = CoroutineScope(Dispatchers.IO + SupervisorJob())

    override fun onServiceConnected() {
        super.onServiceConnected()
        Log.i(TAG, "Service connected")
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        if (event == null) return

        // Pause toggle: skip everything if capture is paused.
        if (CaptureSettings.isPaused(this)) {
            Log.d(TAG, "paused → event skipped")
            return
        }

        val packageName = event.packageName?.toString() ?: return

        // Skip system surfaces — these are not "reading" in any meaningful sense.
        if (packageName in SYSTEM_NOISE_PACKAGES) return

        // Skip user-excluded apps from the M3 Layer 1 exclusion list.
        if (packageName in CaptureSettings.getExcludedPackages(this)) {
            Log.d(TAG, "excluded → package=$packageName")
            return
        }

        val now = System.currentTimeMillis()
        val lastWalk = lastWalkByPackage[packageName] ?: 0L
        if (now - lastWalk < MIN_WALK_INTERVAL_MS) return

        val root = rootInActiveWindow ?: return

        // Firefox private-mode check: skip extraction if the current
        // window shows private-browsing markers in the accessibility tree.
        if (packageName in FIREFOX_PACKAGES && isFirefoxPrivate(root)) {
            Log.d(TAG, "private-mode skip → package=$packageName")
            lastWalkByPackage[packageName] = now
            return
        }

        // Firefox URL surfacing. Hoisted so it can double as the `title` on
        // the outbound capture post. Missing/blank URL (new tab, empty bar)
        // is not a reason to skip capture — title stays null.
        val firefoxUrl: String? = if (packageName in FIREFOX_PACKAGES) {
            extractFirefoxUrl(root)?.also { Log.d(TAG, "firefox url: $it") }
        } else null

        lastWalkByPackage[packageName] = now

        val collected = StringBuilder()
        walkNode(root, collected)

        val rawText = collected.toString().trim()
        if (rawText.isEmpty()) {
            Log.d(TAG, "extract → package=$packageName (no text)")
            return
        }

        val redaction = CredentialRedactor.redact(rawText)
        val text = redaction.text

        val preview = if (text.length > 500) text.substring(0, 500) + "…" else text
        if (redaction.appliedPatterns.isNotEmpty()) {
            Log.i(TAG, "extract → package=$packageName chars=${text.length} redacted=${redaction.appliedPatterns}")
        } else {
            Log.i(TAG, "extract → package=$packageName chars=${text.length}")
        }
        Log.i(TAG, "  text: $preview")

        // Dedup: skip posting when the extracted text for this package is
        // identical to the last successfully-sent payload. Prevents flooding
        // the capture service with re-walks of an idle screen.
        if (lastSentByPackage[packageName] == text) {
            Log.d(TAG, "duplicate → skip package=$packageName")
            return
        }

        sendScope.launch {
            try {
                ApiClient.postCapture(packageName, firefoxUrl, text)
                lastSentByPackage[packageName] = text
                Log.i(TAG, "posted → package=$packageName chars=${text.length}")
            } catch (t: Throwable) {
                Log.e(TAG, "post failed → package=$packageName", t)
            }
        }
    }

    private fun walkNode(node: AccessibilityNodeInfo?, out: StringBuilder) {
        if (node == null) return

        node.text?.toString()?.takeIf { it.isNotBlank() }?.let {
            out.append(it).append(' ')
        }
        node.contentDescription?.toString()?.takeIf { it.isNotBlank() }?.let {
            out.append(it).append(' ')
        }

        for (i in 0 until node.childCount) {
            walkNode(node.getChild(i), out)
        }
    }

    /**
     * Pull the current URL from Firefox's address bar. Tries the indexed
     * lookup first (cheap); falls back to a tree walk matching
     * getViewIdResourceName() because findAccessibilityNodeInfosByViewId
     * normally expects a fully-qualified id and Firefox's semantic id may
     * not be indexed that way on all builds.
     *
     * Returns the cleaned, scheme-less host[/path] or null if the bar is
     * empty / missing (new tab, focused-but-empty bar).
     */
    private fun extractFirefoxUrl(root: AccessibilityNodeInfo): String? {
        val node = root.findAccessibilityNodeInfosByViewId(FIREFOX_URL_VIEW_ID)
            ?.firstOrNull()
            ?: findNodeByViewId(root, FIREFOX_URL_VIEW_ID)
            ?: return null

        val desc = node.contentDescription?.toString() ?: return null
        val cleaned = desc.removeSuffix(FIREFOX_URL_DESC_SUFFIX).trim()
        return cleaned.ifEmpty { null }
    }

    private fun findNodeByViewId(node: AccessibilityNodeInfo?, viewId: String): AccessibilityNodeInfo? {
        if (node == null) return null
        if (node.viewIdResourceName == viewId) return node
        for (i in 0 until node.childCount) {
            val found = findNodeByViewId(node.getChild(i), viewId)
            if (found != null) return found
        }
        return null
    }

    /**
     * Check whether the given Firefox accessibility tree shows private-mode
     * markers. Returns true if any FIREFOX_PRIVATE_MARKERS substring appears
     * in any node's text or contentDescription.
     *
     * Marker strings can break if Firefox renames its UI — when private mode
     * stops being detected after a Firefox update, check these strings against
     * the current build.
     */
    private fun isFirefoxPrivate(node: AccessibilityNodeInfo?): Boolean {
        if (node == null) return false

        val text = node.text?.toString() ?: ""
        val desc = node.contentDescription?.toString() ?: ""

        for (marker in FIREFOX_PRIVATE_MARKERS) {
            if (text.contains(marker, ignoreCase = true)) return true
            if (desc.contains(marker, ignoreCase = true)) return true
        }

        for (i in 0 until node.childCount) {
            if (isFirefoxPrivate(node.getChild(i))) return true
        }
        return false
    }

    override fun onInterrupt() {
        Log.w(TAG, "Service interrupted")
    }
}