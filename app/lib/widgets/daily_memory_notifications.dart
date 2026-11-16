import 'dart:async';

import 'package:flutter/material.dart';

import '../core/conversation_controller.dart';
import '../core/daily_memories.dart';
import 'soft_ui.dart';

/// Lives below MaterialApp so cold and warm notification taps share one route.
class DailyMemoryNotifications extends StatefulWidget {
  final ConversationController conversations;
  final Widget child;
  final DailyMemories? daily;

  const DailyMemoryNotifications({
    super.key,
    required this.conversations,
    required this.child,
    this.daily,
  });

  @override
  State<DailyMemoryNotifications> createState() =>
      _DailyMemoryNotificationsState();
}

class _DailyMemoryNotificationsState extends State<DailyMemoryNotifications>
    with WidgetsBindingObserver {
  late final daily = widget.daily ?? DailyMemories();
  StreamSubscription<void>? subscription;
  final pending = <DailyMemory>[];
  Future<void> reading = Future.value();
  bool presenting = false;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    subscription = daily.events.listen((_) => _readLaunch());
    _readLaunch();
  }

  void _readLaunch() {
    reading = reading.then((_) async {
      try {
        while (mounted) {
          final memory = await daily.takeLaunchMemory();
          if (memory == null || !mounted) break;
          if (!pending.any((m) => m.id == memory.id)) pending.add(memory);
        }
      } catch (_) {
        // A missing/deleted pool item must not prevent access to conversations.
      }
      _presentNext();
    });
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) _presentNext();
  }

  void _presentNext() {
    if (!mounted || presenting || pending.isEmpty) return;
    final lifecycle = WidgetsBinding.instance.lifecycleState;
    if (lifecycle != null && lifecycle != AppLifecycleState.resumed) return;
    presenting = true;
    WidgetsBinding.instance.addPostFrameCallback((_) async {
      if (!mounted) return;
      final memory = pending.removeAt(0);
      await showModalBottomSheet<void>(
        context: context,
        showDragHandle: true,
        isScrollControlled: true,
        backgroundColor: softBackground,
        builder: (sheetContext) => SafeArea(
          child: SingleChildScrollView(
            padding: const EdgeInsets.fromLTRB(24, 4, 24, 28),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              mainAxisSize: MainAxisSize.min,
              children: [
                const Text(
                  '今天的小片段',
                  style: TextStyle(fontSize: 21, fontWeight: FontWeight.w600),
                ),
                const SizedBox(height: 20),
                SelectableText(
                  memory.text,
                  style: const TextStyle(fontSize: 20, height: 1.6),
                ),
                const SizedBox(height: 24),
                Text(
                  '${memory.date} · ${memory.speaker == 'self' ? '你' : 'TA'}的原话',
                  style: const TextStyle(color: Color(0xff7b847e)),
                ),
                const SizedBox(height: 8),
                SelectableText(
                  '“${memory.quote}”',
                  style: const TextStyle(height: 1.6),
                ),
                const SizedBox(height: 24),
                FilledButton.tonal(
                  onPressed: () async {
                    await widget.conversations.initialize();
                    if (!mounted ||
                        !sheetContext.mounted ||
                        !widget.conversations.ready) {
                      return;
                    }
                    // Never overwrite a draft or automatically send a billable question.
                    widget.conversations.newConversation();
                    widget.conversations.updateDraft(memory.question);
                    Navigator.pop(sheetContext);
                  },
                  child: const Text('接着聊聊'),
                ),
              ],
            ),
          ),
        ),
      );
      presenting = false;
      _presentNext();
    });
    WidgetsBinding.instance.ensureVisualUpdate();
  }

  Future<void> openSettings() => showModalBottomSheet<void>(
    context: context,
    showDragHandle: true,
    isScrollControlled: true,
    backgroundColor: softBackground,
    builder: (_) => _DailySettingsSheet(daily: daily),
  );

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    unawaited(subscription?.cancel());
    if (widget.daily == null) daily.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) =>
      _DailyScope(state: this, child: widget.child);
}

class _DailyScope extends InheritedWidget {
  final _DailyMemoryNotificationsState state;
  const _DailyScope({required this.state, required super.child});
  @override
  bool updateShouldNotify(_DailyScope oldWidget) => oldWidget.state != state;
}

class DailyMemorySettingsButton extends StatelessWidget {
  const DailyMemorySettingsButton({super.key});
  @override
  Widget build(BuildContext context) {
    final owner = context
        .dependOnInheritedWidgetOfExactType<_DailyScope>()
        ?.state;
    if (owner == null || !owner.daily.supported) return const SizedBox.shrink();
    return IconButton(
      key: const ValueKey('notification-settings'),
      tooltip: '通知设置',
      onPressed: owner.openSettings,
      icon: const Icon(Icons.notifications_none_rounded),
    );
  }
}

