import 'dart:async';

import 'package:couple_app/core/background_answers.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  const channel = MethodChannel(AndroidBackgroundAnswers.channelName);
  final messenger =
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger;
  late AndroidBackgroundAnswers bridge;
  final calls = <MethodCall>[];

  setUp(() {
    calls.clear();
    bridge = AndroidBackgroundAnswers();
    messenger.setMockMethodCallHandler(channel, (call) async {
      calls.add(call);
      return switch (call.method) {
        'begin' => {'notificationsEnabled': true},
        'requestNotificationPermission' => false,
        'takeLaunchConversation' => 'saved-conversation',
        _ => null,
      };
    });
  });

  tearDown(() {
    bridge.dispose();
    messenger.setMockMethodCallHandler(channel, null);
  });

  Future<void> event(String method, Object? arguments) async {
    final done = Completer<void>();
    messenger.handlePlatformMessage(
      AndroidBackgroundAnswers.channelName,
      const StandardMethodCodec().encodeMethodCall(
        MethodCall(method, arguments),
      ),
      (_) => done.complete(),
    );
    await done.future;
    await Future<void>.delayed(Duration.zero);
  }

  test(
    'native lifecycle carries only IDs and outcome, never private text',
    () async {
      final started = await bridge.begin(jobId: 'j1', conversationId: 'c1');
      expect(started.notificationsEnabled, isTrue);
      await bridge.finish(
        jobId: 'j1',
        conversationId: 'c1',
        outcome: BackgroundAnswerOutcome.success,
      );
      expect(calls.map((c) => c.method), ['begin', 'finish']);
      expect(calls[0].arguments, {'jobId': 'j1', 'conversationId': 'c1'});
      expect(calls[1].arguments, {
        'jobId': 'j1',
        'conversationId': 'c1',
        'outcome': 'success',
      });
    },
  );

  test(
    'native start rejection propagates rather than pretending protected',
    () async {
      messenger.setMockMethodCallHandler(channel, (_) async {
        throw PlatformException(code: 'BACKGROUND_START_DENIED');
      });
      await expectLater(
        bridge.begin(jobId: 'j1', conversationId: 'c1'),
        throwsA(isA<PlatformException>()),
      );
    },
  );

  test('permission denied is distinct from failed background start', () async {
    expect(await bridge.requestNotificationPermission(), isFalse);
    expect(await bridge.takeLaunchConversation(), 'saved-conversation');
  });

  test(
    'native events are typed, malformed or unknown events ignored',
    () async {
      final events = <BackgroundAnswerEvent>[];
      final subscription = bridge.events.listen(events.add);
      await event('openConversation', {'conversationId': 'c1'});
      await event('openConversation', {'conversationId': ''});
      await event('openConversation', null);
      await event('unknown', {'conversationId': 'c2'});
      await event('jobsInterrupted', {
        'jobIds': ['j1', 2, '', 'j2'],
      });
      expect(events, hasLength(2));
      expect((events[0] as OpenAnswerConversation).conversationId, 'c1');
      expect((events[1] as InterruptedAnswerJobs).jobIds, ['j1', 'j2']);
      await subscription.cancel();
    },
  );

  test('dispose is idempotent and closes subscription', () async {
    final done = Completer<void>();
    bridge.events.listen((_) {}, onDone: done.complete);
    bridge.dispose();
    bridge.dispose();
    await done.future;
  });

  test(
    'unsupported platforms do not claim Android notification permissions',
    () async {
      final noop = NoopBackgroundAnswers();
      expect(await noop.takeLaunchConversation(), isNull);
      expect(await noop.events.toList(), isEmpty);
      await noop.begin(jobId: 'j', conversationId: 'c');
      await noop.finish(
        jobId: 'j',
        conversationId: 'c',
        outcome: BackgroundAnswerOutcome.silent,
      );
      noop.dispose();
    },
  );
}
