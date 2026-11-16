package com.sevenu.app

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import io.flutter.plugin.common.BinaryMessenger
import io.flutter.plugin.common.MethodCall
import io.flutter.plugin.common.MethodChannel
import java.lang.ref.WeakReference
import java.util.ArrayDeque

/** All methods, lifecycle callbacks and Service callbacks run on the Android main thread. */
class BackgroundAnswerBridge(private val app: SevenUApplication) : MethodChannel.MethodCallHandler {
    private data class Job(
        val id: String,
        val conversationId: String,
        val waitingBegins: MutableList<MethodChannel.Result> = mutableListOf(),
        var owner: AnswerForegroundService? = null,
    )

    private data class PermissionRequest(val result: MethodChannel.Result, val channelId: String)

    private val jobs = linkedMapOf<String, Job>()
    private var channel: MethodChannel? = null
    private var service: AnswerForegroundService? = null
    private var serviceStartRequested = false
    private var activity = WeakReference<MainActivity>(null)
    private var started = false
    private var resumed = false
    private var permissionInFlight = false
    private var permissionOwner = WeakReference<MainActivity>(null)
    private val permissionResults = mutableListOf<PermissionRequest>()
    private val launchConversations = ArrayDeque<String>()
    private val notifications = app.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
    private val preferences = app.getSharedPreferences("seven_u_background_answers", Context.MODE_PRIVATE)

    fun attachMessenger(messenger: BinaryMessenger) {
        channel = MethodChannel(messenger, CHANNEL).also { it.setMethodCallHandler(this) }
    }

    override fun onMethodCall(call: MethodCall, result: MethodChannel.Result) {
        try {
            when (call.method) {
                "begin" -> begin(identifier(call, "jobId"), identifier(call, "conversationId"), result)
                "finish" -> {
                    val outcome = call.argument<String>("outcome")
                    require(outcome in setOf("success", "error", "silent")) { "outcome 无效。" }
                    finish(identifier(call, "jobId"), identifier(call, "conversationId"), outcome!!)
                    result.success(null)
                }
                "requestNotificationPermission" -> requestNotificationPermission(result)
                "takeLaunchConversation" -> result.success(launchConversations.pollFirst())
                else -> result.notImplemented()
            }
        } catch (error: IllegalArgumentException) {
            result.error("INVALID_ARGUMENT", error.message, null)
        } catch (_: Exception) {
            result.error("BACKGROUND_UNAVAILABLE", "后台保护暂时不可用，请回到 App 后重试。", null)
        }
    }

    private fun identifier(call: MethodCall, key: String): String {
        val value = call.argument<Any>(key)
        require(value is String && value.isNotBlank() && value.length <= 256) { "$key 无效。" }
        return value
    }

