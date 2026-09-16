#!/usr/bin/python3
"""Root-only captured IPC: read the operator's standalone Linux Git credential."""
import os
from pathlib import Path
import stat
import sys

TOKEN = Path("/etc/secondmate-git-credentials/github-token")


def read_token(path=TOKEN):
    if os.geteuid() != 0:
        raise ValueError("root required")
    for parent in reversed(path.parents):
        info = parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
                or info.st_mode & 0o022):
            raise ValueError("untrusted credential directory")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise ValueError("untrusted credential file")
        raw = stream.read(4097)
    if len(raw) > 4096:
        raise ValueError("credential too long")
    token = raw.removesuffix(b"\n")
    if not token or any(c < 33 or c > 126 for c in token):
        raise ValueError("invalid credential")
    return token


def main():
    try:
        if len(sys.argv) != 1:
            raise ValueError("arguments forbidden")
        token = read_token()
    except (OSError, ValueError):
        print("Git credential unavailable; operator action required.", file=sys.stderr)
        return 1
    sys.stdout.buffer.write(token + b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
