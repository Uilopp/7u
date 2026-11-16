import 'dart:async';

import 'package:flutter/widgets.dart';

import 'api.dart';
import 'app_version.dart';
import 'background_answers.dart';
import 'conversations.dart';
import 'question_cancellation.dart';
import 'question_history.dart';

/// Owns the local conversations and in-flight requests, independently of pages.
/// The production instance lives with the retained Flutter engine, not Activity.
class ConversationController extends ChangeNotifier
    with WidgetsBindingObserver {
  final Api api;
  final ConversationRepository repository;
  final BackgroundAnswers background;
  final Future<String?> _version;
  late final StreamSubscription<BackgroundAnswerEvent> _events;
  final Map<String, _AnswerJob> _jobs = {};
  List<Conversation> _conversations = [];
  String? _activeId;
  Future<void>? _initializing;
  Future<void> _navigation = Future.value();
  final List<String> _queuedNavigation = [];
  final Set<String> _consumedNavigation = {};
  Timer? _draftTimer;
  bool _initialized = false;
  bool _disposed = false;
  bool _releaseWhenIdle = false;
  bool _permissionRequested = false;

  bool loading = true;
  String? loadError;
  String? storageError;
  String? backgroundNotice;
  int navigationRevision = 0;
  int notificationNavigationRevision = 0;

  ConversationController({
    required this.api,
    BackgroundAnswers? background,
    Future<String?>? version,
  }) : repository = ConversationRepository(api.local),
       background = background ?? BackgroundAnswers.platform(),
       _version = (version ?? readInstalledVersion()).then<String?>(
         (value) => value,
         onError: (Object _, StackTrace _) => null,
       ) {
    WidgetsBinding.instance.addObserver(this);
    _events = this.background.events.listen(_onEvent, onError: (Object _) {});
    _readLaunchConversation();
    unawaited(initialize());
  }

  List<Conversation> get conversations => List.unmodifiable(_conversations);
  String? get activeId => _activeId;
  Conversation get active =>
      _conversations.firstWhere((c) => c.id == _activeId);
  Set<String> get running => Set.unmodifiable(_jobs.keys);
  bool get ready => !_disposed && !loading && loadError == null;
  bool get busy => _jobs.containsKey(_activeId);

  Future<void> initialize() {
    if (_disposed || _initialized) return Future.value();
    return _initializing ??= _load().whenComplete(() => _initializing = null);
  }

  Future<void> _load() async {
    loading = true;
    loadError = null;
    _notify();
    try {
      final saved = await repository.load();
      if (_disposed) return;
      _conversations = saved.conversations.isEmpty
          ? [Conversation.create()]
          : List.of(saved.conversations);
      _activeId = _conversations.any((c) => c.id == saved.activeId)
          ? saved.activeId
          : _conversations.first.id;
      _initialized = true;
      loading = false;
      // Pending questions from a stopped process are never automatically sent.
      _applyQueuedNavigation();
    } catch (_) {
      if (!_disposed) {
        loading = false;
        loadError = '暂时没有读到保存的对话，请重试。';
      }
    }
    _notify();
  }

  void _notify() {
    if (!_disposed) notifyListeners();
  }

  void _replace(Conversation conversation) {
    final index = _conversations.indexWhere((c) => c.id == conversation.id);
    if (index >= 0) _conversations[index] = conversation;
  }

  bool _current(_AnswerJob job) =>
      !_disposed &&
      identical(_jobs[job.conversationId], job) &&
      _conversations.any((c) => c.id == job.conversationId);

  Future<bool> save() async {
    _draftTimer?.cancel();
    if (!ready) return false;
    try {
      await repository.save(_conversations, _activeId);
      if (!_disposed && storageError != null) {
        storageError = null;
        _notify();
      }
      return true;
    } catch (_) {
      if (!_disposed) {
        storageError = '这段对话暂时没有保存成功，请稍后重试。';
        _notify();
      }
      return false;
    }
  }

  void updateDraft(String value) {
    if (!ready || busy || active.pendingQuestion.isNotEmpty) return;
    _replace(active.copyWith(draft: value));
    _notify();
    _draftTimer?.cancel();
    _draftTimer = Timer(
      const Duration(milliseconds: 400),
      () => unawaited(save()),
    );
  }

  Future<void> send(String text) async {
    if (!ready || busy || active.pendingQuestion.isNotEmpty) return;
    final question = text.trim();
    if (question.isEmpty) return;
    _draftTimer?.cancel();
    _replace(
      active.copyWith(
        draft: '',
        pendingQuestion: question,
        error: '',
        updatedAt: DateTime.now(),
      ),
    );
    await _start(active);
  }

  Future<void> retry() async {
    if (!ready || busy || active.pendingQuestion.isEmpty) return;
    _replace(active.copyWith(error: ''));
    await _start(active);
  }

  Future<void> _start(Conversation conversation) async {
    // Reserve synchronously, before either platform or storage awaits.
    final job = _AnswerJob(conversation);
    _jobs[conversation.id] = job;
    _notify();
    var outcome = BackgroundAnswerOutcome.silent;
    var stage = 'background';
    try {
      late final BackgroundStartResult start;
      try {
        // Start while the sending Activity is still foreground; never pay for
        // a request that Android could not protect or that was not saved.
        start = await background.begin(
          jobId: job.id,
          conversationId: job.conversationId,
        );
      } finally {
        job.beginSettled = true;
      }
      if (!_current(job)) return;
      if (!start.notificationsEnabled && !_permissionRequested) {
        _permissionRequested = true;
        unawaited(_requestPermission());
      }
      stage = 'storage';
      if (!await save()) {
        if (_current(job)) {
          _replace(
            _conversationFor(job).copyWith(error: '问题还没有保存成功，请重试；尚未开始生成回答。'),
          );
          _notify();
        }
        return;
      }
      if (!_current(job)) return;
      final version = await _version;
      if (!_current(job)) return;
      stage = 'model';
      final result = await api.askQuestion(
        job.question,
        history: job.turns,
        cancellation: job.cancellation,
      );
      if (!_current(job)) return;
      _replace(
        _conversationFor(job).copyWith(
          turns: [
            ...job.turns,
            SavedQuestion.fromResult(job.question, result, appVersion: version),
          ],
          pendingQuestion: '',
          error: '',
          updatedAt: DateTime.now(),
        ),
      );
      _notify();
      stage = 'storage';
      if (await save() && _current(job)) {
        outcome = BackgroundAnswerOutcome.success;
      }
    } catch (failure) {
      if (_current(job)) {
        final message = stage == 'background'
            ? '后台回答任务没有启动，请保持 App 在前台后重试。尚未开始生成回答。'
            : failure is ApiException
            ? failure.message
            : '这次连接没有完成，请再试一次。';
        _replace(_conversationFor(job).copyWith(error: message));
        _notify();
        if (await save() && _current(job) && stage != 'background') {
          outcome = BackgroundAnswerOutcome.error;
        }
      }
    } finally {
      if (!_current(job)) outcome = BackgroundAnswerOutcome.silent;
      // Finish once, after the durable answer/error exists, never before save.
      await _finish(job, outcome);
      if (identical(_jobs[job.conversationId], job)) {
        _jobs.remove(job.conversationId);
        _notify();
      }
      _disposeIfReleased();
    }
  }

  Conversation _conversationFor(_AnswerJob job) =>
      _conversations.firstWhere((c) => c.id == job.conversationId);

  Future<void> _requestPermission() async {
    var enabled = false;
    try {
      enabled = await background.requestNotificationPermission();
    } catch (_) {
      // Permission affects notifications, not the running answer.
    }
    if (!_disposed && !enabled) {
      backgroundNotice = '通知未开启，回答仍会保存在对话里。';
      _notify();
    }
  }

  Future<void> _finish(_AnswerJob job, BackgroundAnswerOutcome outcome) async {
    if (job.finished || !job.beginSettled) return;
    job.finished = true;
    try {
      await background.finish(
        jobId: job.id,
        conversationId: job.conversationId,
        outcome: outcome,
      );
    } catch (_) {
      if (!_disposed && outcome == BackgroundAnswerOutcome.success) {
        backgroundNotice = '回答已保存在对话里，但通知暂时未送达。';
        _notify();
      }
    }
  }

  void editPending() {
    if (!ready || busy) return;
    _replace(
      active.copyWith(
        draft: active.pendingQuestion,
        pendingQuestion: '',
        error: '',
      ),
    );
    _notify();
    unawaited(save());
  }

  void newConversation() {
    if (!ready || (!busy && active.isEmpty)) return;
    final conversation = Conversation.create();
    _conversations.add(conversation);
    _activeId = conversation.id;
    navigationRevision++;
    _notify();
    unawaited(save());
  }

  void switchTo(String id) {
    if (!ready || !_conversations.any((c) => c.id == id)) return;
    _activeId = id;
    navigationRevision++;
    _notify();
    unawaited(save());
  }

  Future<void> deleteConversation(String id) async {
    if (!ready) return;
    final job = _jobs.remove(id);
    job?.cancellation.cancel();
    _conversations.removeWhere((c) => c.id == id);
    if (_conversations.isEmpty) _conversations.add(Conversation.create());
    if (_activeId == id) {
      _activeId = _conversations.first.id;
      navigationRevision++;
    }
    _notify();
    if (job != null) unawaited(_finish(job, BackgroundAnswerOutcome.silent));
    await save();
    _disposeIfReleased();
  }

  void _onEvent(BackgroundAnswerEvent event) {
    if (_disposed) return;
    switch (event) {
      case OpenAnswerConversation(:final conversationId):
        _readLaunchConversation(conversationId);
      case InterruptedAnswerJobs(:final jobIds):
        for (final job in _jobs.values.toList()) {
          if (!jobIds.contains(job.id)) continue;
          _jobs.remove(job.conversationId);
          job.cancellation.cancel();
          _replace(_conversationFor(job).copyWith(error: '上次回答因系统中断而停止，请重试。'));
          unawaited(_finish(job, BackgroundAnswerOutcome.silent));
        }
        _notify();
        unawaited(save());
        _disposeIfReleased();
    }
  }

  void _readLaunchConversation([String? fallback]) {
    _navigation = _navigation.then((_) async {
      if (_disposed) return;
      var consumed = false;
      try {
        while (!_disposed) {
          final queued = await background.takeLaunchConversation();
          if (queued == null || queued.isEmpty) break;
          consumed = true;
          _queuedNavigation.add(queued);
          _consumedNavigation.add(queued);
          if (_consumedNavigation.length > 64) {
            _consumedNavigation.remove(_consumedNavigation.first);
          }
        }
      } catch (_) {
        // A warm event can still identify the exact conversation.
      }
      if (_disposed) return;
      if (!consumed &&
          fallback != null &&
          !_consumedNavigation.contains(fallback)) {
        _queuedNavigation.add(fallback);
      }
      _applyQueuedNavigation();
    });
  }

  void _applyQueuedNavigation() {
    if (!ready || _queuedNavigation.isEmpty) return;
    final id = _queuedNavigation.lastWhere(
      (id) => _conversations.any((c) => c.id == id),
      orElse: () => '',
    );
    _queuedNavigation.clear();
    if (id.isNotEmpty) {
      notificationNavigationRevision++;
      switchTo(id); // Never resurrect unknown/deleted IDs.
    }
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state != AppLifecycleState.resumed) unawaited(save());
  }

  /// Used by a default widget-owned instance: removing its page cannot abandon
  /// a paid request. Explicitly injected app/test owners control dispose instead.
  void releaseWhenIdle() {
    if (_disposed) return;
    _releaseWhenIdle = true;
    WidgetsBinding.instance.removeObserver(this);
    unawaited(save());
    _disposeIfReleased();
  }

  void _disposeIfReleased() {
    if (_releaseWhenIdle && _jobs.isEmpty) dispose();
  }

  @override
  void dispose() {
    if (_disposed) return;
    _disposed = true;
    _draftTimer?.cancel();
    WidgetsBinding.instance.removeObserver(this);
    unawaited(_events.cancel());
    for (final job in _jobs.values) {
      job.cancellation.cancel();
      unawaited(_finish(job, BackgroundAnswerOutcome.silent));
    }
    _jobs.clear();
    background.dispose();
    super.dispose();
  }
}

class _AnswerJob {
  final String id = 'answer-${Conversation.create().id}';
  final String conversationId;
  final String question;
  final List<SavedQuestion> turns;
  final QuestionCancellation cancellation = QuestionCancellation();
  bool beginSettled = false;
  bool finished = false;

  _AnswerJob(Conversation conversation)
    : conversationId = conversation.id,
      question = conversation.pendingQuestion,
      turns = List.unmodifiable(conversation.turns);
}
