// Explicit opt-in live diagnostics. Default `flutter test` never calls a model.
// Launch with `python -m tools.qa.check_live_followup --reverse` from the repo root
// so credentials stay out of argv. This explicitly uses real API credits.
import 'dart:convert';
import 'dart:io';

import 'package:couple_app/core/memory_pack.dart';
import 'package:couple_app/core/model_client.dart';
import 'package:couple_app/core/question_agent.dart';
import 'package:couple_app/core/question_history.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:sqflite_common_ffi/sqflite_ffi.dart';

const _enabled = bool.fromEnvironment('LIVE_FOLLOWUP_TEST');
const _outputPath = String.fromEnvironment('LIVE_FOLLOWUP_OUTPUT');
const _questions = [
  '最近有什么值得纪念的事情',
  '我最近对他说的话是什么',
  if (bool.fromEnvironment('LIVE_FOLLOWUP_REVERSE')) '那他最近对我说了什么？',
];

void main() {
  test(
    'live followup preserves the current speaker',
    () async {
      if (_outputPath.isEmpty || ModelClient.defaultKey.isEmpty) {
        fail('Live launcher must provide private output directory and key.');
      }
      final output = Directory(_outputPath);
      await output.create(recursive: true);
      final callsFile = File('${output.path}/calls.jsonl');
      final reportFile = File('${output.path}/report.json');
      final report = <String, Object?>{
        'started_at': DateTime.now().toUtc().toIso8601String(),
        'questions': _questions,
        'model': ModelClient.defaultModel,
        'calls': <Map<String, Object?>>[],
        'turns': <Map<String, Object?>>[],
      };
      String redact(String value) =>
          value.replaceAll(ModelClient.defaultKey, '[MODEL_KEY_REDACTED]');
      Future<void> persist() async {
        await reportFile.writeAsString(
          redact(const JsonEncoder.withIndent('  ').convert(report)),
          flush: true,
        );
      }

      Future<void> event(Map<String, Object?> value) async {
        await callsFile.writeAsString(
          '${redact(jsonEncode(value))}\n',
          mode: FileMode.append,
          flush: true,
        );
      }

      sqfliteFfiInit();
      databaseFactory = databaseFactoryFfi;
      final database = await openDatabase(
        File('assets/memory/memory_pack.db').absolute.path,
        readOnly: true,
        singleInstance: false,
      );
      final memory = MemoryPack.forDatabase(database);
      const live = ModelClient();
      var turn = 0;
      var callNumber = 0;
      final allCalls = report['calls']! as List<Map<String, Object?>>;
      final turns = report['turns']! as List<Map<String, Object?>>;
      final total = Stopwatch()..start();
      final traced = ModelClient(
        transport: (body) async {
          final snapshot = Map<String, Object?>.from(
            jsonDecode(jsonEncode(body)) as Map,
          );
          final messages = (snapshot['messages']! as List)
              .map((row) => Map<String, Object?>.from(row as Map))
              .toList();
          final tools = (snapshot['tools'] as List?)
              ?.map((row) => Map<String, Object?>.from(row as Map))
              .toList();
          final system = messages.first['content']?.toString() ?? '';
          final target =
              RegExp(r'本题目标说话人：(self|partner)').firstMatch(system)?.group(1) ??
              (system.contains('本题目标：两个人的关系') ? 'relationship' : 'unspecified');
          final corrections = messages
              .where((row) => row['role'] == 'user')
              .map((row) => row['content']?.toString() ?? '')
              .where((content) => content.startsWith('上一版不能安全显示：'))
              .toList();
          final call = <String, Object?>{
            'turn': turn + 1,
            'call': ++callNumber,
            'system_target': target,
            'tools_enabled': tools != null,
            'validation_corrections': corrections,
            'request': snapshot,
          };
          allCalls.add(call);
          await event({'event': 'request', ...call});
          await persist();
          final watch = Stopwatch()..start();
          try {
            final response = await live.complete(messages, tools: tools);
            call['elapsed_ms'] = watch.elapsedMilliseconds;
            call['response'] = response;
            final strategy = (response['tool_calls'] as List? ?? [])
                .whereType<Map>()
                .map((raw) => raw['function'])
                .whereType<Map>()
                .map((function) {
                  Object? args;
                  try {
                    args = jsonDecode(
                      function['arguments']?.toString() ?? '{}',
                    );
                  } on FormatException {
                    args = {'invalid_json': true};
                  }
                  return {'tool': function['name'], 'arguments': args};
                })
                .toList();
            call['tool_strategy'] = strategy;
            await event({'event': 'response', ...call});
            await persist();
            // No answers, excerpts, search keywords, or credentials on stdout.
            // ignore: avoid_print
            print(
              jsonEncode({
                'live_turn': turn + 1,
                'call': callNumber,
                'system_target': target,
                'tools': strategy.map((item) => item['tool']).toList(),
                'validation_correction_count': corrections.length,
                'elapsed_ms': call['elapsed_ms'],
              }),
            );
            return response;
          } catch (error) {
            call['elapsed_ms'] = watch.elapsedMilliseconds;
            call['error'] = redact(error.toString());
            await event({'event': 'error', ...call});
            await persist();
            rethrow;
          }
        },
      );
      final agent = QuestionAgent(
        memory: memory,
        model: traced,
        promptLoader: () => File('assets/agent/prompt.md').readAsString(),
      );
      final history = <SavedQuestion>[];
      try {
        for (turn = 0; turn < _questions.length; turn++) {
          final watch = Stopwatch()..start();
          final result = await agent.ask(_questions[turn], history: history);
          final saved = SavedQuestion.fromResult(_questions[turn], result);
          final evidenceChecks = <Map<String, Object?>>[];
          for (final item in saved.evidence) {
            final source = item.msgId == null
                ? null
                : await memory.messageById(item.msgId!);
            evidenceChecks.add({
              'msg_id': item.msgId,
              'speaker': source?.speaker,
              'quote_exact': source?.text.contains(item.quote) == true,
              'speaker_exact': source?.speaker == item.speaker,
            });
          }
          turns.add({
            'turn': turn + 1,
            'question': _questions[turn],
            'elapsed_ms': watch.elapsedMilliseconds,
            'result': result,
            'saved_question': saved.toJson(),
            'evidence_checks': evidenceChecks,
          });
          history.add(saved);
          await persist();
          // ignore: avoid_print
          print(
            jsonEncode({
              'live_turn_complete': turn + 1,
              'elapsed_ms': watch.elapsedMilliseconds,
              'fallback': result.containsKey('fallback_reason'),
              'fallback_reason': result['fallback_reason'],
              'evidence_count': saved.evidence.length,
              'evidence_speakers': saved.evidence
                  .map((e) => e.speaker)
                  .toList(),
            }),
          );
        }
        report['total_elapsed_ms'] = total.elapsedMilliseconds;
        report['completed'] = true;
        await persist();
        expect(turns.length, _questions.length);
        for (final item in turns) {
          final result = item['result']! as Map;
          expect(
            result.containsKey('fallback_reason'),
            isFalse,
            reason: 'A live answer must not exhaust the validation loop.',
          );
          for (final check in item['evidence_checks']! as List) {
            expect((check as Map)['quote_exact'], isTrue);
            expect(check['speaker_exact'], isTrue);
          }
        }
        final secondEvidence = turns[1]['evidence_checks']! as List;
        expect(secondEvidence, isNotEmpty);
        final ownWords = secondEvidence
            .where((e) => (e as Map)['speaker'] == 'self')
            .length;
        expect(
          ownWords,
          greaterThanOrEqualTo((secondEvidence.length + 1) ~/ 2),
          reason: 'Her own words must primarily cite self messages.',
        );
        if (_questions.length == 3) {
          final thirdEvidence = turns[2]['evidence_checks']! as List;
          expect(thirdEvidence, isNotEmpty);
          final hisWords = thirdEvidence
              .where((e) => (e as Map)['speaker'] == 'partner')
              .length;
          expect(
            hisWords,
            greaterThanOrEqualTo((thirdEvidence.length + 1) ~/ 2),
            reason:
                'The reversed question must primarily cite partner messages.',
          );
        }
      } catch (error) {
        report['test_error'] = redact(error.toString());
        report['total_elapsed_ms'] = total.elapsedMilliseconds;
        await persist();
        rethrow;
      } finally {
        await database.close();
      }
    },
    skip: !_enabled,
    timeout: const Timeout(Duration(minutes: 20)),
  );
}
