"""Prepare synthetic-only assets for a fresh clone; never overwrite local data.

Uses only Python's standard library. No archive, credentials, network or model
is read. Release builds must use the private maintenance workflow instead.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile


APP = Path(__file__).resolve().parents[1]
TIMESTAMP = 1704067200  # Fixed synthetic date, 2024-01-01 08:00 in Beijing.


def prepare_assets(app: Path = APP) -> dict:
    app = Path(app).resolve()
    if not (app / 'pubspec.yaml').is_file():
        raise ValueError('Target must be the Flutter directory containing pubspec.yaml.')
    source_prompt = app.parent / 'agent/prompt.md'
    if not source_prompt.is_file():
        raise ValueError('The canonical agent/prompt.md is missing; prepare from a complete clone.')
    memory = app / 'assets/memory/memory_pack.db'
    daily = app / 'assets/memory/daily_memories.json'
    prompt = app / 'assets/agent/prompt.md'
    if memory.exists() or daily.exists():
        raise FileExistsError('Memory assets already exist. Nothing was changed; this tool never replaces private data.')

    topics = ('公园', '面包', '星星', '书签', '云朵')
    kinds = ('question', 'statement', 'memory')
    items = []
    for i in range(15):
        topic = topics[i // 3]
        kind = kinds[i % 3]
        quote = f'合成测试第{i + 1}条：一起聊聊{topic}。'
        text = {'question': f'想聊聊{topic}吗？', 'statement': f'{topic}是测试主题。',
                'memory': f'记得测试里的{topic}。'}[kind]
        msg_id = str(5001 + i)
        identifier = hashlib.sha256(f'{msg_id}\n{quote}\n{kind}\n{text}\n{topic}'.encode()).hexdigest()[:24]
        items.append({'id': identifier, 'kind': kind, 'text': text, 'quote': quote,
                      'msgId': msg_id, 'date': '2024-01-01',
                      'speaker': 'self' if i % 2 == 0 else 'partner', 'anchor': topic})

    memory.parent.mkdir(parents=True, exist_ok=True)
    prompt.parent.mkdir(parents=True, exist_ok=True)
    created = []
    try:
        with tempfile.TemporaryDirectory(prefix='.synthetic-', dir=memory.parent) as directory:
            temporary = Path(directory)
            with closing(sqlite3.connect(temporary / 'memory_pack.db')) as db, db:
                db.executescript('''
                    CREATE TABLE messages(msg_id INTEGER PRIMARY KEY, ts INTEGER NOT NULL,
                        speaker TEXT NOT NULL, text TEXT NOT NULL, source_type TEXT NOT NULL,
                        transcript_origin TEXT);
                    CREATE INDEX messages_time ON messages(ts,msg_id);
                    CREATE INDEX messages_speaker_time ON messages(speaker,ts,msg_id);
                    CREATE TABLE message_links(msg_id INTEGER NOT NULL,target_msg_id INTEGER NOT NULL,
                        PRIMARY KEY(msg_id,target_msg_id));
                    CREATE TABLE events(msg_id INTEGER PRIMARY KEY,ts INTEGER NOT NULL,
                        speaker TEXT NOT NULL,kind TEXT NOT NULL,text_available INTEGER NOT NULL);
                    CREATE INDEX events_time ON events(ts,msg_id);
                    CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                ''')
                for i, item in enumerate(items):
                    db.execute('INSERT INTO messages VALUES(?,?,?,?,?,NULL)',
                        (int(item['msgId']), TIMESTAMP + i, item['speaker'], item['quote'], 'message_text'))
                    db.execute('INSERT INTO events VALUES(?,?,?,?,?)',
                        (int(item['msgId']), TIMESTAMP + i, item['speaker'], 'text', 1))
                db.execute('INSERT INTO message_links VALUES(5002,5001)')
                db.executemany('INSERT INTO metadata VALUES(?,?)', {
                    'schema_version': '1', 'built_at': str(TIMESTAMP),
                    'latest_message_ts': str(TIMESTAMP + 14), 'is_synthetic': 'true',
                    'profile': json.dumps({'status': 'synthetic_test_only', 'facts': []}),
                }.items())
                if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise RuntimeError('Synthetic database integrity check failed.')
            (temporary / 'daily_memories.json').write_text(json.dumps({
                'version': 1, 'is_synthetic': True, 'generator_version': 'synthetic-fixture-v1',
                'items': items,
            }, ensure_ascii=False, indent=2), encoding='utf-8')
            (temporary / 'prompt.md').write_bytes(source_prompt.read_bytes())
            targets = [memory, daily] + ([] if prompt.exists() else [prompt])
            for destination in targets:
                # Publish complete files with exclusive creation: a concurrent
                # private export is never overwritten after our preflight check.
                os.link(temporary / destination.name, destination)
                created.append(destination)
    except Exception:
        for destination in reversed(created):
            destination.unlink()
        raise
    return {'synthetic': True, 'messages': len(items), 'created': [str(path) for path in created]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', type=Path, default=APP, help='Flutter directory (defaults to this script\'s parent project)')
    args = parser.parse_args(argv)
    try:
        result = prepare_assets(args.app)
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(1, f'{exc}\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
