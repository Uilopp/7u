"""Repository locations shared by PC maintenance modules.

No credentials or developer-machine paths belong here. Tests may pass explicit
paths to services; only interactive/CLI defaults resolve through this module.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
