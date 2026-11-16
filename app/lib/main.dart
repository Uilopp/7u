import 'package:flutter/material.dart';
import 'package:flutter_localizations/flutter_localizations.dart';

import 'core/api.dart';
import 'core/conversation_controller.dart';
import 'screens/free_question.dart';
import 'widgets/daily_memory_notifications.dart';
import 'widgets/launch_intro.dart';
import 'widgets/soft_ui.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  final api = Api();
  final conversations = ConversationController(api: api);
  runApp(SevenUApp(api: api, conversationController: conversations));
}

class SevenUApp extends StatefulWidget {
  final Api api;
  final bool showIntro;
  final ConversationController? conversationController;
  const SevenUApp({
    super.key,
    required this.api,
    this.showIntro = true,
    this.conversationController,
  });

  @override
  State<SevenUApp> createState() => _SevenUAppState();
}

class _SevenUAppState extends State<SevenUApp> {
  late ConversationController _conversations;

  @override
  void initState() {
    super.initState();
    _conversations =
        widget.conversationController ??
        ConversationController(api: widget.api);
  }

  @override
  void didUpdateWidget(covariant SevenUApp oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.api != widget.api ||
        oldWidget.conversationController != widget.conversationController) {
      if (oldWidget.conversationController == null) {
        _conversations.releaseWhenIdle();
      }
      _conversations =
          widget.conversationController ??
          ConversationController(api: widget.api);
    }
  }

  @override
  void dispose() {
    if (widget.conversationController == null) _conversations.releaseWhenIdle();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final screen = AskSevenUScreen(
      tasks: _conversations,
      notificationSettingsAction: const DailyMemorySettingsButton(),
    );
    return MaterialApp(
      title: '7u',
      locale: const Locale('zh', 'CN'),
      supportedLocales: const [Locale('zh', 'CN')],
      localizationsDelegates: GlobalMaterialLocalizations.delegates,
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        useMaterial3: true,
        colorScheme: ColorScheme.fromSeed(
          seedColor: const Color(0xff708579),
          surface: softBackground,
        ),
        scaffoldBackgroundColor: softBackground,
        appBarTheme: const AppBarTheme(
          backgroundColor: softBackground,
          surfaceTintColor: Colors.transparent,
        ),
        inputDecorationTheme: const InputDecorationTheme(
          filled: true,
          fillColor: softBackground,
          border: OutlineInputBorder(borderSide: BorderSide.none),
        ),
      ),
      home: DailyMemoryNotifications(
        conversations: _conversations,
        child: widget.showIntro ? LaunchIntro(child: screen) : screen,
      ),
    );
  }
}
