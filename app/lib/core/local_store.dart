import 'dart:convert';

import 'package:sqflite/sqflite.dart';

/// Small app-local store. The bundled memory pack is separate and read-only.
class LocalStore {
  final String databaseName;
  LocalStore({this.databaseName = 'seven_u_local.db'});

  Future<Database>? _opening;
  Future<Database> get database => _opening ??= _openRecoverably();

  Future<Database> _openRecoverably() async {
    try {
      return await openDatabase(
        databaseName,
        version: 1,
        onCreate: (db, version) => db.execute(
          'CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)',
        ),
      );
    } catch (_) {
      _opening = null;
      rethrow;
    }
  }

  Future<void> saveMetadata(String key, Map<String, dynamic> value) async {
    await (await database).insert('metadata', {
      'key': key,
      'value': jsonEncode(value),
    }, conflictAlgorithm: ConflictAlgorithm.replace);
  }

  Future<Map<String, dynamic>?> loadMetadata(String key) async {
    final rows = await (await database).query(
      'metadata',
      where: 'key=?',
      whereArgs: [key],
      limit: 1,
    );
    if (rows.isEmpty) return null;
    return Map<String, dynamic>.from(
      jsonDecode(rows.first['value'] as String) as Map,
    );
  }
}
