package com.sevenu.app

import android.app.AlarmManager
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.provider.Settings
import io.flutter.plugin.common.BinaryMessenger
import io.flutter.plugin.common.MethodCall
import io.flutter.plugin.common.MethodChannel
import org.json.JSONObject
import java.text.SimpleDateFormat
import java.util.ArrayDeque
import java.util.Calendar
import java.util.Locale
import java.util.TimeZone
import kotlin.random.Random

/** Daily reminders are entirely bundled and native; content is never accepted from an Intent. */
class DailyMemoryScheduler(private val app: SevenUApplication) : MethodChannel.MethodCallHandler {
    private data class Memory(
        val id: String,
        val kind: String,
        val text: String,
        val date: String,
        val speaker: String,
        val quote: String,
        val msgId: String,
    ) {
        fun toMap(): Map<String, String> = mapOf(
            "id" to id, "kind" to kind, "text" to text, "date" to date,
            "speaker" to speaker, "quote" to quote, "msgId" to msgId,
        )
    }

    private val preferences = app.getSharedPreferences("seven_u_daily_memories", Context.MODE_PRIVATE)
    private val alarms = app.getSystemService(Context.ALARM_SERVICE) as AlarmManager
    private val notifications = app.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
    private val beijing = TimeZone.getTimeZone("Asia/Shanghai")
    private val launchMemories = ArrayDeque<String>()
    private var channel: MethodChannel? = null
    private var cachedMemories: List<Memory>? = null

    fun attachMessenger(messenger: BinaryMessenger) {
        channel = MethodChannel(messenger, CHANNEL).also { it.setMethodCallHandler(this) }
    }

    override fun onMethodCall(call: MethodCall, result: MethodChannel.Result) {
        try {
            when (call.method) {
                "getSettings" -> result.success(settings())
                "configure" -> {
                    configure(call.arguments as? Map<*, *> ?: throw IllegalArgumentException("设置格式无效。"))
                    result.success(settings())
                }
                "requestPermission" -> {
                    ensureChannel()
                    app.backgroundAnswers.requestNotificationPermission(result, NOTIFICATION_CHANNEL)
                }
                "openSystemSettings" -> {
                    openSystemSettings()
                    result.success(null)
                }
                "takeLaunchMemory" -> result.success(takeLaunchMemory())
                else -> result.notImplemented()
            }
        } catch (error: IllegalArgumentException) {
            result.error("INVALID_ARGUMENT", error.message, null)
        } catch (_: Exception) {
            result.error("DAILY_MEMORY_UNAVAILABLE", "每日一句暂时不可用，请重试。", null)
        }
    }

    private fun settings(): Map<String, Any> = mapOf(
        "enabled" to preferences.getBoolean("enabled", true),
        "hour" to preferences.getInt("hour", 12),
        "minute" to preferences.getInt("minute", 0),
        "privateOnLockScreen" to preferences.getBoolean("privateOnLockScreen", false),
        "notificationsEnabled" to app.backgroundAnswers.notificationsEnabled(NOTIFICATION_CHANNEL),
    )

    private fun configure(values: Map<*, *>) {
        val edit = preferences.edit()
        var scheduleChanged = false
        for (key in listOf("enabled", "privateOnLockScreen")) {
            if (values.containsKey(key)) {
                val value = values[key]
                require(value is Boolean) { "$key 必须是布尔值。" }
                if (key == "enabled" && value != preferences.getBoolean(key, true)) scheduleChanged = true
                edit.putBoolean(key, value)
            }
        }
        for ((key, maximum) in listOf("hour" to 23, "minute" to 59)) {
            if (values.containsKey(key)) {
                val value = values[key]
                require(value is Int && value in 0..maximum) { "$key 超出范围。" }
                if (value != preferences.getInt(key, if (key == "hour") 12 else 0)) scheduleChanged = true
                edit.putInt(key, value)
            }
        }
        check(edit.commit()) { "无法保存通知设置。" }
        reschedule(force = scheduleChanged)
        if (!preferences.getBoolean("enabled", true)) {
            notifications.cancel(NOTIFICATION_TAG, NOTIFICATION_ID)
        } else if (values.containsKey("privateOnLockScreen") &&
            notifications.activeNotifications.any { it.id == NOTIFICATION_ID && it.tag == NOTIFICATION_TAG }
        ) {
            val id = preferences.getString("selected_item_id", null)
            memories().firstOrNull { it.id == id }?.let {
                notifications.notify(NOTIFICATION_TAG, NOTIFICATION_ID, notification(it, onlyAlertOnce = true))
            }
        }
    }

