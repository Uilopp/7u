import 'dart:convert';

import 'package:flutter/services.dart';

import 'memory_pack.dart';
import 'model_client.dart';
import 'question_cancellation.dart';
import 'question_history.dart';

class QuestionAgent {
  static const maxModelCalls = 12;
  static const maxRetrievalRounds = 8;
  static const maxToolCalls = 32;
  static const maxToolCharacters = 240000;
  static const maxHistoryCharacters = 64000;

  final MemoryPack memory;
  final ModelClient model;
  final Future<String> Function() promptLoader;

  QuestionAgent({
    MemoryPack? memory,
    ModelClient? model,
    Future<String> Function()? promptLoader,
  }) : memory = memory ?? MemoryPack(),
       model = model ?? const ModelClient(),
       promptLoader =
           promptLoader ??
           (() => rootBundle.loadString('assets/agent/prompt.md'));

  Future<Map<String, dynamic>> ask(
    String question, {
    List<SavedQuestion> history = const [],
    QuestionCancellation? cancellation,
  }) async {
    cancellation?.throwIfCancelled();
    final task = question.trim();
    if (task.isEmpty || task.length > 500) {
      throw const ModelException('问题需要是 1–500 个字。');
    }
    final prompt = await _readMemory(promptLoader, cancellation: cancellation);
    final retainedHistory = _retainHistory(history);
    // Evidence belongs to this request, never to the shared database instance.
    final visible = <String, Map<String, Object?>>{};
    final verifiedHistory = await _restoreHistory(
      retainedHistory,
      visible,
      cancellation,
    );
    final latest = await _readMemory(
      memory.latestMessageDate,
      cancellation: cancellation,
    );
    final shiftedNow = DateTime.now().toUtc().add(const Duration(hours: 8));
    final beijingNow = DateTime(
      shiftedNow.year,
      shiftedNow.month,
      shiftedNow.day,
      shiftedNow.hour,
      shiftedNow.minute,
      shiftedNow.second,
      shiftedNow.millisecond,
    );
    final system =
        '''$prompt

人物含义固定：self 是她（当前 App 使用者，正文称“你”），partner 是他（赠送者，正文称“TA”）。本轮问谁、谁说话、说给谁听，由你结合完整问题和上下文判断；程序不会根据某个代词替你指定检索人物。引用中的 speaker 必须照抄对应消息的标签。

这是同一段连续对话。历史 user 消息是用户在本段对话中的补充；历史 assistant 消息只是先前生成的回答和建议，不是 QQ 原话，也不能证明私人事实。理解“第二个”“再详细一点”“那我呢”等追问时结合上下文；明确改问人物时以本轮为准。改写、解释或延续已有建议不需要重复检索；要核实新的私人事实，或原有材料不足时，再自行调用工具。
${retainedHistory.length < history.length ? '较早的对话因上下文长度已省略；遇到未保留的指代不要自行编造。' : ''}

以下是本段历史引用经当前记忆包重新核验的原始消息。只有这里和本轮工具实际返回的消息才可以作为逐字引用依据；过时、缺失或未核验的历史引用已移除：
${jsonEncode(visible.values.toList())}

本次请求的权威时间信息：${jsonEncode({
          'current_datetime': '${beijingNow.toIso8601String()}+08:00',
          'current_date': beijingNow.toIso8601String().substring(0, 10),
          'timezone': '北京时间 (UTC+08:00, fixed)',
          'archive': {'latest_message_date': latest},
        })}

本次单题技术边界：最多 $maxRetrievalRounds 轮检索、$maxToolCalls 次工具调用、$maxToolCharacters 字符工具内容。首次提问和每次追问均重新获得完整预算，之前的调用不扣减本轮预算。你决定搜什么、搜多少和何时停止；有足够材料就回答，不必用完预算；尚有关键疑点就继续查。这不是用户额度。''';
    final messages = <Map<String, Object?>>[
      {'role': 'system', 'content': system},
      ...verifiedHistory,
      {'role': 'user', 'content': task},
    ];
    var retrievalRounds = 0;
    var toolCalls = 0;
    var toolCharacters = 0;
    var finalNoticeSent = false;

    for (var call = 0; call < maxModelCalls; call++) {
      cancellation?.throwIfCancelled();
      final allowTools =
          call < maxModelCalls - 2 &&
          retrievalRounds < maxRetrievalRounds &&
          toolCalls < maxToolCalls &&
          toolCharacters < maxToolCharacters;
      if (!allowTools && !finalNoticeSent) {
        messages.add({
          'role': 'user',
          'content': '本轮检索已结束。请用已有材料直接回答能确认的部分，区分建议与未知，不编造缺失事实。只返回规定的 JSON。',
        });
        finalNoticeSent = true;
      }
      // Service failures must keep the pending question retryable in the UI.
      // Available evidence does not turn a network error into a valid answer.
      final response = await model.complete(
        messages,
        tools: allowTools ? _tools : null,
        cancellation: cancellation,
      );
      cancellation?.throwIfCancelled();

      final rawCalls = response['tool_calls'];
      if (rawCalls is List && rawCalls.isNotEmpty) {
        if (allowTools) retrievalRounds++;
        messages.add({
          'role': 'assistant',
          'content': response['content'],
          'tool_calls': rawCalls,
        });
        for (final raw in rawCalls) {
          cancellation?.throwIfCancelled();
          final callData = raw is Map
              ? Map<String, Object?>.from(raw)
              : <String, Object?>{};
          var result = const _ToolResult({
            'error': '检索已达到本次技术边界，请使用已经取得的材料回答。',
          });
          try {
            if (allowTools &&
                toolCalls < maxToolCalls &&
                toolCharacters < maxToolCharacters) {
              toolCalls++;
              final rawFunction = callData['function'];
              if (rawFunction is! Map) {
                throw const FormatException('工具调用需要 function 对象。');
              }
              final function = Map<String, Object?>.from(rawFunction);
              final decoded = jsonDecode(
                function['arguments']?.toString() ?? '{}',
              );
              if (decoded is! Map) {
                throw const FormatException('工具参数必须是 JSON 对象。');
              }
              final arguments = Map<String, Object?>.from(decoded);
              result = await _dispatch(
                function['name']?.toString() ?? '',
                arguments,
                visible,
                cancellation,
              );
            }
          } on FormatException catch (error) {
            result = _ToolResult({'error': error.message});
          } on ModelException {
            rethrow;
          } on QuestionCancelledException {
            rethrow;
          } catch (_) {
            throw const ModelException('本机资料暂时无法读取，请重试；仍失败可重启 App。');
          }
          cancellation?.throwIfCancelled();
          var content = jsonEncode(result.payload);
          final remaining = maxToolCharacters - toolCharacters;
          if (content.length > remaining) {
            content = jsonEncode({'error': '结果超过本题剩余上下文，已停止展开；请使用已经取得的材料回答。'});
          } else {
            // An over-budget response was never shown to the model and cannot
            // silently become a citation or a context-expansion anchor.
            for (final row in result.sources) {
              visible[row.id] = row.toModelJson();
            }
          }
          toolCharacters += content.length;
          messages.add({
            'role': 'tool',
            'tool_call_id': callData['id']?.toString() ?? '',
            'content': content,
          });
        }
        continue;
      }

      try {
        return _validate(response['content']?.toString() ?? '', visible);
      } on FormatException catch (error) {
        messages.addAll([
          {
            'role': 'assistant',
            'content': response['content']?.toString() ?? '',
          },
          {
            'role': 'user',
            'content':
                '上一版不能安全显示：${error.message}。请修正格式或引用标签，保留能确认的有用内容；如确实缺少原文且工具仍可用，可以继续检索。不要编造消息 ID 或删掉必要依据来绕过核验，只返回规定的 JSON。',
          },
        ]);
      }
    }
    // Exhausted generation/validation is a technical failure, not evidence
    // that the archive lacks facts. Do not save it as an assistant answer.
    throw const ModelException('这次回答没有完成整理，请重试。已有对话会保留。');
  }

