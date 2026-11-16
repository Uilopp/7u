import 'dart:async';

import 'package:couple_app/core/api.dart';
import 'package:couple_app/core/conversations.dart';
import 'package:couple_app/core/question_history.dart';
import 'package:couple_app/core/question_cancellation.dart';
import 'package:couple_app/main.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';

import 'free_question_screen_test.dart' as fixtures;

class FaultStore extends fixtures.TestLocalStore {
  bool failReads = false;
  int failedWritesRemaining = 0;
  int writes = 0;
  Completer<void>? writeGate;

  @override
  Future<Map<String, dynamic>?> loadMetadata(String key) async {
    if (failReads) throw StateError('temporary read failure');
    return super.loadMetadata(key);
  }

  @override
  Future<void> saveMetadata(String key, Map<String, dynamic> value) async {
    writes++;
    if (writeGate != null) await writeGate!.future;
    if (failedWritesRemaining > 0) {
      failedWritesRemaining--;
      throw StateError('temporary write failure');
    }
    await super.saveMetadata(key, value);
  }
}

class FaultApi extends Api {
  final requests = <String>[];
  final histories = <List<SavedQuestion>>[];
  final replies = <Completer<Map<String, dynamic>>>[];

  FaultApi(FaultStore local) : super(local: local);

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

  void answer(int index, String body) => replies[index].complete({
    'status': 'done',
    'answer': {'body': body, 'quotes': <Object?>[]},
  });
}

Future<void> openApp(WidgetTester tester, Api api) async {
  await tester.pumpWidget(SevenUApp(api: api, showIntro: false));
  await tester.pumpAndSettle();
}

