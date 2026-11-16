package com.sevenu.app

import android.app.Application
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.embedding.engine.FlutterEngineCache
import io.flutter.embedding.engine.dart.DartExecutor
import io.flutter.plugin.common.MethodChannel

/** One process-owned engine. A Service never creates an engine or replays a question. */
class SevenUApplication : Application() {
    lateinit var backgroundAnswers: BackgroundAnswerBridge
        private set
    lateinit var dailyMemories: DailyMemoryScheduler
        private set
    private var retainedEngine: FlutterEngine? = null

    override fun onCreate() {
        super.onCreate()
        backgroundAnswers = BackgroundAnswerBridge(this)
        dailyMemories = DailyMemoryScheduler(this)
        // This is also reached by a receiver-only process; never create Flutter here.
        runCatching { dailyMemories.reschedule() }
    }

    fun obtainFlutterEngine(): FlutterEngine {
        retainedEngine?.let { return it }
        val engine = FlutterEngine(this)
        retainedEngine = engine
        FlutterEngineCache.getInstance().put(ENGINE_ID, engine)
        val messenger = engine.dartExecutor.binaryMessenger
        MethodChannel(messenger, "seven_u/app_info").setMethodCallHandler { call, result ->
            if (call.method == "versionName") {
                @Suppress("DEPRECATION")
                val info = packageManager.getPackageInfo(packageName, 0)
                result.success(info.versionName)
            } else {
                result.notImplemented()
            }
        }
        backgroundAnswers.attachMessenger(messenger)
        dailyMemories.attachMessenger(messenger)
        engine.dartExecutor.executeDartEntrypoint(DartExecutor.DartEntrypoint.createDefault())
        return engine
    }

    companion object {
        const val ENGINE_ID = "seven_u_application_engine"
    }
}