  Future<_ToolResult> _dispatch(
    String name,
    Map<String, Object?> args,
    Map<String, Map<String, Object?>> visible,
    QuestionCancellation? cancellation,
  ) async {
    cancellation?.throwIfCancelled();
    switch (name) {
      case 'search_chats':
        final requested = _integer(args['limit'], fallback: 30).clamp(1, 400);
        final speaker = _speaker(args['speaker']);
        final keywords = _keywords(args['keywords']);
        final matchAll = _choice(args['match'], ['any', 'all'], 'any') == 'all';
        final ascending =
            _choice(args['order'], ['asc', 'desc'], 'desc') == 'asc';
        final cursor = args['cursor']?.toString();
        final (start, end) = _dateRange(args);
        final rows = await _readQuery(
          () => memory.search(
            keyword: args['keyword']?.toString() ?? '',
            keywords: keywords,
            matchAll: matchAll,
            speaker: speaker,
            start: start,
            end: end,
            limit: requested + 1,
            ascending: ascending,
            cursor: cursor,
          ),
          cancellation: cancellation,
        );
        final selected = rows.take(requested).toList();
        final packed = _packMessages(selected);
        return _ToolResult({
          'messages': packed.map((row) => row.toModelJson()).toList(),
          'retrieval': {
            'requested_limit': requested,
            'returned': packed.length,
            'has_more':
                rows.length > requested || packed.length < selected.length,
            'character_budget_truncated': packed.length < selected.length,
            if (packed.isNotEmpty &&
                (rows.length > requested || packed.length < selected.length))
              'next_cursor': MemoryPack.cursorFor(packed.last),
            'order': ascending ? 'asc' : 'desc',
          },
        }, sources: packed);
      case 'get_context':
        final id = _messageId(args['msg_id']);
        _requireVisible(id, visible);
        final radius = _integer(
          args['radius_seconds'],
          fallback: 600,
        ).clamp(60, 86400);
        final limit = _integer(args['limit'], fallback: 80).clamp(1, 400);
        final rows = await _readMemory(
          () => memory.context(id, radiusSeconds: radius, limit: limit),
          cancellation: cancellation,
        );
        final packed = _packMessages(rows, anchorId: id);
        return _ToolResult({
          'messages': packed.map((row) => row.toModelJson()).toList(),
          'retrieval': {
            'returned': packed.length,
            'requested_limit': limit,
            'has_more': rows.length >= limit || packed.length < rows.length,
            'character_budget_truncated': packed.length < rows.length,
          },
        }, sources: packed);
      case 'get_references':
        final id = _messageId(args['msg_id']);
        _requireVisible(id, visible);
        final rows = _packMessages(
          await _readMemory(
            () => memory.references(id),
            cancellation: cancellation,
          ),
        );
        return _ToolResult({
          'references': rows.map((row) => row.toModelJson()).toList(),
        }, sources: rows);
      case 'get_profile':
        final speaker = _speaker(args['speaker']);
        final profile = await _readMemory(
          memory.profile,
          cancellation: cancellation,
        );
        final facts = (profile['facts'] as List? ?? const [])
            .whereType<Map>()
            .map((item) => Map<String, Object?>.from(item))
            .where((fact) => speaker == null || fact['subject'] == speaker)
            .take(50)
            .toList();
        final verifiedFacts = <Map<String, Object?>>[];
        final sources = <MemoryMessage>[];
        for (final fact in facts) {
          final factSources = <MemoryMessage>[];
          final factEvidence = <Map<String, Object?>>[];
          var verified = true;
          for (final raw
              in (fact['evidence'] as List? ?? const []).whereType<Map>()) {
            final id = raw['msg_id']?.toString() ?? '';
            final quote = raw['quote']?.toString() ?? '';
            final message = await _readMemory(
              () => memory.messageById(id),
              cancellation: cancellation,
            );
            if (message == null ||
                quote.trim().isEmpty ||
                !message.text.contains(quote)) {
              verified = false;
              break;
            }
            factSources.add(message);
            factEvidence.add({...message.toModelJson(), 'quote': quote});
          }
          if (verified && factEvidence.isNotEmpty) {
            verifiedFacts.add({...fact, 'evidence': factEvidence});
            sources.addAll(factSources);
          }
        }
        return _ToolResult({
          ...profile,
          'facts': verifiedFacts,
        }, sources: sources);
      case 'get_context_coverage':
        final id = _messageId(args['msg_id']);
        _requireVisible(id, visible);
        final anchor = await _readMemory(
          () => memory.messageById(id),
          cancellation: cancellation,
        );
        if (anchor == null) throw const FormatException('当前记忆包没有这条消息');
        final radius = _integer(
          args['radius_seconds'],
          fallback: 600,
        ).clamp(60, 86400);
        final rows = await _readMemory(
          () async => (await memory.database).rawQuery(
            '''SELECT kind,text_available,count(*) AS count FROM events
             WHERE ts>=? AND ts<=? GROUP BY kind,text_available''',
            [anchor.timestamp - radius, anchor.timestamp + radius],
          ),
          cancellation: cancellation,
        );
        return _ToolResult({
          'window_seconds': radius * 2,
          'counts': rows,
          'note': '数量只说明附近存在这些记录，不能证明通话接通或解释沉默。',
        });
      case 'count_chats':
        final speaker = _speaker(args['speaker']);
        final keywords = _keywords(args['keywords']);
        final matchAll = _choice(args['match'], ['any', 'all'], 'any') == 'all';
        final groupBy = _choice(args['group_by'], [
          'speaker',
          'day',
          'month',
        ], 'speaker');
        final (start, end) = _dateRange(args);
        return _ToolResult({
          ...await _readQuery(
            () => memory.countChats(
              keyword: args['keyword']?.toString() ?? '',
              keywords: keywords,
              matchAll: matchAll,
              speaker: speaker,
              start: start,
              end: end,
              groupBy: groupBy,
              limit: _integer(args['limit'], fallback: 90).clamp(1, 366),
            ),
            cancellation: cancellation,
          ),
          'note': '仅统计当前记忆包可读消息条数；不是词频、全部QQ消息或感情指标。日期按北京时间。关键词按字面子串匹配，分组截断不影响total。',
        });
      default:
        throw const FormatException('未知检索工具');
    }
  }

