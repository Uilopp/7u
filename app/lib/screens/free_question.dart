import 'dart:async';
import 'dart:math' as math;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../core/conversation_controller.dart';
import '../core/conversations.dart';
import '../core/question_history.dart';
import '../widgets/app_version.dart';
import '../widgets/soft_ui.dart';

class AskSevenUScreen extends StatefulWidget {
  final ConversationController tasks;
  final Widget? notificationSettingsAction;

  const AskSevenUScreen({
    super.key,
    required this.tasks,
    this.notificationSettingsAction,
  });

  @override
  State<AskSevenUScreen> createState() => _AskSevenUScreenState();
}

class _AskSevenUScreenState extends State<AskSevenUScreen> {
  static const suggestions = [
    '最近有什么值得纪念的事？',
    'TA 是一个怎样的人？',
    '我想送 TA 礼物，有什么建议？',
  ];
  final controller = TextEditingController();
  final scroll = ScrollController();
  final inputFocus = FocusNode();
  String? _lastActiveId;
  String? _lastStorageError;
  String? _lastBackgroundNotice;
  int _lastNavigation = -1;
  int _lastNotificationNavigation = 0;
  int _lastTurnCount = 0;

  ConversationController get tasks => widget.tasks;
  List<Conversation> get conversations => tasks.conversations;
  String? get activeId => tasks.activeId;
  bool get loading => tasks.loading;
  String? get loadError => tasks.loadError;
  Conversation get active => tasks.active;
  bool get busy => tasks.busy;

  @override
  void initState() {
    super.initState();
    tasks.addListener(_changed);
    _lastNotificationNavigation = tasks.notificationNavigationRevision;
    _synchronize();
  }

