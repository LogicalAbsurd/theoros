package com.theoros.capture

import android.content.Context

/**
 * Persistent settings for capture behavior.
 *
 * Wraps SharedPreferences so the activity (which writes settings) and the
 * accessibility service (which reads them) share one source of truth.
 *
 * SharedPreferences is the standard Android key/value store for app
 * settings. Backed by an XML file in app-private storage, survives
 * restarts, accessible from any component within the same app.
 */
object CaptureSettings {
    private const val PREFS_NAME = "theoros_capture_settings"
    private const val KEY_PAUSED = "capture_paused"
    private const val KEY_EXCLUDED_PACKAGES = "excluded_packages"
    private const val KEY_URL_EXCLUDED_DOMAINS = "url_excluded_domains"
    private const val KEY_URL_EXCLUDED_PATH_PREFIXES = "url_excluded_path_prefixes"
    private const val KEY_URL_EXCLUSIONS_LAST_REFRESHED = "url_exclusions_last_refreshed"

    fun isPaused(context: Context): Boolean {
        val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        return prefs.getBoolean(KEY_PAUSED, false)
    }

    fun setPaused(context: Context, paused: Boolean) {
        val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        prefs.edit().putBoolean(KEY_PAUSED, paused).apply()
    }

    /**
     * Get the set of package names the operator has chosen to exclude from
     * capture. Returns an empty set if nothing is excluded.
     *
     * Returns a defensive copy so callers can't mutate the underlying store.
     */
    fun getExcludedPackages(context: Context): Set<String> {
        val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        return prefs.getStringSet(KEY_EXCLUDED_PACKAGES, emptySet())?.toSet() ?: emptySet()
    }

    fun setExcludedPackages(context: Context, packages: Set<String>) {
        val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        prefs.edit().putStringSet(KEY_EXCLUDED_PACKAGES, packages).apply()
    }

    /**
     * Cached URL-exclusion lists fetched from the capture service.
     *
     * These mirror /exclusions/urls on the server so the in-browser
     * URL-classification path can run without a network round-trip on every
     * navigation. The server remains the source of truth; this cache is
     * refreshed via [saveUrlExclusions] after a successful pull.
     *
     * Defensive copy on read for the same reason as getExcludedPackages —
     * SharedPreferences hands back its internal Set reference, which the docs
     * warn must not be mutated.
     */
    fun getUrlExcludedDomains(context: Context): Set<String> {
        val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        return prefs.getStringSet(KEY_URL_EXCLUDED_DOMAINS, emptySet())?.toSet() ?: emptySet()
    }

    fun setUrlExcludedDomains(context: Context, domains: Set<String>) {
        val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        prefs.edit().putStringSet(KEY_URL_EXCLUDED_DOMAINS, domains).apply()
    }

    fun getUrlExcludedPathPrefixes(context: Context): Set<String> {
        val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        return prefs.getStringSet(KEY_URL_EXCLUDED_PATH_PREFIXES, emptySet())?.toSet() ?: emptySet()
    }

    fun setUrlExcludedPathPrefixes(context: Context, pathPrefixes: Set<String>) {
        val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        prefs.edit().putStringSet(KEY_URL_EXCLUDED_PATH_PREFIXES, pathPrefixes).apply()
    }

    fun getUrlExclusionsLastRefreshed(context: Context): Long {
        val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        return prefs.getLong(KEY_URL_EXCLUSIONS_LAST_REFRESHED, 0L)
    }

    fun setUrlExclusionsLastRefreshed(context: Context, timestamp: Long) {
        val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        prefs.edit().putLong(KEY_URL_EXCLUSIONS_LAST_REFRESHED, timestamp).apply()
    }

    /**
     * Persist a freshly-fetched exclusions snapshot atomically.
     *
     * All three keys move in one SharedPreferences.edit() commit, so a reader
     * never sees mismatched domain/path-prefix state or a stale timestamp
     * paired with new lists. Lists from the API are converted to Sets for
     * O(1) membership checks in the classifier hot path.
     */
    fun saveUrlExclusions(context: Context, exclusions: ApiClient.UrlExclusions) {
        val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        prefs.edit()
            .putStringSet(KEY_URL_EXCLUDED_DOMAINS, exclusions.domains.toSet())
            .putStringSet(KEY_URL_EXCLUDED_PATH_PREFIXES, exclusions.pathPrefixes.toSet())
            .putLong(KEY_URL_EXCLUSIONS_LAST_REFRESHED, System.currentTimeMillis())
            .apply()
    }
}