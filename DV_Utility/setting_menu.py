"""The DV_Utility tool menu: edited in ``../dv_util_resource/setting.json``, published to GCS and
the RT NAS, read by DV_Utility at every start (not bundled into the exe).

    edit dv_util_resource/setting.json  ->  commit/push  ->  python setting_menu.py publish

At start DV_Utility reads ``DVUtility/menu.json`` from GCS; when GCS fails, the copy on the RT NAS
(signed by the publisher, like every NAS manifest: the NAS is writable by anyone); when both fail,
the last menu it loaded (``~/PCDV/resource/setting.json``); from source, finally the repo file.
A menu change therefore needs no DV_Utility release.

menu.json is ``{"menu": {tool name: entry}}``. Each entry is where the tool's version manifest lives:

    "Tool A": "https://storage.googleapis.com/.../a_version.json"          one source (GCS or NAS)
    "Tool B": {"gcs": "https://storage.googleapis.com/.../b_version.json",
               "nas": "sftp://rt-nas.realsil.com.cn/.../b_version.json"}   both: GCS first, the RT NAS
                                                                            only when GCS fails

DV_Utility v20261123 and older read ``DVUtility/setting.json`` on GCS and understand only the
one-source string form; ``publish`` writes that file too, each entry reduced to its GCS URL.
"""
import json
import os
import tempfile

import rtnas

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE = os.path.normpath(os.path.join(HERE, "..", "dv_util_resource", "setting.json"))

SOURCE_KEYS = ("gcs", "nas")    # also the download order: GCS first, the NAS as fallback

BUCKET = "realtek-pccdcic-dv"
MENU_OBJECT = "DVUtility/menu.json"            # what DV_Utility reads (GCS and, signed, the NAS)
LEGACY_OBJECT = "DVUtility/setting.json"       # for v20261123 and older: string entries only
GCS_MENU_URL = rtnas.GCS_RELEASE_PREFIX + MENU_OBJECT
NAS_MENU_PATH = rtnas.nas_path_for(MENU_OBJECT)
NAS_MENU_URL = rtnas.build_sftp_url(rtnas.NAS_HOST, NAS_MENU_PATH)
_GCS_TIMEOUT = (5, 15)
# the publisher's GCS key (gitignored); the same file upload_to_gcs.py uses
GCS_KEY = os.environ.get("DVUTIL_GCS_KEY", os.path.join(HERE, "newagent-odjkuq-059b56b2f8a0.json"))


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
    """Return the parsed tool map, or raise ValueError - never publish a file DV_Utility cannot load
    (it reads it as UTF-8 JSON; every value is a manifest URL - https, or sftp:// on the company
    RT NAS - or a {"gcs": https URL, "nas": RT NAS sftp URL} pair)."""
    with open(path, "rb") as f:
        return check_menu(json.loads(f.read().decode("utf-8")))


def check_menu(data):
    """Validate a parsed tool map (as :func:`validate` does for the file); return it."""
    if not isinstance(data, dict) or not data:
        raise ValueError("the tool menu must be a non-empty JSON object {tool name: manifest url}")
    for name, entry in data.items():
        _check_entry(name, entry)
    return data


# ── reading the menu (DV_Utility at start) ───────────────────────────────────
def fetch_gcs_menu(get=None):
    """The menu from GCS (only the publisher can write the bucket, so no signature)."""
    import requests
    r = (get or requests.get)(GCS_MENU_URL, timeout=_GCS_TIMEOUT)
    r.raise_for_status()
    doc = json.loads(r.content.decode("utf-8"))
    return check_menu(doc.get("menu") if isinstance(doc, dict) else None)


def fetch_nas_menu(store=None, fetch=None):
    """The menu from the RT NAS: the publisher's signature must verify and the signed nas_path
    must be menu.json's own path, or rtnas raises (no saved NAS login: rtnas.AuthError)."""
    raw = (fetch or rtnas.fetch_url)(NAS_MENU_URL, store=store)
    doc = rtnas.verify_signed_json(raw, expected_path=NAS_MENU_PATH)
    return check_menu(doc.get("menu"))


def load_menu(cache_path, store=None, get=None, fetch=None, fallback_path=None, log=None):
    """The tool menu and where it came from: GCS, else the NAS, else the last menu loaded
    (``cache_path``), else ``fallback_path`` (the repo file when run from source).

    A menu read from GCS or the NAS replaces the cache, so the next start works offline too."""
    def note(msg, *args):
        if log:
            log(msg, *args)

    for label, read in (("GCS", lambda: fetch_gcs_menu(get=get)),
                        ("NAS", lambda: fetch_nas_menu(store=store, fetch=fetch))):
        try:
            menu = read()
        except Exception as e:  # noqa: BLE001 - every failure means: try the next source
            note("tool menu: %s failed: %s", label, e)
            continue
        try:
            tmp = f"{cache_path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(menu, f, ensure_ascii=False, indent=1)
            os.replace(tmp, cache_path)
        except OSError as e:
            note("tool menu: could not save the cache %s: %s", cache_path, e)
        return menu, label
    for label, path in (("cache", cache_path), ("repo", fallback_path)):
        if path and os.path.isfile(path):
            with open(path, "rb") as f:
                return check_menu(json.loads(f.read().decode("utf-8"))), label
    raise RuntimeError("the tool menu could not be read from GCS or the RT NAS, and there is no "
                       "earlier copy on this PC")


# ── publishing the menu (the owner, after editing setting.json) ─────────────
def legacy_menu(menu):
    """For DV_Utility v20261123 and older: every entry as one https URL (its GCS source)."""
    out = {}
    for name, entry in menu.items():
        url = entry if isinstance(entry, str) else entry.get("gcs")
        if url and _is_https(url):
            out[name] = url
    return out


def publish(path=SOURCE, *, upload_gcs=None, publish_nas=None, nas=True, log=print):
    """Validate setting.json and publish it: GCS menu.json and the legacy setting.json, then the
    signed copy on the RT NAS (``nas=False`` skips the NAS)."""
    menu = validate(path)
    if upload_gcs is None:
        from google.cloud import storage
        bucket = storage.Client.from_service_account_json(GCS_KEY).bucket(BUCKET)

        def upload_gcs(obj, data):
            blob = bucket.blob(obj)
            blob.cache_control = "no-cache, max-age=0"
            blob.upload_from_string(data, content_type="application/json; charset=utf-8")
    with tempfile.TemporaryDirectory() as tmp:
        doc = os.path.join(tmp, "menu.json")
        body = json.dumps({"menu": menu}, ensure_ascii=False, indent=1)
        with open(doc, "w", encoding="utf-8") as f:
            f.write(body)
        upload_gcs(MENU_OBJECT, body.encode("utf-8"))
        log(f"GCS {MENU_OBJECT}: {len(menu)} tools")
        upload_gcs(LEGACY_OBJECT, json.dumps(legacy_menu(menu), ensure_ascii=False, indent=1).encode("utf-8"))
        log(f"GCS {LEGACY_OBJECT} (v20261123 and older): {len(legacy_menu(menu))} tools")
        if nas:
            (publish_nas or rtnas.publish_release)([(doc, NAS_MENU_PATH)])
            log(f"NAS {NAS_MENU_PATH}: signed")
    return menu


if __name__ == "__main__":
    import sys
    if sys.argv[1:2] == ["publish"]:
        publish(nas="--no-nas" not in sys.argv)
    else:
        print(f"{len(validate())} tools OK in {SOURCE}")
        print("publish: python setting_menu.py publish [--no-nas]")