    private fun begin(jobId: String, conversationId: String, result: MethodChannel.Result) {
        jobs[jobId]?.let { existing ->
            require(existing.conversationId == conversationId) { "任务与对话不匹配。" }
            if (existing.owner?.isProtecting == true) {
                result.success(startResult())
            } else {
                existing.waitingBegins.add(result)
            }
            return
        }
        if (!resumed || activity.get() == null) {
            result.error("APP_NOT_RESUMED", "请回到 App 后重新发送。", null)
            return
        }
        val job = Job(jobId, conversationId, mutableListOf(result))
        jobs[jobId] = job
        val running = service?.takeIf { it.isProtecting }
        if (running != null) {
            try {
                running.protect(jobs.values.firstOrNull()?.conversationId)
                job.owner = running
                completeBegins(job)
            } catch (_: Exception) {
                jobs.remove(jobId)
                failBegins(job, "BACKGROUND_UNAVAILABLE", "后台保护暂时不可用，请重试。")
            }
            return
        }
        if (!serviceStartRequested) {
            serviceStartRequested = true
            try {
                val intent = Intent(app, AnswerForegroundService::class.java)
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                    app.startForegroundService(intent)
                } else {
                    app.startService(intent)
                }
            } catch (_: Exception) {
                serviceStartRequested = false
                jobs.remove(jobId)
                failBegins(job, "BACKGROUND_UNAVAILABLE", "系统暂未允许后台回答，请回到 App 后重试。")
            }
        }
    }

    private fun finish(jobId: String, conversationId: String, outcome: String) {
        val job = jobs[jobId] ?: return // Idempotent completion, including after an interruption.
        require(job.conversationId == conversationId) { "任务与对话不匹配。" }
        jobs.remove(jobId)
        failBegins(job, "JOB_FINISHED", "任务已经结束。")
        val running = service
        if (jobs.isEmpty()) {
            running?.stopProtection()
        } else if (running?.isProtecting == true) {
            try {
                running.protect(jobs.values.first().conversationId)
            } catch (_: Exception) {
                running.stopProtection()
                serviceInterrupted(running)
            }
        }
        // A permission dialog (including its result-to-resume gap) or split
        // screen can pause the Activity without hiding it. Do not alert over it.
        val visible = resumed || started
        if (outcome != "silent" && !visible && notificationsEnabled()) {
            try {
                notifications.notify("answer:$jobId", FINISHED_NOTIFICATION_ID, finishedNotification(job, outcome))
            } catch (_: SecurityException) {
                // Revoking notification permission never invalidates a saved answer.
            }
        }
    }

    fun serviceStarted(startedService: AnswerForegroundService) {
        service = startedService
        serviceStartRequested = false
        try {
            startedService.protect(jobs.values.firstOrNull()?.conversationId)
            if (jobs.isEmpty()) {
                startedService.stopProtection()
                return
            }
            for (job in jobs.values) {
                job.owner = startedService
                completeBegins(job)
            }
        } catch (_: Exception) {
            val failed = jobs.values.toList()
            jobs.clear()
            startedService.stopProtection()
            val interrupted = mutableListOf<String>()
            for (job in failed) {
                if (job.waitingBegins.isEmpty()) interrupted.add(job.id)
                failBegins(job, "BACKGROUND_UNAVAILABLE", "系统暂未允许后台回答，请回到 App 后重试。")
            }
            sendInterrupted(interrupted)
        }
    }

    fun serviceInterrupted(interruptedService: AnswerForegroundService) {
        val interrupted = jobs.values.filter { it.owner === interruptedService }
        for (job in interrupted) {
            jobs.remove(job.id)
            failBegins(job, "BACKGROUND_INTERRUPTED", "后台回答已中断，可以回到 App 重试。")
        }
        if (service === interruptedService) service = null
        // No completion notification: Dart must first persist the retryable state.
        sendInterrupted(interrupted.map { it.id })
    }

    fun serviceDestroyed(destroyedService: AnswerForegroundService) {
        serviceInterrupted(destroyedService)
    }

    private fun sendInterrupted(jobIds: List<String>) {
        if (jobIds.isNotEmpty()) channel?.invokeMethod("jobsInterrupted", mapOf("jobIds" to jobIds))
    }

    private fun completeBegins(job: Job) {
        val results = job.waitingBegins.toList()
        job.waitingBegins.clear()
        for (result in results) result.success(startResult())
    }

    private fun failBegins(job: Job, code: String, message: String) {
        val results = job.waitingBegins.toList()
        job.waitingBegins.clear()
        for (result in results) result.error(code, message, null)
    }

    private fun startResult() = mapOf("notificationsEnabled" to notificationsEnabled())

    fun activityCreated(host: MainActivity) {
        activity = WeakReference(host)
    }

    fun activityStarted(host: MainActivity) {
        activity = WeakReference(host)
        started = true
    }

    fun activityResumed(host: MainActivity) {
        activity = WeakReference(host)
        started = true
        resumed = true
    }

    fun activityPaused(host: MainActivity) {
        if (activity.get() === host) resumed = false
    }

    fun activityStopped(host: MainActivity) {
        if (activity.get() === host) {
            started = false
            resumed = false
        }
    }

    fun activityDestroyed(host: MainActivity) {
        if (permissionInFlight && permissionOwner.get() === host) completePermission(false)
        if (activity.get() !== host) return
        started = false
        resumed = false
        activity.clear()
    }

    fun requestNotificationPermission(result: MethodChannel.Result, channelId: String = FINISHED_CHANNEL) {
        val host = activity.get()
        if (host == null || host.isFinishing || host.isDestroyed) {
            result.success(false)
            return
        }
        ensureChannels()
        if (notificationsEnabled(channelId)) {
            result.success(true)
            return
        }
        if (permissionInFlight) {
            permissionResults.add(PermissionRequest(result, channelId))
            return
        }
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU || !resumed ||
            app.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED ||
            preferences.getBoolean(PERMISSION_REQUESTED, false)
        ) {
            result.success(false)
            return
        }
        permissionInFlight = true
        permissionOwner = WeakReference(host)
        permissionResults.add(PermissionRequest(result, channelId))
        try {
            host.requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), PERMISSION_REQUEST_CODE)
            // Once actually requested, denial/dismissal does not prompt on every question.
            preferences.edit().putBoolean(PERMISSION_REQUESTED, true).apply()
        } catch (_: Exception) {
            completePermission(false)
        }
    }

    fun permissionResult(host: MainActivity, requestCode: Int) {
        if (requestCode == PERMISSION_REQUEST_CODE && permissionOwner.get() === host && permissionInFlight) {
            completePermission(true)
        }
    }

    private fun completePermission(enabled: Boolean) {
        permissionInFlight = false
        permissionOwner.clear()
        val results = permissionResults.toList()
        permissionResults.clear()
        for (request in results) request.result.success(enabled && notificationsEnabled(request.channelId))
    }

    fun receiveLaunchIntent(intent: Intent?) {
        if (intent?.action != OPEN_CONVERSATION) return
        val id = intent.getStringExtra(EXTRA_CONVERSATION)?.takeIf { it.isNotBlank() && it.length <= 256 } ?: return
        // Consume the Intent extra too, so Activity recreation does not enqueue it again.
        intent.removeExtra(EXTRA_CONVERSATION)
        launchConversations.addLast(id)
        channel?.invokeMethod("openConversation", mapOf("conversationId" to id))
        // The queue remains until Dart calls takeLaunchConversation, even if no handler exists yet.
    }

    fun notificationsEnabled(channelId: String = FINISHED_CHANNEL): Boolean {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            app.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) return false
        if (!notifications.areNotificationsEnabled()) return false
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O &&
            notifications.getNotificationChannel(channelId)?.importance == NotificationManager.IMPORTANCE_NONE
        ) return false
        return true
    }

    private fun ensureChannels() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        notifications.createNotificationChannels(listOf(
            NotificationChannel(RUNNING_CHANNEL, app.getString(R.string.answer_running_channel), NotificationManager.IMPORTANCE_LOW).apply {
                setShowBadge(false)
                lockscreenVisibility = Notification.VISIBILITY_PRIVATE
            },
            NotificationChannel(FINISHED_CHANNEL, app.getString(R.string.answer_finished_channel), NotificationManager.IMPORTANCE_DEFAULT).apply {
                lockscreenVisibility = Notification.VISIBILITY_PRIVATE
            },
        ))
    }

    private fun builder(channelId: String): Notification.Builder {
        ensureChannels()
        @Suppress("DEPRECATION")
        val builder = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) Notification.Builder(app, channelId) else Notification.Builder(app)
        return builder
            .setSmallIcon(R.drawable.ic_answer_notification)
            .setContentTitle(app.getString(R.string.answer_notification_title))
            .setColor(0xFF243D35.toInt())
            .setVisibility(Notification.VISIBILITY_PRIVATE)
    }

    @Suppress("DEPRECATION") // API 24/25 use priority; API 26+ use the low-importance channel.
    fun runningNotification(conversationId: String?): Notification = builder(RUNNING_CHANNEL)
        .setContentText(app.getString(R.string.answer_running_notification))
        .setCategory(Notification.CATEGORY_SERVICE)
        .setPriority(Notification.PRIORITY_LOW)
        .setOngoing(true)
        .setOnlyAlertOnce(true)
        .setContentIntent(openIntent(conversationId, "running"))
        .build()

    private fun finishedNotification(job: Job, outcome: String): Notification = builder(FINISHED_CHANNEL)
        .setContentText(app.getString(if (outcome == "success") R.string.answer_success_notification else R.string.answer_error_notification))
        .setCategory(Notification.CATEGORY_STATUS)
        .setAutoCancel(true)
        .setContentIntent(openIntent(job.conversationId, job.id))
        .build()

    private fun openIntent(conversationId: String?, identity: String): PendingIntent {
        val intent = Intent(app, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP
            if (conversationId != null) {
                action = OPEN_CONVERSATION
                data = Uri.Builder().scheme("sevenu").authority("answer").appendPath(conversationId).appendPath(identity).build()
                putExtra(EXTRA_CONVERSATION, conversationId)
            }
        }
        return PendingIntent.getActivity(app, 0, intent, PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
    }

    companion object {
        const val CHANNEL = "seven_u/background_answers"
        const val RUNNING_NOTIFICATION_ID = 4101
        private const val FINISHED_NOTIFICATION_ID = 4102
        private const val RUNNING_CHANNEL = "seven_u_answers_running"
        private const val FINISHED_CHANNEL = "seven_u_answers_finished"
        private const val OPEN_CONVERSATION = "com.sevenu.app.OPEN_CONVERSATION"
        private const val EXTRA_CONVERSATION = "seven_u.conversation_id"
        private const val PERMISSION_REQUEST_CODE = 7401
        private const val PERMISSION_REQUESTED = "notification_permission_requested"
    }
}
