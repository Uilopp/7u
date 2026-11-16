import 'dart:convert';
import 'dart:io';

import 'package:flutter/services.dart';
import 'package:path_provider/path_provider.dart';
import 'package:sqflite/sqflite.dart';

class MemoryMessage {
  final String id;
  final int timestamp;
  final String speaker;
  final String text;
  final String sourceType;
  final String? transcriptOrigin;

  const MemoryMessage({
    required this.id,
    required this.timestamp,
    required this.speaker,
    required this.text,
    required this.sourceType,
    this.transcriptOrigin,
  });

  factory MemoryMessage.fromRow(Map<String, Object?> row) => MemoryMessage(
    id: row['msg_id'].toString(),
    timestamp: row['ts'] as int,
    speaker: row['speaker'] as String,
    text: row['text'] as String,
    sourceType: row['source_type'] as String,
    transcriptOrigin: row['transcript_origin'] as String?,
  );

  Map<String, Object?> toModelJson() => {
    'msg_id': id,
    'date': _beijingIso(timestamp),
    'speaker': speaker,
    'text': text,
    'source_type': sourceType,
    if (transcriptOrigin != null) 'transcript_origin': transcriptOrigin,
  };

  static String _beijingIso(int seconds) {
    final shifted = DateTime.fromMillisecondsSinceEpoch(
      seconds * 1000,
      isUtc: true,
    ).add(const Duration(hours: 8));
    final value = DateTime(
      shifted.year,
      shifted.month,
      shifted.day,
      shifted.hour,
      shifted.minute,
      shifted.second,
      shifted.millisecond,
      shifted.microsecond,
    );
    return '${value.toIso8601String()}+08:00';
  }
}

class MemoryPack {
  static const assetPath = 'assets/memory/memory_pack.db';
  Future<Database>? _opening;

  MemoryPack();
  MemoryPack.forDatabase(Database database) : _opening = Future.value(database);

  Future<Database> get database => _opening ??= _openRecoverably();

  Future<Database> _openRecoverably() async {
    try {
      return await _open();
    } catch (_) {
      // Share an in-flight attempt, but never cache a transient opening failure.
      // The next UI retry can reinstall/open the asset without restarting.
      _opening = null;
      rethrow;
    }
  }

  Future<Database> _open() async {
    final directory = await getApplicationSupportDirectory();
    await directory.create(recursive: true);
    final target = File('${directory.path}/memory_pack.db');
    final temporary = File('${directory.path}/memory_pack.installing.db');
    final ByteData data = await rootBundle.load(assetPath);
    final bytes = data.buffer.asUint8List(
      data.offsetInBytes,
      data.lengthInBytes,
    );
    await temporary.writeAsBytes(bytes, flush: true);
    if (await target.exists()) await target.delete();
    await temporary.rename(target.path);
    return openDatabase(target.path, readOnly: true, singleInstance: true);
  }

  Future<List<MemoryMessage>> search({
    String keyword = '',
    List<String> keywords = const [],
    bool matchAll = false,
    String? speaker,
    int? start,
    int? end,
    int limit = 30,
    bool ascending = false,
    String? cursor,
  }) async {
    final (clauses, arguments) = _filters(
      keyword: keyword,
      keywords: keywords,
      matchAll: matchAll,
      speaker: speaker,
      start: start,
      end: end,
    );
    if (cursor != null) {
      final (timestamp, id) = _parseCursor(cursor);
      final comparison = ascending ? '>' : '<';
      clauses.add('(ts $comparison ? OR (ts=? AND msg_id $comparison ?))');
      arguments.addAll([timestamp, timestamp, id]);
    }
    // The agent asks for one extra row to calculate has_more.
    final rows = await (await database).query(
      'messages',
      where: clauses.isEmpty ? null : clauses.join(' AND '),
      whereArgs: arguments,
      orderBy:
          'ts ${ascending ? 'ASC' : 'DESC'},msg_id ${ascending ? 'ASC' : 'DESC'}',
      limit: limit.clamp(1, 401),
    );
    return rows.map(MemoryMessage.fromRow).toList();
  }

  /// Continue the same filters and direction after this message, not after the
  /// extra lookahead row used to determine whether more results are available.
  static String cursorFor(MemoryMessage message) => base64Url
      .encode(utf8.encode(jsonEncode([message.timestamp, message.id])))
      .replaceAll('=', '');

  static (int, int) _parseCursor(String cursor) {
    const invalid = FormatException('cursor 格式无效，请从查询结果重新分页。');
    try {
      if (cursor.isEmpty || cursor.length > 160) throw invalid;
      final value = jsonDecode(
        utf8.decode(base64Url.decode(base64Url.normalize(cursor))),
      );
      if (value is! List ||
          value.length != 2 ||
          value[0] is! int ||
          value[1] is! String) {
        throw invalid;
      }
      final timestamp = value[0] as int;
      final idText = value[1] as String;
      final id = int.tryParse(idText);
      if (timestamp < 0 ||
          timestamp > 253402300799 ||
          !RegExp(r'^\d{1,19}$').hasMatch(idText) ||
          id == null ||
          id < 0) {
        throw invalid;
      }
      return (timestamp, id);
    } on FormatException {
      throw invalid;
    }
  }

