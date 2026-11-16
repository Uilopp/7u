import 'package:couple_app/core/conversations.dart';
import 'package:couple_app/core/conversation_controller.dart';
import 'package:couple_app/main.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';

import 'free_question_screen_test.dart' as fixtures;

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  final messenger =
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger;
  const versionChannel = MethodChannel('seven_u/app_info');
  setUp(
    () => messenger.setMockMethodCallHandler(
      versionChannel,
      (_) async => '1.0.260927',
    ),
  );
  tearDown(() => messenger.setMockMethodCallHandler(versionChannel, null));

  testWidgets('background transition saves a draft before debounce', (
    tester,
  ) async {
    final api = fixtures.TestApi();
    await fixtures.launch(tester, api);
    await tester.enterText(
      find.byKey(const ValueKey('question-input')),
      '还没等到自动保存就切后台',
    );
    await tester.pump();
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.inactive);
    await tester.pump();
    final stored = await ConversationRepository(api.local).load();
    expect(stored.conversations.single.draft, '还没等到自动保存就切后台');
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
    await tester.pumpAndSettle();
  });

  testWidgets('interrupted question restores pending state and retries once', (
    tester,
  ) async {
    final api = fixtures.TestApi();
    final engine = ConversationController(api: api);
    await tester.pumpWidget(
      SevenUApp(api: api, showIntro: false, conversationController: engine),
    );
    await tester.pumpAndSettle();
    await fixtures.send(tester, '退出前还在等回答');
    await tester.pumpWidget(const SizedBox.shrink());
    // Destroying an engine/process is different from merely replacing a page.
    engine.dispose();
    await tester.pumpAndSettle();
    await fixtures.launch(tester, api);
    expect(find.text('退出前还在等回答'), findsOneWidget);
    expect(find.byKey(const ValueKey('retry-message')), findsOneWidget);
    expect(api.requests, hasLength(1));
    await tester.tap(find.byKey(const ValueKey('retry-message')));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 20));
    expect(api.requests, hasLength(2));
    api.answer(0, '旧页面迟到的回答不能覆盖新请求');
    api.answer(1, '重试后的回答');
    await tester.pumpAndSettle();
    final stored = await ConversationRepository(api.local).load();
    expect(stored.conversations.single.turns, hasLength(1));
    expect(stored.conversations.single.turns.single.body, '重试后的回答');
    expect(stored.conversations.single.pendingQuestion, isEmpty);
  });

  testWidgets(
    'two concurrent conversations keep their own answers and history',
    (tester) async {
      final api = fixtures.TestApi();
      await fixtures.launch(tester, api);
      await fixtures.send(tester, '第一段等待中的问题');
      await tester.tap(find.byKey(const ValueKey('new-conversation')));
      await tester.pumpAndSettle();
      await fixtures.send(tester, '第二段等待中的问题');
      api.answer(0, '只属于第一段的回答');
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 20));
      expect(find.text('只属于第一段的回答'), findsNothing);
      api.answer(1, '只属于第二段的回答');
      await tester.pumpAndSettle();
      expect(find.text('只属于第二段的回答'), findsOneWidget);
      final stored = await ConversationRepository(api.local).load();
      expect(stored.conversations[0].turns.single.body, '只属于第一段的回答');
      expect(stored.conversations[1].turns.single.body, '只属于第二段的回答');
      expect(api.histories.every((history) => history.isEmpty), isTrue);
    },
  );

  testWidgets(
    'larger system text keeps the title and version within the appbar',
    (tester) async {
      tester.view.physicalSize = const Size(360, 640);
      tester.view.devicePixelRatio = 1;
      tester.platformDispatcher.textScaleFactorTestValue = 1.5;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      addTearDown(tester.platformDispatcher.clearTextScaleFactorTestValue);
      final api = fixtures.TestApi();
      await fixtures.launch(tester, api);
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets(
    'landscape keyboard keeps a multiline composer within the screen',
    (tester) async {
      tester.view.physicalSize = const Size(640, 360);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      addTearDown(tester.view.resetViewInsets);
      final api = fixtures.TestApi();
      await fixtures.launch(tester, api);
      tester.view.viewInsets = const FakeViewPadding(bottom: 180);
      await tester.pumpAndSettle();
      await tester.enterText(
        find.byKey(const ValueKey('question-input')),
        '第一行\n第二行\n第三行\n第四行\n第五行',
      );
      await tester.pump();
      expect(tester.takeException(), isNull);
      expect(
        tester.getBottomLeft(find.byKey(const ValueKey('ask-seven-u'))).dy,
        lessThanOrEqualTo(180),
      );
      await tester.tap(find.byKey(const ValueKey('ask-seven-u')));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 20));
      expect(api.requests.single, '第一行\n第二行\n第三行\n第四行\n第五行');
      api.answer(0, '完整收到了五行内容。');
      await tester.pumpAndSettle();
    },
  );
}