  List<MemoryMessage> _packMessages(
    List<MemoryMessage> rows, {
    String? anchorId,
  }) {
    final prioritized = [
      if (anchorId != null) ...rows.where((row) => row.id == anchorId),
      ...rows.where((row) => row.id != anchorId),
    ];
    final result = <MemoryMessage>[];
    var characters = 0;
    for (final row in prioritized) {
      final length = jsonEncode(row.toModelJson()).length;
      if (characters + length > 48000) break;
      characters += length;
      result.add(row);
    }
    if (anchorId != null) {
      result.sort((a, b) {
        final time = a.timestamp.compareTo(b.timestamp);
        return time != 0 ? time : int.parse(a.id).compareTo(int.parse(b.id));
      });
    }
    return result;
  }

  void _requireVisible(String id, Map<String, Map<String, Object?>> visible) {
    if (!visible.containsKey(id)) {
      throw const FormatException('请先在本次对话中检索到这条消息');
    }
  }

  Future<T> _readMemory<T>(
    Future<T> Function() operation, {
    QuestionCancellation? cancellation,
  }) async {
    try {
      return cancellation == null
          ? await operation()
          : await cancellation.run(operation);
    } on QuestionCancelledException {
      rethrow;
    } catch (_) {
      // SQL, file paths and storage failures are not evidence or tool-argument
      // mistakes. Keep the user's question pending and offer the UI retry path.
      throw const ModelException('本机资料暂时无法读取，请重试；仍失败可重启 App。');
    }
  }

