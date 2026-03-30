"""Encode an SSH key file into a zlib+hex blob for embedding in app_settings.py.

Usage:
    python tools/encode_ssh_key.py [path_to_key]

If no path is given, defaults to local_data/id_rsa_gitsrv (relative to release_package/).
Copy the output and replace _EMBEDDED_KEY in app_settings.py.
"""

from __future__ import annotations

import base64
import sys
import textwrap
import zlib
from pathlib import Path

DEFAULT_KEY = Path(__file__).resolve() / "id_rsa_gitsrv"


def main() -> None:
    key_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_KEY
    if not key_path.exists():
        print(f"Error: {key_path} not found", file=sys.stderr)
        sys.exit(1)

    raw = key_path.read_bytes()
    blob = base64.b16encode(zlib.compress(raw, 9)).decode()
    lines = textwrap.wrap(blob, 76)

    print("_EMBEDDED_KEY = (")
    for line in lines:
        print(f'    b"{line}"')
    print(")")


if __name__ == "__main__":
    main()
