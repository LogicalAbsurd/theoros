package com.theoros.capture

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.TimeZone
import java.util.concurrent.TimeUnit

/**
 * Thin HTTP client for the capture service's /exclusions/urls endpoints.
 *
 * Auth: Cloudflare Access service token. Every request carries the
 * CF-Access-Client-Id / CF-Access-Client-Secret header pair; values come from
 * BuildConfig (populated from local.properties at build time — see
 * build.gradle.kts part 1). Base URL is BuildConfig.THEOROS_BASE_URL.
 *
 * Errors: any non-2xx status or network failure throws an exception whose
 * message includes the HTTP status (when available) and the response body
 * snippet, so failures surface usefully in Logcat. Callers are expected to
 * catch; this class never crashes the app.
 */
object ApiClient {

    private val client: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(20, TimeUnit.SECONDS)
        .readTimeout(20, TimeUnit.SECONDS)
        .writeTimeout(20, TimeUnit.SECONDS)
        .build()

    private val JSON = "application/json; charset=utf-8".toMediaType()

    data class UrlExclusions(
        val domains: List<String>,
        val pathPrefixes: List<String>,
    )

    /**
     * Build a Request.Builder pre-targeted at [path] on the configured base
     * URL and with the Cloudflare Access service-token headers attached. The
     * caller supplies the HTTP method and body.
     */
    private fun requestBuilder(path: String): Request.Builder {
        val base = BuildConfig.THEOROS_BASE_URL.trimEnd('/')
        return Request.Builder()
            .url(base + path)
            .header("CF-Access-Client-Id", BuildConfig.CF_ACCESS_CLIENT_ID)
            .header("CF-Access-Client-Secret", BuildConfig.CF_ACCESS_CLIENT_SECRET)
    }

    suspend fun getUrlExclusions(): UrlExclusions = withContext(Dispatchers.IO) {
        val req = requestBuilder("/exclusions/urls").get().build()
        client.newCall(req).execute().use { resp ->
            val bodyStr = resp.body?.string().orEmpty()
            if (!resp.isSuccessful) {
                throw ApiException(resp.code, bodyStr, "GET /exclusions/urls")
            }
            val json = JSONObject(bodyStr)
            UrlExclusions(
                domains = json.optJSONArray("domains").toStringList(),
                pathPrefixes = json.optJSONArray("path_prefixes").toStringList(),
            )
        }
    }

    suspend fun addUrlExclusion(kind: String, value: String) = withContext(Dispatchers.IO) {
        val body = JSONObject().put("kind", kind).put("value", value)
            .toString().toRequestBody(JSON)
        val req = requestBuilder("/exclusions/urls").post(body).build()
        client.newCall(req).execute().use { resp ->
            if (!resp.isSuccessful) {
                throw ApiException(resp.code, resp.body?.string().orEmpty(), "POST /exclusions/urls")
            }
        }
    }

    suspend fun removeUrlExclusion(kind: String, value: String) = withContext(Dispatchers.IO) {
        // The Cloudflare/REST endpoint expects a JSON body on DELETE. OkHttp
        // doesn't allow body on .delete() by default convention but does via
        // .method("DELETE", body); that's the canonical way to send one.
        val body = JSONObject().put("kind", kind).put("value", value)
            .toString().toRequestBody(JSON)
        val req = requestBuilder("/exclusions/urls").method("DELETE", body).build()
        client.newCall(req).execute().use { resp ->
            if (!resp.isSuccessful) {
                throw ApiException(resp.code, resp.body?.string().orEmpty(), "DELETE /exclusions/urls")
            }
        }
    }

    suspend fun postCapture(
        sourceApp: String,
        title: String?,
        rawText: String,
    ) = withContext(Dispatchers.IO) {
        // ISO-8601 UTC, second precision, trailing 'Z' — matches the desktop
        // capture service's expected captured_at format.
        val now = Date()
        val iso = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss'Z'", Locale.US).apply {
            timeZone = TimeZone.getTimeZone("UTC")
        }.format(now)
        // Local calendar date, not UTC — gives the desktop service a
        // per-day session scope aligned with how the operator experiences
        // "today," so its tab_session_id + content_hash dedup applies to
        // mobile captures across a day of use of the same app.
        val localDate = SimpleDateFormat("yyyy-MM-dd", Locale.US).format(now)
        val rawMetadata = JSONObject()
            .put("tab_session_id", "$sourceApp:$localDate")
        val json = JSONObject()
            .put("source_tier", "mobile")
            .put("content_text", rawText)
            .put("source_app", sourceApp)
            .put("captured_at", iso)
            .put("raw_metadata", rawMetadata)
        if (title != null) json.put("title", title)
        val body = json.toString().toRequestBody(JSON)
        val req = requestBuilder("/capture").post(body).build()
        client.newCall(req).execute().use { resp ->
            if (!resp.isSuccessful) {
                throw ApiException(resp.code, resp.body?.string().orEmpty(), "POST /capture")
            }
        }
    }

    private fun JSONArray?.toStringList(): List<String> {
        if (this == null) return emptyList()
        val out = ArrayList<String>(length())
        for (i in 0 until length()) out.add(getString(i))
        return out
    }

    class ApiException(
        val status: Int,
        val responseBody: String,
        endpoint: String,
    ) : RuntimeException("$endpoint failed: HTTP $status — ${responseBody.take(500)}")
}
