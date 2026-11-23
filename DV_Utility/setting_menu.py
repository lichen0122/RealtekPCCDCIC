"""The DV_Utility tool menu: ``../dv_util_resource/setting.json``, bundled into the exe.

DV_Utility no longer downloads the menu at start; ``release_dv_utility.py`` validates this file and
packs it into ``DV_Utility.exe``. Changing the menu therefore means a DV_Utility release:

    edit dv_util_resource/setting.json  ->  commit/push  ->  python release_dv_utility.py

(DV_Utility v20261123 and older still read ``DVUtility/setting.json`` on GCS; that object is left
as it is for them.)
"""
import json
import os

import rtnas

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE = os.path.normpath(os.path.join(HERE, "..", "dv_util_resource", "setting.json"))


def validate(path=SOURCE):
    """Return the parsed tool map, or raise ValueError - never bundle a file DV_Utility cannot load
    (it reads it as UTF-8 JSON and every value must be an https version-manifest URL, or an
    sftp:// URL on the company RT NAS for tools published there)."""
    with open(path, "rb") as f:
        data = json.loads(f.read().decode("utf-8"))
    if not isinstance(data, dict) or not data:
        raise ValueError("setting.json must be a non-empty JSON object {tool name: manifest url}")
    for name, url in data.items():
        if isinstance(url, str) and url.startswith("https://"):
            continue
        try:
            host, _port, _path = rtnas.parse_sftp_url(url)
        except ValueError:
            host = None
        if host != rtnas.NAS_HOST:
            raise ValueError(f"{name!r}: value must be an https URL or an sftp://{rtnas.NAS_HOST}/... "
                             f"URL, got {url!r}")
    return data