  static (List<String>, List<Object?>) _filters({
    String keyword = '',
    List<String> keywords = const [],
    bool matchAll = false,
    String? speaker,
    int? start,
    int? end,
  }) {
    final clauses = <String>[];
    final arguments = <Object?>[];
    final terms = {keyword.trim(), ...keywords.map((word) => word.trim())}
      ..remove('');
    if (terms.length > 20 || terms.any((word) => word.length > 200)) {
      throw const FormatException('关键词合并去重后最多 20 个，每个不超过 200 字。');
    }
    if (terms.isNotEmpty) {
      clauses.add(
        '(${List.filled(terms.length, 'instr(text,?)>0').join(matchAll ? ' AND ' : ' OR ')})',
      );
      arguments.addAll(terms);
    }
    if (speaker != null) {
      if (speaker != 'self' && speaker != 'partner') {
        throw const FormatException('speaker 必须是 self 或 partner');
      }
      clauses.add('speaker=?');
      arguments.add(speaker);
    }
    if (start != null) {
      clauses.add('ts>=?');
      arguments.add(start);
    }
    if (end != null) {
      clauses.add('ts<?');
      arguments.add(end);
    }
    return (clauses, arguments);
  }

  /// Counts readable messages, not calls, images, or other unavailable events.
  /// Keywords are literal substrings, with exactly the same filters as search.
  Future<Map<String, Object?>> countChats({
    String keyword = '',
    List<String> keywords = const [],
    bool matchAll = false,
    String? speaker,
    int? start,
    int? end,
    String groupBy = 'speaker',
    int limit = 90,
  }) async {
    final groupExpression = switch (groupBy) {
      'speaker' => 'speaker',
      'day' => "strftime('%Y-%m-%d',ts,'unixepoch','+8 hours')",
      'month' => "strftime('%Y-%m',ts,'unixepoch','+8 hours')",
      _ => throw const FormatException('group_by 必须是 speaker、day 或 month'),
    };
    final (clauses, arguments) = _filters(
      keyword: keyword,
      keywords: keywords,
      matchAll: matchAll,
      speaker: speaker,
      start: start,
      end: end,
    );
    final where = clauses.isEmpty ? '' : ' WHERE ${clauses.join(' AND ')}';
    final db = await database;
    final totals = await db.rawQuery(
      'SELECT count(*) AS total FROM messages$where',
      arguments,
    );
    final safeLimit = limit.clamp(1, 366);
    final groups = await db.rawQuery(
      'SELECT $groupExpression AS "group",count(*) AS count '
      'FROM messages$where GROUP BY $groupExpression '
      'ORDER BY "group" ASC LIMIT ?',
      [...arguments, safeLimit + 1],
    );
    return {
      'total': totals.single['total'],
      'group_by': groupBy,
      'groups': groups.take(safeLimit).toList(),
      'has_more': groups.length > safeLimit,
    };
  }

  Future<MemoryMessage?> messageById(String id) async {
    if (!RegExp(r'^\d{1,19}$').hasMatch(id)) return null;
    final rows = await (await database).query(
      'messages',
      where: 'msg_id=?',
      whereArgs: [id],
      limit: 1,
    );
    return rows.isEmpty ? null : MemoryMessage.fromRow(rows.single);
  }

  Future<List<MemoryMessage>> context(
    String id, {
    int radiusSeconds = 600,
    int limit = 80,
  }) async {
    final anchor = await messageById(id);
    if (anchor == null) throw const FormatException('当前记忆包没有这条消息');
    // Select nearest rows first, keeping the anchor even in a busy window.
    // Chronological LIMIT used to drop anchors near the end of large windows.
    final radius = radiusSeconds.clamp(0, 86400);
    final rows = await (await database).rawQuery(
      '''SELECT * FROM messages WHERE ts>=? AND ts<=?
         ORDER BY CASE WHEN msg_id=? THEN 0 ELSE 1 END,
         abs(ts-?),abs(msg_id-?),msg_id LIMIT ?''',
      [
        anchor.timestamp - radius,
        anchor.timestamp + radius,
        anchor.id,
        anchor.timestamp,
        anchor.id,
        limit.clamp(1, 400),
      ],
    );
    final result = rows.map(MemoryMessage.fromRow).toList();
    result.sort((a, b) {
      final time = a.timestamp.compareTo(b.timestamp);
      return time != 0 ? time : int.parse(a.id).compareTo(int.parse(b.id));
    });
    return result;
  }

  Future<List<MemoryMessage>> references(String id) async {
    final rows = await (await database).rawQuery(
      '''SELECT m.* FROM message_links l JOIN messages m
         ON m.msg_id=l.target_msg_id WHERE l.msg_id=? LIMIT 10''',
      [int.parse(id)],
    );
    return rows.map(MemoryMessage.fromRow).toList();
  }

  Future<Map<String, Object?>> profile() async {
    final rows = await (await database).query(
      'metadata',
      columns: ['value'],
      where: 'key=?',
      whereArgs: ['profile'],
      limit: 1,
    );
    if (rows.isEmpty) return {'status': 'not_consolidated', 'facts': []};
    return Map<String, Object?>.from(
      jsonDecode(rows.first['value'] as String) as Map,
    );
  }

  Future<String?> latestMessageDate() async {
    final rows = await (await database).query(
      'metadata',
      columns: ['value'],
      where: 'key=?',
      whereArgs: ['latest_message_ts'],
      limit: 1,
    );
    if (rows.isEmpty) return null;
    final value = int.tryParse(rows.first['value'] as String);
    return value == null ? null : MemoryMessage._beijingIso(value);
  }
}
