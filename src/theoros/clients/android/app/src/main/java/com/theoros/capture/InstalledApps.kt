package com.theoros.capture

import android.content.Context
import android.content.pm.PackageManager

/**
 * Helper for querying the list of user-installed apps on the device.
 *
 * We deliberately filter to apps that have a launcher icon — system
 * services, OEM hidden apps, and similar don't show up. The operator
 * wants to exclude apps they actually use, not see a list of 400
 * internal Samsung packages.
 */
object InstalledApps {

    data class AppInfo(
        val packageName: String,
        val label: String,
    )

    /**
     * List all apps that have a launcher activity, sorted by display label.
     *
     * Cheap to call once on screen creation; not cheap enough to call on
     * every recomposition. Cache the result in remember{} at the call site.
     */
    fun list(context: Context): List<AppInfo> {
        val pm = context.packageManager
        val intent = android.content.Intent(android.content.Intent.ACTION_MAIN).apply {
            addCategory(android.content.Intent.CATEGORY_LAUNCHER)
        }
        val resolveInfos = pm.queryIntentActivities(intent, 0)

        return resolveInfos
            .map {
                AppInfo(
                    packageName = it.activityInfo.packageName,
                    label = it.loadLabel(pm).toString(),
                )
            }
            .distinctBy { it.packageName }
            .sortedBy { it.label.lowercase() }
    }
}