  // Query parameter errors are correctable by the model. Storage errors are
  // not evidence gaps and must still reach the UI as retryable failures.
  Future<T> _readQuery<T>(
    Future<T> Function() operation, {
    QuestionCancellation? cancellation,
  }) async {
    try {
      return cancellation == null
          ? await operation()
          : await cancellation.run(operation);
    } on QuestionCancelledException {
      rethrow;
    } on FormatException {
      rethrow;
    } catch (_) {
      throw const ModelException('本机资料暂时无法读取，请重试；仍失败可重启 App。');
    }
  }

  String? _speaker(Object? value) {
    if (value == null) return null;
    if (value != 'self' && value != 'partner') {
      throw const FormatException('speaker 必须是 self 或 partner');
    }
    return value as String;
  }

  List<String> _keywords(Object? value) {
    if (value == null) return const [];
    if (value is! List ||
        value.length > 20 ||
        value.any((item) => item is! String || item.length > 200)) {
      throw const FormatException('keywords 必须是最多20个字符串，每个不超过200字');
    }
    return value.cast<String>();
  }

  String _choice(Object? value, List<String> choices, String fallback) {
    if (value == null) return fallback;
    if (!choices.contains(value)) {
      throw FormatException('参数必须是 ${choices.join("、")} 之一');
    }
    return value as String;
  }

