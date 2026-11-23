from google.cloud import storage

def upload_file(bucket_name: str, source_file: str, destination_blob: str):
    # 使用 gcloud auth 登入後產生的預設憑證
    client = storage.Client.from_service_account_json("newagent-odjkuq-059b56b2f8a0.json")
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(destination_blob)

    # 避免快取舊檔
    blob.cache_control = "no-cache, max-age=0"

    # 上傳檔案（會覆蓋同名檔案）
    blob.upload_from_filename(source_file)

    # 確保 metadata 更新
    blob.patch()

    # 公開網址 (如果 bucket/檔案有設公開存取)
    public_url = f"https://storage.googleapis.com/{bucket_name}/{destination_blob}"
    print("Public URL:", public_url)

    # 產生簽名網址（10 分鐘有效）
    signed_url = blob.generate_signed_url(
        version="v4",
        expiration=600,  # 秒數
        method="GET"
    )
    print("Signed URL:", signed_url)


if __name__ == "__main__":
    import json
    import os
    import tempfile

    import release_dv_utility as rel

    # 過渡用: v20261123 以前的舊版只會讀 GCS 的 version.json, 也只會從 GCS 下載 zip。
    # 新版 (主程式 + dv_updater.exe) 只讀 NAS (release_dv_utility.py --publish-nas)。
    # 這裡把同一顆 zip 放上 GCS, 並上傳 zip_url 換回 GCS 的 version.json, 讓舊版能升到新版;
    # 升上去之後就只看 NAS 了。
    here = os.path.dirname(os.path.abspath(__file__))
    bucket = "realtek-pccdcic-dv"

    upload_file(
        bucket,
        os.path.join(here, "DV_Utility.zip"),
        "DVUtility/DV_Utility.zip",
    )

    with open(os.path.join(here, "publish_version.json"), encoding="utf-8") as f:
        legacy = json.load(f)
    legacy["zip_url"] = rel.GCS_ZIP_URL
    with tempfile.TemporaryDirectory() as tmp:
        legacy_path = os.path.join(tmp, "version.json")
        with open(legacy_path, "w", encoding="utf-8") as f:
            json.dump(legacy, f, ensure_ascii=False, indent=2)
        upload_file(bucket, legacy_path, "DVUtility/version.json")
