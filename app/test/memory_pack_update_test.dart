import 'dart:io';
import 'package:couple_app/core/conversations.dart';
import 'package:couple_app/core/local_store.dart';
import 'package:couple_app/core/memory_pack.dart';
import 'package:couple_app/core/question_history.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:sqflite_common_ffi/sqflite_ffi.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  sqfliteFfiInit();
  databaseFactory = databaseFactoryFfi;

  test('new APK memory replaces the old pack while conversations survive', () async {
    final directory = await Directory.systemTemp.createTemp(
      'seven-u-pack-test-',
    );
    addTearDown(() => directory.delete(recursive: true));
    final messenger =
        TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger;
    messenger.setMockMethodCallHandler(
      const MethodChannel('plugins.flutter.io/path_provider'),
      (_) async => directory.path,
    );
    addTearDown(
      () => messenger.setMockMethodCallHandler(
        const MethodChannel('plugins.flutter.io/path_provider'),
        null,
      ),
    );

    Future<ByteData> packBytes(String name, String text) async {
      final path = '${directory.path}/$name.db';
      final db = await databaseFactoryFfi.openDatabase(path);
      await db.execute('''CREATE TABLE messages(
        msg_id INTEGER PRIMARY KEY,ts INTEGER,speaker TEXT,text TEXT,
        source_type TEXT,transcript_origin TEXT)''');
      await db.execute(
        'CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT)',
      );
      await db.insert('messages', {
        'msg_id': 123,
        'ts': 1750000000,
        'speaker': 'partner',
        'text': text,
        'source_type': 'voice_transcript',
        'transcript_origin': 'human_verified',
      });
      await db.close();
      final bytes = await File(path).readAsBytes();
      return bytes.buffer.asByteData(bytes.offsetInBytes, bytes.lengthInBytes);
    }

    var asset = await packBytes('first', '旧语音转写');
    messenger.setMockMessageHandler('flutter/assets', (message) async => asset);
    addTearDown(() => messenger.setMockMessageHandler('flutter/assets', null));

    final local = LocalStore(
      databaseName: '${directory.path}/seven_u_local.db',
    );
    final saved = Conversation.create().copyWith(
      turns: [
        SavedQuestion(
          question: '之前他说过什么？',
          title: '旧回答',
          body: '那时保存的回答。',
          evidence: const [],
          createdAt: DateTime(2026, 9, 1),
        ),
      ],
    );
    await ConversationRepository(local).save([saved], saved.id);
    final oldPack = MemoryPack();
    expect((await oldPack.messageById('123'))!.text, '旧语音转写');
    await (await oldPack.database).close();
    await (await local.database).close();

    // Android restarts the process after an APK replacement. A fresh runtime
    // installs its bundled asset, while the separate user database is retained.
    asset = await packBytes('second', '修正后的语音转写。');
    final newPack = MemoryPack();
    expect((await newPack.messageById('123'))!.text, '修正后的语音转写。');
    final reopened = LocalStore(
      databaseName: '${directory.path}/seven_u_local.db',
    );
    final state = await ConversationRepository(reopened).load();
    expect(state.conversations.single.turns.single.body, '那时保存的回答。');
    expect(state.activeId, saved.id);
    await (await newPack.database).close();
    await (await reopened.database).close();
  });
}
