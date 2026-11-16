package com.sevenu.app

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** A short, offline receiver: no Flutter engine, service, network or question replay. */
class DailyMemoryReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent?) {
        val scheduler = (context.applicationContext as SevenUApplication).dailyMemories
        try {
            if (intent?.action == DailyMemoryScheduler.ACTION_DELIVER) scheduler.deliver()
        } catch (_: Exception) {
            // A missing/corrupt bundle or OEM notification failure must not crash
            // a receiver-only process or start any fallback network work.
        } finally {
            // Reboot/update/clock changes only schedule the next future occurrence.
            // Delivery also re-arms a one-shot inexact alarm, never a background loop.
            runCatching { scheduler.reschedule(force = true) }
        }
    }
}