  List<SavedQuestion> _retainHistory(List<SavedQuestion> history) {
    var characters = 0;
    final retained = <SavedQuestion>[];
    for (final turn in history.reversed) {
      final size = jsonEncode(turn.toJson()).length;
      if (characters + size > maxHistoryCharacters) break;
      characters += size;
      retained.add(turn);
    }
    return retained.reversed.toList();
  }

  Future<List<Map<String, Object?>>> _restoreHistory(
    List<SavedQuestion> history,
    Map<String, Map<String, Object?>> visible,
    QuestionCancellation? cancellation,
  ) async {
    final restored = <Map<String, Object?>>[];
    final cache = <String, MemoryMessage?>{};
    var sourceCharacters = 0;
    for (final turn in history) {
      cancellation?.throwIfCancelled();
      final evidence = <Map<String, Object?>>[];
      for (final saved in turn.evidence) {
        final id = saved.msgId;
        if (id == null || !RegExp(r'^\d{1,19}$').hasMatch(id)) continue;
        if (!cache.containsKey(id)) {
          cache[id] = await _readMemory(
            () => memory.messageById(id),
            cancellation: cancellation,
          );
        }
        final source = cache[id];
        if (source == null ||
            saved.quote.trim().isEmpty ||
            !source.text.contains(saved.quote)) {
          continue;
        }
        final canonical = source.toModelJson();
        if (!visible.containsKey(id)) {
          final length = jsonEncode(canonical).length;
          if (sourceCharacters + length > maxHistoryCharacters) continue;
          sourceCharacters += length;
          visible[id] = canonical;
        }
        evidence.add({
          'msg_id': id,
          'quote': saved.quote,
          'speaker': source.speaker,
        });
      }
      restored.add({'role': 'user', 'content': turn.question});
      restored.add({
        'role': 'assistant',
        'content': jsonEncode({
          'status': 'ok',
          'items': [
            {'title': turn.title, 'body': turn.body, 'evidence': evidence},
          ],
        }),
      });
    }
    return restored;
  }

  Map<String, dynamic> _validate(
    String content,
    Map<String, Map<String, Object?>> visible,
  ) {
    final decoded = jsonDecode(content);
    if (decoded is! Map || decoded['status'] != 'ok') {
      throw const FormatException('status 必须为 ok');
    }
    final items = decoded['items'];
    if (items is! List || items.length != 1 || items.first is! Map) {
      throw const FormatException('必须恰好返回一个答案');
    }
    final item = Map<String, Object?>.from(items.first as Map);
    final title = item['title']?.toString().trim() ?? '';
    final body = item['body']?.toString().trim() ?? '';
    if (title.isEmpty ||
        title.length > 60 ||
        body.isEmpty ||
        body.length > 4000) {
      throw const FormatException('答案标题或正文长度无效');
    }
    final rawEvidence = item['evidence'];
    if (rawEvidence is! List || rawEvidence.length > 12) {
      throw const FormatException('依据必须是 0–12 条');
    }
    final quotes = <Map<String, Object?>>[];
    for (final raw in rawEvidence) {
      if (raw is! Map) throw const FormatException('依据格式无效');
      final id = _messageId(raw['msg_id']);
      final quote = raw['quote']?.toString() ?? '';
      final source = visible[id];
      if (source == null ||
          quote.trim().isEmpty ||
          !(source['text']?.toString() ?? '').contains(quote)) {
        throw const FormatException('引用不是本次可见且已核验的逐字原话');
      }
      if (raw['speaker'] != source['speaker'] ||
          !const ['self', 'partner'].contains(raw['speaker'])) {
        throw FormatException(
          '引用 $id 的 speaker 应为 ${source['speaker']}，请按原文说话人修正引用和正文称呼；说话人不等于收话人或被描述者',
        );
      }
      quotes.add({
        'msg_id': id,
        'quote': quote,
        'speaker': source['speaker'] ?? 'unknown',
        'date': source['date'],
        'source_type': source['source_type'] ?? 'message_text',
      });
    }
    return {
      'status': 'done',
      'answer': {'title': title, 'body': body, 'quotes': quotes},
    };
  }

