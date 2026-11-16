import 'dart:math';

import 'local_store.dart';
import 'question_history.dart';

/// A conversation is stored on this device; it never modifies the QQ archive.
class Conversation {
  final String id;
  final DateTime createdAt;
  final DateTime updatedAt;
  final List<SavedQuestion> turns;
  final String draft;
  final String pendingQuestion;
  final String error;

  Conversation({
    required this.id,
    required this.createdAt,
    required this.updatedAt,
    List<SavedQuestion> turns = const [],
    this.draft = '',
    this.pendingQuestion = '',
    this.error = '',
  }) : turns = List.unmodifiable(turns);

  factory Conversation.create() {
    final now = DateTime.now();
    return Conversation(
      id: '${now.microsecondsSinceEpoch}-${Random.secure().nextInt(1 << 32)}',
      createdAt: now,
      updatedAt: now,
    );
  }

  bool get isEmpty => turns.isEmpty && pendingQuestion.isEmpty && draft.isEmpty;

  String get title {
    final text = turns.isNotEmpty ? turns.first.question : pendingQuestion;
    if (text.isEmpty) return draft.isEmpty ? '新的对话' : '还没说完的话';
    return text.replaceAll(RegExp(r'\s+'), ' ').trim();
  }

  String get preview => pendingQuestion.isNotEmpty
      ? pendingQuestion
      : turns.isNotEmpty
      ? turns.last.body
      : draft;

  Conversation copyWith({
    List<SavedQuestion>? turns,
    String? draft,
    String? pendingQuestion,
    String? error,
    DateTime? updatedAt,
  }) => Conversation(
    id: id,
    createdAt: createdAt,
    updatedAt: updatedAt ?? this.updatedAt,
    turns: turns ?? this.turns,
    draft: draft ?? this.draft,
    pendingQuestion: pendingQuestion ?? this.pendingQuestion,
    error: error ?? this.error,
  );

  factory Conversation.fromJson(Map<String, dynamic> value) => Conversation(
    id: value['id'] as String,
    createdAt: DateTime.parse(value['created_at'] as String),
    updatedAt: DateTime.parse(value['updated_at'] as String),
    turns: (value['turns'] as List? ?? const [])
        .whereType<Map>()
        .map((row) => SavedQuestion.fromJson(Map<String, dynamic>.from(row)))
        .toList(),
    draft: value['draft']?.toString() ?? '',
    pendingQuestion: value['pending_question']?.toString() ?? '',
    error: value['error']?.toString() ?? '',
  );

  Map<String, dynamic> toJson() => {
    'id': id,
    'created_at': createdAt.toIso8601String(),
    'updated_at': updatedAt.toIso8601String(),
    'turns': turns.map((turn) => turn.toJson()).toList(),
    'draft': draft,
    'pending_question': pendingQuestion,
    'error': error,
  };
}

class ConversationState {
  final List<Conversation> conversations;
  final String? activeId;

  const ConversationState(this.conversations, this.activeId);
}

class ConversationRepository {
  static const key = 'conversations_v1';
  final LocalStore local;
  Future<void> _writes = Future.value();

  ConversationRepository(this.local);

  Future<ConversationState> load() async {
    await _writes;
    final data = await local.loadMetadata(key);
    if (data != null) {
      final items = (data['conversations'] as List? ?? const [])
          .whereType<Map>()
          .map((row) => Conversation.fromJson(Map<String, dynamic>.from(row)))
          .toList();
      return ConversationState(items, data['active_id'] as String?);
    }
    // Keep already saved single answers as independent conversations.
    final legacy = await local.loadMetadata('question_history_v1');
    final previous = (legacy?['items'] as List? ?? const [])
        .whereType<Map>()
        .map((row) => SavedQuestion.fromJson(Map<String, dynamic>.from(row)))
        .where((item) => item.question.isNotEmpty && item.body.isNotEmpty)
        .toList();
    final items = [
      for (var i = 0; i < previous.length; i++)
        Conversation(
          id: 'import-${previous[i].createdAt.microsecondsSinceEpoch}-$i',
          createdAt: previous[i].createdAt,
          updatedAt: previous[i].createdAt,
          turns: [previous[i]],
        ),
    ];
    final activeId = items.isEmpty ? null : items.first.id;
    await save(items, activeId);
    return ConversationState(items, activeId);
  }

  Future<void> save(List<Conversation> conversations, String? activeId) {
    // Snapshot before queueing; a late save must not resurrect deleted chats.
    final payload = <String, dynamic>{
      'active_id': activeId,
      'conversations': conversations.map((item) => item.toJson()).toList(),
    };
    final write = _writes.then((_) => local.saveMetadata(key, payload));
    _writes = write.then<void>((_) {}, onError: (Object _, StackTrace _) {});
    return write;
  }
}
