import dv_updater


def test_parse_args():
    ns = dv_updater.parse_args([
        '--version', 'v20260717',
        '--zip-url', 'https://x/DV_Utility.zip',
        '--sha256', 'deadbeef',
        '--target', r'C:\PCDV\DV_Utility.exe',
        '--parent-pid', '4321',
    ])
    assert ns.version == 'v20260717'
    assert ns.zip_url == 'https://x/DV_Utility.zip'
    assert ns.sha256 == 'deadbeef'
    assert ns.target == r'C:\PCDV\DV_Utility.exe'
    assert ns.parent_pid == 4321


import hashlib
import http.server
import threading
import functools


def _serve_dir(directory):
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(directory))
    httpd = http.server.HTTPServer(('127.0.0.1', 0), handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, httpd.server_address[1]


def test_download_ok(tmp_path):
    payload = b'hello-update-payload'
    (tmp_path / 'a.bin').write_bytes(payload)
    httpd, port = _serve_dir(tmp_path)
    try:
        dest = tmp_path / 'out.bin'
        sha = hashlib.sha256(payload).hexdigest()
        dv_updater.download(f'http://127.0.0.1:{port}/a.bin', str(dest), sha)
        assert dest.read_bytes() == payload
    finally:
        httpd.shutdown()


def test_download_sha_mismatch_removes_file(tmp_path):
    (tmp_path / 'a.bin').write_bytes(b'xxxx')
    httpd, port = _serve_dir(tmp_path)
    try:
        dest = tmp_path / 'out.bin'
        import pytest
        with pytest.raises(RuntimeError):
            dv_updater.download(f'http://127.0.0.1:{port}/a.bin', str(dest), 'wrong_sha')
        assert not dest.exists()
    finally:
        httpd.shutdown()


import zipfile


def test_extract_main_exe(tmp_path):
    zpath = tmp_path / 'DV_Utility.zip'
    with zipfile.ZipFile(zpath, 'w') as z:
        z.writestr('DV_Utility.exe', b'NEW-EXE-BYTES')
        z.writestr('dv_updater.exe', b'UPDATER-BYTES')
        z.writestr('resource/x.txt', b'noise')
    out = tmp_path / 'extracted.exe'
    dv_updater.extract_main_exe(str(zpath), str(out))
    assert out.read_bytes() == b'NEW-EXE-BYTES'


def test_extract_main_exe_missing(tmp_path):
    zpath = tmp_path / 'bad.zip'
    with zipfile.ZipFile(zpath, 'w') as z:
        z.writestr('readme.txt', b'no exe here')
    import pytest
    with pytest.raises(RuntimeError):
        dv_updater.extract_main_exe(str(zpath), str(tmp_path / 'out.exe'))
