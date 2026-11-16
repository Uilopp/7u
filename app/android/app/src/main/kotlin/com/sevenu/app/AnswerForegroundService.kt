package com.sevenu.app

import android.app.Service
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager

/** Protects work already running in the retained engine; never owns/restarts that work. */
class AnswerForegroundService : Service() {
    private val handler = Handler(Looper.getMainLooper())
    private var wakeLock: PowerManager.WakeLock? = null
    var isProtecting = false
        private set
    private val bridge: BackgroundAnswerBridge
        get() = (application as SevenUApplication).backgroundAnswers
    private val watchdog = Runnable { interruptAndStop() }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        // Even if finish arrived before this callback, briefly promote first to
        // satisfy startForegroundService's deadline, then stop when no jobs remain.
        bridge.serviceStarted(this)
        return START_NOT_STICKY
    }

    fun protect(conversationId: String?) {
        val notification = bridge.runningNotification(conversationId)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(
                BackgroundAnswerBridge.RUNNING_NOTIFICATION_ID,
                notification,
                ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC,
            )
        } else {
            startForeground(BackgroundAnswerBridge.RUNNING_NOTIFICATION_ID, notification)
        }
        if (!isProtecting) {
            isProtecting = true
            val power = getSystemService(POWER_SERVICE) as PowerManager
            wakeLock = power.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "7u:background_answers").apply {
                setReferenceCounted(false)
                acquire(MAX_PROTECTION_MILLIS)
            }
            // A technical watchdog, not a user quota. Never report success on expiry.
            handler.postDelayed(watchdog, MAX_PROTECTION_MILLIS)
        }
    }

    fun stopProtection() {
        releaseProtection()
        stopSelf()
    }

    private fun releaseProtection() {
        handler.removeCallbacks(watchdog)
        wakeLock?.let { if (it.isHeld) it.release() }
        wakeLock = null
        isProtecting = false
        stopForeground(STOP_FOREGROUND_REMOVE)
    }

    private fun interruptAndStop() {
        // Stop promptly before crossing the channel; Dart may be busy or absent.
        stopProtection()
        bridge.serviceInterrupted(this)
    }

    override fun onTimeout(startId: Int, fgsType: Int) {
        interruptAndStop()
    }

    override fun onDestroy() {
        releaseProtection()
        bridge.serviceDestroyed(this)
        super.onDestroy()
    }

    companion object {
        const val MAX_PROTECTION_MILLIS = 2 * 60 * 60 * 1000L
    }
}
