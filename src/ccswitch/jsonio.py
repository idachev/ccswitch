"""Atomic JSON read/write helpers.

Both ``~/.claude.json`` and ``~/.config/ccswitch/accounts.json`` go through
``write_json_atomic`` so a partial write never leaves a corrupt file behind:
the data is serialized to a temp file in the same directory, parsed back to
validate, then ``os.replace``'d over the destination.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def read_json(path: Path) -> dict[str, Any] | None:
    """Return the parsed JSON object at ``path``, or ``None`` if it does not exist.

    Raises ``json.JSONDecodeError`` on malformed content — callers decide
    whether to surface or recover from that.
    """
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None


def write_json_atomic(path: Path, data: dict[str, Any], mode: int = 0o600) -> None:
    """Atomically write ``data`` as JSON to ``path`` with the given file mode.

    Parent directories are created as needed. The temp file is unlinked on
    failure so the original (if any) survives every error path.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path = path.resolve()
    serialized = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    json.loads(serialized)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    except BaseException:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise
