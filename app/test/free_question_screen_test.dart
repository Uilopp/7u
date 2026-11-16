import 'dart:async';

import 'package:couple_app/core/api.dart';
import 'package:couple_app/core/conversations.dart';
import 'package:couple_app/core/local_store.dart';
import 'package:couple_app/core/question_history.dart';
import 'package:couple_app/core/question_cancellation.dart';
import 'package:couple_app/main.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';

class TestLocalStore extends LocalStore {
  final Map<String, Map<String, dynamic>> values = {};
  @override
  Future<Map<String, dynamic>?> loadMetadata(String key) async => values[key];
  @override
  Future<void> saveMetadata(String key, Map<String, dynamic> value) async {
    values[key] = value;
  }
}

class TestApi extends Api {
  final requests = <String>[];
  final histories = <List<SavedQuestion>>[];
  final replies = <Completer<Map<String, dynamic>>>[];

  TestApi() : super(local: TestLocalStore());

  @override
  Future<Map<String, dynamic>> askQuestion(
    String question, {
    List<SavedQuestion> history = const [],
    QuestionCancellation? cancellation,
  }) {
    requests.add(question);
    histories.add(List.of(history));
    final reply = Completer<Map<String, dynamic>>();
    replies.add(reply);
    return reply.future;
  }

  void answer(int index, String text, {bool evidence = false}) =>
      replies[index].complete({
        'status': 'done',
        'answer': {
          'title': '后台标题不展示',
          'body': text,
          'quotes': evidence
              ? [
                  {
                    'msg_id': '123',
                    'speaker': 'partner',
                    'quote': '测试语音原话',
                    'date': '2026-09-01T10:20:00+08:00',
                    'source_type': 'voice_transcript',
                  },
                ]
              : [],
        },
      });
}

Future<void> send(WidgetTester tester, String text) async {
  await tester.enterText(find.byKey(const ValueKey('question-input')), text);
  await tester.pump();
  await tester.tap(find.byKey(const ValueKey('ask-seven-u')));
  await tester.pump();
  await tester.pump(const Duration(milliseconds: 20));
}

