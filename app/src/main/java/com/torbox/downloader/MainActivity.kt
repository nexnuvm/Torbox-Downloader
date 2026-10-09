package com.torbox.downloader

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat

class MainActivity : AppCompatActivity() {

    private lateinit var webView: WebView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        requestNotificationPermissionIfNeeded()

        // Start the backend as a foreground service so it survives the app
        // being backgrounded -- see DownloadService.kt.
        val serviceIntent = Intent(this, DownloadService::class.java)
        ContextCompat.startForegroundService(this, serviceIntent)

        webView = WebView(this)
        webView.settings.javaScriptEnabled = true
        webView.settings.domStorageEnabled = true   // the UI uses localStorage for the saved API key
        webView.webViewClient = WebViewClient()
        setContentView(webView)

        // The Python HTTP server binds 127.0.0.1:8080 -- see
        // torbox_server.py's start_server(). Give it a moment to come up on
        // first launch; a short delay avoids a blank "connection refused"
        // page on a cold start.
        webView.postDelayed({ loadUi() }, 800)
    }

    private fun loadUi() {
        webView.loadUrl("http://127.0.0.1:8080/")
    }

    override fun onResume() {
        super.onResume()
        // Re-point the WebView at the server in case it was navigated away
        // or the page failed to load before the server was ready.
        if (webView.url == null || webView.url == "about:blank") {
            loadUi()
        }
    }

    private fun requestNotificationPermissionIfNeeded() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED
            ) {
                ActivityCompat.requestPermissions(
                    this, arrayOf(Manifest.permission.POST_NOTIFICATIONS), 100
                )
            }
        }
    }
}
