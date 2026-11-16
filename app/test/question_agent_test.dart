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

  test('model chooses retrieval and exact evidence is returned', () async {
    final database = await openDatabase(inMemoryDatabasePath);
    await database.execute('''CREATE TABLE messages(
      msg_id INTEGER PRIMARY KEY,ts INTEGER,speaker TEXT,text TEXT,
      source_type TEXT,transcript_origin TEXT)''');
    await database.execute(
      'CREATE TABLE message_links(msg_id INTEGER,target_msg_id INTEGER,PRIMARY KEY(msg_id,target_msg_id))',
    );
    await database.execute(
      'CREATE TABLE events(msg_id INTEGER PRIMARY KEY,ts INTEGER,speaker TEXT,kind TEXT,text_available INTEGER)',
    );
    await database.execute(
      'CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT)',
    );
    await database.insert('metadata', {
      'key': 'latest_message_ts',
      'value': '1760000000',
    });
    await database.insert('metadata', {
      'key': 'profile',
      'value': '{"status":"available","facts":[]}',
    });
    await database.insert('messages', {
      'msg_id': 123,
      'ts': 1750000000,
      'speaker': 'partner',
      'text': '我的生日是十月三日',
      'source_type': 'voice_transcript',
      'transcript_origin': 'human_verified',
    });

    var calls = 0;
    final model = ModelClient(
      key: 'test',
      transport: (body) async {
        calls++;
        if (calls == 1) {
          return {
            'role': 'assistant',
            'content': null,
            'tool_calls': [
              {
                'id': 'call_1',
                'type': 'function',
                'function': {
                  'name': 'search_chats',
                  'arguments': '{"keyword":"生日","limit":10}',
                },
              },
            ],
          };
        }
        final messages = body['messages'] as List;
        expect(jsonEncode(messages), contains('voice_transcript'));
        return {
          'role': 'assistant',
          'content': jsonEncode({
            'status': 'ok',
            'items': [
              {
                'title': 'TA 的生日',
                'body': '现有记录里，TA 说自己的生日是十月三日。',
                'evidence': [
                  {'msg_id': '123', 'speaker': 'partner', 'quote': '我的生日是十月三日'},
                ],
              },
            ],
          }),
        };
      },
    );
    final agent = QuestionAgent(
      memory: MemoryPack.forDatabase(database),
      model: model,
      promptLoader: () async => '测试提示词',
    );

    final result = await agent.ask('他生日是什么时候？');
    expect(calls, 2);
    expect(result['status'], 'done');
    final answer = result['answer'] as Map;
    expect(answer['body'], contains('十月三日'));
    final quote = (answer['quotes'] as List).single as Map;
    expect(quote['speaker'], 'partner');
    expect(quote['source_type'], 'voice_transcript');
    await database.close();
  });

  test(
    'followup carries conversation and reuses verified evidence without tools',
    () async {
      final database = await _database();
      final history = [_saved('他生日是什么时候？')];
      var calls = 0;
      final agent = _agent(database, (request) async {
        calls++;
        final messages = request['messages'] as List;
        expect(messages.map((raw) => (raw as Map)['role']), [
          'system',
          'user',
          'assistant',
          'user',
        ]);
        expect((messages[1] as Map)['content'], '他生日是什么时候？');
        expect((messages.last as Map)['content'], '说短一点');
        expect((messages.first as Map)['content'], contains('经当前记忆包重新核验'));
        return _answer('十月三日。', id: '123', quote: '我的生日是十月三日');
      });
      final result = await agent.ask('说短一点', history: history);
      expect(calls, 1);
      expect((result['answer'] as Map)['body'], '十月三日。');
      expect(((result['answer'] as Map)['quotes'] as List).length, 1);
    },
  );

  test('model resolves 那我呢 using history and chooses self retrieval', () async {
    final database = await _database();
    var calls = 0;
    final agent = _agent(database, (request) async {
      calls++;
      final messages = request['messages'] as List;
      expect((messages.first as Map)['content'], isNot(contains('本题目标说话人')));
      expect((messages[1] as Map)['content'], '他生日是什么时候？');
      if (calls == 1) {
        return _tool('search_chats', {'keyword': '生日', 'speaker': 'self'});
      }
      final tool =
          jsonDecode((messages.last as Map)['content'] as String) as Map;
      expect(((tool['messages'] as List).single as Map)['speaker'], 'self');
      return _answer('你是六月五日。', id: '124', quote: '我的生日是六月五日');
    });
    final result = await agent.ask('那我呢？', history: [_saved('他生日是什么时候？')]);
    final quotes = (result['answer'] as Map)['quotes'] as List;
    expect(calls, 2);
    expect((quotes.single as Map)['speaker'], 'self');
  });

  test('mixed-person question does not force a speaker filter', () async {
    final database = await _database();
    var calls = 0;
    final agent = _agent(database, (request) async {
      calls++;
      if (calls == 1) return _tool('search_chats', {'keyword': '生日'});
      final messages = request['messages'] as List;
      final tool =
          jsonDecode((messages.last as Map)['content'] as String) as Map;
      expect((tool['messages'] as List).length, 2);
      return {
        'content': jsonEncode({
          'status': 'ok',
          'items': [
            {
              'title': '两个人的生日',
              'body': '你是六月五日，TA 是十月三日。',
              'evidence': [
                {'msg_id': '124', 'speaker': 'self', 'quote': '我的生日是六月五日'},
                {'msg_id': '123', 'speaker': 'partner', 'quote': '我的生日是十月三日'},
              ],
            },
          ],
        }),
      };
    });
    final result = await agent.ask('我和他分别什么时候生日？');
    expect(((result['answer'] as Map)['quotes'] as List).length, 2);
  });

  test('saved speaker labels are replaced with database ownership', () async {
    final database = await _database();
    final history = [_saved('他生日是什么时候？', speaker: 'self')];
    final agent = _agent(database, (request) async {
      final messages = request['messages'] as List;
      final previous =
          jsonDecode((messages[2] as Map)['content'] as String) as Map;
      final item = (previous['items'] as List).single as Map;
      expect(((item['evidence'] as List).single as Map)['speaker'], 'partner');
      return _answer('TA 说十月三日。', id: '123', quote: '我的生日是十月三日');
    });
    final result = await agent.ask('再具体一点', history: history);
    final evidence =
        ((result['answer'] as Map)['quotes'] as List).single as Map;
    expect(evidence['speaker'], 'partner');
  });

  test(
    'mixed possessives and followups are not assigned a hardcoded target',
    () async {
      final database = await _database();
      var question = '我的生日和他的生日都说一下';
      final agent = _agent(database, (request) async {
        final messages = request['messages'] as List;
        expect((messages.first as Map)['content'], isNot(contains('本题目标')));
        expect((messages.last as Map)['content'], question);
        return _answer('我们接着聊。');
      });
      final history = [
        _saved('我的生日是什么时候？', id: '124', speaker: 'self', quote: '我的生日是六月五日'),
      ];
      await agent.ask(question, history: history);
      question = '还有其他建议吗？';
      await agent.ask(question, history: history);
    },
  );

  for (final variation in ['missing_id', 'changed_quote', 'removed_message']) {
    test('history cannot become evidence when $variation', () async {
      final database = await _database();
      if (variation == 'removed_message') {
        await database.delete('messages', where: 'msg_id=?', whereArgs: [123]);
      }
      final history = [
        _saved(
          '他生日是什么时候？',
          id: variation == 'missing_id' ? null : '123',
          quote: variation == 'changed_quote' ? '我的生日是八月八日' : '我的生日是十月三日',
        ),
      ];
      var calls = 0;
      final agent = _agent(database, (request) async {
        calls++;
        final messages = request['messages'] as List;
        final previous =
            jsonDecode((messages[2] as Map)['content'] as String) as Map;
        final item = (previous['items'] as List).single as Map;
        expect(item['evidence'], isEmpty);
        expect(
          (messages.first as Map)['content'],
          isNot(contains('"msg_id":"123"')),
        );
        return _answer('这是上一轮说过的。', id: '123', quote: '我的生日是十月三日');
      });
      await expectLater(
        agent.ask('再说一遍', history: history),
        throwsA(isA<ModelException>()),
      );
      expect(calls, QuestionAgent.maxModelCalls);
    });
  }

  test(
    'role switch cannot relabel a previous source as the other person',
    () async {
      final database = await _database();
      var calls = 0;
      final agent = _agent(database, (request) async {
        calls++;
        if (calls > 1) {
          expect(
            jsonEncode(request['messages']),
            contains('speaker 应为 partner'),
          );
        }
        return _answer(
          '你的生日是十月三日。',
          id: '123',
          quote: '我的生日是十月三日',
          speaker: 'self',
        );
      });
      await expectLater(
        agent.ask('那我呢？', history: [_saved('他生日是什么时候？')]),
        throwsA(isA<ModelException>()),
      );
      expect(calls, QuestionAgent.maxModelCalls);
    },
  );

  test(
    'model can repair a mismatched speaker without losing the source',
    () async {
      final database = await _database();
      var calls = 0;
      final agent = _agent(database, (request) async {
        calls++;
        if (calls == 1) {
          return _answer(
            '你说十月三日。',
            id: '123',
            quote: '我的生日是十月三日',
            speaker: 'self',
          );
        }
        expect(jsonEncode(request['messages']), contains('speaker 应为 partner'));
        return _answer('TA 说十月三日。', id: '123', quote: '我的生日是十月三日');
      });
      final result = await agent.ask('刚才是谁说的？', history: [_saved('他生日是什么时候？')]);
      expect(calls, 2);
      final quote = ((result['answer'] as Map)['quotes'] as List).single as Map;
      expect(quote['msg_id'], '123');
      expect(quote['speaker'], 'partner');
    },
  );

  test(
    'evidence with no speaker must be repaired, not implicitly trusted',
    () async {
      final database = await _database();
      var calls = 0;
      final agent = _agent(database, (_) async {
        calls++;
        final answer = _answer('TA 是十月三日。', id: '123', quote: '我的生日是十月三日');
        final decoded = jsonDecode(answer['content'] as String) as Map;
        final item = (decoded['items'] as List).single as Map;
        ((item['evidence'] as List).single as Map).remove('speaker');
        return {'content': jsonEncode(decoded)};
      });
      await expectLater(
        agent.ask('说短一点', history: [_saved('他生日是什么时候？')]),
        throwsA(isA<ModelException>()),
      );
      expect(calls, QuestionAgent.maxModelCalls);
    },
  );

  for (final (question, speaker, id, quote) in [
    ('我最近对他说的话是什么', 'self', '124', '我的生日是六月五日'),
    ('他最近对我说的话是什么', 'partner', '123', '我的生日是十月三日'),
    ('他说我是什么样的人', 'partner', '123', '我的生日是十月三日'),
    ('我以前说他哪里好', 'self', '124', '我的生日是六月五日'),
  ]) {
    test(
      'speaker is source ownership, not recipient or topic: $question',
      () async {
        final database = await _database();
        var calls = 0;
        final agent = _agent(database, (request) async {
          calls++;
          expect(
            ((request['messages'] as List).first as Map)['content'],
            isNot(contains('本题目标')),
          );
          if (calls == 1) return _tool('search_chats', {'speaker': speaker});
          return _answer(
            '这条是${speaker == 'self' ? '你' : 'TA'}的原话。',
            id: id,
            quote: quote,
            speaker: speaker,
          );
        });
        final result = await agent.ask(
          question,
          history: [_saved('最近有什么值得纪念的事情')],
        );
        final evidence =
            ((result['answer'] as Map)['quotes'] as List).single as Map;
        expect(calls, 2);
        expect(evidence['speaker'], speaker);
        expect(evidence['msg_id'], id);
        expect(evidence['quote'], quote);
      },
    );
  }

  for (final toolName in [
    'get_context',
    'get_references',
    'get_context_coverage',
  ]) {
    test('$toolName cannot access another conversation anchor', () async {
      final database = await _database();
      final memory = MemoryPack.forDatabase(database);
      var firstCalls = 0;
      await QuestionAgent(
        memory: memory,
        model: ModelClient(
          transport: (_) async {
            firstCalls++;
            return firstCalls == 1
                ? _tool('search_chats', {'keyword': '生日'})
                : _answer('TA 是十月三日。', id: '123', quote: '我的生日是十月三日');
          },
        ),
        promptLoader: () async => '测试提示词',
      ).ask('他生日是什么时候？');
      var calls = 0;
      final second = QuestionAgent(
        memory: memory,
        model: ModelClient(
          transport: (request) async {
            calls++;
            if (calls == 1) return _tool(toolName, {'msg_id': '123'});
            final messages = request['messages'] as List;
            final tool =
                jsonDecode((messages.last as Map)['content'] as String) as Map;
            expect(tool['error'], contains('先在本次对话中检索'));
            return _answer('目前没有这段对话的依据。');
          },
        ),
        promptLoader: () async => '测试提示词',
      );
      await second.ask('帮我看看记录');
      expect(calls, 2);
    });
  }

  test(
    'all requested calls receive tool responses after budget exhaustion',
    () async {
      final database = await _database();
      var calls = 0;
      final agent = _agent(database, (request) async {
        calls++;
        if (calls == 1) {
          return {
            'content': null,
            'tool_calls': List.generate(
              QuestionAgent.maxToolCalls + 2,
              (i) =>
                  ((_tool('search_chats', {
                            'keyword': '不存在',
                          }, id: 'call_$i')['tool_calls'])
                          as List)
                      .single,
            ),
          };
        }
        final messages = request['messages'] as List;
        final replies = messages
            .whereType<Map>()
            .where((item) => item['role'] == 'tool')
            .toList();
        expect(replies.length, QuestionAgent.maxToolCalls + 2);
        expect(
          replies.map((item) => item['tool_call_id']).toSet().length,
          QuestionAgent.maxToolCalls + 2,
        );
        expect(
          jsonDecode(
            replies[QuestionAgent.maxToolCalls - 1]['content'] as String,
          )['error'],
          isNull,
        );
        expect(
          jsonDecode(replies.last['content'] as String)['error'],
          contains('技术边界'),
        );
        return _answer('暂时没有找到相关记录。');
      });
      await agent.ask('找一件有趣的事情');
      expect(calls, 2);
    },
  );

  test(
    'first question and followup each receive the full independent budget',
    () async {
      final database = await _database();
      final callCounts = [0, 0];
      var turn = 0;
      final perRound =
          QuestionAgent.maxToolCalls ~/ QuestionAgent.maxRetrievalRounds;
      expect(
        perRound * QuestionAgent.maxRetrievalRounds,
        QuestionAgent.maxToolCalls,
      );
      final agent = _agent(database, (request) async {
        final call = ++callCounts[turn];
        final replies = (request['messages'] as List)
            .whereType<Map>()
            .where((message) => message['role'] == 'tool')
            .toList();
        expect(replies.length, (call - 1) * perRound);
        for (final reply in replies) {
          expect(jsonDecode(reply['content'] as String)['error'], isNull);
        }
        if (call <= QuestionAgent.maxRetrievalRounds) {
          expect(request['tools'], isNotEmpty);
          return {
            'content': null,
            'tool_calls': [
              for (var tool = 0; tool < perRound; tool++)
                (_tool('search_chats', {
                          'keyword': '没有匹配',
                        }, id: 'turn_${turn}_${call}_$tool')['tool_calls']
                        as List)
                    .single,
            ],
          };
        }
        expect(request['tools'], isNull);
        return _answer('这一轮没有找到匹配的记录。');
      });
      const firstQuestion = '找一些值得纪念的事';
      final first = await agent.ask(firstQuestion);
      turn = 1;
      final second = await agent.ask(
        '再找一些',
        history: [SavedQuestion.fromResult(firstQuestion, first)],
      );
      expect(second['status'], 'done');
      expect(callCounts, List.filled(2, QuestionAgent.maxRetrievalRounds + 1));
    },
  );

  test('undelivered over-budget source is not a context anchor', () async {
    final database = await _database();
    const sourceCount = QuestionAgent.maxToolCharacters ~/ 40000;
    final undeliveredId = '${200 + sourceCount - 1}';
    for (var i = 0; i < sourceCount; i++) {
      await database.insert('messages', {
        'msg_id': 200 + i,
        'ts': 1750000000 + i,
        'speaker': 'partner',
        'text': '长消息$i${'字' * 40000}',
        'source_type': 'message_text',
      });
    }
    var calls = 0;
    final agent = _agent(database, (request) async {
      calls++;
      if (calls == 1) {
        return {
          'content': null,
          'tool_calls': [
            for (var i = 0; i < sourceCount; i++)
              (_tool('search_chats', {
                        'keyword': '长消息$i',
                      }, id: 's$i')['tool_calls']
                      as List)
                  .single,
            (_tool('get_context', {
                      'msg_id': undeliveredId,
                    }, id: 'context')['tool_calls']
                    as List)
                .single,
          ],
        };
      }
      final messages = request['messages'] as List;
      final replies = messages
          .whereType<Map>()
          .where((item) => item['role'] == 'tool')
          .toList();
      expect(replies.length, sourceCount + 1);
      expect(
        jsonDecode(replies[sourceCount - 1]['content'] as String)['error'],
        contains('剩余上下文'),
      );
      expect(
        jsonDecode(replies[sourceCount]['content'] as String)['error'],
        contains('先在本次对话中检索'),
      );
      return _answer('只使用实际展开的片段。');
    });
    await agent.ask('帮我回忆长消息');
  });

  test('dense context window always retains its anchor', () async {
    final database = await _database();
    for (var i = 0; i < 200; i++) {
      await database.insert('messages', {
        'msg_id': 1000 + i,
        'ts': 1750000000 + i,
        'speaker': i.isEven ? 'partner' : 'self',
        'text': '第$i条消息',
        'source_type': 'message_text',
      });
    }
    final rows = await MemoryPack.forDatabase(database).context('1190');
    expect(rows.length, 80);
    expect(rows.map((row) => row.id), contains('1190'));
    expect(
      rows.map((row) => row.timestamp).toList(),
      orderedEquals(rows.map((row) => row.timestamp).toList()..sort()),
    );
  });

  test('timezone-less date tool arguments use Beijing time', () async {
    final database = await _database();
    final timestamp =
        DateTime.parse('2026-09-27T08:00:00+08:00').millisecondsSinceEpoch ~/
        1000;
    await database.insert('messages', {
      'msg_id': 500,
      'ts': timestamp,
      'speaker': 'partner',
      'text': '八点的早餐',
      'source_type': 'message_text',
    });
    var calls = 0;
    final agent = _agent(database, (request) async {
      calls++;
      if (calls == 1) {
        return _tool('search_chats', {
          'start': '2026-09-27T08:00:00',
          'end': '2026-09-27T08:00:01',
        });
      }
      final messages = request['messages'] as List;
      final result =
          jsonDecode((messages.last as Map)['content'] as String) as Map;
      expect(((result['messages'] as List).single as Map)['msg_id'], '500');
      return _answer('这天八点在聊早餐。', id: '500', quote: '八点的早餐');
    });
    await agent.ask('这一天发生了什么？');
  });

  test(
    'followup network failure remains retryable despite verified history',
    () async {
      final database = await _database();
      final agent = _agent(database, (_) async {
        throw const ModelException('网络连接失败，请检查网络后重试。');
      });
      await expectLater(
        agent.ask('说短一点', history: [_saved('他生日是什么时候？')]),
        throwsA(isA<ModelException>()),
      );
    },
  );

  test('network failure after retrieval remains retryable', () async {
    final database = await _database();
    var calls = 0;
    final agent = _agent(database, (_) async {
      calls++;
      if (calls == 1) return _tool('search_chats', {'keyword': '生日'});
      throw const ModelException('这次回答超时了，请再试一次。');
    });
    await expectLater(agent.ask('他生日是什么时候？'), throwsA(isA<ModelException>()));
    expect(calls, 2);
  });

  for (final greeting in ['你好！', '  你好 ～ \n', '谢谢。', 'Hello!']) {
    test('first greeting $greeting does not force retrieval', () async {
      final database = await _database();
      var calls = 0;
      final agent = _agent(database, (_) async {
        calls++;
        return _answer('你好，慢慢聊。');
      });
      final result = await agent.ask(greeting);
      expect(calls, 1);
      expect(result['status'], 'done');
    });
  }

  test(
    'expanded answer can contain 4000 characters and 12 verified quotes',
    () async {
      final database = await _database();
      final evidence = <Map<String, Object?>>[];
      for (var index = 0; index < 12; index++) {
        final id = '${600 + index}';
        final quote = '拓展资料第$index条';
        final speaker = index.isEven ? 'self' : 'partner';
        await database.insert('messages', {
          'msg_id': int.parse(id),
          'ts': 1750000000 + index,
          'speaker': speaker,
          'text': quote,
          'source_type': 'message_text',
        });
        evidence.add({'msg_id': id, 'speaker': speaker, 'quote': quote});
      }
      var calls = 0;
      final agent = _agent(database, (_) async {
        calls++;
        if (calls == 1) return _tool('search_chats', {'keyword': '拓展资料'});
        return {
          'content': jsonEncode({
            'status': 'ok',
            'items': [
              {'title': '完整回答', 'body': '文' * 4000, 'evidence': evidence},
            ],
          }),
        };
      });
      final result = await agent.ask('把两个人的相关原话整理一下');
      final answer = result['answer'] as Map;
      expect(calls, 2);
      expect((answer['body'] as String).length, 4000);
      expect((answer['quotes'] as List).length, 12);
      expect(
        (answer['quotes'] as List).map((raw) => (raw as Map)['speaker']),
        evidence.map((raw) => raw['speaker']),
      );
    },
  );

  test(
    'model can choose retrieval for a private question after a greeting',
    () async {
      final database = await _database();
      var calls = 0;
      final agent = _agent(database, (request) async {
        calls++;
        if (calls == 1) {
          expect(request['tools'], isNotEmpty);
          return _tool('search_chats', {'keyword': '生日'});
        }
        return _answer('TA 的生日是十月三日。', id: '123', quote: '我的生日是十月三日');
      });
      final result = await agent.ask('你好！他生日是什么时候？');
      expect(calls, 2);
      expect(((result['answer'] as Map)['quotes'] as List).length, 1);
    },
  );

  test(
    'SQLite retrieval failure is retryable and never sent to the model',
    () async {
      final database = await _database();
      await database.execute('DROP TABLE messages');
      var calls = 0;
      final agent = _agent(database, (_) async {
        calls++;
        if (calls > 1) throw StateError('Storage error was sent to the model');
        return _tool('search_chats', {'keyword': '生日'});
      });
      await expectLater(
        agent.ask('他生日是什么时候？'),
        throwsA(
          isA<ModelException>().having(
            (error) => error.message,
            'message',
            '本机资料暂时无法读取，请重试；仍失败可重启 App。',
          ),
        ),
      );
      expect(calls, 1);
    },
  );

  test(
    'corrupt stored profile is a storage error not malformed tool arguments',
    () async {
      final database = await _database();
      await database.insert('metadata', {
        'key': 'profile',
        'value': '{broken JSON',
      });
      var calls = 0;
      final agent = _agent(database, (_) async {
        calls++;
        return _tool('get_profile', {});
      });
      await expectLater(
        agent.ask('他是一个什么样的人？'),
        throwsA(
          isA<ModelException>().having(
            (error) => error.message,
            'message',
            '本机资料暂时无法读取，请重试；仍失败可重启 App。',
          ),
        ),
      );
      expect(calls, 1);
    },
  );

  test('history source read failure stops before any model request', () async {
    final database = await _database();
    await database.execute('DROP TABLE messages');
    var calls = 0;
    final agent = _agent(database, (_) async {
      calls++;
      return _answer('不要显示这个。');
    });
    await expectLater(
      agent.ask('再说一遍', history: [_saved('他生日是什么时候？')]),
      throwsA(isA<ModelException>()),
    );
    expect(calls, 0);
  });

  test(
    'bad tool argument shape and speaker remain correctable by the model',
    () async {
      final database = await _database();
      var calls = 0;
      final agent = _agent(database, (request) async {
        calls++;
        if (calls == 1) {
          return {
            'content': null,
            'tool_calls': [
              {
                'id': 'bad_shape',
                'type': 'function',
                'function': {'name': 'search_chats', 'arguments': '[]'},
              },
              (_tool('search_chats', {
                        'speaker': 'nobody',
                      }, id: 'bad_speaker')['tool_calls']
                      as List)
                  .single,
            ],
          };
        }
        if (calls == 2) {
          final replies = (request['messages'] as List)
              .whereType<Map>()
              .where((message) => message['role'] == 'tool')
              .toList();
          expect(replies.length, 2);
          expect(
            jsonDecode(replies[0]['content'] as String)['error'],
            '工具参数必须是 JSON 对象。',
          );
          expect(
            jsonDecode(replies[1]['content'] as String)['error'],
            'speaker 必须是 self 或 partner',
          );
          return _tool('search_chats', {'keyword': '生日', 'speaker': 'partner'});
        }
        return _answer('TA 的生日是十月三日。', id: '123', quote: '我的生日是十月三日');
      });
      final result = await agent.ask('他生日是什么时候？');
      expect(calls, 3);
      expect(((result['answer'] as Map)['quotes'] as List).length, 1);
    },
  );
}

