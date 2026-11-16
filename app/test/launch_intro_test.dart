import 'package:couple_app/widgets/launch_intro.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  testWidgets('intro initializes child but holds interaction until revealed', (
    tester,
  ) async {
    var presses = 0;
    await tester.pumpWidget(
      MaterialApp(
        home: LaunchIntro(
          child: Scaffold(
            body: TextButton(
              onPressed: () => presses++,
              child: const Text('开始聊天'),
            ),
          ),
        ),
      ),
    );
    expect(find.text('开始聊天'), findsOneWidget);
    expect(find.byKey(const ValueKey('launch-intro')), findsOneWidget);
    await tester.tap(find.text('开始聊天'), warnIfMissed: false);
    expect(presses, 0);

    await tester.pump();
    await tester.pump(const Duration(milliseconds: 1200));
    expect(find.byKey(const ValueKey('launch-intro')), findsNothing);
    await tester.tap(find.text('开始聊天'));
    expect(presses, 1);
  });

  testWidgets('rebuilding child does not replay the intro', (tester) async {
    Widget app(String label) => MaterialApp(
      home: LaunchIntro(child: Scaffold(body: Text(label))),
    );
    await tester.pumpWidget(app('第一段对话'));
    await tester.pumpAndSettle();
    await tester.pumpWidget(app('第二段对话'));
    expect(find.text('第二段对话'), findsOneWidget);
    expect(find.byKey(const ValueKey('launch-intro')), findsNothing);
  });

  testWidgets('reduced motion opens the app immediately', (tester) async {
    await tester.pumpWidget(
      const MaterialApp(
        home: MediaQuery(
          data: MediaQueryData(disableAnimations: true),
          child: LaunchIntro(child: Scaffold(body: Text('可以聊天了'))),
        ),
      ),
    );
    expect(find.text('可以聊天了'), findsOneWidget);
    expect(find.byKey(const ValueKey('launch-intro')), findsNothing);
    expect(tester.binding.transientCallbackCount, 0);
  });

  testWidgets('leaving during intro disposes the animation', (tester) async {
    await tester.pumpWidget(
      const MaterialApp(home: LaunchIntro(child: SizedBox.expand())),
    );
    await tester.pump(const Duration(milliseconds: 150));
    await tester.pumpWidget(const SizedBox());
    await tester.pump(const Duration(seconds: 2));
    expect(tester.takeException(), isNull);
    expect(tester.binding.transientCallbackCount, 0);
  });
}
