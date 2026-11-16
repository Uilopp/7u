import 'dart:async';
import 'dart:io';

import 'package:couple_app/core/memory_pack.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:sqflite_common_ffi/sqflite_ffi.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  sqfliteFfiInit();
  databaseFactory = databaseFactoryFfi;

  late Directory directory;
  late ByteData asset;
  final messenger =
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger;
  const pathChannel = MethodChannel('plugins.flutter.io/path_provider');

  setUp(() async {
    directory = await Directory.systemTemp.createTemp('seven-u-pack-retry-');
    final source = '${directory.path}/bundled.db';
    final db = await databaseFactoryFfi.openDatabase(source);
    try {
      await db.execute('''CREATE TABLE messages(
        msg_id INTEGER PRIMARY KEY,ts INTEGER,speaker TEXT,text TEXT,
        source_type TEXT,transcript_origin TEXT)''');
      await db.insert('messages', {
        'msg_id': 123,
        'ts': 1750000000,
        'speaker': 'partner',
        'text': '恢复后的记忆。',
        'source_type': 'message_text',
      });
    } finally {
      await db.close();
    }
    final bytes = await File(source).readAsBytes();
    asset = bytes.buffer.asByteData(bytes.offsetInBytes, bytes.lengthInBytes);
    messenger.setMockMethodCallHandler(
      pathChannel,
      (_) async => directory.path,
    );
    messenger.setMockMessageHandler('flutter/assets', (_) async => asset);
  });

  tearDown(() async {
    messenger.setMockMethodCallHandler(pathChannel, null);
    messenger.setMockMessageHandler('flutter/assets', null);
    await directory.delete(recursive: true);
  });

  test(
    'path failure is shared once and the same memory pack can retry',
    () async {
      var pathCalls = 0;
      var assetCalls = 0;
      messenger.setMockMethodCallHandler(pathChannel, (_) async {
        if (++pathCalls == 1) {
          throw PlatformException(code: 'temporarily_unavailable');
        }
        return directory.path;
      });
      messenger.setMockMessageHandler('flutter/assets', (_) async {
        assetCalls++;
        return asset;
      });
      final pack = MemoryPack();
      final failed = pack.database;
      expect(identical(pack.database, failed), isTrue);
      await expectLater(failed, throwsA(isA<PlatformException>()));

      final retried = pack.database;
      expect(identical(retried, failed), isFalse);
      final recovered = await retried;
      try {
        expect((await pack.messageById('123'))!.text, '恢复后的记忆。');
        expect(identical(pack.database, retried), isTrue);
        expect(identical(await pack.database, recovered), isTrue);
        expect(pathCalls, 2);
        expect(assetCalls, 1);
      } finally {
        await recovered.close();
      }
    },
  );

  test(
    'missing asset can recover without recreating the memory pack',
    () async {
      var assetCalls = 0;
      messenger.setMockMessageHandler('flutter/assets', (_) async {
        assetCalls++;
        return assetCalls == 1 ? null : asset;
      });
      final pack = MemoryPack();
      await expectLater(pack.database, throwsA(isA<FlutterError>()));

      final recovered = await pack.database;
      try {
        expect((await pack.messageById('123'))!.text, '恢复后的记忆。');
        expect(assetCalls, 2);
      } finally {
        await recovered.close();
      }
    },
  );

  test('concurrent and later successful opens reuse one connection', () async {
    final releaseAsset = Completer<ByteData>();
    final requestedAsset = Completer<void>();
    var assetCalls = 0;
    messenger.setMockMessageHandler('flutter/assets', (_) async {
      assetCalls++;
      requestedAsset.complete();
      return releaseAsset.future;
    });
    final pack = MemoryPack();
    final first = pack.database;
    final second = pack.database;
    expect(identical(first, second), isTrue);
    await requestedAsset.future;
    releaseAsset.complete(asset);

    final opened = await first;
    try {
      expect(identical(await second, opened), isTrue);
      expect(identical(await pack.database, opened), isTrue);
      expect((await pack.messageById('123'))!.text, '恢复后的记忆。');
      expect(assetCalls, 1);
    } finally {
      await opened.close();
    }
  });
}
