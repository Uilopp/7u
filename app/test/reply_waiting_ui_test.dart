import 'package:couple_app/core/api.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';

import 'free_question_screen_test.dart' as fixtures;

const waitingKey = ValueKey('reply-waiting-indicator');

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  const versionChannel = MethodChannel('seven_u/app_info');
  final messenger =
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger;
  setUp(
    () => messenger.setMockMethodCallHandler(
      versionChannel,
      (_) async => '0.1.260928',
    ),
  );
  tearDown(() => messenger.setMockMethodCallHandler(versionChannel, null));

  testWidgets(
    'pending dots pulse quietly and the ticker stops after completion',
    (tester) async {
      final api = fixtures.TestApi();
      await fixtures.launch(tester, api);
      expect(find.byKey(waitingKey), findsNothing);
      await fixtures.send(tester, '想一起做点什么');
      final before = tester
          .widget<Opacity>(find.byKey(const ValueKey('reply-waiting-dot-0')))
          .opacity;
      await tester.pump(const Duration(milliseconds: 300));
      final after = tester
          .widget<Opacity>(find.byKey(const ValueKey('reply-waiting-dot-0')))
          .opacity;
      expect(after, isNot(before));
      expect(
        after,
        isNot(
          tester
              .widget<Opacity>(
                find.byKey(const ValueKey('reply-waiting-dot-1')),
              )
              .opacity,
        ),
      );
      expect(find.text('7u 正在想…').hitTestable(), findsOneWidget);
      expect(find.text('7u 正在想…'), findsOneWidget);
      expect(find.byType(CircularProgressIndicator), findsNothing);
      api.answer(0, '可以一起散个步。');
      await tester.pumpAndSettle();
      expect(find.byKey(waitingKey), findsNothing);
    },
  );

  testWidgets(
    'an error removes waiting and retry restores it until completion',
    (tester) async {
      final api = fixtures.TestApi();
      await fixtures.launch(tester, api);
      await fixtures.send(tester, '网络断开时的问题');
      api.replies[0].completeError(const ApiException('暂时断网'));
      await tester.pumpAndSettle();
      expect(find.byKey(waitingKey), findsNothing);
      expect(find.text('暂时断网'), findsOneWidget);
      await tester.tap(find.byKey(const ValueKey('retry-message')));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 300));
      expect(find.text('7u 正在想…').hitTestable(), findsOneWidget);
      api.answer(1, '重试后好了。');
      await tester.pumpAndSettle();
      expect(find.byKey(waitingKey), findsNothing);
    },
  );

  testWidgets('waiting belongs to the visible conversation only', (
    tester,
  ) async {
    final api = fixtures.TestApi();
    await fixtures.launch(tester, api);
    await fixtures.send(tester, '会话一还在等待');
    await tester.tap(find.byKey(const ValueKey('new-conversation')));
    await tester.pumpAndSettle();
    expect(find.byKey(waitingKey), findsNothing);
    await fixtures.send(tester, '会话二也在等待');
    await tester.pump(const Duration(milliseconds: 300));
    expect(find.byKey(waitingKey), findsOneWidget);
    api.answer(0, '会话一完成。');
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 300));
    expect(find.byKey(waitingKey), findsOneWidget);
    expect(find.text('会话一完成。'), findsNothing);
    api.answer(1, '会话二完成。');
    await tester.pumpAndSettle();
    expect(find.byKey(waitingKey), findsNothing);
  });

  testWidgets(
    'status stays visible during a followup while reading a long answer',
    (tester) async {
      final api = fixtures.TestApi();
      await fixtures.launch(tester, api);
      await fixtures.send(tester, '先聊一段很长的回忆');
      api.answer(0, List.filled(100, '这是一段较长的回忆。').join('\n'));
      await tester.pumpAndSettle();
      await fixtures.send(tester, '那后来呢');
      await tester.pump(const Duration(milliseconds: 300));
      final timeline = find.byWidgetPredicate(
        (widget) =>
            widget is ListView && widget.key.toString().contains('timeline-'),
      );
      await tester.drag(timeline, const Offset(0, 1000));
      await tester.pump(const Duration(seconds: 1));
      expect(find.text('7u 正在想…').hitTestable(), findsOneWidget);
      api.answer(1, '后来接着聊了下去。');
      await tester.pumpAndSettle();
      expect(find.byKey(waitingKey), findsNothing);
    },
  );

  testWidgets(
    'landscape keyboard leaves room for the quiet status and composer',
    (tester) async {
      tester.view.physicalSize = const Size(640, 360);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      addTearDown(tester.view.resetViewInsets);
      final api = fixtures.TestApi();
      await fixtures.launch(tester, api);
      tester.view.viewInsets = const FakeViewPadding(bottom: 180);
      await tester.pumpAndSettle();
      await fixtures.send(tester, '横屏里继续聊');
      await tester.pump(const Duration(milliseconds: 300));
      expect(tester.takeException(), isNull);
      expect(find.text('7u 正在想…').hitTestable(), findsOneWidget);
      expect(
        tester.getBottomLeft(find.byKey(const ValueKey('ask-seven-u'))).dy,
        lessThanOrEqualTo(180),
      );
      api.answer(0, '可以。');
      await tester.pumpAndSettle();
    },
  );

  testWidgets('an unusually tall keyboard cannot overflow the pending footer', (
    tester,
  ) async {
    tester.view.physicalSize = const Size(640, 320);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    addTearDown(tester.view.resetViewInsets);
    final api = fixtures.TestApi();
    await fixtures.launch(tester, api);
    await fixtures.send(tester, '很小的空间里也在等待');
    tester.view.viewInsets = const FakeViewPadding(bottom: 220);
    await tester.pump(const Duration(milliseconds: 300));
    expect(tester.takeException(), isNull);
    expect(find.byKey(waitingKey), findsOneWidget);
    expect(
      tester.getBottomLeft(find.byKey(const ValueKey('ask-seven-u'))).dy,
      lessThanOrEqualTo(100),
    );
    api.answer(0, '回答好了。');
    await tester.pumpAndSettle();
    expect(tester.takeException(), isNull);
  });
}
