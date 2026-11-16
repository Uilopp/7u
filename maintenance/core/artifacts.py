"""Publish complete JSON snapshots without exposing partially written reports."""

import json
import os
import tempfile
import time
from pathlib import Path


def publish_json(destination, value):
    destination = Path(destination)
    # Serialization errors must not touch a previously valid report.
    payload = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        # Same-directory rename: a reader sees the previous or the new snapshot.
        # Windows readers can briefly deny replacement even though the payload
        # is complete. Retry the same owned temp file for this transient case.
        for attempt in range(6):
            try:
                os.replace(temporary, destination)
                break
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.02 * (attempt + 1))
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
