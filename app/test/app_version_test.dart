import 'package:couple_app/widgets/app_version.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  const channel = MethodChannel('seven_u/app_info');
  final messenger =
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger;

  tearDown(() => messenger.setMockMethodCallHandler(channel, null));

  testWidgets('shows installed version, without internal build code', (
    tester,
  ) async {
    messenger.setMockMethodCallHandler(channel, (call) async {
      expect(call.method, 'versionName');
      return '1.0.260927';
    });
    await tester.pumpWidget(
      const MaterialApp(home: Scaffold(body: AppVersion())),
    );
    await tester.pumpAndSettle();
    expect(find.text('v1.0.260927'), findsOneWidget);
  });

  testWidgets('unavailable package info never invents a version', (
    tester,
  ) async {
    await tester.pumpWidget(
      const MaterialApp(home: Scaffold(body: AppVersion())),
    );
    await tester.pumpAndSettle();
    expect(find.byKey(const ValueKey('app-version')), findsNothing);
    expect(tester.takeException(), isNull);
  });
}
