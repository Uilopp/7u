import 'dart:convert';

import 'package:couple_app/core/memory_pack.dart';
import 'package:couple_app/core/model_client.dart';
import 'package:couple_app/core/question_agent.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:sqflite_common_ffi/sqflite_ffi.dart';

void main() {
  sqfliteFfiInit();
  databaseFactory = databaseFactoryFfi;
  late Database database;

  setUp(() async {
    database = await openDatabase(inMemoryDatabasePath);
    await database.execute('''CREATE TABLE messages(
      msg_id INTEGER PRIMARY KEY,ts INTEGER,speaker TEXT,text TEXT,
      source_type TEXT,transcript_origin TEXT)''');
    await database.execute(
      'CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT)',
    );
    await database.execute('''CREATE TABLE events(
      msg_id INTEGER PRIMARY KEY,ts INTEGER,speaker TEXT,kind TEXT,
      text_available INTEGER)''');
  });
  tearDown(() => database.close());

  Future<void> add(
    int id,
    String speaker,
    String text, {
    int ts = 1750000000,
  }) async {
    await database.insert('messages', {
      'msg_id': id,
      'ts': ts,
      'speaker': speaker,
      'text': text,
      'source_type': 'message_text',
    });
  }

  Future<void> addProfile() async {
    await add(9, 'self', '我们一起学做饭吧');
    await add(10, 'partner', '好啊，周末一起试试');
    await add(11, 'self', '你真有耐心，每次都会等我');
    await add(12, 'partner', '这句话没有给任何有效档案作证');
    await database.insert('metadata', {
      'key': 'profile',
      'value': jsonEncode({
        'status': 'available',
        'facts': [
          {
            'id': 'shared_plan',
            'subject': 'relationship',
            'claim': '两个人曾约好周末一起学做饭',
            'evidence': [
              {'msg_id': '9', 'speaker': 'partner', 'quote': '我们一起学做饭吧'},
              {'msg_id': '10', 'speaker': 'self', 'quote': '好啊，周末一起试试'},
            ],
          },
          {
            'id': 'described_partner',
            'subject': 'partner',
            'claim': '她曾形容他很有耐心',
            'evidence': [
              {'msg_id': '11', 'speaker': 'partner', 'quote': '你真有耐心'},
            ],
          },
          {
            'id': 'changed_quote',
            'subject': 'partner',
            'evidence': [
              {'msg_id': '12', 'quote': '不存在的原话'},
            ],
          },
          {
            'id': 'missing_id',
            'subject': 'relationship',
            'evidence': [
              {'msg_id': '999', 'quote': '不存在的消息'},
            ],
          },
          {
            'id': 'partly_invalid',
            'subject': 'relationship',
            'evidence': [
              {'msg_id': '12', 'quote': '这句话'},
              {'msg_id': '999', 'quote': '不存在的消息'},
            ],
          },
          {'id': 'no_evidence', 'subject': 'self', 'evidence': []},
        ],
      }),
    });
  }

  QuestionAgent agent(
    Future<Map<String, Object?>> Function(Map<String, Object?>) respond,
  ) => QuestionAgent(
    memory: MemoryPack.forDatabase(database),
    model: ModelClient(transport: respond),
    promptLoader: () async => '测试提示词',
  );

  test('profile accepts relationship and reported-person facts with real ownership', () async {
    await addProfile();
    var calls = 0;
    final questionAgent = agent((request) async {
      calls++;
      if (calls == 1) return _tools([_call('get_profile', {})]);
      if (calls == 2) {
        final profile = _replies(request).single;
        final facts = (profile['facts'] as List).cast<Map>();
        expect(facts.map((fact) => fact['id']), [
          'shared_plan',
          'described_partner',
        ]);
        expect(
          (facts.first['evidence'] as List).map(
            (raw) => (raw as Map)['speaker'],
          ),
          ['self', 'partner'],
        );
        expect(
          ((facts.last['evidence'] as List).single as Map)['speaker'],
          'self',
        );
        // A real row used only by an invalid fact must not become a visible source.
        return _tools([
          _call('get_context', {'msg_id': '12'}),
        ]);
      }
      expect(_replies(request).last['error'], contains('先在本次对话中检索'));
      return _answer();
    });
    expect((await questionAgent.ask('看看我们的档案'))['status'], 'done');
    expect(calls, 3);
  });

  test(
    'profile speaker filter selects fact subject without changing quote author',
    () async {
      await addProfile();
      var calls = 0;
      final questionAgent = agent((request) async {
        calls++;
        if (calls == 1) {
          return _tools([
            _call('get_profile', {'speaker': 'partner'}),
          ]);
        }
        final fact = (_replies(request).single['facts'] as List).single as Map;
        expect(fact['id'], 'described_partner');
        expect(fact['subject'], 'partner');
        final evidence = (fact['evidence'] as List).single as Map;
        expect(evidence['msg_id'], '11');
        expect(evidence['speaker'], 'self');
        expect(evidence['quote'], '你真有耐心');
        return _answer();
      });
      await questionAgent.ask('她以前是怎么形容他的');
      expect(calls, 2);
    },
  );

  test(
    'context packing preserves numeric ID order at an equal timestamp',
    () async {
      await add(9, 'self', '前一句');
      await add(10, 'partner', '锚点');
      await add(11, 'self', '后一句');
      var calls = 0;
      final questionAgent = agent((request) async {
        calls++;
        if (calls == 1) {
          return _tools([
            _call('search_chats', {'keyword': '锚点'}),
          ]);
        }
        if (calls == 2) {
          expect(_ids(_replies(request).single), ['10']);
          return _tools([
            _call('get_context', {'msg_id': '10', 'limit': 3}),
          ]);
        }
        expect(_ids(_replies(request).last), ['9', '10', '11']);
        return _answer();
      });
      await questionAgent.ask('看看这句话前后在说什么');
      expect(calls, 3);
    },
  );

  for (final order in ['asc', 'desc']) {
    test(
      'search dispatch passes OR keywords, $order order, and page cursor',
      () async {
        await add(9, 'self', '喜欢猫');
        await add(10, 'partner', '喜欢狗');
        await add(11, 'self', '猫和狗都喜欢');
        await add(12, 'partner', '喜欢鱼');
        final firstIds = order == 'asc' ? ['9', '10'] : ['11', '10'];
        final secondIds = order == 'asc' ? ['11'] : ['9'];
        final arguments = <String, Object?>{
          'keyword': '猫',
          'keywords': ['狗'],
          'match': 'any',
          'order': order,
          'limit': 2,
        };
        var calls = 0;
        final questionAgent = agent((request) async {
          calls++;
          if (calls == 1) return _tools([_call('search_chats', arguments)]);
          if (calls == 2) {
            final page = _replies(request).single;
            expect(_ids(page), firstIds);
            final retrieval = page['retrieval'] as Map;
            expect(retrieval['has_more'], true);
            expect(retrieval['order'], order);
            expect(retrieval['next_cursor'], isA<String>());
            return _tools([
              _call('search_chats', {
                ...arguments,
                'cursor': retrieval['next_cursor'],
              }),
            ]);
          }
          final page = _replies(request).last;
          expect(_ids(page), secondIds);
          expect((page['retrieval'] as Map)['has_more'], false);
          expect((page['retrieval'] as Map).containsKey('next_cursor'), false);
          return _answer();
        });
        await questionAgent.ask('把相关聊天继续翻页');
        expect(calls, 3);
      },
    );
  }

  test('search dispatch passes AND matching and speaker filtering', () async {
    await add(9, 'self', '喜欢猫');
    await add(10, 'partner', '猫和狗都喜欢');
    await add(11, 'self', '猫和狗都喜欢');
    var calls = 0;
    final questionAgent = agent((request) async {
      calls++;
      if (calls == 1) {
        return _tools([
          _call('search_chats', {
            'keyword': '猫',
            'keywords': ['狗'],
            'match': 'all',
            'speaker': 'self',
          }),
        ]);
      }
      expect(_ids(_replies(request).single), ['11']);
      return _answer();
    });
    await questionAgent.ask('我有没有同时提到猫和狗');
    expect(calls, 2);
  });

  test('count reports readable-message scope and empty results without fake quotes', () async {
    await add(9, 'self', '猫');
    await add(10, 'partner', '狗');
    await add(11, 'self', '猫和狗');
    await database.insert('events', {
      'msg_id': 999,
      'ts': 1750000000,
      'speaker': 'partner',
      'kind': 'image',
      'text_available': 0,
    });
    var calls = 0;
    final questionAgent = agent((request) async {
      calls++;
      if (calls == 1) {
        return _tools([
          _call('count_chats', {
            'keywords': ['猫', '狗'],
            'group_by': 'speaker',
          }, id: 'all'),
          _call('count_chats', {'keyword': '不存在'}, id: 'empty'),
        ]);
      }
      final replies = _replies(request);
      expect(replies.first['total'], 3);
      expect(replies.first['groups'], [
        {'group': 'partner', 'count': 1},
        {'group': 'self', 'count': 2},
      ]);
      expect(replies.first['note'], contains('可读消息条数'));
      expect(replies.first['note'], contains('不是词频'));
      expect(replies.first.containsKey('messages'), false);
      expect(replies.last['total'], 0);
      expect(replies.last['groups'], isEmpty);
      expect(replies.last['has_more'], false);
      return _answer('匹配的可读消息有三条，另一个关键词没有匹配。');
    });
    final result = await questionAgent.ask('帮我统计一下');
    expect(((result['answer'] as Map)['quotes'] as List), isEmpty);
    expect(calls, 2);
  });

  test('count cannot expose individual messages as context anchors', () async {
    await add(9, 'self', '从未向模型返回的原话');
    var calls = 0;
    final questionAgent = agent((request) async {
      calls++;
      if (calls == 1) return _tools([_call('count_chats', {})]);
      if (calls == 2) {
        expect(_replies(request).single['total'], 1);
        return _tools([
          _call('get_context', {'msg_id': '9'}),
        ]);
      }
      expect(_replies(request).last['error'], contains('先在本次对话中检索'));
      return _answer();
    });
    await questionAgent.ask('统计后展开一条原话');
    expect(calls, 3);
  });

  test(
    'invalid query cursor and group parameter can be repaired by the model',
    () async {
      await add(9, 'self', '有效原话');
      var calls = 0;
      final questionAgent = agent((request) async {
        calls++;
        if (calls == 1) {
          return _tools([
            _call('search_chats', {
              'cursor': 'not-base64-json',
            }, id: 'bad_cursor'),
            _call('count_chats', {'group_by': 'not-a-group'}, id: 'bad_group'),
          ]);
        }
        if (calls == 2) {
          final replies = _replies(request);
          expect(replies[0]['error'], contains('cursor 格式无效'));
          expect(replies[1]['error'], contains('speaker、day、month'));
          return _tools([
            _call('search_chats', {'order': 'asc'}, id: 'fixed_search'),
            _call('count_chats', {'group_by': 'day'}, id: 'fixed_group'),
          ]);
        }
        final replies = _replies(request);
        expect(_ids(replies[2]), ['9']);
        expect(replies[3]['total'], 1);
        expect(replies[3]['group_by'], 'day');
        return _answer();
      });
      expect((await questionAgent.ask('查询并统计相关原话'))['status'], 'done');
      expect(calls, 3);
    },
  );

  for (final name in ['search_chats', 'count_chats']) {
    test(
      '$name rejects invalid dates and allows corrected ISO ranges',
      () async {
        final midnight =
            DateTime.parse('2026-09-27T00:00:00+08:00')
                .millisecondsSinceEpoch ~/
            1000;
        await add(9, 'self', '当天原话', ts: midnight);
        final invalidRanges = <Map<String, Object?>>[
          {'start': '2026-02-30'},
          {'start': '2026-13-01'},
          {'start': '2026-09-27T24:00:00'},
          {'start': '2026-09-27T10:00:00+08:99'},
          {'start': '2026-09-28', 'end': '2026-09-27'},
          {'start': '2026-09-27', 'end': '2026-09-27'},
        ];
        final validRanges = <Map<String, Object?>>[
          {'start': '2026-09-27', 'end': '2026-09-28'},
          {
            'start': '2026-09-27T00:00:00+08:00',
            'end': '2026-09-27T00:00:01+08:00',
          },
          {'start': '2026-09-26T16:00:00Z', 'end': '2026-09-26T16:00:01Z'},
        ];
        var calls = 0;
        final questionAgent = agent((request) async {
          calls++;
          if (calls == 1) {
            return _tools([
              for (var index = 0; index < invalidRanges.length; index++)
                _call(name, invalidRanges[index], id: 'invalid_$index'),
            ]);
          }
          if (calls == 2) {
            final replies = _replies(request);
            expect(replies.length, invalidRanges.length);
            for (final reply in replies) {
              expect(reply['error'], isA<String>());
              expect(reply.containsKey('messages'), false);
              expect(reply.containsKey('total'), false);
            }
            return _tools([
              for (var index = 0; index < validRanges.length; index++)
                _call(name, validRanges[index], id: 'valid_$index'),
            ]);
          }
          final validReplies = _replies(request)
              .skip(invalidRanges.length)
              .toList();
          expect(validReplies.length, validRanges.length);
          for (final reply in validReplies) {
            expect(reply['error'], isNull);
            if (name == 'search_chats') {
              expect(_ids(reply), ['9']);
            } else {
              expect(reply['total'], 1);
            }
          }
          return _answer();
        });
        expect((await questionAgent.ask('查询这天的消息'))['status'], 'done');
        expect(calls, 3);
      },
    );

    test('$name SQL failure propagates as a retryable local error', () async {
      await database.execute('DROP TABLE messages');
      var calls = 0;
      final questionAgent = agent((_) async {
        calls++;
        if (calls > 1) {
          throw StateError('Database exception leaked back to model');
        }
        return _tools([_call(name, {})]);
      });
      await expectLater(
        questionAgent.ask('找找相关记录'),
        throwsA(
          isA<ModelException>().having(
            (error) => error.message,
            'friendly message',
            '本机资料暂时无法读取，请重试；仍失败可重启 App。',
          ),
        ),
      );
      expect(calls, 1);
    });
  }
}

Map<String, Object?> _call(
  String name,
  Map<String, Object?> args, {
  String id = 'call',
}) => {
  'id': id,
  'type': 'function',
  'function': {'name': name, 'arguments': jsonEncode(args)},
};

Map<String, Object?> _tools(List<Map<String, Object?>> calls) => {
  'content': null,
  'tool_calls': calls,
};

List<Map> _replies(Map<String, Object?> request) =>
    (request['messages'] as List)
        .whereType<Map>()
        .where((message) => message['role'] == 'tool')
        .map((message) => jsonDecode(message['content'] as String) as Map)
        .toList();

List<String> _ids(Map result) => (result['messages'] as List)
    .map((raw) => (raw as Map)['msg_id'] as String)
    .toList();

Map<String, Object?> _answer([String body = '已完成核对。']) => {
  'content': jsonEncode({
    'status': 'ok',
    'items': [
      {'title': '结果', 'body': body, 'evidence': []},
    ],
  }),
};
