"""Controller access to worker-owned handoff files, without following links."""

import os
import stat
from contextlib import contextmanager

LIMIT = 1024 * 1024


@contextmanager
def _open(path, *, write=False):
    flags = os.O_NOFOLLOW | os.O_NONBLOCK
    flags |= (os.O_WRONLY | os.O_CREAT) if write else os.O_RDONLY
    fd = os.open(path, flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("Worker handoff must be a regular, unlinked file")
        if not write and info.st_size > LIMIT:
            raise ValueError("Worker handoff exceeds the size limit")
        # Validate before truncating: hard links must not modify host files.
        if write:
            os.ftruncate(fd, 0)
        with os.fdopen(fd, "w" if write else "r", encoding="utf-8") as stream:
            fd = None
            yield stream
    finally:
        if fd is not None:
            os.close(fd)


def read_handoff(path):
    with _open(path) as stream:
        text = stream.read(LIMIT + 1)
    if len(text.encode("utf-8")) > LIMIT:
        raise ValueError("Worker handoff exceeds the size limit")
    return text


def write_handoff(path, text):
    if len(text.encode("utf-8")) > LIMIT:
        raise ValueError("Worker handoff exceeds the size limit")
    with _open(path, write=True) as stream:
        stream.write(text)