  @override
  void didUpdateWidget(covariant AskSevenUScreen oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.tasks != tasks) {
      oldWidget.tasks.removeListener(_changed);
      tasks.addListener(_changed);
      _lastActiveId = null;
      _lastNavigation = -1;
      _lastNotificationNavigation = tasks.notificationNavigationRevision;
      _synchronize();
    }
  }

  @override
  void dispose() {
    tasks.removeListener(_changed);
    controller.dispose();
    scroll.dispose();
    inputFocus.dispose();
    super.dispose();
  }

  void _synchronize() {
    if (!tasks.ready) return;
    final switched =
        _lastActiveId != activeId ||
        _lastNavigation != tasks.navigationRevision;
    final added = active.turns.length > _lastTurnCount;
    final nearBottom = !scroll.hasClients || scroll.position.extentAfter < 180;
    if (controller.text != active.draft) {
      controller.value = TextEditingValue(
        text: active.draft,
        selection: TextSelection.collapsed(offset: active.draft.length),
      );
    }
    _lastActiveId = activeId;
    _lastNavigation = tasks.navigationRevision;
    _lastTurnCount = active.turns.length;
    if (switched || (added && nearBottom)) _scrollToBottom();
  }

  void _changed() {
    if (!mounted) return;
    final fromNotification =
        _lastNotificationNavigation != tasks.notificationNavigationRevision;
    _lastNotificationNavigation = tasks.notificationNavigationRevision;
    _synchronize();
    if (fromNotification) {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (!mounted) return;
        Navigator.of(context).popUntil((route) => route.isFirst);
        _scrollToBottom();
      });
    }
    final saveError = tasks.storageError;
    final notice = tasks.backgroundNotice;
    if (saveError != null && saveError != _lastStorageError) {
      _showNotice(saveError);
    }
    if (notice != null && notice != _lastBackgroundNotice) _showNotice(notice);
    _lastStorageError = saveError;
    _lastBackgroundNotice = notice;
    setState(() {});
  }

  void _showNotice(String text) {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (mounted) {
        ScaffoldMessenger.of(context)
            .showSnackBar(SnackBar(content: Text(text)));
      }
    });
  }

  void _draftChanged(String value) => tasks.updateDraft(value);

  Future<void> _send([String? suggestion]) async {
    if (busy || active.pendingQuestion.isNotEmpty) return;
    final question = (suggestion ?? controller.text).trim();
    if (question.isEmpty) return;
    inputFocus.unfocus();
    final request = tasks.send(question);
    _scrollToBottom();
    await request;
  }

  Future<void> _retry() => tasks.retry();

  void _editPending() {
    if (busy) return;
    tasks.editPending();
    inputFocus.requestFocus();
  }

  void _newConversation() {
    if (!busy && active.isEmpty) {
      inputFocus.requestFocus();
      return;
    }
    tasks.newConversation();
  }

  void _switchTo(String id) {
    tasks.switchTo(id);
    inputFocus.unfocus();
  }

  void _scrollToBottom() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (mounted && scroll.hasClients) {
        scroll.jumpTo(scroll.position.maxScrollExtent);
      }
    });
  }

  void _showConversations() {
    inputFocus.unfocus();
    showModalBottomSheet<void>(
      context: context,
      backgroundColor: softBackground,
      showDragHandle: true,
      isScrollControlled: true,
      builder: (sheetContext) => StatefulBuilder(
        builder: (context, updateSheet) {
          final items = conversations.where((c) => !c.isEmpty).toList()
            ..sort((a, b) => b.updatedAt.compareTo(a.updatedAt));
          return SafeArea(
            child: SizedBox(
              height: MediaQuery.sizeOf(context).height * .7,
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  const Padding(
                    padding: EdgeInsets.fromLTRB(22, 4, 22, 20),
                    child: Text(
                      '我们的对话',
                      style: TextStyle(
                        fontSize: 23,
                        fontWeight: FontWeight.w700,
                      ),
                    ),
                  ),
                  Expanded(
                    child: items.isEmpty
                        ? const Center(
                            child: Text(
                              '聊过的话，会留在这里。',
                              style: TextStyle(color: Color(0xff7b847e)),
                            ),
                          )
                        : ListView.separated(
                            padding: const EdgeInsets.fromLTRB(16, 0, 16, 24),
                            itemCount: items.length,
                            separatorBuilder: (_, _) =>
                                const SizedBox(height: 8),
                            itemBuilder: (_, index) {
                              final item = items[index];
                              return ListTile(
                                key: ValueKey('conversation-${item.id}'),
                                selected: item.id == activeId,
                                shape: RoundedRectangleBorder(
                                  borderRadius: BorderRadius.circular(18),
                                ),
                                tileColor: const Color(0x77ffffff),
                                selectedTileColor: const Color(0xffdfe7e2),
                                title: Text(
                                  item.title,
                                  maxLines: 1,
                                  overflow: TextOverflow.ellipsis,
                                  style: const TextStyle(
                                    fontWeight: FontWeight.w600,
                                  ),
                                ),
                                subtitle: Text(
                                  item.preview,
                                  maxLines: 2,
                                  overflow: TextOverflow.ellipsis,
                                ),
                                onTap: () {
                                  Navigator.pop(sheetContext);
                                  _switchTo(item.id);
                                },
                                trailing: IconButton(
                                  key: ValueKey('delete-${item.id}'),
                                  tooltip: '删除对话',
                                  icon: const Icon(
                                    Icons.delete_outline_rounded,
                                    size: 21,
                                  ),
                                  onPressed: () async {
                                    final accepted = await showDialog<bool>(
                                      context: sheetContext,
                                      builder: (context) => AlertDialog(
                                        title: const Text('删除这段对话？'),
                                        content: const Text('这段问答会从手机中移除。'),
                                        actions: [
                                          TextButton(
                                            onPressed: () =>
                                                Navigator.pop(context, false),
                                            child: const Text('保留'),
                                          ),
                                          TextButton(
                                            onPressed: () =>
                                                Navigator.pop(context, true),
                                            child: const Text('删除'),
                                          ),
                                        ],
                                      ),
                                    );
                                    if (accepted != true ||
                                        !mounted ||
                                        !sheetContext.mounted) {
                                      return;
                                    }
                                    unawaited(
                                      tasks.deleteConversation(item.id),
                                    );
                                    updateSheet(() {});
                                  },
                                ),
                              );
                            },
                          ),
                  ),
                ],
              ),
            ),
          );
        },
      ),
    );
  }

  void _showEvidence(SavedQuestion turn) {
    showModalBottomSheet<void>(
      context: context,
      backgroundColor: softBackground,
      showDragHandle: true,
      isScrollControlled: true,
      builder: (context) => SafeArea(
        child: ConstrainedBox(
          constraints: BoxConstraints(
            maxHeight: MediaQuery.sizeOf(context).height * .75,
          ),
          child: SingleChildScrollView(
            padding: const EdgeInsets.fromLTRB(22, 4, 22, 24),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                const Text(
                  '为什么这样说',
                  style: TextStyle(fontSize: 22, fontWeight: FontWeight.w700),
                ),
                const SizedBox(height: 20),
                for (final evidence in turn.evidence)
                  _EvidenceTile(evidence: evidence),
              ],
            ),
          ),
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final ready = !loading && loadError == null;
    return Scaffold(
      backgroundColor: softBackground,
      appBar: AppBar(
        backgroundColor: softBackground,
        leading: IconButton(
          key: const ValueKey('question-history'),
          tooltip: '我们的对话',
          onPressed: ready ? _showConversations : null,
          icon: const Icon(Icons.forum_outlined, size: 23),
        ),
        centerTitle: true,
        title: const Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Text(
              '7u',
              style: TextStyle(
                fontSize: 25,
                fontWeight: FontWeight.w700,
                letterSpacing: -.8,
              ),
            ),
            AppVersion(),
          ],
        ),
        actions: [
          if (widget.notificationSettingsAction != null)
            widget.notificationSettingsAction!,
          IconButton(
            key: const ValueKey('new-conversation'),
            tooltip: '新的对话',
            onPressed: ready ? _newConversation : null,
            icon: const Icon(Icons.add_rounded, size: 27),
          ),
          const SizedBox(width: 8),
        ],
      ),
      body: loading
          ? const Center(
              child: SizedBox(
                width: 20,
                height: 20,
                child: CircularProgressIndicator(strokeWidth: 2),
              ),
            )
          : loadError != null
          ? Center(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Text(loadError!),
                  TextButton(
                    onPressed: tasks.initialize,
                    child: const Text('重试'),
                  ),
                ],
              ),
            )
          : LayoutBuilder(
              builder: (context, constraints) {
                final waitingHeight = busy
                    ? MediaQuery.textScalerOf(context).scale(12) * 1.4 + 4
                    : 0.0;
                final composerHeight = constraints.maxHeight - waitingHeight;
                return Column(
                  children: [
                    if (tasks.storageError != null)
                      Padding(
                        padding: const EdgeInsets.symmetric(horizontal: 20),
                        child: Row(
                          children: [
                            const Expanded(
                              child: Text(
                                '有内容尚未保存到本机',
                                style: TextStyle(
                                  fontSize: 12,
                                  color: Color(0xff7b756c),
                                ),
                              ),
                            ),
                            TextButton(
                              key: const ValueKey('retry-conversation-save'),
                              onPressed: tasks.save,
                              child: const Text('重试保存'),
                            ),
                          ],
                        ),
                      ),
                    Expanded(
                      child:
                          active.turns.isEmpty && active.pendingQuestion.isEmpty
                          ? _welcome()
                          : _timeline(),
                    ),
                    ConstrainedBox(
                      constraints: BoxConstraints(
                        maxHeight: constraints.maxHeight,
                      ),
                      child: SingleChildScrollView(
                        key: const ValueKey('conversation-composer-footer'),
                        primary: false,
                        reverse: true,
                        child: Column(
                          mainAxisSize: MainAxisSize.min,
                          children: [
                            if (busy) const _ReplyWaitingIndicator(),
                            _composer(composerHeight > 0 ? composerHeight : 0),
                          ],
                        ),
                      ),
                    ),
                  ],
                );
              },
            ),
    );
  }

  Widget _welcome() => ListView(
    key: const ValueKey('conversation-welcome'),
    padding: const EdgeInsets.fromLTRB(26, 56, 26, 30),
    children: [
      const Text(
        '关于你们，\n慢慢聊。',
        style: TextStyle(
          fontSize: 34,
          height: 1.45,
          fontWeight: FontWeight.w600,
          color: Color(0xff34463e),
          letterSpacing: -1,
        ),
      ),
      const SizedBox(height: 12),
      const Text(
        '想起什么，就从这里开始。',
        style: TextStyle(fontSize: 14, color: Color(0xff7d8982)),
      ),
      const SizedBox(height: 40),
      for (final question in suggestions)
        Padding(
          padding: const EdgeInsets.only(bottom: 12),
          child: TextButton(
            onPressed: () => _send(question),
            style: TextButton.styleFrom(
              backgroundColor: const Color(0x55ffffff),
              foregroundColor: const Color(0xff526b5f),
              padding: const EdgeInsets.symmetric(horizontal: 17, vertical: 16),
              shape: RoundedRectangleBorder(
                borderRadius: BorderRadius.circular(18),
              ),
            ),
            child: Row(
              children: [
                Expanded(
                  child: Text(
                    question,
                    style: const TextStyle(fontSize: 14, height: 1.5),
                  ),
                ),
                const SizedBox(width: 12),
                const Icon(Icons.north_east_rounded, size: 16),
              ],
            ),
          ),
        ),
    ],
  );

  Widget _timeline() => ListView.builder(
    key: ValueKey('timeline-$activeId'),
    controller: scroll,
    padding: const EdgeInsets.fromLTRB(20, 20, 20, 24),
    keyboardDismissBehavior: ScrollViewKeyboardDismissBehavior.onDrag,
    itemCount:
        active.turns.length * 2 + (active.pendingQuestion.isNotEmpty ? 2 : 0),
    itemBuilder: (context, index) {
      if (index < active.turns.length * 2) {
        final turn = active.turns[index ~/ 2];
        return index.isEven
            ? _userMessage(turn.question)
            : _assistantMessage(turn);
      }
      if (index.isEven) return _userMessage(active.pendingQuestion);
      if (busy) {
        // The live status stays next to the composer, also visible while
        // reading an earlier answer in this conversation.
        return const SizedBox.shrink();
      }
      return Padding(
        padding: const EdgeInsets.fromLTRB(4, 6, 0, 24),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              active.error.isEmpty ? '上一条还没有完成，可以继续重试。' : active.error,
              style: const TextStyle(color: Color(0xff7b756c), height: 1.6),
            ),
            Row(
              children: [
                TextButton(
                  key: const ValueKey('retry-message'),
                  onPressed: _retry,
                  child: const Text('重试'),
                ),
                TextButton(onPressed: _editPending, child: const Text('修改问题')),
              ],
            ),
          ],
        ),
      );
    },
  );

  Widget _userMessage(String text) => Padding(
    padding: const EdgeInsets.only(left: 36, bottom: 20, top: 6),
    child: Align(
      alignment: Alignment.centerRight,
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 18, vertical: 13),
        decoration: BoxDecoration(
          color: const Color(0xffdce5de),
          borderRadius: BorderRadius.circular(21)
              .copyWith(bottomRight: const Radius.circular(6)),
        ),
        child: SelectableText(
          text,
          style: const TextStyle(
            fontSize: 16,
            height: 1.65,
            color: Color(0xff344b3d),
          ),
        ),
      ),
    ),
  );

  Widget _assistantMessage(SavedQuestion turn) => Padding(
    padding: const EdgeInsets.fromLTRB(4, 0, 8, 30),
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          '7u · ${turn.appVersion == null ? '早期版本' : 'v${turn.appVersion}'}',
          style: const TextStyle(
            fontSize: 13,
            fontWeight: FontWeight.w700,
            color: Color(0xff7b9182),
          ),
        ),
        const SizedBox(height: 10),
        SelectableText(
          turn.body,
          style: const TextStyle(
            fontSize: 16,
            height: 1.85,
            color: Color(0xff343d38),
          ),
        ),
        Align(
          alignment: Alignment.centerLeft,
          child: IconButton(
            key: ValueKey(
              'copy-answer-${turn.createdAt.microsecondsSinceEpoch}',
            ),
            tooltip: '复制完整回答',
            icon: const Icon(Icons.content_copy_rounded, size: 16),
            color: const Color(0xff75887b),
            onPressed: () async {
              try {
                await Clipboard.setData(ClipboardData(text: turn.body));
                if (mounted) _showNotice('已复制');
              } catch (_) {
                if (mounted) _showNotice('暂时未能复制，请重试。');
              }
            },
          ),
        ),
        if (turn.evidence.isNotEmpty)
          Padding(
            padding: const EdgeInsets.only(top: 8),
            child: TextButton.icon(
              key: const ValueKey('show-question-evidence'),
              onPressed: () => _showEvidence(turn),
              icon: const Icon(Icons.bookmark_border_rounded, size: 16),
              label: const Text('为什么这样说', style: TextStyle(fontSize: 12)),
              style: TextButton.styleFrom(
                foregroundColor: const Color(0xff75887b),
                padding: EdgeInsets.zero,
              ),
            ),
          ),
      ],
    ),
  );

  Widget _composer(double availableHeight) {
    final blocked = busy || active.pendingQuestion.isNotEmpty;
    final safeBottom = MediaQuery.paddingOf(context).bottom;
    final verticalPadding = 8 + (safeBottom > 14 ? safeBottom : 14) + 10 + 22;
    final lineHeight = MediaQuery.textScalerOf(context).scale(16) * 1.5;
    // Shrink the text viewport when the landscape keyboard leaves little space.
    // The entire draft remains in the controller and can scroll inside the field.
    final visibleLines = ((availableHeight - verticalPadding - 48) / lineHeight)
        .floor()
        .clamp(1, 5);
    return SafeArea(
      top: false,
      minimum: const EdgeInsets.fromLTRB(16, 8, 16, 14),
      child: SoftPanel(
        padding: const EdgeInsets.fromLTRB(8, 5, 7, 5),
        borderRadius: BorderRadius.circular(26),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.end,
          children: [
            Expanded(
              child: ConstrainedBox(
                constraints: BoxConstraints(
                  maxHeight: visibleLines * lineHeight + 22,
                ),
                child: TextField(
                  key: const ValueKey('question-input'),
                  controller: controller,
                  focusNode: inputFocus,
                  enabled: !blocked,
                  minLines: 1,
                  maxLines: 5,
                  maxLength: 500,
                  keyboardType: TextInputType.multiline,
                  textInputAction: TextInputAction.newline,
                  style: const TextStyle(fontSize: 16, height: 1.5),
                  decoration: InputDecoration(
                    hintText: active.turns.isEmpty ? '想聊点什么…' : '接着聊…',
                    hintStyle: const TextStyle(color: Color(0xff919c95)),
                    counterText: '',
                    filled: false,
                    border: InputBorder.none,
                    enabledBorder: InputBorder.none,
                    focusedBorder: InputBorder.none,
                    contentPadding: const EdgeInsets.fromLTRB(12, 11, 8, 11),
                  ),
                  onChanged: _draftChanged,
                ),
              ),
            ),
            Padding(
              padding: const EdgeInsets.only(bottom: 3),
              child: IconButton.filled(
                key: const ValueKey('ask-seven-u'),
                tooltip: '发送',
                onPressed: blocked || controller.text.trim().isEmpty
                    ? null
                    : () => _send(),
                style: IconButton.styleFrom(
                  backgroundColor: const Color(0xff647f6e),
                  foregroundColor: Colors.white,
                  disabledBackgroundColor: const Color(0xffdde3df),
                  disabledForegroundColor: const Color(0xffabb6ae),
                  minimumSize: const Size(42, 42),
                ),
                icon: const Icon(Icons.arrow_upward_rounded, size: 22),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _ReplyWaitingIndicator extends StatefulWidget {
  const _ReplyWaitingIndicator();

  @override
  State<_ReplyWaitingIndicator> createState() => _ReplyWaitingIndicatorState();
}

class _ReplyWaitingIndicatorState extends State<_ReplyWaitingIndicator>
    with SingleTickerProviderStateMixin {
  late final AnimationController pulse = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 1500),
  );
  bool reducedMotion = false;

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    final media = MediaQuery.of(context);
    reducedMotion = media.disableAnimations || media.accessibleNavigation;
    if (reducedMotion) {
      pulse.stop();
    } else if (!pulse.isAnimating) {
      pulse.repeat();
    }
  }

  @override
  void dispose() {
    pulse.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => Padding(
    padding: const EdgeInsets.fromLTRB(25, 2, 25, 2),
    child: Semantics(
      key: const ValueKey('reply-waiting-indicator'),
      liveRegion: true,
      label: '7u 正在回复',
      child: ExcludeSemantics(
        child: AnimatedBuilder(
          animation: pulse,
          builder: (context, child) => Row(
            children: [
              for (var index = 0; index < 3; index++) _dot(index),
              const SizedBox(width: 5),
              const Flexible(
                child: Text(
                  '7u 正在想…',
                  style: TextStyle(
                    fontSize: 12,
                    height: 1.4,
                    color: Color(0xff74867b),
                  ),
                ),
              ),
            ],
          ),
        ),
      ),
    ),
  );

  Widget _dot(int index) {
    final progress =
        (math.sin((pulse.value - index * .17) * math.pi * 2) + 1) / 2;
    return Opacity(
      key: ValueKey('reply-waiting-dot-$index'),
      opacity: reducedMotion ? 1 : .4 + progress * .6,
      child: Transform.translate(
        offset: Offset(0, reducedMotion ? 0 : -progress * 2),
        child: Container(
          width: 4,
          height: 4,
          margin: const EdgeInsets.only(right: 3),
          decoration: BoxDecoration(
            color: const [
              Color(0xff7b9182),
              Color(0xff9aaa9e),
              Color(0xffb6c1b9),
            ][index],
            shape: BoxShape.circle,
          ),
        ),
      ),
    );
  }
}

class _EvidenceTile extends StatelessWidget {
  final QuestionEvidence evidence;
  const _EvidenceTile({required this.evidence});

  @override
  Widget build(BuildContext context) {
    final speaker = evidence.speaker == 'self'
        ? '你'
        : evidence.speaker == 'partner'
        ? 'TA'
        : '记录';
    final source = evidence.sourceType == 'voice_transcript' ? '语音' : '文字';
    final rawDate = evidence.date;
    final date = rawDate == null
        ? ''
        : rawDate.replaceFirst('T', ' ').split('+').first;
    return Container(
      margin: const EdgeInsets.only(bottom: 12),
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: const Color(0x88ffffff),
        borderRadius: BorderRadius.circular(18),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            [
              if (date.isNotEmpty) date.substring(0, date.length.clamp(0, 16)),
              speaker,
              source,
            ].join(' · '),
            style: const TextStyle(color: Color(0xff7b887e), fontSize: 12),
          ),
          const SizedBox(height: 8),
          SelectableText(
            '“${evidence.quote}”',
            style: const TextStyle(height: 1.7, fontSize: 15),
          ),
          if (evidence.msgId != null) ...[
            const SizedBox(height: 10),
            SelectableText(
              '消息 ID ${evidence.msgId}',
              style: const TextStyle(fontSize: 11, color: Color(0xff697970)),
            ),
          ],
        ],
      ),
    );
  }
}
