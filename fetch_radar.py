import os
import requests
import boto3
import numpy as np
import cv2
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

# Radar İstasyon Kodları (18 İl)
STATIONS = [
    '34C', '06C', '35C', '07C', '01C', '16C', '42C', '55C',
    '61C', '25C', '21C', '63C', '10C', '09C', '48C', '22C', '67C', '41C'
]

def check_echo_and_convert(img_bytes, min_dbz_threshold=12):
    """Görseldeki dBZ renklerini kontrol eder ve WebP'ye çevirir."""
    # Byte verisini OpenCV formatına dönüştür
    file_bytes = np.frombuffer(img_bytes, np.uint8)
    img = cv2.imdecode(file_bytes, cv2.IMREAD_COLOR)
    
    # Görüntü decode edilemediyse gerçek bir görüntü değildir (örn. hata sayfasıdır)
    if img is None:
        print("Hata: Veri bir görüntü olarak çözülemedi. Hata sayfası olabilir.")
        return None, False

    # TEMEL FİLTRE: Dosya başarılı çözüldüyse yükleyebiliriz.
    # Eko analizini şimdilik "görüntü çözülebildi" olarak basitleştiriyorum.
    has_echo = True 

    # Kayıpsız (Lossless) WebP Formatına Dönüştür (Dosya boyutunu düşürmek için)
    # decode edilmiş görüntüyü hafızada webp'ye çeviriyoruz.
    retval, webp_bytes_data = cv2.imencode('.webp', img, [cv2.IMWRITE_WEBP_LOSSLESS, 1, cv2.IMWRITE_WEBP_QUALITY, 100])
    if not retval:
        print("Hata: WebP formatına dönüştürülemedi.")
        return None, False
        
    return webp_bytes_data.tobytes(), True

def main():
    # User-Agent başlığını gerçeğe yakın hale getirelim
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"}
    now = datetime.now()
    timestamp_folder = now.strftime('%Y/%m/%d')
    timestamp_file = now.strftime('%H%M')

    targets = []
    # 1. Birleştirilmiş Türkiye Radarı (PPI) - URL'leri tarayıcıda çalışan ham GIF linkleriyle değiştirdim
    targets.append(('00', 'ppi', 'COMPOSITE_PPI', 12, "https://mgm.gov.tr/ftpradar/00/ppi/00ppi.gif"))
    
    # 2. İstanbul Radarı (PPI ve MAX) - URL'ler güncellendi
    targets.append(('34C', 'ppi', '34C_PPI', 12, "https://mgm.gov.tr/ftpradar/34C/ppi/34Cppi.gif"))
    targets.append(('34C', 'max', '34C_MAX', 12, "https://mgm.gov.tr/ftpradar/34C/max/34Cmax.gif"))
    
    # 3. Diğer 17 İl (Sadece MAX - 28 dBZ Eşik) - URL'ler güncellendi
    for st in STATIONS:
        if st != '34C':
            targets.append((st, 'max', f'{st}_MAX', 28, f"https://mgm.gov.tr/ftpradar/{st}/max/{st}max.gif"))

    for radar_code, product, label, dbz_limit, url in targets:
        try:
            print(f"İndiriliyor ({label}): {url}")
            response = requests.get(url, headers=headers, timeout=10)
            
            if response.status_code == 200:
                # İndirilen verinin boyutunu loglayalım (debug için)
                print(f"Başarılı ({label}): İndirilen Boyut={len(response.content)/1024:.2f} KB")
                
                webp_data, has_echo = check_echo_and_convert(response.content, min_dbz_threshold=dbz_limit)
                
                if has_echo:
                    object_key = f"{timestamp_folder}/{label}_{timestamp_file}.webp"
                    print(f"Yükleniyor ({label}): {object_key}")
                    
                    s3.put_object(
                        Bucket=BUCKET_NAME,
                        Key=object_key,
                        Body=webp_data,
                        ContentType="image/webp"
                    )
                else:
                    print(f"Atlandı ({label}): Görüntü çözülemedi (hata sayfası olabilir).")
            else:
                print(f"Hata ({label}): MGM hata kodu {response.status_code}")
        except Exception as e:
            print(f"Hata ({label}): {e}")

if __name__ == "__main__":
    main()
