package com.torbox.downloader

import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.os.Build
import android.os.PowerManager
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import java.io.File

/**
 * Everything Python calls back into Android for. This is the direct
 * replacement for the old termux-wake-lock / termux-wake-unlock /
 * termux-notification / termux-wifi-connectioninfo subprocess calls.
 *
 * Importantly, these are plain in-process method calls -- there is no
 * companion-app process that Android's battery optimizer can suspend out
 * from under them, which is the entire class of bug (unbounded subprocess
 * hangs) that most of the debugging in the Termux version was about. That
 * failure mode structurally cannot happen here.
 *
 * Passed into the Python module once at service startup
 * (see DownloadService.kt) via: torbox_server.set_bridge(AndroidBridge)
 */
class AndroidBridge(private val context: Context) {

    private val channelId = "torbox_downloads"
    private var wakeLock: PowerManager.WakeLock? = null

    init {
        createNotificationChannel()
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                channelId,
                "Download progress",
                NotificationManager.IMPORTANCE_LOW
            )
            val manager = context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
            manager.createNotificationChannel(channel)
        }
    }

    // --- Wake lock -----------------------------------------------------
    // Called from Python at the start/end of each download, same call
    // sites as the old run_termux_cmd(["termux-wake-lock"/"-unlock"]).

    @Synchronized
    fun acquireWakeLock() {
        if (wakeLock?.isHeld == true) return
        val pm = context.getSystemService(Context.POWER_SERVICE) as PowerManager
        wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "TorBoxDownloader::download")
        wakeLock?.acquire(30 * 60 * 1000L /* 30 min safety cap, renewed by repeated calls */)
    }

    @Synchronized
    fun releaseWakeLock() {
        wakeLock?.let { if (it.isHeld) it.release() }
        wakeLock = null
    }

    // --- Notifications ---------------------------------------------------
    // title/content as plain strings -- mirrors send_notification()'s
    // signature closely so the Python call sites barely change.

    fun sendNotification(title: String, content: String) {
        val builder = NotificationCompat.Builder(context, channelId)
            .setContentTitle(title)
            .setContentText(content)
            .setSmallIcon(android.R.drawable.stat_sys_download)
            .setOnlyAlertOnce(true)
            .setAutoCancel(true)

        try {
            NotificationManagerCompat.from(context).notify(1001, builder.build())
        } catch (e: SecurityException) {
            // POST_NOTIFICATIONS not granted (API 33+) -- fail silently,
            // same as the old Termux version did when termux-notification
            // wasn't available.
        }
    }

    // --- Network check ---------------------------------------------------
    // Direct replacement for check_wifi(). No Location-permission
    // requirement at all, unlike termux-wifi-connectioninfo.

    fun isOnWifi(): Boolean {
        val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
        val network = cm.activeNetwork ?: return false
        val caps = cm.getNetworkCapabilities(network) ?: return false
        return caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)
    }

    // --- Storage -----------------------------------------------------
    // App-private external storage: a plain filesystem path aria2c can
    // write to directly with no Storage Access Framework / scoped-storage
    // restrictions, and no extra permission prompt. Visible to the user
    // via a file manager under Android/data/com.torbox.downloader/files/.
    //
    // If you want completed files to land in the public Downloads folder
    // instead, add a SAF "export" step that copies from here into a
    // user-picked DocumentFile tree after each download completes --
    // aria2c itself cannot write directly through a SAF URI.

    fun getDownloadDir(): String {
        val dir = File(context.getExternalFilesDir(null), "downloads")
        if (!dir.exists()) dir.mkdirs()
        return dir.absolutePath
    }

    fun getDbPath(): String {
        return File(context.filesDir, "torbox_queue.db").absolutePath
    }

    fun getNativeLibDir(): String {
        return context.applicationInfo.nativeLibraryDir
    }
}
