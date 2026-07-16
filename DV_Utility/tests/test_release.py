import hashlib
import json

import release_dv_utility as rel


def test_write_manifest(tmp_path, monkeypatch):
    zip_bytes = b'PK-fake-zip-bytes'
    zip_path = tmp_path / 'DV_Utility.zip'
    zip_path.write_bytes(zip_bytes)
    out = tmp_path / 'publish_version.json'
    monkeypatch.setenv('DVUTIL_RELEASE_NOTE', '測試備註')

    pub = rel.write_manifest('v20260716.3', str(zip_path), out_path=str(out))

    assert pub['version'] == 'v20260716.3'
    assert pub['zip_url'] == rel.GCS_ZIP_URL
    assert pub['size'] == len(zip_bytes)
    assert pub['sha256'] == hashlib.sha256(zip_bytes).hexdigest()
    assert pub['release_note'] == '測試備註'
    # 實際寫出的檔案內容與回傳一致
    assert json.loads(out.read_text(encoding='utf-8')) == pub


def test_write_manifest_default_release_note_empty(tmp_path, monkeypatch):
    zip_path = tmp_path / 'DV_Utility.zip'
    zip_path.write_bytes(b'x')
    out = tmp_path / 'm.json'
    monkeypatch.delenv('DVUTIL_RELEASE_NOTE', raising=False)
    pub = rel.write_manifest('v20260716', str(zip_path), out_path=str(out))
    assert pub['release_note'] == ''
