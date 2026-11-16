"""Canonical read view. Source rows stay untouched when type mappings improve."""

import json
import os
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from maintenance.core.references import initialize as initialize_references


def initialize_archive(path):
    """Create a complete empty archive exclusively; caller holds maintenance locks.

    Existing archives, including invalid files, are never repaired or replaced.
    Publishing a completed temporary database avoids leaving a half-created
    archive if schema creation fails or another process creates the destination.
    """
    path = Path(path).resolve()
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".new-archive-", suffix=".db", dir=path.parent)
    os.close(descriptor)
    temporary = Path(name)
    try:
        with closing(sqlite3.connect(temporary)) as db, db:
            db.executescript("""
                CREATE TABLE messages(msg_id INTEGER PRIMARY KEY,ts INTEGER,direction INTEGER,
                    content_type INTEGER,msg_kind TEXT,text TEXT,content TEXT);
                CREATE TABLE message_provenance(msg_id INTEGER PRIMARY KEY,sender_qq INTEGER,
                    speaker TEXT,raw_direction INTEGER,raw_msg_type INTEGER,audio_md5 TEXT,
                    audio_path TEXT,voice_state TEXT);
                CREATE INDEX messages_time ON messages(ts,msg_id);
                CREATE INDEX voice_state_index ON message_provenance(voice_state,msg_id);
            """)
            initialize_references(db)
            refresh_normalized_view(db)
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Empty archive initialization failed integrity validation")
        try:
            os.link(temporary, path)
        except FileExistsError:
            return False
        return True
    finally:
        temporary.unlink(missing_ok=True)


def reject_synthetic_database(path):
    """Reject a marked developer fixture without creating or modifying a file."""
    path = Path(path).resolve()
    if not path.is_file():
        return
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        if (
            db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata'").fetchone()
            is None
        ):
            return
        metadata = dict(db.execute("SELECT key,value FROM metadata"))
    synthetic = str(metadata.get("is_synthetic", "")).lower() in ("true", "1", "yes")
    synthetic |= str(metadata.get("generator_version", "")).startswith("synthetic")
    try:
        profile = json.loads(metadata.get("profile", "{}"))
    except TypeError, json.JSONDecodeError:
        profile = {}
    synthetic |= isinstance(profile, dict) and profile.get("status") == "synthetic_test_only"
    if synthetic:
        raise ValueError("检测到合成测试数据库；正式发布只允许真实导入资料，请移除开发测试资产后重新维护。")


def refresh_normalized_view(db):
    # The caller owns the transaction. Keep the old raw/exported classification
    # in msg_kind; raw QQNT type 5 is a gray/system tip, not an authored reply.
    db.execute("DROP VIEW IF EXISTS normalized_messages")
    db.execute("""CREATE VIEW normalized_messages AS
        SELECT m.*, p.sender_qq, p.speaker, p.raw_msg_type,
        CASE WHEN p.raw_msg_type=6 THEN 'voice'
             WHEN p.raw_msg_type=5 THEN 'sys'
             WHEN p.raw_msg_type=2 AND m.content_type=6 AND m.msg_kind='text'
                  AND (m.text IS NULL OR m.text='') THEN 'sticker'
             ELSE m.msg_kind END AS normalized_kind,
        p.audio_path, p.voice_state
        FROM messages m JOIN message_provenance p USING(msg_id)""")
