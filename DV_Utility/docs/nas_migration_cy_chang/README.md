# Moving your DV_Utility tools from GCS to the company RT NAS

For: cy_chang (R7227). From: lichen.liu. Date: 2026-11-23.
Hand this file and the `rtnas.py` next to it to Claude Code in each tool repo.

## Why

DV_Utility (v20261120 or newer) can now download tools from the company RT NAS over SFTP instead of
the public GCS bucket. My 8 tools moved on 2026-11-20. Your three DV_Utility menu entries are still on
`storage.googleapis.com/cyvisionbot_test_2`:

| DV_Utility menu name      | Current manifest (GCS)                                          | Zip today |
|---------------------------|-----------------------------------------------------------------|-----------|
| `FileExchange - OA to NX` | `cyvisionbot_test_2/Upload_to_NX/version_ctrl/version_RTK.json` | `https://www.cyvisionbot.com/app_download/Upload_to_NX_cpu_LATEST` |
| `Register - Spec. Editor` | `cyvisionbot_test_2/RegisterEditor/version_ctrl/version_RTK.json` | `.../RegisterEditor/version_ctrl/RegisterEditor_cpu_<ver>.zip` |
| `SD - ICIDETool`          | `cyvisionbot_test_2/IcIdeTool/version_ctrl/version_RTK.json`    | `.../IcIdeTool/version_ctrl/IcIdeTool_cpu_<ver>.zip` |

Your manifest format does not change. You add an upload step that puts the zip and a **signed** copy
of the manifest on the NAS, and DV_Utility's `setting.json` points at the NAS manifest.

## How DV_Utility treats an `sftp://` tool (rules your release must meet)

1. **Login.** Every user logs in with their own domain account (`rtdomain\<user>`). DV_Utility asks
   once and stores the password DPAPI-encrypted in `~/.PCDV/rtnas/credential.json`. Your tool itself
   needs no login code.
2. **Signed manifest.** The NAS is world-writable (`rwxrwxrwx`), so the manifest fetched from the NAS
   must carry an Ed25519 `signature` from a key pinned in DV_Utility (`rtnas.TRUSTED_RELEASE_KEYS`).
   Unsigned or wrongly signed -> "發佈檔簽章驗證失敗", no install.
3. **Path binding.** The signed `nas_path` field must equal the path of the URL in `setting.json`
   exactly. A signed manifest copied elsewhere is refused.
4. **sha256.** Every `sftp://` zip in `update_info[]` must carry `sha256`; the download must match it.
   (An `https://` zip inside a NAS manifest is allowed; its `sha256` is checked only if present.)
5. **No rollback.** DV_Utility installs a NAS manifest only when `version` is strictly newer than the
   installed one (numeric parts compared in order, so `2.11.0` < `2.12.0` < `2.12.1`). To undo a bad
   release, publish the old build under a higher version.
6. `https://` tools keep the old behaviour, so nothing breaks until `setting.json` is switched.

`rtnas.publish_release()` (in the attached `rtnas.py`) does 2-4 for you.

## One-time setup (cy_chang's PC)

1. Copy the attached `rtnas.py` into each tool repo **unchanged** (it is kept byte-identical across
   repos; do not edit it, ask lichen.liu if something needs to change). Needs `paramiko` and
   `cryptography` in the repo's release venv (`pip install paramiko cryptography`).
2. Save your NAS login (real terminal, it prompts for the password):
   `python rtnas.py login` then `python rtnas.py check`.
3. Create the release signing key and send the public key to lichen.liu (next section).
4. Pick your NAS directory. Suggested: `/Audio/Temp_RT/cychang/<same dir as on GCS>/...`, e.g.
   `/Audio/Temp_RT/cychang/RegisterEditor/version_ctrl/`. `put_atomic` creates missing directories.

## Release signing key (cy_chang)

You need **one** key pair for all three tools, created once on the PC you release from.

| Half        | Where it lives                                                           | Who sees it |
|-------------|--------------------------------------------------------------------------|-------------|
| Private key | `%USERPROFILE%\.PCDV\rtnas\release_ed25519.key`, DPAPI-encrypted, readable only by your Windows account on this PC | Nobody. Never copy, mail, commit or upload it. |
| Public key  | 44-character base64 string (32 raw bytes)                                | lichen.liu pins it in DV_Utility. Not secret. |

