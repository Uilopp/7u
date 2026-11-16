import 'dart:async';

import 'package:couple_app/core/api.dart';
import 'package:couple_app/core/background_answers.dart';
import 'package:couple_app/core/conversation_controller.dart';
import 'package:couple_app/core/conversations.dart';
import 'package:couple_app/core/local_store.dart';
import 'package:couple_app/core/question_cancellation.dart';
import 'package:couple_app/core/question_history.dart';
import 'package:flutter_test/flutter_test.dart';

class ControllerStore extends LocalStore {
  final values = <String, Map<String, dynamic>>{};
  int failWrites = 0;
  Completer<void>? writeGate;
  Completer<void>? readGate;

  @override
  Future<Map<String, dynamic>?> loadMetadata(String key) async {
    if (readGate != null) await readGate!.future;
    return values[key];
  }

  @override
  Future<void> saveMetadata(String key, Map<String, dynamic> value) async {
    if (writeGate != null) await writeGate!.future;
    if (failWrites > 0) {
      failWrites--;
      throw StateError('simulated storage failure');
    }
    values[key] = value;
  }
}

class ControllerApi extends Api {
  final questions = <String>[];
  final histories = <List<SavedQuestion>>[];
  final tokens = <QuestionCancellation?>[];
  final replies = <Completer<Map<String, dynamic>>>[];

  ControllerApi(ControllerStore store) : super(local: store);

  @override
  Future<Map<String, dynamic>> askQuestion(
    String question, {
    List<SavedQuestion> history = const [],
    QuestionCancellation? cancellation,
  }) {
    questions.add(question);
    histories.add(List.of(history));
    tokens.add(cancellation);
    final reply = Completer<Map<String, dynamic>>();
    replies.add(reply);
    // Intentionally ignore cancellation here to exercise late-completion guards.
    return reply.future;
  }

  void answer(int index, String body) => replies[index].complete({
    'status': 'done',
    'answer': {'title': '回答', 'body': body, 'quotes': []},
  });
}

class ControllerBackground implements BackgroundAnswers {
  final notifications = StreamController<BackgroundAnswerEvent>.broadcast(
    sync: true,
  );
  final started = <({String jobId, String conversationId})>[];
  final finished =
      <
        ({String jobId, String conversationId, BackgroundAnswerOutcome outcome})
      >[];
  final launchQueue = <String>[];
  bool failBegin = false;
  bool notificationsEnabled = true;
  int permissionRequests = 0;
  Completer<bool>? permission;
  bool disposed = false;

  @override
  Stream<BackgroundAnswerEvent> get events => notifications.stream;

  @override
  Future<BackgroundStartResult> begin({
    required String jobId,
    required String conversationId,
  }) async {
    started.add((jobId: jobId, conversationId: conversationId));
    if (failBegin) throw StateError('service start failed');
    return BackgroundStartResult(notificationsEnabled: notificationsEnabled);
  }

  @override
  Future<void> finish({
    required String jobId,
    required String conversationId,
    required BackgroundAnswerOutcome outcome,
  }) async {
    finished.add((
      jobId: jobId,
      conversationId: conversationId,
      outcome: outcome,
    ));
  }

  @override
  Future<bool> requestNotificationPermission() async {
    permissionRequests++;
    return permission == null ? false : await permission!.future;
  }

  @override
  Future<String?> takeLaunchConversation() async =>
      launchQueue.isEmpty ? null : launchQueue.removeAt(0);

  @override
  void dispose() {
    if (disposed) return;
    disposed = true;
    unawaited(notifications.close());
  }
}

