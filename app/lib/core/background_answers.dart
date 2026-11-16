import 'dart:async';
import 'dart:io';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';

enum BackgroundAnswerOutcome { success, error, silent }

class BackgroundStartResult {
  final bool notificationsEnabled;

  const BackgroundStartResult({required this.notificationsEnabled});
}

sealed class BackgroundAnswerEvent {
  const BackgroundAnswerEvent();
}

class OpenAnswerConversation extends BackgroundAnswerEvent {
  final String conversationId;

  const OpenAnswerConversation(this.conversationId);
}

class InterruptedAnswerJobs extends BackgroundAnswerEvent {
  final List<String> jobIds;

  const InterruptedAnswerJobs(this.jobIds);
}

/// Keeps one user-initiated answer alive; it never runs or replays the agent.
abstract class BackgroundAnswers {
  factory BackgroundAnswers.platform() => !kIsWeb && Platform.isAndroid
      ? AndroidBackgroundAnswers()
      : NoopBackgroundAnswers();

  Future<BackgroundStartResult> begin({
    required String jobId,
    required String conversationId,
  });
  Future<void> finish({
    required String jobId,
    required String conversationId,
    required BackgroundAnswerOutcome outcome,
  });
  Future<bool> requestNotificationPermission();
  Future<String?> takeLaunchConversation();
  Stream<BackgroundAnswerEvent> get events;
  void dispose();
}

class AndroidBackgroundAnswers implements BackgroundAnswers {
  static const channelName = 'seven_u/background_answers';
  final MethodChannel _channel;
  final _events = StreamController<BackgroundAnswerEvent>.broadcast();
  bool _disposed = false;

  AndroidBackgroundAnswers({MethodChannel? channel})
    : _channel = channel ?? const MethodChannel(channelName) {
    _channel.setMethodCallHandler(_onNativeEvent);
  }

  @override
  Stream<BackgroundAnswerEvent> get events => _events.stream;

  Future<void> _onNativeEvent(MethodCall call) async {
    if (_disposed) return;
    final args = call.arguments;
    if (args is! Map) return;
    switch (call.method) {
      case 'openConversation':
        final id = args['conversationId'];
        if (id is String && id.isNotEmpty) {
          _events.add(OpenAnswerConversation(id));
        }
      case 'jobsInterrupted':
        final ids = args['jobIds'];
        if (ids is List) {
          _events.add(
            InterruptedAnswerJobs(
              List.unmodifiable(
                ids.whereType<String>().where((id) => id.isNotEmpty),
              ),
            ),
          );
        }
    }
  }

  @override
  Future<BackgroundStartResult> begin({
    required String jobId,
    required String conversationId,
  }) async {
    // Missing native registration is an error on Android, not silent protection.
    final result = await _channel.invokeMapMethod<String, dynamic>('begin', {
      'jobId': jobId,
      'conversationId': conversationId,
    });
    return BackgroundStartResult(
      notificationsEnabled: result?['notificationsEnabled'] == true,
    );
  }

  @override
  Future<void> finish({
    required String jobId,
    required String conversationId,
    required BackgroundAnswerOutcome outcome,
  }) => _channel.invokeMethod<void>('finish', {
    'jobId': jobId,
    'conversationId': conversationId,
    'outcome': outcome.name,
  });

  @override
  Future<bool> requestNotificationPermission() async =>
      await _channel.invokeMethod<bool>('requestNotificationPermission') ??
      false;

  @override
  Future<String?> takeLaunchConversation() =>
      _channel.invokeMethod<String>('takeLaunchConversation');

  @override
  void dispose() {
    if (_disposed) return;
    _disposed = true;
    _channel.setMethodCallHandler(null);
    unawaited(_events.close());
  }
}

/// Desktop tests and currently unsupported platforms have no Android service.
class NoopBackgroundAnswers implements BackgroundAnswers {
  @override
  Stream<BackgroundAnswerEvent> get events => const Stream.empty();

  @override
  Future<BackgroundStartResult> begin({
    required String jobId,
    required String conversationId,
  }) async => const BackgroundStartResult(notificationsEnabled: true);

  @override
  Future<void> finish({
    required String jobId,
    required String conversationId,
    required BackgroundAnswerOutcome outcome,
  }) async {}

  @override
  Future<bool> requestNotificationPermission() async => true;

  @override
  Future<String?> takeLaunchConversation() async => null;

  @override
  void dispose() {}
}
