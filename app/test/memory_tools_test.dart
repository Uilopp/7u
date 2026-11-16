import 'dart:convert';

import 'package:couple_app/core/memory_pack.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:sqflite_common_ffi/sqflite_ffi.dart';

void main() {
  sqfliteFfiInit();
  databaseFactory = databaseFactoryFfi;
  late Database database;
  late MemoryPack memory;

  setUp(() async {
    database = await openDatabase(inMemoryDatabasePath);
    await database.execute('''CREATE TABLE messages(
      msg_id INTEGER PRIMARY KEY,ts INTEGER,speaker TEXT,text TEXT,
      source_type TEXT,transcript_origin TEXT)''');
    memory = MemoryPack.forDatabase(database);
  });
  tearDown(() => database.close());

  Future<void> add(
    int id, {
    int ts = 1750000000,
    String speaker = 'self',
    String text = '今天一起吃饭',
  }) => database
      .insert('messages', {
        'msg_id': id,
        'ts': ts,
        'speaker': speaker,
        'text': text,
        'source_type': 'text',
        'transcript_origin': null,
      })
      .then((_) {});

  for (final ascending in [false, true]) {
    test(
      'tuple cursor pages same-second IDs without gaps: $ascending',
      () async {
        const ids = [1, 2, 9, 10, 100, 9223372036854775807];
        for (final id in ids) {
          await add(id);
        }
        await add(101, ts: 1749999999);
        await add(102, ts: 1750000001);
        final expected = ascending
            ? ['101', ...ids.map((id) => '$id'), '102']
            : ['102', ...ids.reversed.map((id) => '$id'), '101'];
        final found = <String>[];
        String? cursor;
        for (var page = 0; page < 6; page++) {
          final rows = await memory.search(
            ascending: ascending,
            limit: 2,
            cursor: cursor,
          );
          if (rows.isEmpty) break;
          found.addAll(rows.map((row) => row.id));
          cursor = MemoryPack.cursorFor(rows.last);
        }
        expect(found, expected);
        expect(found.toSet().length, expected.length);
      },
    );
  }

  test('cursor continues only matching filters and exclusive end', () async {
    await add(1, ts: 10, text: '喜欢猫');
    await add(2, ts: 11, text: '喜欢猫', speaker: 'partner');
    await add(3, ts: 12, text: '喜欢狗');
    await add(4, ts: 13, text: '喜欢猫');
    await add(5, ts: 14, text: '喜欢猫');
    final first = await memory.search(
      keyword: '猫',
      speaker: 'self',
      start: 10,
      end: 14,
      ascending: true,
      limit: 1,
    );
    final next = await memory.search(
      keyword: '猫',
      speaker: 'self',
      start: 10,
      end: 14,
      ascending: true,
      limit: 10,
      cursor: MemoryPack.cursorFor(first.single),
    );
    expect(first.single.id, '1');
    expect(next.map((row) => row.id), ['4']);
  });

  test('OR and AND merge, trim, and deduplicate literal keywords', () async {
    await add(1, text: '喜欢猫');
    await add(2, text: '喜欢狗');
    await add(3, text: '猫和狗都喜欢');
    await add(4, text: '喜欢鸟');
    final either = await memory.search(
      keyword: ' 猫 ',
      keywords: ['狗', '猫', ''],
      ascending: true,
    );
    final both = await memory.search(
      keyword: ' 猫 ',
      keywords: ['狗', ' 猫 ', ''],
      matchAll: true,
    );
    expect(either.map((row) => row.id), ['1', '2', '3']);
    expect(both.single.id, '3');
    expect((await memory.search(keywords: ['', ' '])).length, 4);
  });

  test('search uses literal substrings, never LIKE patterns or SQL', () async {
    await add(1, text: '完成100%_好');
    await add(2, text: '完成100XX好');
    await add(3, text: "记录 ' OR 1=1 -- 原话");
    expect((await memory.search(keyword: '%_')).single.id, '1');
    expect((await memory.search(keywords: ["' OR 1=1 --"])).single.id, '3');
    expect(await memory.search(keyword: '不存在'), isEmpty);
  });

  test(
    'keyword bounds apply after deduplication to search and count',
    () async {
      await add(1, text: '猫');
      final tooMany = List.generate(21, (index) => '词$index');
      await expectLater(
        memory.search(keywords: tooMany),
        throwsA(isA<FormatException>()),
      );
      await expectLater(
        memory.countChats(keywords: tooMany),
        throwsA(isA<FormatException>()),
      );
      await expectLater(
        memory.search(keyword: '长' * 201),
        throwsA(isA<FormatException>()),
      );
      await expectLater(
        memory.countChats(keywords: ['长' * 201]),
        throwsA(isA<FormatException>()),
      );
      expect(
        (await memory.search(
          keyword: '猫',
          keywords: List.filled(25, ' 猫 '),
        )).single.id,
        '1',
      );
      expect(await memory.search(keyword: '长' * 200), isEmpty);
      expect(
        (await memory.countChats(keywords: tooMany.take(20).toList()))['total'],
        0,
      );
    },
  );

  test('invalid cursors are correctable parameter errors', () async {
    String encode(Object? value) =>
        base64Url.encode(utf8.encode(jsonEncode(value)));
    final invalid = [
      '',
      'not-a-valid-cursor',
      'x' * 161,
      encode(null),
      encode({}),
      encode([1750000000]),
      encode([1750000000, '1', 'extra']),
      encode(['1750000000', '1']),
      encode([1750000000.5, '1']),
      encode([-1, '1']),
      encode([253402300800, '1']),
      encode([1750000000, 1]),
      encode([1750000000, '-1']),
      encode([1750000000, '9223372036854775808']),
      encode([1750000000, '1 OR 1=1']),
    ];
    for (final cursor in invalid) {
      await expectLater(
        memory.search(cursor: cursor),
        throwsA(isA<FormatException>()),
        reason: 'Cursor must be rejected without querying SQL',
      );
    }
  });

  test(
    'counts use the same OR AND speaker and date filters as search',
    () async {
      await add(1, ts: 10, text: '猫');
      await add(2, ts: 11, text: '狗', speaker: 'partner');
      await add(3, ts: 12, text: '猫和狗');
      await add(4, ts: 13, text: '猫和狗', speaker: 'partner');
      await add(5, ts: 14, text: '猫和狗');
      final either = await memory.countChats(
        keyword: '猫',
        keywords: ['狗', '猫'],
        start: 10,
        end: 14,
      );
      expect(either, {
        'total': 4,
        'group_by': 'speaker',
        'has_more': false,
        'groups': [
          {'group': 'partner', 'count': 2},
          {'group': 'self', 'count': 2},
        ],
      });
      final both = await memory.countChats(
        keyword: '猫',
        keywords: ['狗'],
        matchAll: true,
        speaker: 'self',
        start: 10,
        end: 14,
      );
      expect(both['total'], 1);
      expect(both['groups'], [
        {'group': 'self', 'count': 1},
      ]);
    },
  );

  test('day and month grouping use fixed Beijing midnight', () async {
    final midnight =
        DateTime.parse('2026-09-01T00:00:00+08:00').millisecondsSinceEpoch ~/
        1000;
    await add(1, ts: midnight - 1);
    await add(2, ts: midnight);
    await add(3, ts: midnight + 1, speaker: 'partner');
    final days = await memory.countChats(groupBy: 'day');
    expect(days['groups'], [
      {'group': '2026-08-31', 'count': 1},
      {'group': '2026-09-01', 'count': 2},
    ]);
    final months = await memory.countChats(groupBy: 'month');
    expect(months['groups'], [
      {'group': '2026-08', 'count': 1},
      {'group': '2026-09', 'count': 2},
    ]);
  });

  test(
    'count group limit does not truncate total and flags further groups',
    () async {
      await add(1, ts: 1750000000);
      await add(2, ts: 1750086400);
      await add(3, ts: 1750172800);
      final first = await memory.countChats(groupBy: 'day', limit: 2);
      expect(first['total'], 3);
      expect((first['groups'] as List).length, 2);
      expect(first['has_more'], true);
      final all = await memory.countChats(groupBy: 'day', limit: 3);
      expect(all['has_more'], false);
    },
  );

  test('empty and reversed count ranges return no evidence', () async {
    await add(1);
    for (final result in [
      await memory.countChats(keyword: '不存在'),
      await memory.countChats(start: 1750000001),
      await memory.countChats(start: 1750000000, end: 1750000000),
      await memory.countChats(start: 1750000001, end: 1750000000),
    ]) {
      expect(result['total'], 0);
      expect(result['groups'], isEmpty);
      expect(result['has_more'], false);
    }
  });

  test('speaker and groupBy reject unrecognized SQL-like input', () async {
    await expectLater(
      memory.search(speaker: "self' OR 1=1 --"),
      throwsA(isA<FormatException>()),
    );
    await expectLater(
      memory.countChats(speaker: 'nobody'),
      throwsA(isA<FormatException>()),
    );
    await expectLater(
      memory.countChats(groupBy: 'speaker; DROP TABLE messages;'),
      throwsA(isA<FormatException>()),
    );
    expect((await memory.countChats())['total'], 0);
  });

  test(
    'dense context can expand while preserving anchor and chronology',
    () async {
      final batch = database.batch();
      for (var id = 1; id <= 500; id++) {
        batch.insert('messages', {
          'msg_id': id,
          'ts': 1750000000 + id ~/ 2,
          'speaker': id.isEven ? 'self' : 'partner',
          'text': '消息$id',
          'source_type': 'text',
          'transcript_origin': null,
        });
      }
      await batch.commit(noResult: true);
      final defaultWindow = await memory.context('495');
      final expanded = await memory.context('495', limit: 300);
      final capped = await memory.context('495', limit: 999);
      final anchorOnly = await memory.context('495', limit: 1);
      expect(defaultWindow.length, 80);
      expect(expanded.length, 300);
      expect(capped.length, 400);
      expect(anchorOnly.single.id, '495');
      expect(defaultWindow.map((row) => row.id), contains('495'));
      expect(expanded.map((row) => row.id), contains('495'));
      final ids = expanded.map((row) => int.parse(row.id)).toList();
      expect(ids, orderedEquals([...ids]..sort()));
      expect((await memory.search(limit: 999)).length, 401);
    },
  );

  test(
    'context respects radius, includes boundary and sorts equal seconds',
    () async {
      await add(9, ts: 100);
      await add(10, ts: 100);
      await add(2, ts: 99);
      await add(3, ts: 101);
      await add(4, ts: 102);
      final context = await memory.context('10', radiusSeconds: 1, limit: 400);
      expect(context.map((row) => row.id), ['2', '9', '10', '3']);
      await expectLater(memory.context('999'), throwsA(isA<FormatException>()));
    },
  );

  test('count caps displayed groups at 366 without losing total', () async {
    final batch = database.batch();
    for (var id = 1; id <= 368; id++) {
      batch.insert('messages', {
        'msg_id': id,
        'ts': 1750000000 + id * 86400,
        'speaker': 'self',
        'text': '消息$id',
        'source_type': 'text',
        'transcript_origin': null,
      });
    }
    await batch.commit(noResult: true);
    final result = await memory.countChats(groupBy: 'day', limit: 999);
    expect(result['total'], 368);
    expect((result['groups'] as List).length, 366);
    expect(result['has_more'], true);
  });
}
