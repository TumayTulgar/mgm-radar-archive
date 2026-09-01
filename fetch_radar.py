import os
import requests
import boto3
from datetime import datetime

# GitHub Secrets üzerinden gelen R2 bilgileri
ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID")
ACCESS_KEY = os.environ.get("R2_ACCESS_KEY_ID")
SECRET_KEY = os.environ.get("R2_SECRET_ACCESS_KEY")
BUCKET_NAME = os.environ.get("R2_BUCKET_NAME")

# Cloudflare R2 Endpoint URL
R2_ENDPOINT = f"https://{ACCOUNT_ID}.r2.cloudflarestorage.com"

# S3 İstemcisi Kurulumu
s3 = boto3.client(
    service_name="s3",
    endpoint_url=R2_ENDPOINT,
    aws_access_key_id=ACCESS_KEY,
    aws_secret_access_key=SECRET_KEY,
    region_name="auto"
)

# MGM Radar Görüntü Bağlantısı
RADAR_URL = "https://www.mgm.gov.tr/FTPDATA/uzal/radar/ppi/ppi_34C.png"

def main():
    headers = {"User-Agent": "Mozilla/5.0"}
    response = requests.get(RADAR_URL, headers=headers)
    
    if response.status_code == 200:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        object_name = f"radar_34C_{timestamp}.png"
        
        # Doğrudan Cloudflare R2'ye yükle
        s3.put_object(
            Bucket=BUCKET_NAME,
            Key=object_name,
            Body=response.content,
            ContentType="image/png"
        )
        print(f"Görsel başarıyla R2 deposuna yüklendi: {object_name}")
    else:
        print(f"MGM'den görsel alınamadı. Hata kodu: {response.status_code}")

if __name__ == "__main__":
    main()