  int _integer(Object? value, {required int fallback}) =>
      value is int ? value : fallback;

  String _messageId(Object? value) {
    final id = value?.toString() ?? '';
    if (!RegExp(r'^\d{1,19}$').hasMatch(id)) {
      throw const FormatException('消息 ID 无效');
    }
    return id;
  }

  int? _dateBoundary(Object? value) {
    if (value == null || value.toString().trim().isEmpty) return null;
    var text = value.toString().trim();
    final parts = RegExp(
      r'^(\d{4})-(\d{2})-(\d{2})(?:[Tt ](\d{2}):(\d{2})(?::(\d{2})(?:\.\d{1,6})?)?([Zz]|[+-]\d{2}:?\d{2})?)?$',
    ).firstMatch(text);
    if (parts == null) throw const FormatException('日期需要有效的ISO日期或时间');
    final year = int.parse(parts[1]!);
    final month = int.parse(parts[2]!);
    final day = int.parse(parts[3]!);
    final hour = int.parse(parts[4] ?? '0');
    final minute = int.parse(parts[5] ?? '0');
    final second = int.parse(parts[6] ?? '0');
    if (year < 1 ||
        month < 1 ||
        month > 12 ||
        day < 1 ||
        day > DateTime.utc(year, month + 1, 0).day ||
        hour > 23 ||
        minute > 59 ||
        second > 59) {
      throw const FormatException('日期或时间不存在，请检查年月日和时分秒');
    }
    final zone = parts[7];
    if (zone != null && zone.length > 1) {
      final digits = zone.substring(1).replaceAll(':', '');
      if (int.parse(digits.substring(0, 2)) > 23 ||
          int.parse(digits.substring(2)) > 59) {
        throw const FormatException('时区偏移无效');
      }
    }
    if (RegExp(r'^\d{4}-\d{2}-\d{2}$').hasMatch(text)) {
      text = '${text}T00:00:00+08:00';
    } else if (!RegExp(r'(?:[zZ]|[+-]\d{2}:?\d{2})$').hasMatch(text)) {
      // A timezone-less tool argument is always a Beijing time, regardless
      // of the phone's timezone.
      text = '$text+08:00';
    }
    final parsed = DateTime.tryParse(text);
    if (parsed == null) throw const FormatException('日期格式无效');
    return parsed.toUtc().millisecondsSinceEpoch ~/ 1000;
  }

  (int?, int?) _dateRange(Map<String, Object?> args) {
    final start = _dateBoundary(args['start']);
    final end = _dateBoundary(args['end']);
    if (start != null && end != null && start >= end) {
      throw const FormatException('end是排他终点，必须晚于start；查询整天请用次日零点作为end');
    }
    return (start, end);
  }
}

class _ToolResult {
  final Map<String, Object?> payload;
  final List<MemoryMessage> sources;

  const _ToolResult(this.payload, {this.sources = const []});
}

