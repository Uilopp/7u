import 'dart:io';

import 'package:couple_app/core/local_store.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:sqflite_common_ffi/sqflite_ffi.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  sqfliteFfiInit();
  databaseFactory = databaseFactoryFfi;

  test(
    'a transient open failure does not poison later local history retries',
    () async {
      final temporary = await Directory.systemTemp.createTemp(
        'seven-u-local-retry-',
      );
      final blocker = File('${temporary.path}/blocked');
      await blocker.writeAsString('temporary path obstruction');
      final store = LocalStore(databaseName: '${blocker.path}/conversation.db');
      final failed = store.database;
      expect(identical(failed, store.database), isTrue);
      await expectLater(failed, throwsA(anything));
      await blocker.delete();
      await Directory(blocker.path).create();
      try {
        await store.saveMetadata('test', {'preserved': true});
        expect(await store.loadMetadata('test'), {'preserved': true});
        expect(identical(store.database, failed), isFalse);
        expect(identical(await store.database, await store.database), isTrue);
      } finally {
        await (await store.database).close();
        await temporary.delete(recursive: true);
      }
    },
  );
}
