import 'package:flutter/services.dart';

Future<String?> readInstalledVersion() async {
  try {
    return await const MethodChannel('seven_u/app_info')
        .invokeMethod<String>('versionName');
  } on MissingPluginException {
    return null;
  } on PlatformException {
    return null;
  }
}