Future<void> deleteOnlyConversation(WidgetTester tester) async {
  await tester.tap(find.byKey(const ValueKey('question-history')));
  await tester.pumpAndSettle();
  await tester.tap(find.byTooltip('删除对话'));
  await tester.pumpAndSettle();
  await tester.tap(find.text('删除'));
  await tester.pumpAndSettle();
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  final messenger =
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger;
  const channel = MethodChannel('seven_u/app_info');
  setUp(
    () =>
        messenger.setMockMethodCallHandler(channel, (_) async => '1.0.260927'),
  );
  tearDown(() => messenger.setMockMethodCallHandler(channel, null));

  testWidgets(
    'rapid repeated send starts only one request and one saved turn',
    (tester) async {
      final api = fixtures.TestApi();
      await fixtures.launch(tester, api);
      await tester.enterText(
        find.byKey(const ValueKey('question-input')),
        '快速连点发送',
      );
      await tester.pump();
      for (var i = 0; i < 8; i++) {
        await tester.tap(find.byKey(const ValueKey('ask-seven-u')));
      }
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 20));
      expect(api.requests, ['快速连点发送']);
      api.answer(0, '只生成一次。');
      await tester.pumpAndSettle();
      final saved = await ConversationRepository(api.local).load();
      expect(saved.conversations.single.turns, hasLength(1));
    },
  );

  testWidgets('rapid repeated retry cannot duplicate a pending request', (
    tester,
  ) async {
    final api = fixtures.TestApi();
    await fixtures.launch(tester, api);
    await fixtures.send(tester, '失败后快速重试');
    api.replies[0].completeError(const ApiException('暂时断网'));
    await tester.pumpAndSettle();
    for (var i = 0; i < 8; i++) {
      await tester.tap(find.byKey(const ValueKey('retry-message')));
    }
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 20));
    expect(api.requests, ['失败后快速重试', '失败后快速重试']);
    expect(api.histories.last, isEmpty);
    api.answer(1, '这次完成了。');
    await tester.pumpAndSettle();
    expect(
      (await ConversationRepository(
        api.local,
      ).load()).conversations.single.turns,
      hasLength(1),
    );
  });

  testWidgets('read failure never replaces saved data and retry restores it', (
    tester,
  ) async {
    final local = FaultStore();
    final original = Conversation.create().copyWith(
      draft: '原来还没发出的草稿',
      turns: [
        SavedQuestion(
          question: '已经保存的问题',
          title: '',
          body: '已经保存的回答',
          evidence: const [],
          createdAt: DateTime(2026, 9, 1),
          appVersion: '1.0.260901',
        ),
      ],
    );
    final payload = {
      'active_id': original.id,
      'conversations': [original.toJson()],
    };
    local.values[ConversationRepository.key] = payload;
    local.failReads = true;
    final api = FaultApi(local);
    await openApp(tester, api);
    expect(find.text('暂时没有读到保存的对话，请重试。'), findsOneWidget);
    expect(local.writes, 0);
    expect(local.values[ConversationRepository.key], payload);
    expect(api.requests, isEmpty);

    local.failReads = false;
    await tester.tap(find.text('重试'));
    await tester.pumpAndSettle();
    expect(find.text('已经保存的回答'), findsOneWidget);
    expect(find.text('原来还没发出的草稿'), findsOneWidget);
    expect(find.text('7u · v1.0.260901'), findsOneWidget);
    expect(local.writes, 0);
  });

  testWidgets('deleting the last conversation creates a usable empty chat', (
    tester,
  ) async {
    final api = fixtures.TestApi();
    await fixtures.launch(tester, api);
    await fixtures.send(tester, '即将删除的问题');
    api.answer(0, '即将删除的回答');
    await tester.pumpAndSettle();
    await deleteOnlyConversation(tester);
    expect(find.text('聊过的话，会留在这里。'), findsOneWidget);
    Navigator.of(tester.element(find.text('我们的对话'))).pop();
    await tester.pumpAndSettle();
    await fixtures.send(tester, '删除以后继续问');
    expect(api.histories.last, isEmpty);
    api.answer(1, '新的回答正常保存。');
    await tester.pumpAndSettle();
    final saved = await ConversationRepository(api.local).load();
    expect(saved.conversations, hasLength(1));
    expect(saved.conversations.single.turns.single.question, '删除以后继续问');
    expect(find.text('即将删除的回答'), findsNothing);
  });

  testWidgets(
    'pending save failure prevents a paid request and explicit retry recovers',
    (tester) async {
      final local = FaultStore();
      final api = FaultApi(local);
      await openApp(tester, api);
      local.failedWritesRemaining = 1;
      await fixtures.send(tester, '一次写入失败之后');
      expect(find.text('这段对话暂时没有保存成功，请稍后重试。'), findsOneWidget);
      expect(api.requests, isEmpty);
      expect(find.byKey(const ValueKey('retry-message')), findsOneWidget);
      await tester.tap(find.byKey(const ValueKey('retry-message')));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 20));
      expect(api.requests, ['一次写入失败之后']);
      api.answer(0, '恢复后保存成功。');
      await tester.pumpAndSettle();
      final saved = await ConversationRepository(local).load();
      expect(saved.conversations.single.turns.single.body, '恢复后保存成功。');
      expect(saved.conversations.single.pendingQuestion, isEmpty);
    },
  );

  testWidgets(
    'deleting while the initial save waits prevents a later request',
    (tester) async {
      final local = FaultStore();
      final api = FaultApi(local);
      await openApp(tester, api);
      final gate = Completer<void>();
      local.writeGate = gate;
      await fixtures.send(tester, '还没落盘就决定删除');
      expect(api.requests, isEmpty);
      await tester.tap(find.byKey(const ValueKey('new-conversation')));
      await tester.pumpAndSettle();
      await deleteOnlyConversation(tester);
      gate.complete();
      await tester.pumpAndSettle();
      expect(tester.takeException(), isNull);
      expect(api.requests, isEmpty);
      final saved = await ConversationRepository(local).load();
      expect(
        saved.conversations.every((conversation) => conversation.isEmpty),
        isTrue,
      );
    },
  );
}
