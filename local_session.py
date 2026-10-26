"""Persistent identity for one local prototype user; not browser authentication."""
import os
from pathlib import Path
import uuid


def load_session(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        os.chmod(path, 0o600)
        return str(uuid.UUID(path.read_text().strip()))
    with os.fdopen(descriptor, "w") as stream:
        value = str(uuid.uuid4())
        stream.write(value + "\n")
    return value
