import 'dart:convert';

import 'package:couple_app/core/memory_pack.dart';
import 'package:couple_app/core/model_client.dart';
import 'package:couple_app/core/question_agent.dart';
import 'package:couple_app/core/question_history.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:sqflite_common_ffi/sqflite_ffi.dart';

void main() {
  sqfliteFfiInit();
  databaseFactory = databaseFactoryFfi;

  for (final (question, speaker) in [
    ('我最近对他说的话是什么', 'self'),
    ('他最近对我说的话是什么', 'partner'),
    ('他说我是什么样的人', 'partner'),
    ('我以前说他哪里好', 'self'),
  ]) {
    test(
      'speaker is chosen by model, not pronoun matching: $question',
      () async {
        final memory = await _memory();
        var calls = 0;
        final agent = _agent(memory, (request) async {
          calls++;
          if (calls == 1) return _search(speaker: speaker);
          return _answer(speaker);
        });
        final result = await agent.ask(question, history: [_history]);
        expect(calls, 2);
        expect(result['status'], 'done');
        expect(
          ((result['answer'] as Map)['quotes'] as List).single,
          containsPair('speaker', speaker),
        );
      },
    );
  }

  test('exact two-question sequence accepts her statements to him', () async {
    final memory = await _memory();
    var calls = 0;
    final agent = _agent(memory, (_) async {
      calls++;
      if (calls.isOdd) return _search();
      return calls == 2 ? _answer('partner') : _answer('self');
    });
    const question = '最近有什么值得纪念的事情';
    final first = SavedQuestion.fromResult(question, await agent.ask(question));
    final second = await agent.ask('我最近对他说的话是什么', history: [first]);
    expect(calls, 4);
    expect(
      ((second['answer'] as Map)['quotes'] as List).single,
      containsPair('speaker', 'self'),
    );
    expect(second['fallback_reason'], isNull);
  });

  test(
    'mismatched speaker is repaired, rather than trusting the model label',
    () async {
      final memory = await _memory();
      var calls = 0;
      final agent = _agent(memory, (request) async {
        calls++;
        if (calls == 1) return _search();
        if (calls == 2) return _answer('self', claimedSpeaker: 'partner');
        expect(jsonEncode(request), contains('speaker'));
        return _answer('self');
      });
      final result = await agent.ask('我对他说过什么');
      expect(calls, 3);
      expect(
        ((result['answer'] as Map)['quotes'] as List).single,
        containsPair('speaker', 'self'),
      );
    },
  );

  test(
    'invalid generation exhaustion is retryable, not a made-up lack of records',
    () async {
      final memory = await _memory();
      var calls = 0;
      final agent = _agent(memory, (_) async {
        calls++;
        return {'content': '{broken'};
      });
      await expectLater(
        agent.ask('说短一点', history: [_history]),
        throwsA(isA<ModelException>()),
      );
      expect(calls, QuestionAgent.maxModelCalls);
    },
  );

  test(
    'mixed speakers remain valid context even for a question about him',
    () async {
      final memory = await _memory();
      var calls = 0;
      final agent = _agent(memory, (_) async {
        if (++calls == 1) return _search();
        final value = jsonDecode(_answer('self')['content'] as String) as Map;
        (value['items'][0]['evidence'] as List).add({
          'msg_id': '2',
          'speaker': 'partner',
          'quote': '一起去散步',
        });
        return {'content': jsonEncode(value)};
      });
      final result = await agent.ask('他如何回应我的邀请');
      expect(((result['answer'] as Map)['quotes'] as List).length, 2);
      expect(calls, 2);
    },
  );

  test(
    'each turn has a fresh full retrieval budget, including followups',
    () async {
      final memory = await _memory();
      var currentCalls = 0;
      final agent = _agent(memory, (request) async {
        currentCalls++;
        if (currentCalls <= QuestionAgent.maxRetrievalRounds) {
          expect(request['tools'], isNotEmpty);
          return _search();
        }
        expect(request.containsKey('tools'), isFalse);
        return _answer('self');
      });
      final first = SavedQuestion.fromResult(
        '最近有什么值得纪念的事情',
        await agent.ask('最近有什么值得纪念的事情'),
      );
      expect(currentCalls, QuestionAgent.maxRetrievalRounds + 1);
      currentCalls = 0;
      await agent.ask('我最近对他说的话是什么', history: [first]);
      expect(currentCalls, QuestionAgent.maxRetrievalRounds + 1);
    },
  );

  test(
    'model can answer general questions without forced private retrieval',
    () async {
      final memory = await _memory();
      var calls = 0;
      final agent = _agent(memory, (_) async {
        calls++;
        return {
          'content': jsonEncode({
            'status': 'ok',
            'items': [
              {'title': '写信', 'body': '可以从一件印象深刻的小事写起。', 'evidence': []},
            ],
          }),
        };
      });
      await agent.ask('如何给他写一封信？先讲通用的方法，不用查我们的记录');
      expect(calls, 1);
    },
  );
}

final _history = SavedQuestion(
  question: '最近有什么值得纪念的事情',
  title: '一起走走',
  body: '聊过一起散步。',
  evidence: const [],
  createdAt: DateTime(2026, 9, 27),
);

Future<MemoryPack> _memory() async {
  final db = await databaseFactoryFfi.openDatabase(inMemoryDatabasePath);
  addTearDown(db.close);
  await db.execute(
    'CREATE TABLE messages(msg_id INTEGER PRIMARY KEY,ts INTEGER,speaker TEXT,text TEXT,source_type TEXT,transcript_origin TEXT)',
  );
  await db.execute('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT)');
  for (final (id, speaker, text) in [
    (1, 'self', '今晚想和你散步'),
    (2, 'partner', '一起去散步'),
  ]) {
    await db.insert('messages', {
      'msg_id': id,
      'ts': 1790480000 + id,
      'speaker': speaker,
      'text': text,
      'source_type': 'message_text',
    });
  }
  return MemoryPack.forDatabase(db);
}

QuestionAgent _agent(
  MemoryPack memory,
  Future<Map<String, Object?>> Function(Map<String, Object?>) transport,
) => QuestionAgent(
  memory: memory,
  model: ModelClient(transport: transport),
  promptLoader: () async => '固定视角：self=她，partner=他。',
);

Map<String, Object?> _search({String? speaker}) => {
  'content': null,
  'tool_calls': [
    {
      'id': 'lookup',
      'type': 'function',
      'function': {
        'name': 'search_chats',
        'arguments': jsonEncode({'speaker': ?speaker}),
      },
    },
  ],
};

Map<String, Object?> _answer(String speaker, {String? claimedSpeaker}) => {
  'content': jsonEncode({
    'status': 'ok',
    'items': [
      {
        'title': '散步',
        'body': speaker == 'self' ? '你提到今晚想和TA散步。' : 'TA说一起去散步。',
        'evidence': [
          {
            'msg_id': speaker == 'self' ? '1' : '2',
            'speaker': claimedSpeaker ?? speaker,
            'quote': speaker == 'self' ? '今晚想和你散步' : '一起去散步',
          },
        ],
      },
    ],
  }),
};
