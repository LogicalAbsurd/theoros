package com.theoros.capture

import android.os.Bundle
import android.util.Log
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Button
import androidx.compose.material3.Checkbox
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.compose.runtime.rememberCoroutineScope
import com.theoros.capture.ui.theme.TheorosTheme
import kotlinx.coroutines.launch

enum class Screen { STATUS, APP_EXCLUSIONS, URL_EXCLUSIONS }

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        setContent {
            TheorosTheme {
                var current by remember { mutableStateOf(Screen.STATUS) }
                Scaffold(modifier = Modifier.fillMaxSize()) { innerPadding ->
                    when (current) {
                        Screen.STATUS -> StatusScreen(
                            modifier = Modifier.padding(innerPadding),
                            onOpenAppExclusions = { current = Screen.APP_EXCLUSIONS },
                            onOpenUrlExclusions = { current = Screen.URL_EXCLUSIONS },
                        )
                        Screen.APP_EXCLUSIONS -> ExclusionsScreen(
                            modifier = Modifier.padding(innerPadding),
                            onBack = { current = Screen.STATUS },
                        )
                        Screen.URL_EXCLUSIONS -> UrlExclusionsScreen(
                            modifier = Modifier.padding(innerPadding),
                            onBack = { current = Screen.STATUS },
                        )
                    }
                }
            }
        }
    }
}

@Composable
fun StatusScreen(
    modifier: Modifier = Modifier,
    onOpenAppExclusions: () -> Unit = {},
    onOpenUrlExclusions: () -> Unit = {},
) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var paused by remember { mutableStateOf(CaptureSettings.isPaused(context)) }

    LaunchedEffect(Unit) {
        paused = CaptureSettings.isPaused(context)
    }

    Column(
        modifier = modifier.fillMaxSize().padding(24.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Text(
            text = "Theoros Capture",
            style = MaterialTheme.typography.headlineMedium,
        )
        Text(
            text = if (paused) "Capture is PAUSED" else "Capture is ACTIVE",
            style = MaterialTheme.typography.titleMedium,
        )
        Button(onClick = {
            val newPaused = !paused
            CaptureSettings.setPaused(context, newPaused)
            paused = newPaused
        }) {
            Text(if (paused) "Resume capture" else "Pause capture")
        }
        Button(onClick = onOpenAppExclusions) {
            Text("App Exclusions")
        }
        Button(onClick = onOpenUrlExclusions) {
            Text("URL Exclusions")
        }
        Button(onClick = {
            scope.launch {
                try {
                    ApiClient.postCapture("test-button", "M5 method test", "hello from the android method")
                    Log.i("TheorosTest", "TEST CAPTURE OK")
                } catch (t: Throwable) {
                    Log.e("TheorosTest", "TEST CAPTURE failed", t)
                }
            }
        }) {
            Text("TEST CAPTURE")
        }
    }
}

@Composable
fun ExclusionsScreen(
    modifier: Modifier = Modifier,
    onBack: () -> Unit = {},
) {
    val context = LocalContext.current

    // Cache the installed apps list — querying it on every recomposition
    // would be wasteful. remember{} computes once per composable lifetime.
    val apps = remember { InstalledApps.list(context) }

    // Mutable state for the current exclusion set. Initialized from prefs,
    // updated as the user toggles, persisted on every change.
    var excluded by remember { mutableStateOf(CaptureSettings.getExcludedPackages(context)) }

    Column(modifier = modifier.fillMaxSize()) {
        Row(
            modifier = Modifier.fillMaxWidth().padding(16.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            TextButton(onClick = onBack) {
                Text("← Back")
            }
            Text(
                text = "Excluded apps",
                style = MaterialTheme.typography.titleLarge,
                modifier = Modifier.padding(start = 8.dp),
            )
        }
        Text(
            text = "Apps you check here are skipped entirely — no text from them is captured.",
            style = MaterialTheme.typography.bodySmall,
            modifier = Modifier.padding(horizontal = 16.dp, vertical = 8.dp),
        )
        LazyColumn(modifier = Modifier.fillMaxSize()) {
            items(apps, key = { it.packageName }) { app ->
                val isExcluded = app.packageName in excluded
                Row(
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(horizontal = 16.dp, vertical = 4.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Checkbox(
                        checked = isExcluded,
                        onCheckedChange = { nowExcluded ->
                            val newSet = if (nowExcluded) {
                                excluded + app.packageName
                            } else {
                                excluded - app.packageName
                            }
                            CaptureSettings.setExcludedPackages(context, newSet)
                            excluded = newSet
                        },
                    )
                    Column(modifier = Modifier.padding(start = 8.dp)) {
                        Text(text = app.label, style = MaterialTheme.typography.bodyLarge)
                        Text(
                            text = app.packageName,
                            style = MaterialTheme.typography.bodySmall,
                        )
                    }
                }
            }
        }
    }
}