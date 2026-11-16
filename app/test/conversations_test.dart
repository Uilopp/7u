import 'package:couple_app/core/conversations.dart';
import 'package:couple_app/core/local_store.dart';
import 'package:couple_app/core/question_history.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:sqflite_common_ffi/sqflite_ffi.dart';

void main() {
  sqfliteFfiInit();
  databaseFactory = databaseFactoryFfi;

  test(
    'persist pending questions, drafts, exact evidence and active conversation',
    () async {
      final local = LocalStore(databaseName: inMemoryDatabasePath);
      final repository = ConversationRepository(local);
      final first = Conversation.create().copyWith(
        turns: [
          SavedQuestion(
            question: '他呢',
            title: '回答',
            body: '原话有提到。',
            createdAt: DateTime.now(),
            appVersion: '1.0.260927',
            evidence: const [
              QuestionEvidence(
                msgId: '8000000000000000123',
                speaker: 'partner',
                quote: '原话',
                sourceType: 'voice_transcript',
              ),
            ],
          ),
        ],
      );
      final second = Conversation.create().copyWith(
        pendingQuestion: '还没回来',
        draft: '',
        error: '连接断开',
      );
      await repository.save([first, second], second.id);
      final restored = await ConversationRepository(local).load();
      expect(restored.activeId, second.id);
      expect(
        restored.conversations.first.turns.single.appVersion,
        '1.0.260927',
      );
      expect(restored.conversations.last.pendingQuestion, '还没回来');
      expect(
        restored.conversations.first.turns.single.evidence.single.msgId,
        '8000000000000000123',
      );
      expect(
        restored.conversations.first.turns.single.evidence.single.sourceType,
        'voice_transcript',
      );
      // Rapid edits and deletion are serialized, so an older save cannot win.
      final writes = [
        repository.save([first, second.copyWith(draft: '第一次输入')], second.id),
        repository.save([first, second.copyWith(draft: '第二次输入')], second.id),
        repository.save([first], first.id),
      ];
      await Future.wait(writes);
      expect((await repository.load()).conversations.map((c) => c.id), [
        first.id,
      ]);
      await (await local.database).close();
    },
  );

  test(
    'legacy single answers migrate once and cannot return after deletion',
    () async {
      final local = LocalStore(databaseName: inMemoryDatabasePath);
      await local.saveMetadata('question_history_v1', {
        'items': [
          SavedQuestion(
            question: '旧问题',
            title: '旧标题',
            body: '旧答案',
            evidence: const [],
            createdAt: DateTime.now(),
          ).toJson(),
        ],
      });
      final repository = ConversationRepository(local);
      final state = await repository.load();
      expect(state.conversations.single.turns.single.body, '旧答案');
      expect(state.conversations.single.turns.single.appVersion, isNull);
      await repository.save([], null);
      expect(
        (await ConversationRepository(local).load()).conversations,
        isEmpty,
      );
      await (await local.database).close();
    },
  );
}