const Map<String, Object?> _filterProperties = {
  'keyword': {'type': 'string', 'maxLength': 200},
  'keywords': {
    'type': 'array',
    'maxItems': 20,
    'items': {'type': 'string', 'maxLength': 200},
    'description': '与keyword合并。默认任意关键词命中，也可match=all要求同时出现；都是字面子串，不是正则。',
  },
  'match': {
    'type': 'string',
    'enum': ['any', 'all'],
  },
  'start': {'type': 'string', 'description': '含起点的北京时间ISO日期或时间'},
  'end': {'type': 'string', 'description': '不含终点的北京时间ISO日期或时间'},
  'speaker': {
    'type': 'string',
    'enum': ['self', 'partner'],
    'description': '发言者，不是收话人或被谈论者；省略则双方。',
  },
};

const List<Map<String, Object?>> _tools = [
  {
    'type': 'function',
    'function': {
      'name': 'search_chats',
      'description':
          '搜索聊天原话。支持空关键词浏览、多关键词组合、升降序和翻页。self是她，partner是赠送者。语音转写带标签。',
      'parameters': {
        'type': 'object',
        'properties': {
          ..._filterProperties,
          'order': {
            'type': 'string',
            'enum': ['asc', 'desc'],
            'description': '默认desc从最新开始；asc从最早开始。',
          },
          'cursor': {
            'type': 'string',
            'description': '照抄上页next_cursor；保持相同筛选条件和order继续，不自行编造。',
          },
          'limit': {'type': 'integer', 'minimum': 1, 'maximum': 400},
        },
        'additionalProperties': false,
      },
    },
  },
  {
    'type': 'function',
    'function': {
      'name': 'get_context',
      'description': '展开已检索消息前后的双方语境。',
      'parameters': {
        'type': 'object',
        'properties': {
          'msg_id': {'type': 'string', 'pattern': r'^\d+$'},
          'radius_seconds': {
            'type': 'integer',
            'minimum': 60,
            'maximum': 86400,
          },
          'limit': {
            'type': 'integer',
            'minimum': 1,
            'maximum': 400,
            'description': '默认80条，保留锚点并按时间返回最接近的消息。',
          },
        },
        'required': ['msg_id'],
        'additionalProperties': false,
      },
    },
  },
  {
    'type': 'function',
    'function': {
      'name': 'get_references',
      'description': '追踪已检索消息的明确回复或引用关系。',
      'parameters': {
        'type': 'object',
        'properties': {
          'msg_id': {'type': 'string', 'pattern': r'^\d+$'},
        },
        'required': ['msg_id'],
        'additionalProperties': false,
      },
    },
  },
  {
    'type': 'function',
    'function': {
      'name': 'get_profile',
      'description': '读取经过人工接受且仍有原话依据的历史档案。',
      'parameters': {
        'type': 'object',
        'properties': {
          'speaker': {
            'type': 'string',
            'enum': ['self', 'partner'],
            'description': '可选画像对象；不指定则读取双方。',
          },
        },
        'additionalProperties': false,
      },
    },
  },
  {
    'type': 'function',
    'function': {
      'name': 'get_context_coverage',
      'description': '统计消息附近的通话、媒体和不可用语音等记录；数量不是内容证据。',
      'parameters': {
        'type': 'object',
        'properties': {
          'msg_id': {'type': 'string', 'pattern': r'^\d+$'},
          'radius_seconds': {
            'type': 'integer',
            'minimum': 60,
            'maximum': 86400,
          },
        },
        'required': ['msg_id'],
        'additionalProperties': false,
      },
    },
  },
  {
    'type': 'function',
    'function': {
      'name': 'count_chats',
      'description':
          '按筛选条件精确统计可读消息条数，按说话人/北京日期/月份分组；无需翻遍原话计数。统计不提供原话，也不代表通话/媒体数量或感情状况。',
      'parameters': {
        'type': 'object',
        'properties': {
          ..._filterProperties,
          'group_by': {
            'type': 'string',
            'enum': ['speaker', 'day', 'month'],
          },
          'limit': {
            'type': 'integer',
            'minimum': 1,
            'maximum': 366,
            'description': '返回分组数，默认90；total始终是所有匹配消息数。',
          },
        },
        'additionalProperties': false,
      },
    },
  },
];
