"""Publish dv_util_resource/setting.json (the DV_Utility tool menu) to GCS.

DV_Utility downloads this file at every start from
``https://storage.googleapis.com/realtek-pccdcic-dv/DVUtility/setting.json`` (see
``dv_utility.SETTING_URLS``). GCS objects uploaded here are ``no-cache``, so an edit is live
immediately - unlike raw.github.com, which is CDN-cached for ~5 minutes.

The repo copy ``../dv_util_resource/setting.json`` stays the single place you edit. Flow:

    edit dv_util_resource/setting.json  ->  commit/push (history)  ->  python publish_setting.py

Credentials: the GCS service-account key ``newagent-odjkuq-*.json`` next to this script
(secret - never commit).
"""
import json
import os
import sys

BUCKET = "realtek-pccdcic-dv"
DEST_BLOB = "DVUtility/setting.json"
HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE = os.path.normpath(os.path.join(HERE, "..", "dv_util_resource", "setting.json"))
CREDENTIALS = os.path.join(HERE, "newagent-odjkuq-059b56b2f8a0.json")


def validate(path):
    """Return the parsed tool map, or raise ValueError - never publish a file DV_Utility cannot load
    (it reads it as UTF-8 JSON and every value must be an https version-manifest URL)."""
    with open(path, "rb") as f:
        data = json.loads(f.read().decode("utf-8"))
    if not isinstance(data, dict) or not data:
        raise ValueError("setting.json must be a non-empty JSON object {tool name: manifest url}")
    for name, url in data.items():
        if not isinstance(url, str) or not url.startswith("https://"):
            raise ValueError(f"{name!r}: value must be an https URL, got {url!r}")
    return data


def publish(source=SOURCE, credentials=CREDENTIALS):
    data = validate(source)
    from google.cloud import storage  # lazy: only needed for the real upload

    client = storage.Client.from_service_account_json(credentials)
    blob = client.bucket(BUCKET).blob(DEST_BLOB)
    blob.cache_control = "no-cache, max-age=0"
    blob.content_type = "application/json; charset=utf-8"
    blob.upload_from_filename(source, content_type="application/json; charset=utf-8")
    blob.patch()  # push the cache_control metadata
    print(f"Published {len(data)} tools -> https://storage.googleapis.com/{BUCKET}/{DEST_BLOB}")


if __name__ == "__main__":
    try:
        publish()
    except (OSError, ValueError) as e:
        sys.exit(f"ERROR: {e}")
