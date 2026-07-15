"""把 WebUtility.zip 與 publish_version.json 上傳到 GCS。

憑證來源 (擇一):
  * 環境變數 GOOGLE_APPLICATION_CREDENTIALS = 服務帳戶金鑰 json 路徑;
  * 或沿用 DV_Utility 目錄下既有的金鑰 (預設 fallback);
  * 或機器已有 gcloud ADC (application default credentials)。
金鑰勿進版 (見 .gitignore)。

⚠️ 上傳會覆蓋 GCS 上的檔案。與 DV_Utility 不同, WebUtility 沒有自我更新, 所以上傳「不會」
   自動推送給既有使用者 —— 使用者是下次自行下載 / 由 IT 部署時才取得新版。
"""

import os

from google.cloud import storage

_here  = os.path.dirname(os.path.abspath(__file__))
BUCKET = 'realtek-pccdcic-dv'


def _client():
    key = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS') or \
        os.path.join(_here, '..', 'DV_Utility', 'newagent-odjkuq-059b56b2f8a0.json')
    if os.path.isfile(key):
        return storage.Client.from_service_account_json(key)
    return storage.Client()      # 退回機器上的 ADC


def upload_file(client, source_file, destination_blob):
    blob = client.bucket(BUCKET).blob(destination_blob)
    blob.cache_control = 'no-cache, max-age=0'
    blob.upload_from_filename(source_file)
    blob.patch()
    print('Public URL:', f'https://storage.googleapis.com/{BUCKET}/{destination_blob}')


if __name__ == '__main__':
    client = _client()
    upload_file(client, os.path.join(_here, 'WebUtility.zip'), 'WebUtility/WebUtility.zip')
    upload_file(client, os.path.join(_here, 'publish_version.json'), 'WebUtility/version.json')
