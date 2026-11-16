package com.sevenu.app

import android.content.Context
import android.content.Intent
import android.os.Bundle
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine

class MainActivity : FlutterActivity() {
    private val app: SevenUApplication
        get() = application as SevenUApplication

    override fun provideFlutterEngine(context: Context): FlutterEngine = app.obtainFlutterEngine()

    override fun shouldDestroyEngineWithHost(): Boolean = false

    // Notification URI identity is not a Flutter navigation route.
    override fun shouldHandleDeeplinking(): Boolean = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        app.backgroundAnswers.activityCreated(this)
        app.backgroundAnswers.receiveLaunchIntent(intent)
        app.dailyMemories.receiveLaunchIntent(intent)
    }

    override fun onStart() {
        super.onStart()
        app.backgroundAnswers.activityStarted(this)
    }

    override fun onResume() {
        super.onResume()
        app.backgroundAnswers.activityResumed(this)
    }

    override fun onPause() {
        app.backgroundAnswers.activityPaused(this)
        super.onPause()
    }

    override fun onStop() {
        app.backgroundAnswers.activityStopped(this)
        super.onStop()
    }

    override fun onDestroy() {
        app.backgroundAnswers.activityDestroyed(this)
        super.onDestroy()
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        app.backgroundAnswers.receiveLaunchIntent(intent)
        app.dailyMemories.receiveLaunchIntent(intent)
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray,
    ) {
        app.backgroundAnswers.permissionResult(this, requestCode)
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
    }
}