class _DailySettingsSheet extends StatefulWidget {
  final DailyMemories daily;
  const _DailySettingsSheet({required this.daily});
  @override
  State<_DailySettingsSheet> createState() => _DailySettingsSheetState();
}

class _DailySettingsSheetState extends State<_DailySettingsSheet>
    with WidgetsBindingObserver {
  DailyMemorySettings? settings;
  bool working = false;
  String? error;
  int revision = 0;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    unawaited(_refresh());
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed && !working) unawaited(_refresh());
  }

  Future<void> _refresh() async {
    final requestRevision = ++revision;
    try {
      final value = await widget.daily.getSettings();
      if (mounted && requestRevision == revision) {
        setState(() {
          settings = value;
          error = null;
        });
      }
    } catch (_) {
      if (mounted && requestRevision == revision) {
        setState(() => error = '通知设置暂时无法读取，请重试。');
      }
    }
  }

  Future<void> _change(DailyMemorySettings next) async {
    if (working) return;
    final requestRevision = ++revision;
    setState(() => working = true);
    try {
      final saved = await widget.daily.configure(next);
      if (mounted && requestRevision == revision) {
        setState(() {
          settings = saved;
          error = null;
        });
      }
    } catch (_) {
      if (mounted && requestRevision == revision) {
        setState(() => error = '设置未保存，请重试。');
      }
    } finally {
      if (mounted) setState(() => working = false);
    }
  }

  Future<void> _permission() async {
    setState(() => working = true);
    try {
      await widget.daily.requestPermission();
      await _refresh();
    } catch (_) {
      if (mounted) setState(() => error = '请在系统通知设置里允许 7u 通知。');
    } finally {
      if (mounted) setState(() => working = false);
    }
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final value = settings;
    return SafeArea(
      child: SingleChildScrollView(
        padding: const EdgeInsets.fromLTRB(20, 4, 20, 24),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            const Text(
              '通知',
              style: TextStyle(fontSize: 22, fontWeight: FontWeight.w600),
            ),
            const SizedBox(height: 12),
            if (value == null && error == null)
              const Center(child: CircularProgressIndicator()),
            if (value != null) ...[
              SwitchListTile.adaptive(
                contentPadding: EdgeInsets.zero,
                title: const Text('每天的小片段'),
                subtitle: const Text('随机送来一个小问题、一句陈述或一段回忆'),
                value: value.enabled,
                onChanged: working
                    ? null
                    : (enabled) => _change(value.copyWith(enabled: enabled)),
              ),
              ListTile(
                contentPadding: EdgeInsets.zero,
                title: const Text('提醒时间 · 北京时间'),
                trailing: Text(
                  '${value.hour.toString().padLeft(2, '0')}:${value.minute.toString().padLeft(2, '0')}',
                ),
                enabled: value.enabled && !working,
                onTap: () async {
                  final selected = await showTimePicker(
                    context: context,
                    initialTime: TimeOfDay(
                      hour: value.hour,
                      minute: value.minute,
                    ),
                    builder: (context, child) => MediaQuery(
                      data: MediaQuery.of(context)
                          .copyWith(alwaysUse24HourFormat: true),
                      child: child!,
                    ),
                  );
                  if (selected != null && mounted) {
                    await _change(
                      value.copyWith(
                        hour: selected.hour,
                        minute: selected.minute,
                      ),
                    );
                  }
                },
              ),
              SwitchListTile.adaptive(
                contentPadding: EdgeInsets.zero,
                title: const Text('锁屏显示内容'),
                value: !value.privateOnLockScreen,
                onChanged: working
                    ? null
                    : (preview) => _change(
                        value.copyWith(privateOnLockScreen: !preview),
                      ),
              ),
              const SizedBox(height: 8),
              const Text(
                '无需联网。省电或系统限制可能使提醒稍晚送达。回答完成通知仍由系统通知设置单独管理。',
                style: TextStyle(
                  fontSize: 13,
                  height: 1.5,
                  color: Color(0xff7b847e),
                ),
              ),
              if (!value.notificationsEnabled) ...[
                const SizedBox(height: 14),
                const Text(
                  '尚未允许通知，或每日提醒分类已被关闭。问答仍可继续运行。',
                  style: TextStyle(fontSize: 13),
                ),
                TextButton(
                  onPressed: working ? null : _permission,
                  child: const Text('允许通知'),
                ),
              ],
              TextButton(
                onPressed: working
                    ? null
                    : () async {
                        try {
                          await widget.daily.openSystemSettings();
                        } catch (_) {
                          if (mounted) {
                            setState(() => error = '请在手机设置中打开 7u 的通知设置。');
                          }
                        }
                      },
                child: const Text('系统通知设置'),
              ),
            ],
            if (error != null) ...[
              Text(
                error!,
                style: TextStyle(color: Theme.of(context).colorScheme.error),
              ),
              if (value == null)
                TextButton(onPressed: _refresh, child: const Text('重试')),
            ],
          ],
        ),
      ),
    );
  }
}
