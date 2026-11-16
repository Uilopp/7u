import 'dart:async';
import 'dart:io';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';

class DailyMemory {
  final String id;
  final String text;
  final String date;
  final String speaker;
  final String quote;
  final String msgId;

  const DailyMemory({
    required this.id,
    required this.text,
    required this.date,
    required this.speaker,
    required this.quote,
    required this.msgId,
  });

  static DailyMemory? parse(Object? value) {
    if (value is! Map) return null;
    final fields = ['id', 'text', 'date', 'speaker', 'quote', 'msgId'];
    if (fields.any(
      (key) => value[key] is! String || (value[key] as String).isEmpty,
    )) {
      return null;
    }
    if (!['self', 'partner'].contains(value['speaker']) ||
        !RegExp(r'^\d{4}-\d{2}-\d{2}$').hasMatch(value['date']) ||
        !RegExp(r'^\d+$').hasMatch(value['msgId']) ||
        (value['text'] as String).length > 15 ||
        (value['quote'] as String).length > 100) {
      return null;
    }
    return DailyMemory(
      id: value['id'],
      text: value['text'],
      date: value['date'],
      speaker: value['speaker'],
      quote: value['quote'],
      msgId: value['msgId'],
    );
  }

  String get question => '聊聊 $date 的这段回忆（消息 ID：$msgId）：$text';
}

class DailyMemorySettings {
  final bool enabled;
  final int hour;
  final int minute;
  final bool privateOnLockScreen;
  final bool notificationsEnabled;

  const DailyMemorySettings({
    this.enabled = true,
    this.hour = 12,
    this.minute = 0,
    this.privateOnLockScreen = false,
    this.notificationsEnabled = false,
  });

  factory DailyMemorySettings.fromMap(Map<dynamic, dynamic>? value) {
    final hour = value?['hour'];
    final minute = value?['minute'];
    return DailyMemorySettings(
      enabled: value?['enabled'] != false,
      hour: hour is int && hour >= 0 && hour < 24 ? hour : 12,
      minute: minute is int && minute >= 0 && minute < 60 ? minute : 0,
      privateOnLockScreen: value?['privateOnLockScreen'] == true,
      notificationsEnabled: value?['notificationsEnabled'] == true,
    );
  }

  DailyMemorySettings copyWith({
    bool? enabled,
    int? hour,
    int? minute,
    bool? privateOnLockScreen,
  }) => DailyMemorySettings(
    enabled: enabled ?? this.enabled,
    hour: hour ?? this.hour,
    minute: minute ?? this.minute,
    privateOnLockScreen: privateOnLockScreen ?? this.privateOnLockScreen,
    notificationsEnabled: notificationsEnabled,
  );

  Map<String, Object> toConfiguration() => {
    'enabled': enabled,
    'hour': hour,
    'minute': minute,
    'privateOnLockScreen': privateOnLockScreen,
  };
}

/// Daily reminders are local Android alarms backed by a verified bundled pool.
class DailyMemories {
  static const channelName = 'seven_u/daily_memories';
  final MethodChannel _channel;
  final bool supported;
  final _events = StreamController<void>.broadcast();
  bool _disposed = false;

  DailyMemories({MethodChannel? channel, bool? supported})
    : _channel = channel ?? const MethodChannel(channelName),
      supported = supported ?? (!kIsWeb && Platform.isAndroid) {
    if (this.supported) {
      _channel.setMethodCallHandler((call) async {
        if (!_disposed && call.method == 'openDailyMemory') _events.add(null);
      });
    }
  }

  Stream<void> get events => _events.stream;

  Future<DailyMemory?> takeLaunchMemory() async => supported
      ? DailyMemory.parse(
          await _channel.invokeMethod<Object?>('takeLaunchMemory'),
        )
      : null;

  Future<DailyMemorySettings> getSettings() async => supported
      ? DailyMemorySettings.fromMap(
          await _channel.invokeMapMethod('getSettings'),
        )
      : const DailyMemorySettings();

  Future<DailyMemorySettings> configure(DailyMemorySettings settings) async {
    if (!supported) return settings;
    await _channel.invokeMethod<void>('configure', settings.toConfiguration());
    return getSettings();
  }

  Future<bool> requestPermission() async =>
      supported &&
      (await _channel.invokeMethod<bool>('requestPermission') ?? false);

  Future<void> openSystemSettings() async {
    if (supported) await _channel.invokeMethod<void>('openSystemSettings');
  }

  void dispose() {
    if (_disposed) return;
    _disposed = true;
    if (supported) _channel.setMethodCallHandler(null);
    unawaited(_events.close());
  }
}