    /** Preserve an already queued inexact alarm when simply reopening the app. */
    fun reschedule(force: Boolean = false) {
        val existing = alarmIntent(create = false)
        if (!preferences.getBoolean("enabled", true)) {
            existing?.let { alarms.cancel(it) }
            preferences.edit().remove("scheduled_at").apply()
            return
        }
        val now = System.currentTimeMillis()
        val today = dayKey(now)
        val previous = preferences.getLong("scheduled_at", 0)
        if (!force && existing != null && previous > 0 &&
            dayKey(previous) >= today && preferences.getString("delivered_day", null) != dayKey(previous)
        ) return
        val next = deliveryCalendar(now)
        if (next.timeInMillis <= now || preferences.getString("delivered_day", null) == today) {
            next.add(Calendar.DAY_OF_YEAR, 1)
        }
        alarms.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, next.timeInMillis, alarmIntent(create = true)!!)
        preferences.edit().putLong("scheduled_at", next.timeInMillis).apply()
    }

    private fun deliveryCalendar(now: Long): Calendar = Calendar.getInstance(beijing).apply {
        timeInMillis = now
        set(Calendar.HOUR_OF_DAY, preferences.getInt("hour", 12))
        set(Calendar.MINUTE, preferences.getInt("minute", 0))
        set(Calendar.SECOND, 0)
        set(Calendar.MILLISECOND, 0)
    }

    private fun dayKey(time: Long): String = SimpleDateFormat("yyyy-MM-dd", Locale.ROOT).apply {
        timeZone = beijing
    }.format(time)

    private fun alarmIntent(create: Boolean): PendingIntent? = PendingIntent.getBroadcast(
        app, ALARM_REQUEST_CODE,
        Intent(app, DailyMemoryReceiver::class.java).setAction(ACTION_DELIVER),
        (if (create) PendingIntent.FLAG_UPDATE_CURRENT else PendingIntent.FLAG_NO_CREATE) or PendingIntent.FLAG_IMMUTABLE,
    )

    /** At most once per Beijing day, including time edits, restart and app replacement. */
    fun deliver() {
        if (!preferences.getBoolean("enabled", true)) return
        ensureChannel()
        if (!app.backgroundAnswers.notificationsEnabled(NOTIFICATION_CHANNEL)) return
        val now = System.currentTimeMillis()
        val today = dayKey(now)
        if (preferences.getString("delivered_day", null) == today || now < deliveryCalendar(now).timeInMillis) return
        val all = memories()
        if (all.isEmpty()) return
        val used = preferences.getStringSet("used_item_ids", emptySet())!!.intersect(all.map { it.id }.toSet()).toMutableSet()
        val selected = if (preferences.getString("selected_day", null) == today) {
            all.firstOrNull { it.id == preferences.getString("selected_item_id", null) }
        } else null
        val memory = selected ?: run {
            // Random direction first, then unseen content in that direction.
            val direction = all.map { it.kind }.distinct().random(Random.Default)
            val inDirection = all.filter { it.kind == direction }
            var unseen = inDirection.filter { it.id !in used }
            if (unseen.isEmpty()) {
                used.removeAll(inDirection.map { it.id }.toSet())
                unseen = inDirection
            }
            unseen.random(Random.Default)
        }
        used.add(memory.id)
        // Commit the once-per-day guard before posting. A process kill cannot
        // result in a duplicate; a kill in this tiny gap can skip, never replay.
        if (!preferences.edit()
                .putString("delivered_day", today)
                .putString("selected_day", today)
                .putString("selected_item_id", memory.id)
                .putStringSet("used_item_ids", used)
                .commit()
        ) return
        try {
            notifications.notify(NOTIFICATION_TAG, NOTIFICATION_ID, notification(memory))
        } catch (_: SecurityException) {
            // Permission can be revoked between the check and posting; no backfill.
        }
    }

    private fun memories(): List<Memory> {
        cachedMemories?.let { return it }
        val root = app.assets.open(ASSET_PATH).bufferedReader(Charsets.UTF_8).use { JSONObject(it.readText()) }
        require(root.optInt("version") == 1) { "每日一句版本无效。" }
        val items = root.getJSONArray("items")
        val result = mutableListOf<Memory>()
        val ids = mutableSetOf<String>()
        for (index in 0 until items.length()) {
            val item = items.optJSONObject(index) ?: continue
            fun field(name: String): String = item.opt(name) as? String ?: ""
            val memory = Memory(field("id"), field("kind"), field("text"), field("date"), field("speaker"), field("quote"), field("msgId"))
            if (memory.id.isBlank() || memory.id.length > 256 || memory.id in ids ||
                memory.kind !in setOf("question", "statement", "memory") ||
                memory.text.isBlank() || memory.text.length > 15 ||
                memory.quote.isBlank() || memory.quote.length > 100 ||
                memory.speaker !in setOf("self", "partner") ||
                !Regex("\\d{4}-\\d{2}-\\d{2}").matches(memory.date) ||
                !Regex("\\d{1,19}").matches(memory.msgId)
            ) continue
            ids.add(memory.id)
            result.add(memory)
        }
        return result.toList().also { cachedMemories = it }
    }

    fun receiveLaunchIntent(intent: Intent?) {
        if (intent?.action != ACTION_OPEN) return
        val id = intent.getStringExtra(EXTRA_ID)?.takeIf { it.isNotBlank() && it.length <= 256 } ?: return
        intent.removeExtra(EXTRA_ID)
        launchMemories.addLast(id)
        channel?.invokeMethod("openDailyMemory", mapOf("id" to id))
    }

    private fun takeLaunchMemory(): Map<String, String>? {
        if (launchMemories.isEmpty()) return null
        // Do not discard a queued click if loading the bundled asset fails.
        val available = memories()
        while (launchMemories.isNotEmpty()) {
            val id = launchMemories.removeFirst()
            available.firstOrNull { it.id == id }?.let { return it.toMap() }
        }
        return null
    }

    private fun ensureChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            notifications.createNotificationChannel(NotificationChannel(
                NOTIFICATION_CHANNEL, app.getString(R.string.daily_memory_channel), NotificationManager.IMPORTANCE_DEFAULT,
            ))
        }
    }

    private fun notification(memory: Memory, onlyAlertOnce: Boolean = false): Notification {
        ensureChannel()
        @Suppress("DEPRECATION")
        fun base(): Notification.Builder = (if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            Notification.Builder(app, NOTIFICATION_CHANNEL)
        } else Notification.Builder(app))
            .setSmallIcon(R.drawable.ic_answer_notification)
            .setColor(0xFF243D35.toInt())
            .setContentTitle(app.getString(R.string.daily_memory_title))
            .setCategory(Notification.CATEGORY_REMINDER)
        val private = preferences.getBoolean("privateOnLockScreen", false)
        val intent = Intent(app, MainActivity::class.java).apply {
            action = ACTION_OPEN
            flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP
            data = Uri.Builder().scheme("sevenu").authority("daily").appendPath(memory.id).build()
            putExtra(EXTRA_ID, memory.id)
        }
        return base()
            .setContentText(memory.text)
            .setStyle(Notification.BigTextStyle().bigText(memory.text))
            .setAutoCancel(true)
            .setOnlyAlertOnce(onlyAlertOnce)
            .setVisibility(if (private) Notification.VISIBILITY_PRIVATE else Notification.VISIBILITY_PUBLIC)
            .setPublicVersion(base().setContentText(app.getString(R.string.daily_memory_private_preview)).build())
            .setContentIntent(PendingIntent.getActivity(app, 0, intent, PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE))
            .build()
    }

    private fun openSystemSettings() {
        val intent = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS).putExtra(Settings.EXTRA_APP_PACKAGE, app.packageName)
        } else {
            Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:${app.packageName}"))
        }
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        app.startActivity(intent)
    }

    companion object {
        const val CHANNEL = "seven_u/daily_memories"
        const val ACTION_DELIVER = "com.sevenu.app.DELIVER_DAILY_MEMORY"
        private const val ACTION_OPEN = "com.sevenu.app.OPEN_DAILY_MEMORY"
        private const val EXTRA_ID = "seven_u.daily_memory_id"
        private const val ASSET_PATH = "flutter_assets/assets/memory/daily_memories.json"
        private const val NOTIFICATION_CHANNEL = "daily_memories"
        private const val NOTIFICATION_ID = 5101
        private const val NOTIFICATION_TAG = "daily_memory"
        private const val ALARM_REQUEST_CODE = 7501
    }
}