### 1. Generate

Run in a real terminal, in any repo that has the attached `rtnas.py` (the key file is per Windows
user, not per repo, so all your repos share it):

```
python rtnas.py keygen
```

Output:

```
Saved a new release signing key at C:\Users\<you>\.PCDV\rtnas\release_ed25519.key
Public key (pin in TRUSTED_RELEASE_KEYS): <44-char base64, ends with "=">
```

- If a key already exists, `keygen` refuses. Do **not** add `--force` unless the old key is truly
  lost or leaked: `--force` moves the old file to `release_ed25519.key.bak<timestamp>` and makes a
  new key, and every manifest you sign afterwards is rejected until lichen.liu pins the new public key
  and releases DV_Utility again.
- `python rtnas.py pubkey` prints the public key of the saved key again at any time.

### 2. Send the public key to lichen.liu

Send this, filled in, over Teams 1:1:

```
DV_Utility NAS release key for cy_chang (R7227)
Public key: <output of python rtnas.py pubkey>
Created:    <YYYY-MM-DD> on <PC name>
Tools:      FileExchange - OA to NX, Register - Spec. Editor, SD - ICIDETool
NAS dir:    /Audio/Temp_RT/cychang/
```

Because the key decides whose releases DV_Utility trusts, confirm it by a second channel too: read the
first 6 and last 6 characters to lichen.liu in person or by phone.

### 3. Signing flow (what happens on every release)

`rtnas.publish_release(plan)` in your `upload_to_nas.py` does all of it; you never sign by hand:

1. Checks every file in the plan exists (before touching the key or the NAS).
2. Loads your private key from `release_ed25519.key` (DPAPI decrypt; only works as you on this PC).
3. For each JSON in the plan, writes a temp copy that
   - has `update_info[].sha256` = SHA-256 of the zip with the same file name in the plan,
   - has `nas_path` = the NAS path it is uploaded to,
   - gets `signature` = base64 Ed25519 signature over the JSON without `signature`, keys sorted,
     no whitespace, UTF-8 (`json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False)`).
4. Logs in with your saved NAS login and uploads the zip first, then the signed JSON, each as
   `<name>.tmp` + size check + rename, so a user never sees half a release.

Your committed manifest is never changed; only the uploaded copy carries `sha256`, `nas_path` and
`signature`. Editing the NAS copy afterwards (even one character) breaks the signature: always
re-run `upload_to_nas.py` instead.

### 4. If something goes wrong

