package com.torbox.downloader

import android.app.Application
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform

class TorBoxApplication : Application() {
    override fun onCreate() {
        super.onCreate()
        if (!Python.isStarted()) {
            Python.start(AndroidPlatform(this))
        }
    }
}
