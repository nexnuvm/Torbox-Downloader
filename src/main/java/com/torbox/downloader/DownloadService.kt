package com.torbox.downloader

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Intent
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import com.chaquo.python.Python

/**
 * Foreground service hosting the Python backend (the same queue engine,
 * SQLite persistence, TorBox API logic, and HTTP server as the Termux
 * version -- see app/src/main/python/torbox_server.py).
 *
 * Running as a foreground service is the Android-native equivalent of what
 * termux-wake-lock plus "just keep the Termux session alive" was
 * approximating: it tells Android this process is doing user-visible work
 * and should not be killed when the app is backgrounded.
 */
class DownloadService : Service() {

    private val serviceChannelId = "torbox_service"
    private var started = false

    override fun onCreate() {
        super.onCreate()
        createServiceChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (!started) {
            started = true
            startForeground(2001, buildForegroundNotification())
            startPythonBackend()
        }
        return START_STICKY
    }

    private fun startPythonBackend() {
        val bridge = AndroidBridge(applicationContext)

        Thread {
            val py = Python.getInstance()
            val module = py.getModule("torbox_server")

            // Wire the Android-native replacements in before starting the
            // server -- see torbox_server.py's set_bridge()/start_server().
            module.callAttr("set_bridge", bridge)
            module.callAttr("set_download_dir", bridge.getDownloadDir())
            module.callAttr("set_db_path", bridge.getDbPath())
            module.callAttr("set_aria2c_path", "${bridge.getNativeLibDir()}/libaria2c.so")

            // Blocks forever running the HTTP server's serve_forever() loop,
            // same as the Termux version's __main__ block -- that's why this
            // runs on its own Thread rather than the service's main thread.
            module.callAttr("start_server")
        }.start()
    }

    private fun createServiceChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                serviceChannelId,
                "Background service",
                NotificationManager.IMPORTANCE_MIN
            )
            val manager = getSystemService(NotificationManager::class.java)
            manager.createNotificationChannel(channel)
        }
    }

    private fun buildForegroundNotification() =
        NotificationCompat.Builder(this, serviceChannelId)
            .setContentTitle("TorBox Downloader")
            .setContentText("Running in the background")
            .setSmallIcon(android.R.drawable.stat_sys_download)
            .setOngoing(true)
            .build()

    override fun onDestroy() {
        try {
            Python.getInstance().getModule("torbox_server").callAttr("stop_server")
        } catch (e: Exception) {
            // Best-effort -- the process may already be going down.
        }
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null
}