| Symptom | Cause / fix |
|---------|-------------|
| `No release signing key — run: python rtnas.py keygen` | No key on this PC/account. Generate one and send the new public key. |
| DV_Utility shows 「發佈檔簽章驗證失敗」 | Public key not pinned yet (or user's DV_Utility is older than the release that pins it), the NAS file was edited by hand, or `setting.json` URL path differs from `nas_path`. Run the self-check below. |
| DV_Utility does not update | NAS `version` is not strictly newer than the installed one. |
| New PC / reinstalled Windows / key lost or leaked | `python rtnas.py keygen --force`, send the new public key, tell lichen.liu whether the old key must be removed (leaked = yes). |

## Per-repo change (what Claude Code should do in each tool repo)

Add `upload_to_nas.py` next to the existing GCS upload script and call it from the release flow
right after the GCS upload, so both channels stay identical while users migrate. Template:

```python
"""Publish <Tool> to the company RT NAS: zip first, then the signed manifest."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import rtnas

NAS_DIR = "/Audio/Temp_RT/cychang/RegisterEditor/version_ctrl"   # per tool
MANIFEST = Path("version_ctrl/version_RTK.json")  # the manifest you already upload to GCS


def nas_manifest(src: Path, zip_path: Path, out_dir: str) -> Path:
    """Temp copy of the manifest whose zip URL points at the NAS. The committed file is untouched."""
    data = json.loads(src.read_text(encoding="utf-8"))
    for entry in data.get("update_info", []):
        name = entry["url"].rsplit("/", 1)[-1]
        if name != zip_path.name:
            raise SystemExit(f"manifest names {name}, but the zip to upload is {zip_path.name}")
        entry["url"] = rtnas.build_sftp_url(rtnas.NAS_HOST, f"{NAS_DIR}/{zip_path.name}")
    out = Path(out_dir) / src.name
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def main(zip_path: str) -> None:
    zip_path = Path(zip_path)
    with tempfile.TemporaryDirectory() as tmp:
        plan = [
            (zip_path, f"{NAS_DIR}/{zip_path.name}"),                        # archive first
            (nas_manifest(MANIFEST, zip_path, tmp), f"{NAS_DIR}/{MANIFEST.name}"),  # manifest last
        ]
        # Checks every file, loads your signing key, stamps the zip's sha256 into update_info,
        # adds nas_path, signs, then uploads each file atomically (tmp + rename).
        rtnas.publish_release(plan)


if __name__ == "__main__":
    main(sys.argv[1])
```

Notes for Claude Code:

- Keep the existing GCS upload. Do not modify `rtnas.py`. Do not commit `release_ed25519.key`,
  `credential.json` or any GCS key.
- The zip file name on the NAS must equal the last path segment of `update_info[].url` (the sha256 is
  matched by file name).
- `Upload_to_NX`: its zip comes from `www.cyvisionbot.com`, not GCS. Either upload that zip to the NAS
  too (recommended: users then never need the internet) and point `update_info[].url` at it, or keep
  the https link (DV_Utility accepts it, but then nothing guarantees the zip matches the signed
  manifest unless you also add its `sha256`).
- Keep the manifest fields DV_Utility already reads: `target_directory`, `version`, `update_info[]`
  (`url`, `overwrite`, `extract_to`), `current_version`, `exe_name`, `release_note`.
- Tests (pytest, no network): the plan has the zip before the manifest; the NAS manifest's zip URL is
  `sftp://rt-nas.realsil.com.cn<NAS_DIR>/<zip>`; a name mismatch exits. `rtnas.prepare_release(plan,
  key, out_dir)` with a key from `rtnas.generate_signing_key()` lets a test check the signed output
  with `rtnas.verify_manifest(data, trusted=(pub,), expected_path=...)`.

## Self-check after the first upload

`trusted=(PUB,)` checks against your own key, so this works before lichen.liu has pinned it.

```python
import json, rtnas
URL = "sftp://rt-nas.realsil.com.cn/Audio/Temp_RT/cychang/RegisterEditor/version_ctrl/version_RTK.json"
PUB = "<your public key from rtnas.py pubkey>"
m = rtnas.verify_manifest(json.loads(rtnas.fetch_url(URL)), trusted=(PUB,),
                          expected_path=rtnas.parse_sftp_url(URL)[2])
rtnas.require_sha256_for_sftp(m)
zip_url = m["update_info"][0]["url"]
rtnas.fetch_url(zip_url, expected_sha256=m["update_info"][0]["sha256"])   # raises on mismatch
print("OK", m["version"])
```

## setting.json (DV_Utility tool menu)

The menu lives in `RealtekPCCDCIC/dv_util_resource/setting.json` (repo `lichen0122/RealtekPCCDCIC`);
it is bundled into `DV_Utility.exe` at release time (since the release after v20261123), so a menu
change reaches users only with a new DV_Utility release. Format: one JSON object,
`"menu name": "manifest URL"`. Values must be `https://...` or `sftp://rt-nas.realsil.com.cn/...`
(the release build refuses anything else).

The URL path must be exactly the `NAS_DIR` + manifest name you uploaded (it is the signed `nas_path`).
After the switch your three entries would read:

```json
{
	"FileExchange - OA to NX":                          "sftp://rt-nas.realsil.com.cn/Audio/Temp_RT/cychang/Upload_to_NX/version_ctrl/version_RTK.json",
	"Register - Spec. Editor":                          "sftp://rt-nas.realsil.com.cn/Audio/Temp_RT/cychang/RegisterEditor/version_ctrl/version_RTK.json",
	"Tcon - TconAutoGen":                               "sftp://rt-nas.realsil.com.cn/Audio/Temp_RT/lichen.liu/TconAutoGen/tcon_auto_gen_version.json",
	"...":                                              "(other entries unchanged)",
	"SD - ICIDETool":                                   "sftp://rt-nas.realsil.com.cn/Audio/Temp_RT/cychang/IcIdeTool/version_ctrl/version_RTK.json"
}
```

Send lichen.liu the final URLs (or a PR changing only your three lines; keep the key order and the
column alignment). Do not change other people's entries.

## What lichen.liu does with the public key

Only DV_Utility verifies the manifests of the menu tools, so the key has to be pinned in
DV_Utility's `rtnas.py`; the other copies are updated only to keep the file byte-identical.

1. **Check the key.** It must decode to 32 bytes and match what cy_chang read out by the second channel:
   `python -c "import base64,sys; print(len(base64.b64decode(sys.argv[1])))" <key>` -> `32`.
2. **Check it against a real upload** (after cy_chang ran `upload_to_nas.py` once), from
   `RealtekPCCDCIC/DV_Utility` with the NAS login saved:

   ```python
   import json, rtnas
   URL = "sftp://rt-nas.realsil.com.cn/Audio/Temp_RT/cychang/RegisterEditor/version_ctrl/version_RTK.json"
   m = rtnas.verify_manifest(json.loads(rtnas.fetch_url(URL)), trusted=("<cy_chang key>",),
                             expected_path=rtnas.parse_sftp_url(URL)[2])
   rtnas.require_sha256_for_sftp(m); print("OK", m["version"])
   ```
3. **Pin it.** In `RealtekPCCDCIC/DV_Utility/rtnas.py` append to the tuple, one key per line with
   owner and date:

   ```python
   TRUSTED_RELEASE_KEYS = (
       "MdUcG0n/9RNBWZXxj4hIisfTwQmZFJFONW8XeUM85FM=",   # lichen.liu, created 2026-11-20
       "<cy_chang key>",                                  # cy_chang (R7227), created <YYYY-MM-DD>
   )
   ```

   Make the same one-line change in the other `rtnas.py` copies (pcdic_skills, GateLevelDesign,
   tcon_visio_autogen, audio_data_path_visio, RomCodeConvertion, pcdic_monthly_meeting,
   card_note_web) so they stay identical; those apps do not need a release for it.
4. **Test:** `python -m pytest tests/test_rtnas.py tests/test_dv_utility_nas.py` in `DV_Utility`
   (`test_trusted_release_keys_are_pinned` checks every pinned key is 32 bytes).
5. **Commit** (`rtnas: trust cy_chang's NAS release key`) and **release DV_Utility**:
   `python release_dv_utility.py` (CTC signing, needs `HSM_UUID`), then `python upload_to_gcs.py`.
   DV_Utility has no self-update: tell users to re-download the zip.
6. **Switch `setting.json`** to cy_chang's three `sftp://` URLs only after most users run the new
   DV_Utility, then release DV_Utility (the menu is bundled in the exe).

Removing a key (leaked, person left): delete its line in every copy and release DV_Utility again.
Old DV_Utility builds keep trusting it, so also ask users to re-download. A trusted key can sign any
tool's manifest path, not only cy_chang's, so pin only keys confirmed by the second channel.

## Rollout order (who does what)

| # | Who          | Step |
|---|--------------|------|
| 1 | cy_chang     | One-time setup; `keygen`; send the public key (Teams + second channel). |
| 2 | cy_chang     | Add `upload_to_nas.py` to each repo, publish the current version to the NAS, run the self-check with `trusted=(your key,)`. Nobody reads these files yet, so this is safe before step 3. |
| 3 | lichen.liu   | Steps 1-5 of "What lichen.liu does with the public key": verify, pin, test, release DV_Utility. |
| 4 | lichen.liu   | Switch the three `setting.json` entries and publish, once users run the new DV_Utility. |
| 5 | cy_chang     | Every later release goes to GCS and the NAS (same version on both). Retire GCS later, together. |

Known limits: `/Audio/Temp_RT` retention is unknown, and anyone can delete files there (users then
just stop getting updates; installed tools keep working). Contact lichen.liu for problems with the
NAS login, signatures or DV_Utility.