Future<void> flushController() async {
  for (var index = 0; index < 6; index++) {
    await Future<void>.delayed(Duration.zero);
  }
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  late ControllerStore store;
  late ControllerApi api;
  late ControllerBackground background;
  late ConversationController controller;

  setUp(() async {
    store = ControllerStore();
    api = ControllerApi(store);
    background = ControllerBackground();
    controller = ConversationController(
      api: api,
      background: background,
      version: Future.value('0.1.260928'),
    );
    await controller.initialize();
  });
  tearDown(() => controller.dispose());

  test(
    'service start failure keeps pending question without paid request',
    () async {
      background.failBegin = true;
      await controller.send('请帮我回忆');
      expect(api.questions, isEmpty);
      expect(controller.active.pendingQuestion, '请帮我回忆');
      expect(controller.active.error, contains('后台回答任务没有启动'));
      expect(controller.running, isEmpty);
      expect(
        background.finished.single.outcome,
        BackgroundAnswerOutcome.silent,
      );
      expect(
        (await controller.repository.load())
            .conversations
            .single
            .pendingQuestion,
        '请帮我回忆',
      );
    },
  );

  test(
    'pending save failure prevents model request and retry can recover',
    () async {
      store.failWrites = 1;
      await controller.send('先存好再回答');
      expect(api.questions, isEmpty);
      expect(controller.storageError, isNotNull);
      expect(controller.active.error, contains('尚未开始生成'));
      expect(
        background.finished.single.outcome,
        BackgroundAnswerOutcome.silent,
      );
      final retry = controller.retry();
      await flushController();
      expect(api.questions, ['先存好再回答']);
      api.answer(0, '已经保存完成。');
      await retry;
      expect(controller.storageError, isNull);
      expect(background.finished.last.outcome, BackgroundAnswerOutcome.success);
      expect(controller.active.turns.single.appVersion, '0.1.260928');
    },
  );

  test(
    'completion notification waits for the answer to finish saving',
    () async {
      final operation = controller.send('完成后告诉我');
      await flushController();
      expect(api.questions, hasLength(1));
      store.writeGate = Completer<void>();
      api.answer(0, '这是完整回答。');
      await flushController();
      expect(controller.active.turns.single.body, '这是完整回答。');
      expect(background.finished, isEmpty);
      store.writeGate!.complete();
      await operation;
      final saved = await controller.repository.load();
      expect(saved.conversations.single.turns.single.body, '这是完整回答。');
      expect(saved.conversations.single.pendingQuestion, isEmpty);
      expect(
        background.finished.single.outcome,
        BackgroundAnswerOutcome.success,
      );
    },
  );

  test(
    'final save failure keeps in-memory answer and never notifies success',
    () async {
      final operation = controller.send('不能丢失的回答');
      await flushController();
      store.failWrites = 1;
      api.answer(0, '暂时只保存在内存里。');
      await operation;
      expect(controller.storageError, isNotNull);
      expect(controller.active.turns.single.body, '暂时只保存在内存里。');
      expect(
        background.finished.single.outcome,
        BackgroundAnswerOutcome.silent,
      );
      expect(
        (await controller.repository.load())
            .conversations
            .single
            .pendingQuestion,
        '不能丢失的回答',
      );
      expect(await controller.save(), true);
      expect(
        (await controller.repository.load())
            .conversations
            .single
            .turns
            .single
            .body,
        '暂时只保存在内存里。',
      );
      expect(api.questions, hasLength(1));
      expect(background.finished, hasLength(1));
    },
  );

  test(
    'permission prompt cannot block a running model and denial is quiet',
    () async {
      background.notificationsEnabled = false;
      background.permission = Completer<bool>();
      final operation = controller.send('没授权通知也可以回答');
      await flushController();
      expect(background.permissionRequests, 1);
      expect(api.questions, hasLength(1));
      api.answer(0, '回答正常保存。');
      await operation;
      background.permission!.complete(false);
      await flushController();
      expect(controller.backgroundNotice, '通知未开启，回答仍会保存在对话里。');
      final second = controller.send('继续聊');
      await flushController();
      expect(background.permissionRequests, 1);
      api.answer(1, '继续回答。');
      await second;
    },
  );

  test('concurrent jobs finish in reverse order without replacing another conversation', () async {
    final firstId = controller.activeId;
    final first = controller.send('第一段问题');
    controller.newConversation();
    final secondId = controller.activeId;
    final second = controller.send('第二段问题');
    await flushController();
    expect(controller.running, {firstId, secondId});
    api.answer(1, '第二段先完成。');
    await second;
    expect(controller.running, {firstId});
    api.answer(0, '第一段后完成。');
    await first;
    final saved = await controller.repository.load();
    expect(saved.conversations.first.turns.single.body, '第一段后完成。');
    expect(saved.conversations.last.turns.single.body, '第二段先完成。');
    expect(background.finished.map((job) => job.conversationId), [
      secondId,
      firstId,
    ]);
    expect(background.started.map((job) => job.jobId).toSet(), hasLength(2));
  });

  test(
    'deletion cancels the job and late answer neither restores nor notifies',
    () async {
      final id = controller.activeId!;
      final operation = controller.send('稍后删除');
      await flushController();
      await controller.deleteConversation(id);
      expect(api.tokens.single!.isCancelled, true);
      api.answer(0, '不应该复活的回答');
      await operation;
      expect(controller.conversations.any((item) => item.id == id), false);
      expect(
        (await controller.repository.load()).conversations.any(
          (item) => item.id == id,
        ),
        false,
      );
      expect(background.finished, hasLength(1));
      expect(
        background.finished.single.outcome,
        BackgroundAnswerOutcome.silent,
      );
    },
  );

  test('interrupted old token cannot overwrite an explicit retry', () async {
    final original = controller.send('系统曾中断的问题');
    await flushController();
    final oldJob = background.started.single.jobId;
    background.notifications.add(InterruptedAnswerJobs([oldJob]));
    expect(api.tokens.first!.isCancelled, true);
    expect(controller.active.error, contains('系统中断'));
    final retry = controller.retry();
    await flushController();
    expect(api.questions, hasLength(2));
    api.answer(1, '新请求完成。');
    await retry;
    api.answer(0, '旧请求迟到。');
    await original;
    expect(controller.active.turns.single.body, '新请求完成。');
    expect(
      background.finished.where(
        (job) => job.outcome == BackgroundAnswerOutcome.success,
      ),
      hasLength(1),
    );
    expect(background.started[0].jobId, isNot(background.started[1].jobId));
  });

  test(
    'queued cold navigation waits for storage and ignores a deleted target',
    () async {
      controller.dispose();
      final first = Conversation.create().copyWith(draft: '第一段');
      final second = Conversation.create().copyWith(pendingQuestion: '尚未完成的问题');
      store.values[ConversationRepository.key] = {
        'active_id': first.id,
        'conversations': [first.toJson(), second.toJson()],
      };
      store.readGate = Completer<void>();
      background = ControllerBackground()
        ..launchQueue.addAll([second.id, 'deleted-id']);
      controller = ConversationController(
        api: api,
        background: background,
        version: Future.value(null),
      );
      await flushController();
      expect(controller.loading, true);
      store.readGate!.complete();
      await controller.initialize();
      await flushController();
      expect(controller.activeId, second.id);
      expect(controller.running, isEmpty);
      expect(api.questions, isEmpty);
      expect(controller.active.pendingQuestion, '尚未完成的问题');
      expect(background.launchQueue, isEmpty);
      await controller.deleteConversation(second.id);
      background.launchQueue.add(second.id);
      background.notifications.add(OpenAnswerConversation(second.id));
      await flushController();
      expect(controller.activeId, first.id);
      expect(controller.conversations, hasLength(1));
    },
  );

  test(
    'released page owner completes then disposes without losing the answer',
    () async {
      final operation = controller.send('页面关闭后继续');
      await flushController();
      controller.releaseWhenIdle();
      expect(background.disposed, false);
      api.answer(0, '已经在后台完成。');
      await operation;
      expect(background.disposed, true);
      expect(
        background.finished.single.outcome,
        BackgroundAnswerOutcome.success,
      );
      expect(
        (await ConversationRepository(
          store,
        ).load()).conversations.single.turns.single.body,
        '已经在后台完成。',
      );
    },
  );
}
