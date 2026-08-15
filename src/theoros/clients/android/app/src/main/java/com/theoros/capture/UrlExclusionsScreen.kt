package com.theoros.capture

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.launch

/**
 * Editor for the URL-exclusion lists kept by the capture service.
 *
 * Two sections — domains and path prefixes — each rendered as a removable
 * list with an add button. The screen seeds initial state from the local
 * cache so it renders instantly, then refreshes from the server on first
 * composition. Add/remove operations call the server, then re-fetch the
 * canonical list so the local cache and the displayed state never lie
 * about what the server actually has.
 *
 * Network failures show as an inline banner rather than crashing — the
 * cached state stays visible so the screen remains useful offline.
 */
@Composable
fun UrlExclusionsScreen(
    modifier: Modifier = Modifier,
    onBack: () -> Unit = {},
) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()

    var domains by remember {
        mutableStateOf(CaptureSettings.getUrlExcludedDomains(context))
    }
    var pathPrefixes by remember {
        mutableStateOf(CaptureSettings.getUrlExcludedPathPrefixes(context))
    }
    var loading by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }
    // null = no dialog open; "domain" or "path_prefix" = which add dialog.
    var addKind by remember { mutableStateOf<String?>(null) }

    // op() runs first (the mutating call — add/remove — or a no-op for a
    // pure refresh), then we re-fetch the canonical list, persist it, and
    // update state. One helper because all three flows share the
    // load/error/persist shape; three copy-pastes would drift.
    suspend fun runAndRefresh(label: String, op: suspend () -> Unit) {
        loading = true
        error = null
        try {
            op()
            val fresh = ApiClient.getUrlExclusions()
            CaptureSettings.saveUrlExclusions(context, fresh)
            domains = fresh.domains.toSet()
            pathPrefixes = fresh.pathPrefixes.toSet()
        } catch (e: ApiClient.ApiException) {
            error = "$label failed: HTTP ${e.status} — ${e.responseBody.take(120)}"
        } catch (e: Exception) {
            error = "$label failed: ${e.message ?: e.javaClass.simpleName}"
        } finally {
            loading = false
        }
    }

    LaunchedEffect(Unit) {
        runAndRefresh("Refresh") {}
    }

    Column(modifier = modifier.fillMaxSize()) {
        Row(
            modifier = Modifier.fillMaxWidth().padding(16.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            TextButton(onClick = onBack) {
                Text("← Back")
            }
            Text(
                text = "URL exclusions",
                style = MaterialTheme.typography.titleLarge,
                modifier = Modifier.padding(start = 8.dp).weight(1f),
            )
            TextButton(
                onClick = { scope.launch { runAndRefresh("Refresh") {} } },
                enabled = !loading,
            ) {
                Text("↻ Refresh")
            }
        }
        Text(
            text = "URLs matching these domains or path prefixes are skipped by the browser-side capture.",
            style = MaterialTheme.typography.bodySmall,
            modifier = Modifier.padding(horizontal = 16.dp, vertical = 4.dp),
        )

        if (loading) {
            Row(
                modifier = Modifier.fillMaxWidth().padding(8.dp),
                horizontalArrangement = Arrangement.Center,
            ) {
                CircularProgressIndicator()
            }
        }
        error?.let { msg ->
            Text(
                text = msg,
                color = MaterialTheme.colorScheme.error,
                style = MaterialTheme.typography.bodySmall,
                modifier = Modifier.padding(horizontal = 16.dp, vertical = 4.dp),
            )
        }

        LazyColumn(modifier = Modifier.fillMaxSize()) {
            item { SectionHeader("Domains") }
            items(domains.sorted(), key = { "domain:$it" }) { value ->
                ExclusionRow(
                    text = value,
                    enabled = !loading,
                    onRemove = {
                        scope.launch {
                            runAndRefresh("Remove") {
                                ApiClient.removeUrlExclusion("domain", value)
                            }
                        }
                    },
                )
            }
            item {
                AddButton(
                    label = "+ Domain",
                    enabled = !loading,
                    onClick = { addKind = "domain" },
                )
            }
            item {
                HorizontalDivider(modifier = Modifier.padding(vertical = 16.dp))
            }
            item { SectionHeader("Path prefixes") }
            items(pathPrefixes.sorted(), key = { "path:$it" }) { value ->
                ExclusionRow(
                    text = value,
                    enabled = !loading,
                    onRemove = {
                        scope.launch {
                            runAndRefresh("Remove") {
                                ApiClient.removeUrlExclusion("path_prefix", value)
                            }
                        }
                    },
                )
            }
            item {
                AddButton(
                    label = "+ Path prefix",
                    enabled = !loading,
                    onClick = { addKind = "path_prefix" },
                )
            }
        }
    }

    addKind?.let { kind ->
        AddExclusionDialog(
            kind = kind,
            onDismiss = { addKind = null },
            onConfirm = { value ->
                addKind = null
                scope.launch {
                    runAndRefresh("Add") {
                        ApiClient.addUrlExclusion(kind, value)
                    }
                }
            },
        )
    }
}

@Composable
private fun SectionHeader(text: String) {
    Text(
        text = text,
        style = MaterialTheme.typography.titleMedium,
        modifier = Modifier.padding(horizontal = 16.dp, vertical = 8.dp),
    )
}

@Composable
private fun ExclusionRow(
    text: String,
    enabled: Boolean,
    onRemove: () -> Unit,
) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = 16.dp, vertical = 4.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(
            text = text,
            style = MaterialTheme.typography.bodyLarge,
            modifier = Modifier.weight(1f),
        )
        TextButton(onClick = onRemove, enabled = enabled) {
            Text("Remove")
        }
    }
}

@Composable
private fun AddButton(label: String, enabled: Boolean, onClick: () -> Unit) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = 16.dp, vertical = 8.dp),
    ) {
        Button(onClick = onClick, enabled = enabled) {
            Text(label)
        }
    }
}

@Composable
private fun AddExclusionDialog(
    kind: String,
    onDismiss: () -> Unit,
    onConfirm: (String) -> Unit,
) {
    var text by remember { mutableStateOf("") }
    val title = if (kind == "domain") "Add domain" else "Add path prefix"
    val placeholder = if (kind == "domain") "example.com" else "/admin/"
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(title) },
        text = {
            OutlinedTextField(
                value = text,
                onValueChange = { text = it },
                singleLine = true,
                placeholder = { Text(placeholder) },
            )
        },
        confirmButton = {
            TextButton(
                onClick = {
                    val trimmed = text.trim()
                    if (trimmed.isNotEmpty()) onConfirm(trimmed)
                },
                enabled = text.trim().isNotEmpty(),
            ) {
                Text("Add")
            }
        },
        dismissButton = {
            TextButton(onClick = onDismiss) {
                Text("Cancel")
            }
        },
    )
}
