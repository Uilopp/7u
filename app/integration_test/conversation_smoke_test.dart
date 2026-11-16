import 'dart:convert';

import 'package:couple_app/core/conversations.dart';
import 'package:couple_app/core/app_version.dart';
import 'package:couple_app/core/local_store.dart';
import 'package:couple_app/core/memory_pack.dart';
import 'package:couple_app/core/model_client.dart';
import 'package:couple_app/core/question_agent.dart';
import 'package:couple_app/core/question_cancellation.dart';
import 'package:couple_app/core/question_history.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:integration_test/integration_test.dart';
import 'package:sqflite/sqflite.dart';

class ObservedModel extends ModelClient {
  final inputs = <List<Map<String, Object?>>>[];

  @override
  Future<Map<String, Object?>> complete(
    List<Map<String, Object?>> messages, {
    List<Map<String, Object?>>? tools,
    QuestionCancellation? cancellation,
  }) {
    inputs.add(
      (jsonDecode(jsonEncode(messages)) as List)
          .map((row) => Map<String, Object?>.from(row as Map))
          .toList(),
    );
    return super.complete(messages, tools: tools, cancellation: cancellation);
  }
}

void main() {
  IntegrationTestWidgetsFlutterBinding.ensureInitialized();

  testWidgets(
    'real APK memory + DeepSeek + persisted followup',
    (tester) async {
      final memory = MemoryPack();
      final model = ObservedModel();
      final agent = QuestionAgent(memory: memory, model: model);
      const expectedArchiveDate = String.fromEnvironment(
        'EXPECTED_ARCHIVE_DATE',
      );
      final latestArchiveDate = await memory.latestMessageDate();
      if (expectedArchiveDate.isNotEmpty) {
        expect(latestArchiveDate, startsWith(expectedArchiveDate));
      }
      const firstQuestion = '他平时喜欢聊些什么？挑两个有记录支持的话题就好。';
      final watch = Stopwatch()..start();
      final first = await agent.ask(firstQuestion);
      expect(first['fallback_reason'], isNull);
      final saved = SavedQuestion.fromResult(
        firstQuestion,
        first,
        appVersion: await readInstalledVersion(),
      );
      expect(saved.appVersion, isNotNull);
      expect(saved.body.trim(), isNotEmpty);
      expect(saved.evidence, isNotEmpty);
      for (final evidence in saved.evidence) {
        expect(evidence.speaker, 'partner');
        final source = await memory.messageById(evidence.msgId!);
        expect(source!.text.contains(evidence.quote), isTrue);
      }
      final firstMs = watch.elapsedMilliseconds;
      const databaseName = 'seven_u_conversation_smoke.db';
      final local = LocalStore(databaseName: databaseName);
      final repository = ConversationRepository(local);
      addTearDown(() async {
        await (await local.database).close();
        await deleteDatabase(databaseName);
        await (await memory.database).close();
      });
      final conversation = Conversation.create().copyWith(turns: [saved]);
      await repository.save([conversation], conversation.id);
      final restored = await ConversationRepository(local).load();
      expect(
        restored.conversations.single.turns.single.appVersion,
        saved.appVersion,
      );
      final priorCalls = model.inputs.length;
      watch.reset();
      final second = await agent.ask(
        '请把刚才的回答压缩成两句话，不增加新的信息。',
        history: restored.conversations.single.turns,
      );
      expect(second['fallback_reason'], isNull);
      expect((second['answer'] as Map)['body'], isNotEmpty);
      final input = model.inputs[priorCalls];
      expect(
        input.where((m) => m['role'] == 'user').map((m) => m['content']),
        contains(firstQuestion),
      );
      final previousReply = jsonDecode(
        input.firstWhere((m) => m['role'] == 'assistant')['content'] as String,
      ) as Map;
      expect((previousReply['items'] as List).single['body'], saved.body);
      String? freshDateAnswer;
      if (expectedArchiveDate.isNotEmpty) {
        final question = '$expectedArchiveDate 这天我们聊了些什么？只依据当天记录，区分是谁说的。';
        final fresh = await agent.ask(question);
        expect(fresh['fallback_reason'], isNull);
        final day = SavedQuestion.fromResult(question, fresh);
        expect(day.evidence, isNotEmpty);
        for (final evidence in day.evidence) {
          final source = await memory.messageById(evidence.msgId!);
          expect(source, isNotNull);
          expect(
            source!.toModelJson()['date'],
            startsWith(expectedArchiveDate),
          );
          expect(evidence.speaker, source.speaker);
          expect(source.text.contains(evidence.quote), isTrue);
        }
        freshDateAnswer = day.body;
      }
      debugPrint(
        'LIVE_CONVERSATION_RESULT ${jsonEncode({'first_answer': saved.body, 'followup_answer': (second['answer'] as Map)['body'], 'archive_latest': latestArchiveDate, 'fresh_date_answer': freshDateAnswer, 'first_ms': firstMs, 'first_model_calls': priorCalls, 'total_model_calls': model.inputs.length, 'evidence_count': saved.evidence.length})}',
      );
    },
    skip: !const bool.fromEnvironment('LIVE_QUESTION_TEST'),
    timeout: const Timeout(Duration(minutes: 8)),
  );
}
