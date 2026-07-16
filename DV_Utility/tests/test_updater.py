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