Future<Database> _database() async {
  final database = await databaseFactoryFfi.openDatabase(inMemoryDatabasePath);
  addTearDown(database.close);
  await database.execute('''CREATE TABLE messages(
    msg_id INTEGER PRIMARY KEY,ts INTEGER,speaker TEXT,text TEXT,
    source_type TEXT,transcript_origin TEXT)''');
  await database.execute(
    'CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT)',
  );
  await database.execute(
    'CREATE TABLE message_links(msg_id INTEGER,target_msg_id INTEGER)',
  );
  await database.execute(
    'CREATE TABLE events(msg_id INTEGER PRIMARY KEY,ts INTEGER,speaker TEXT,kind TEXT,text_available INTEGER)',
  );
  for (final (id, speaker, text) in [
    (123, 'partner', '我的生日是十月三日'),
    (124, 'self', '我的生日是六月五日'),
  ]) {
    await database.insert('messages', {
      'msg_id': id,
      'ts': 1750000000,
      'speaker': speaker,
      'text': text,
      'source_type': 'message_text',
    });
  }
  return database;
}

QuestionAgent _agent(
  Database database,
  Future<Map<String, Object?>> Function(Map<String, Object?>) transport,
) => QuestionAgent(
  memory: MemoryPack.forDatabase(database),
  model: ModelClient(transport: transport),
  promptLoader: () async => '测试提示词',
);

SavedQuestion _saved(
  String question, {
  String? id = '123',
  String speaker = 'partner',
  String quote = '我的生日是十月三日',
}) => SavedQuestion(
  question: question,
  title: 'TA 的生日',
  body: 'TA 是十月三日生日。',
  evidence: [QuestionEvidence(msgId: id, speaker: speaker, quote: quote)],
  createdAt: DateTime(2026, 9, 27),
);

Map<String, Object?> _tool(
  String name,
  Map<String, Object?> args, {
  String id = 'call_1',
}) => {
  'content': null,
  'tool_calls': [
    {
      'id': id,
      'type': 'function',
      'function': {'name': name, 'arguments': jsonEncode(args)},
    },
  ],
};

Map<String, Object?> _answer(
  String body, {
  String? id,
  String? quote,
  String? speaker,
}) => {
  'content': jsonEncode({
    'status': 'ok',
    'items': [
      {
        'title': '回答',
        'body': body,
        'evidence': [
          if (id != null)
            {
              'msg_id': id,
              'speaker': speaker ?? (id == '124' ? 'self' : 'partner'),
              'quote': quote,
            },
        ],
      },
    ],
  }),
};
