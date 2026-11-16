import 'package:flutter/material.dart';

import '../core/app_version.dart';

/// Read the installed package, so the label cannot drift from the actual APK.
class AppVersion extends StatefulWidget {
  const AppVersion({super.key});

  @override
  State<AppVersion> createState() => _AppVersionState();
}

class _AppVersionState extends State<AppVersion> {
  late final Future<String?> version = readInstalledVersion();

  @override
  Widget build(BuildContext context) => FutureBuilder<String?>(
    future: version,
    builder: (context, snapshot) {
      final value = snapshot.data;
      if (value == null || value.isEmpty) return const SizedBox.shrink();
      return Text(
        'v$value',
        key: const ValueKey('app-version'),
        semanticsLabel: '版本 $value',
        style: const TextStyle(
          fontSize: 11,
          fontWeight: FontWeight.w400,
          color: Color(0xff697970),
          letterSpacing: .3,
        ),
      );
    },
  );
}
