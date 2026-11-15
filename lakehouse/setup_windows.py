"""Windows only: fetch the two files Hadoop's local file system needs (winutils.exe and hadoop.dll) into lakehouse/.hadoop/bin, checking each SHA-256.

    python -m lakehouse.setup_windows

Spark itself is pure JVM and runs on Windows, but Hadoop calls winutils.exe to read and set file permissions, so even a Delta table on the local disk fails
with "Could not locate Hadoop executable" without it. The binaries are third party builds of Hadoop 3.4.0 for Windows (kontext-tech/winutils) and are not
part of this repository; Linux and macOS (and CI) need none of this.
"""
from __future__ import annotations

import hashlib
import os
import sys
import urllib.request

from . import config

BASE = "https://raw.githubusercontent.com/kontext-tech/winutils/master/hadoop-3.4.0-win10-x64/bin/"
FILES = {
    "winutils.exe": "f7294eca96ba9f2efcf66ff89db16ace67ae667692a09b49f024472b32fb354c",
    "hadoop.dll": "8769e86a8ad1bffb6a6038215eb82f523b25d92cca3d4dd32f62d4ff02418f8a",
}


def main() -> int:
    if os.name != "nt":
        print("nothing to do: winutils is only needed on Windows")
        return 0
    target = config.ROOT / ".hadoop" / "bin"
    target.mkdir(parents=True, exist_ok=True)
    for name, digest in FILES.items():
        dest = target / name
        if not dest.exists() or hashlib.sha256(dest.read_bytes()).hexdigest() != digest:
            with urllib.request.urlopen(BASE + name, timeout=60) as resp:  # noqa: S310 - fixed https address
                data = resp.read()
            if hashlib.sha256(data).hexdigest() != digest:
                print("checksum mismatch for", name)
                return 1
            dest.write_bytes(data)
        print("ok", dest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
