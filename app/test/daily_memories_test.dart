import 'dart:async';

import 'package:couple_app/core/conversation_controller.dart';
import 'package:couple_app/core/daily_memories.dart';
import 'package:couple_app/widgets/daily_memory_notifications.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';

import 'free_question_screen_test.dart' show TestApi;

const sample = <String, Object>{
  'id': 'memory-123',
  'text': '还想一起看日落吗？',
  'date': '2025-06-08',
  'speaker': 'partner',
  'quote': '我们一起看日落',
  'msgId': '123',
};

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  final messenger =
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger;
  const channel = MethodChannel(DailyMemories.channelName);
  late DailyMemories daily;
  late Map<String, dynamic> settings;
  late List<Object?> launches;
  late List<MethodCall> calls;

  setUp(() {
    calls = [];
    launches = [];
    settings = {
      'enabled': true,
      'hour': 12,
      'minute': 0,
      'privateOnLockScreen': false,
      'notificationsEnabled': false,
    };
    daily = DailyMemories(supported: true);
    messenger.setMockMethodCallHandler(channel, (call) async {
      calls.add(call);
      switch (call.method) {
        case 'getSettings':
          return settings;
        case 'configure':
          settings.addAll(Map<String, dynamic>.from(call.arguments));
          return null;
        case 'takeLaunchMemory':
          return launches.isEmpty ? null : launches.removeAt(0);
        case 'requestPermission':
          return false;
        default:
          return null;
      }
    });
  });

  tearDown(() {
    daily.dispose();
    messenger.setMockMethodCallHandler(channel, null);
  });

  test(
    'typed memory retains source and builds an unsent grounded question',
    () {
      final memory = DailyMemory.parse(sample)!;
      expect(memory.speaker, 'partner');
      expect(memory.quote, '我们一起看日落');
      expect(memory.question, contains('消息 ID：123'));
      expect(memory.question, contains('2025-06-08'));
      expect(memory.question.length, lessThan(500));
    },
  );

  test('malformed launch payload cannot become an ungrounded memory', () {
    for (final data in [
      null,
      {},
      {...sample, 'speaker': 'stranger'},
      {...sample, 'msgId': 'oops'},
      {...sample, 'text': '超长' * 100},
      {...sample, 'quote': ''},
      {...sample, 'date': 'tomorrow'},
    ]) {
      expect(DailyMemory.parse(data), isNull);
    }
  });

  test('notification text accepts fifteen characters but rejects sixteen', () {
    expect(DailyMemory.parse({...sample, 'text': '字' * 15}), isNotNull);
    expect(DailyMemory.parse({...sample, 'text': '字' * 16}), isNull);
    expect(DailyMemory.parse({...sample, 'text': '${'字' * 14}。'}), isNotNull);
    expect(DailyMemory.parse({...sample, 'text': '${'字' * 15}。'}), isNull);
  });

  test('settings default to noon and clamp malformed time fields', () {
    final value = DailyMemorySettings.fromMap({'hour': 99, 'minute': -1});
    expect(value.hour, 12);
    expect(value.minute, 0);
    expect(value.privateOnLockScreen, isFalse);
  });

  test('configuration stays local and permission denial is explicit', () async {
    final initial = await daily.getSettings();
    final saved = await daily.configure(
      initial.copyWith(hour: 13, minute: 25, privateOnLockScreen: true),
    );
    expect(saved.hour, 13);
    expect(saved.minute, 25);
    expect(saved.privateOnLockScreen, isTrue);
    expect(await daily.requestPermission(), isFalse);
    expect(calls.where((c) => c.method == 'configure').single.arguments, {
      'enabled': true,
      'hour': 13,
      'minute': 25,
      'privateOnLockScreen': true,
    });
  });

  Future<ConversationController> launch(
    WidgetTester tester,
    TestApi api,
  ) async {
    final controller = ConversationController(
      api: api,
      version: Future.value('0.1.260927'),
    );
    await controller.initialize();
    await tester.pumpWidget(
      MaterialApp(
        home: DailyMemoryNotifications(
          conversations: controller,
          daily: daily,
          child: const Scaffold(
            appBar: null,
            body: Center(child: DailyMemorySettingsButton()),
          ),
        ),
      ),
    );
    await tester.pumpAndSettle();
    return controller;
  }

  Future<void> emitTap() async {
    final complete = Completer<void>();
    messenger.handlePlatformMessage(
      DailyMemories.channelName,
      const StandardMethodCodec().encodeMethodCall(
        const MethodCall('openDailyMemory', {'id': 'memory-123'}),
      ),
      (_) => complete.complete(),
    );
    await complete.future;
  }

  testWidgets(
    'cold notification opens exact dated source without calling model',
    (tester) async {
      launches.add(sample);
      final api = TestApi();
      final controller = await launch(tester, api);
      expect(find.text('今天的小片段'), findsOneWidget);
      expect(find.text(sample['text'] as String), findsOneWidget);
      expect(find.text('2025-06-08 · TA的原话'), findsOneWidget);
      expect(api.requests, isEmpty);
      await tester.pumpWidget(const SizedBox());
      controller.dispose();
    },
  );

  testWidgets(
    'continue chatting preserves existing draft and never auto-sends',
    (tester) async {
      final api = TestApi();
      final controller = await launch(tester, api);
      final oldId = controller.activeId;
      controller.updateDraft('尚未说完的内容');
      launches.add(sample);
      await emitTap();
      await tester.pumpAndSettle();
      await tester.tap(find.text('接着聊聊'));
      await tester.pumpAndSettle();
      expect(controller.activeId, isNot(oldId));
      expect(
        controller.conversations.firstWhere((c) => c.id == oldId).draft,
        '尚未说完的内容',
      );
      expect(controller.active.draft, contains('消息 ID：123'));
      expect(api.requests, isEmpty);
      await tester.pumpWidget(const SizedBox());
      controller.dispose();
    },
  );

  testWidgets(
    'daily toggle and lockscreen preference persist independently of answers',
    (tester) async {
      final api = TestApi();
      final controller = await launch(tester, api);
      await tester.tap(find.byKey(const ValueKey('notification-settings')));
      await tester.pumpAndSettle();
      expect(find.text('12:00'), findsOneWidget);
      await tester.tap(find.text('每天的小片段'));
      await tester.pumpAndSettle();
      expect(settings['enabled'], isFalse);
      await tester.tap(find.text('锁屏显示内容'));
      await tester.pumpAndSettle();
      expect(settings['privateOnLockScreen'], isTrue);
      expect(api.requests, isEmpty);
      await tester.pumpWidget(const SizedBox());
      controller.dispose();
    },
  );

  testWidgets(
    'permission failure remains visible and does not disable daily preference',
    (tester) async {
      final api = TestApi();
      final controller = await launch(tester, api);
      await tester.tap(find.byKey(const ValueKey('notification-settings')));
      await tester.pumpAndSettle();
      await tester.tap(find.text('允许通知'));
      await tester.pumpAndSettle();
      expect(find.textContaining('尚未允许通知'), findsOneWidget);
      expect(settings['enabled'], isTrue);
      expect(api.requests, isEmpty);
      await tester.pumpWidget(const SizedBox());
      controller.dispose();
    },
  );
}
