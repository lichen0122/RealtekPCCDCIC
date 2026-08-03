import hashlib
import json

import pytest

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


# --- CTC 送簽前置檢查 (fail-fast gate) ---------------------------------------
# CTC 送簽現為 release 預設; 這組確認 build 前的前置檢查會在缺 uuid / 找不到
# hsm-cli / 沒有認證來源時中止 (SystemExit), 設定齊全時放行。hsm_dir 以參數注入
# 指向空資料夾, 才不會被 repo 內真實的 hsm/hsm.config.yaml 自動補足認證。
_CTC_ENV = ('DVUTIL_HSM_CLI', 'DVUTIL_HSM_CONFIG', 'DVUTIL_HSM_SERVER',
            'DVUTIL_HSM_USER', 'DVUTIL_HSM_TOKEN', 'DVUTIL_HSM_UUID', 'HSM_UUID')


def _clear_ctc_env(monkeypatch):
    for k in _CTC_ENV:
        monkeypatch.delenv(k, raising=False)


def test_resolve_ctc_params_reads_env(monkeypatch, tmp_path):
    _clear_ctc_env(monkeypatch)
    cli = tmp_path / 'hsm-cli.exe'
    cli.write_bytes(b'x')
    monkeypatch.setenv('DVUTIL_HSM_CLI', str(cli))
    monkeypatch.setenv('DVUTIL_HSM_UUID', 'the-uuid')
    monkeypatch.setenv('DVUTIL_HSM_SERVER', 'srv')
    monkeypatch.setenv('DVUTIL_HSM_TOKEN', 'tok')
    p = rel._resolve_ctc_params(hsm_dir=str(tmp_path / 'empty_hsm'))
    assert p['hsm_cli'] == str(cli)
    assert p['uuid'] == 'the-uuid'
    assert p['server'] == 'srv'
    assert p['token'] == 'tok'
    assert p['config'] is None   # 無設定檔, 走 server+token


def test_check_ctc_prereqs_ok(monkeypatch, tmp_path):
    _clear_ctc_env(monkeypatch)
    cli = tmp_path / 'hsm-cli.exe'
    cli.write_bytes(b'x')
    monkeypatch.setenv('DVUTIL_HSM_CLI', str(cli))
    monkeypatch.setenv('DVUTIL_HSM_UUID', 'u')
    monkeypatch.setenv('DVUTIL_HSM_SERVER', 'srv')
    monkeypatch.setenv('DVUTIL_HSM_TOKEN', 'tok')
    # 齊全 -> 不 raise
    rel._check_ctc_prereqs(hsm_dir=str(tmp_path / 'empty_hsm'))


def test_check_ctc_prereqs_missing_uuid_aborts(monkeypatch, tmp_path):
    _clear_ctc_env(monkeypatch)
    cli = tmp_path / 'hsm-cli.exe'
    cli.write_bytes(b'x')
    monkeypatch.setenv('DVUTIL_HSM_CLI', str(cli))
    monkeypatch.setenv('DVUTIL_HSM_SERVER', 'srv')
    monkeypatch.setenv('DVUTIL_HSM_TOKEN', 'tok')
    with pytest.raises(SystemExit) as ei:   # 缺 uuid
        rel._check_ctc_prereqs(hsm_dir=str(tmp_path / 'empty_hsm'))
    assert 'uuid' in str(ei.value).lower()


def test_check_ctc_prereqs_missing_cli_aborts(monkeypatch, tmp_path):
    _clear_ctc_env(monkeypatch)
    monkeypatch.setenv('DVUTIL_HSM_CLI', str(tmp_path / 'nope.exe'))  # 指向不存在的檔
    monkeypatch.setenv('DVUTIL_HSM_UUID', 'u')
    monkeypatch.setenv('DVUTIL_HSM_SERVER', 'srv')
    monkeypatch.setenv('DVUTIL_HSM_TOKEN', 'tok')
    with pytest.raises(SystemExit) as ei:
        rel._check_ctc_prereqs(hsm_dir=str(tmp_path / 'empty_hsm'))
    assert 'hsm-cli' in str(ei.value)


def test_check_ctc_prereqs_missing_auth_aborts(monkeypatch, tmp_path):
    _clear_ctc_env(monkeypatch)
    cli = tmp_path / 'hsm-cli.exe'
    cli.write_bytes(b'x')
    monkeypatch.setenv('DVUTIL_HSM_CLI', str(cli))
    monkeypatch.setenv('DVUTIL_HSM_UUID', 'u')
    # 無 config / server / token, 且 hsm_dir 為空 -> 無認證來源
    with pytest.raises(SystemExit) as ei:
        rel._check_ctc_prereqs(hsm_dir=str(tmp_path / 'empty_hsm'))
    assert '認證' in str(ei.value)