Future<void> launch(WidgetTester tester, TestApi api) async {
  await tester.pumpWidget(SevenUApp(api: api, showIntro: false));
  await tester.pumpAndSettle();
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  const versionChannel = MethodChannel('seven_u/app_info');
  final messenger =
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger;
  setUp(
    () => messenger.setMockMethodCallHandler(
      versionChannel,
      (_) async => '1.0.260927',
    ),
  );
  tearDown(() => messenger.setMockMethodCallHandler(versionChannel, null));

  testWidgets(
    'followups carry completed turns and show per-message voice evidence',
    (tester) async {
      final api = TestApi();
      await launch(tester, api);
      await send(tester, '给他推荐礼物');
      api.answer(0, '可以一起选一本书。', evidence: true);
      await tester.pumpAndSettle();
      expect(find.text('可以一起选一本书。'), findsOneWidget);
      expect(find.text('后台标题不展示'), findsNothing);
      await tester.tap(find.byKey(const ValueKey('show-question-evidence')));
      await tester.pumpAndSettle();
      expect(find.text('2026-09-01 10:20 · TA · 语音'), findsOneWidget);
      expect(find.text('消息 ID 123'), findsOneWidget);
      Navigator.of(tester.element(find.text('为什么这样说').last)).pop();
      await tester.pumpAndSettle();
      await send(tester, '第二个呢？');
      expect(api.histories.last.single.question, '给他推荐礼物');
      expect(api.histories.last.single.evidence.single.msgId, '123');
      api.answer(1, '继续讨论礼物。');
      await tester.pumpAndSettle();
      expect(find.text('继续讨论礼物。'), findsOneWidget);
      expect(find.text('可以一起选一本书。'), findsOneWidget);
    },
  );

  testWidgets(
    'new conversation isolates context, drafts and history survive relaunch',
    (tester) async {
      final api = TestApi();
      await launch(tester, api);
      await send(tester, '第一段对话');
      api.answer(0, '第一段回答');
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const ValueKey('new-conversation')));
      await tester.pumpAndSettle();
      await send(tester, '第二段对话');
      expect(api.histories.last, isEmpty);
      api.answer(1, '第二段回答');
      await tester.pumpAndSettle();
      await tester.enterText(
        find.byKey(const ValueKey('question-input')),
        '留到明天的问题',
      );
      await tester.pump(const Duration(milliseconds: 500));
      await tester.pumpWidget(const SizedBox());
      await tester.pump();
      await launch(tester, api);
      expect(find.text('第二段回答'), findsOneWidget);
      expect(find.text('留到明天的问题'), findsOneWidget);
      await tester.tap(find.byKey(const ValueKey('question-history')));
      await tester.pumpAndSettle();
      await tester.tap(find.text('第一段对话'));
      await tester.pumpAndSettle();
      expect(find.text('第一段回答'), findsOneWidget);
      expect(find.text('第二段回答'), findsNothing);
    },
  );

  testWidgets(
    'retry preserves one pending user message and excludes failed attempts from history',
    (tester) async {
      final api = TestApi();
      await launch(tester, api);
      await send(tester, '这次的问题');
      api.replies[0].completeError(ApiException('网络暂时不可用'));
      await tester.pumpAndSettle();
      expect(find.text('这次的问题'), findsOneWidget);
      await tester.tap(find.byKey(const ValueKey('retry-message')));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 20));
      expect(api.histories.last, isEmpty);
      api.answer(1, '现在回答完成');
      await tester.pumpAndSettle();
      expect(find.text('这次的问题'), findsOneWidget);
      expect(find.text('现在回答完成'), findsOneWidget);
    },
  );

  testWidgets(
    'a delayed answer returns to its original conversation and deletion stays deleted',
    (tester) async {
      final api = TestApi();
      await launch(tester, api);
      await send(tester, '等待中的对话');
      await tester.tap(find.byKey(const ValueKey('new-conversation')));
      await tester.pumpAndSettle();
      expect(find.text('等待中的对话'), findsNothing);
      await tester.tap(find.byKey(const ValueKey('question-history')));
      await tester.pumpAndSettle();
      await tester.tap(find.byTooltip('删除对话'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('删除'));
      await tester.pumpAndSettle();
      api.answer(0, '不应重新出现');
      await tester.pumpAndSettle();
      expect(find.text('不应重新出现'), findsNothing);
      final state = await ConversationRepository(api.local).load();
      expect(state.conversations.any((c) => c.title == '等待中的对话'), isFalse);
    },
  );

  testWidgets('upgrade keeps old answers and their original version labels', (
    tester,
  ) async {
    const channel = MethodChannel('seven_u/app_info');
    final messenger =
        TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger;
    var installed = '1.0.260927';
    messenger.setMockMethodCallHandler(channel, (_) async => installed);
    addTearDown(() => messenger.setMockMethodCallHandler(channel, null));
    final api = TestApi();
    await launch(tester, api);
    await send(tester, '升级前的问题');
    api.answer(0, '升级前的回答不会被改写。');
    await tester.pumpAndSettle();
    expect(find.text('7u · v1.0.260927'), findsOneWidget);
    await tester.pumpWidget(const SizedBox.shrink());
    await tester.pumpAndSettle();
    installed = '1.0.261001';
    await launch(tester, api);
    expect(find.text('v1.0.261001'), findsOneWidget);
    expect(find.text('7u · v1.0.260927'), findsOneWidget);
    expect(find.text('升级前的回答不会被改写。'), findsOneWidget);
    await send(tester, '升级后继续问');
    api.answer(1, '新版生成的回答。');
    await tester.pumpAndSettle();
    expect(find.text('7u · v1.0.261001'), findsOneWidget);
    final state = await ConversationRepository(api.local).load();
    expect(state.conversations.single.turns.map((turn) => turn.appVersion), [
      '1.0.260927',
      '1.0.261001',
    ]);
  });

  testWidgets('compact screen and keyboard keep composer usable', (
    tester,
  ) async {
    tester.view.physicalSize = const Size(360, 640);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    addTearDown(tester.view.resetViewInsets);
    final api = TestApi();
    await launch(tester, api);
    tester.view.viewInsets = const FakeViewPadding(bottom: 300);
    await tester.pumpAndSettle();
    await tester.enterText(
      find.byKey(const ValueKey('question-input')),
      '键盘打开时继续输入',
    );
    await tester.pump();
    expect(tester.takeException(), isNull);
    expect(
      tester.getBottomLeft(find.byKey(const ValueKey('ask-seven-u'))).dy,
      lessThan(340),
    );
  });
}
