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
    import os

    here = os.path.dirname(os.path.abspath(__file__))
    bucket = "realtek-pccdcic-dv"

    # 主程式 zip (含 DV_Utility.exe + dv_updater.exe)
    upload_file(
        bucket,
        os.path.join(here, "DV_Utility.zip"),
        "DVUtility/DV_Utility.zip",
    )

    # 更新用的版本資訊 (release_dv_utility.py 產生); 主程式 update_check 會抓這顆比版本、
    # dv_updater.exe 依它下載並驗 sha256。
    upload_file(
        bucket,
        os.path.join(here, "publish_version.json"),
        "DVUtility/version.json",
    )