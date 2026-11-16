import 'package:couple_app/core/conversation_controller.dart';
import 'package:couple_app/core/conversations.dart';
import 'package:couple_app/core/background_answers.dart';
import 'package:couple_app/core/question_history.dart';
import 'package:couple_app/main.dart';
import 'package:couple_app/screens/free_question.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';

import 'conversation_controller_test.dart' as controller_fixtures;
import 'free_question_screen_test.dart' as fixtures;

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  final messenger =
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger;
  const versionChannel = MethodChannel('seven_u/app_info');
  setUp(
    () => messenger.setMockMethodCallHandler(
      versionChannel,
      (_) async => '0.1.260928',
    ),
  );
  tearDown(() => messenger.setMockMethodCallHandler(versionChannel, null));

  testWidgets(
    'an app-owned task persists and notifies after its page is destroyed',
    (tester) async {
      final store = controller_fixtures.ControllerStore();
      final api = controller_fixtures.ControllerApi(store);
      final background = controller_fixtures.ControllerBackground();
      final tasks = ConversationController(
        api: api,
        background: background,
        version: Future.value('0.1.260928'),
      );
      addTearDown(tasks.dispose);
      await tester.pumpWidget(
        SevenUApp(api: api, showIntro: false, conversationController: tasks),
      );
      await tester.pumpAndSettle();
      await fixtures.send(tester, '离开页面后完成');
      expect(api.questions, ['离开页面后完成']);
      await tester.pumpWidget(const SizedBox.shrink());
      await tester.pump();
      expect(tester.binding.transientCallbackCount, 0);
      api.answer(0, '回到页面时这句话仍然在。');
      await tester.pumpAndSettle();
      expect(background.finished, hasLength(1));
      final state = await tasks.repository.load();
      expect(state.conversations.single.turns.single.body, '回到页面时这句话仍然在。');
      expect(state.conversations.single.pendingQuestion, isEmpty);
      await tester.pumpWidget(
        SevenUApp(api: api, showIntro: false, conversationController: tasks),
      );
      await tester.pumpAndSettle();
      expect(find.text('回到页面时这句话仍然在。'), findsOneWidget);
      expect(api.questions, hasLength(1));
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets(
    'default page owner lets an already-paid answer complete after removal',
    (tester) async {
      final api = fixtures.TestApi();
      await fixtures.launch(tester, api);
      await fixtures.send(tester, '默认页面也不应丢失回答');
      await tester.pumpWidget(const SizedBox.shrink());
      api.answer(0, '页面已经关闭，回答照常保存。');
      await tester.pumpAndSettle();
      final state = await ConversationRepository(api.local).load();
      expect(state.conversations.single.turns.single.body, '页面已经关闭，回答照常保存。');
      await fixtures.launch(tester, api);
      expect(find.text('页面已经关闭，回答照常保存。'), findsOneWidget);
      expect(api.requests, hasLength(1));
    },
  );

  testWidgets(
    'answer notification dismisses overlays and opens the exact answer at its end',
    (tester) async {
      final store = controller_fixtures.ControllerStore();
      final api = controller_fixtures.ControllerApi(store);
      final background = controller_fixtures.ControllerBackground();
      final target = Conversation.create().copyWith(
        turns: [
          SavedQuestion(
            question: '通知指向这段对话',
            title: '回答',
            body: List.filled(60, '需要滚动才能读完的长回答。').join('\n'),
            evidence: const [],
            createdAt: DateTime(2026, 9, 28),
          ),
        ],
      );
      final other = Conversation.create().copyWith(
        turns: [
          SavedQuestion(
            question: '另一段对话',
            title: '回答',
            body: '当前页面的回答',
            evidence: const [],
            createdAt: DateTime(2026, 9, 29),
          ),
        ],
      );
      store.values[ConversationRepository.key] = {
        'active_id': other.id,
        'conversations': [target.toJson(), other.toJson()],
      };
      final tasks = ConversationController(
        api: api,
        background: background,
        version: Future.value(null),
      );
      addTearDown(tasks.dispose);
      await tester.pumpWidget(
        SevenUApp(api: api, showIntro: false, conversationController: tasks),
      );
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const ValueKey('question-history')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(ValueKey('delete-${other.id}')));
      await tester.pumpAndSettle();
      expect(find.text('删除这段对话？'), findsOneWidget);
      background.launchQueue.add(target.id);
      background.notifications.add(OpenAnswerConversation(target.id));
      await tester.pumpAndSettle();
      expect(find.text('我们的对话'), findsNothing);
      expect(find.text('删除这段对话？'), findsNothing);
      expect(tasks.activeId, target.id);
      expect(tasks.conversations, hasLength(2));
      expect(find.byTooltip('复制完整回答').hitTestable(), findsOneWidget);
      expect(api.questions, isEmpty);

      await tester.tap(find.byKey(const ValueKey('question-history')));
      await tester.pumpAndSettle();
      background.launchQueue.add('deleted-conversation');
      background.notifications.add(
        const OpenAnswerConversation('deleted-conversation'),
      );
      await tester.pumpAndSettle();
      expect(find.text('我们的对话'), findsOneWidget);
      // Ordinary navigation is not a notification and must not pop another
      // component's sheet; the daily-memory action dismisses its own sheet.
      tasks.newConversation();
      await tester.pumpAndSettle();
      expect(find.text('我们的对话'), findsOneWidget);
      Navigator.of(tester.element(find.text('我们的对话'))).pop();
      await tester.pumpAndSettle();
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets(
    'answer copy button copies the entire body without labels or truncation',
    (tester) async {
      String? copied;
      messenger.setMockMethodCallHandler(SystemChannels.platform, (call) async {
        if (call.method == 'Clipboard.setData') {
          copied = (call.arguments as Map)['text'] as String;
        }
        return null;
      });
      addTearDown(
        () => messenger.setMockMethodCallHandler(SystemChannels.platform, null),
      );
      final api = fixtures.TestApi();
      await fixtures.launch(tester, api);
      await fixtures.send(tester, '复制这份完整回答');
      const text = '第一段完整内容。\n\n第二段保留换行、标点和 emoji 🐱。\n最后一句也需要复制。';
      api.answer(0, text);
      await tester.pumpAndSettle();
      await tester.tap(find.byTooltip('复制完整回答'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 300));
      expect(copied, text);
      expect(find.text('已复制'), findsOneWidget);
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets('reduced motion keeps the waiting dots static without a ticker', (
    tester,
  ) async {
    final api = fixtures.TestApi();
    final tasks = ConversationController(api: api, version: Future.value(null));
    addTearDown(tasks.dispose);
    await tester.pumpWidget(
      MaterialApp(
        home: MediaQuery(
          data: const MediaQueryData(disableAnimations: true),
          child: AskSevenUScreen(tasks: tasks),
        ),
      ),
    );
    await tester.pumpAndSettle();
    await fixtures.send(tester, '减少动态效果时照常等待');
    await tester.pumpAndSettle();
    for (var index = 0; index < 3; index++) {
      expect(
        tester
            .widget<Opacity>(find.byKey(ValueKey('reply-waiting-dot-$index')))
            .opacity,
        1,
      );
    }
    expect(tester.binding.transientCallbackCount, 0);
    api.answer(0, '仍然正常完成。');
    await tester.pumpAndSettle();
    expect(find.byKey(const ValueKey('reply-waiting-indicator')), findsNothing);
  });
}
