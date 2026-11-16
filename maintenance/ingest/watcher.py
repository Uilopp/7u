"""One stable QQ snapshot import, called inside the maintenance workflow lock."""

from __future__ import annotations

import sqlite3
from contextlib import closing

from maintenance.ingest.importer import import_connection
from maintenance.ingest.snapshot import encrypted_snapshot
from maintenance.paths import ROOT


def poll(config):
    with encrypted_snapshot(config) as source:
        with closing(sqlite3.connect(ROOT / config["database"])) as target:
            return import_connection(source, target, config)
