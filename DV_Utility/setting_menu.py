"""The DV_Utility tool menu: ``../dv_util_resource/setting.json``, bundled into the exe.

DV_Utility no longer downloads the menu at start; ``release_dv_utility.py`` validates this file and
packs it into ``DV_Utility.exe``. Changing the menu therefore means a DV_Utility release:

    edit dv_util_resource/setting.json  ->  commit/push  ->  python release_dv_utility.py

Each value is where the tool's version manifest lives, in one of two forms:

    "Tool A": "https://storage.googleapis.com/.../a_version.json"          one source (GCS or NAS)
    "Tool B": {"gcs": "https://storage.googleapis.com/.../b_version.json",
               "nas": "sftp://rt-nas.realsil.com.cn/.../b_version.json"}   both: GCS first, the RT NAS
                                                                            only when GCS fails

(DV_Utility v20261123 and older still read ``DVUtility/setting.json`` on GCS and understand only the
one-source string form; that object is left as it is for them.)
"""
import json
import os

import rtnas

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE = os.path.normpath(os.path.join(HERE, "..", "dv_util_resource", "setting.json"))

SOURCE_KEYS = ("gcs", "nas")    # also the download order: GCS first, the NAS as fallback


def _is_https(url):
    return isinstance(url, str) and url.startswith("https://")


def _is_nas(url):
    try:
        host, _port, _path = rtnas.parse_sftp_url(url)
    except (ValueError, TypeError, AttributeError):
        return False
    return host == rtnas.NAS_HOST


def sources(entry):
    """The manifest URLs of one menu entry, in the order DV_Utility tries them.

    A string is one source; a {"gcs": ..., "nas": ...} object is GCS first, then the NAS.
    """
    if isinstance(entry, str):
        return [entry]
    return [entry[k] for k in SOURCE_KEYS if entry.get(k)]


def source_label(url):
    """The name shown to users for where a file comes from: "NAS" for the RT NAS, else "GCS"."""
    return "NAS" if rtnas.is_sftp_url(url) else "GCS"


def _check_entry(name, entry):
    if isinstance(entry, str):
        if _is_https(entry) or _is_nas(entry):
            return
        raise ValueError(f"{name!r}: value must be an https URL or an sftp://{rtnas.NAS_HOST}/... "
                         f"URL, got {entry!r}")
    if not isinstance(entry, dict):
        raise ValueError(f"{name!r}: value must be a URL string or {{\"gcs\": ..., \"nas\": ...}}, "
                         f"got {entry!r}")
    extra = set(entry) - set(SOURCE_KEYS)
    if extra or not entry:
        raise ValueError(f"{name!r}: allowed keys are {list(SOURCE_KEYS)}, got {sorted(entry)}")
    if "gcs" in entry and not _is_https(entry["gcs"]):
        raise ValueError(f"{name!r}: \"gcs\" must be an https URL, got {entry['gcs']!r}")
    if "nas" in entry and not _is_nas(entry["nas"]):
        raise ValueError(f"{name!r}: \"nas\" must be an sftp://{rtnas.NAS_HOST}/... URL, "
                         f"got {entry['nas']!r}")


def validate(path=SOURCE):
    """Return the parsed tool map, or raise ValueError - never bundle a file DV_Utility cannot load
    (it reads it as UTF-8 JSON; every value is a manifest URL - https, or sftp:// on the company
    RT NAS - or a {"gcs": https URL, "nas": RT NAS sftp URL} pair)."""
    with open(path, "rb") as f:
        data = json.loads(f.read().decode("utf-8"))
    if not isinstance(data, dict) or not data:
        raise ValueError("setting.json must be a non-empty JSON object {tool name: manifest url}")
    for name, entry in data.items():
        _check_entry(name, entry)
    return data
