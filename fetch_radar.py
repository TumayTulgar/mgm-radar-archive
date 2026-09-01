import os
import io
import time
import random
import hashlib
import requests
import boto3
import numpy as np
from PIL import Image
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ==========================================
# AYARLAR
# ==========================================
REQUIRE_ECHO = True 
TURKEY_TZ = timezone(timedelta(hours=3))

ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID")
ACCESS_KEY = os.environ.get("R2_ACCESS_KEY_ID")
SECRET_KEY = os.environ.get("R2_SECRET_ACCESS_KEY")
BUCKET_NAME = os.environ.get("R2_BUCKET_NAME")

R2_ENDPOINT = f"https://{ACCOUNT_ID}.r2.cloudflarestorage.com"

s3 = boto3.client(
    service_name="s3",
    endpoint_url=R2_ENDPOINT,
    aws_access_key_id=ACCESS_KEY,
    aws_secret_access_key=SECRET_KEY,
    region_name="auto"
)

# İstanbul (34C) dahil edilerek tam 18 istasyon listelendi
STATION_MAP = [
    ('34', 'ist', '34C'), ('03', 'afy', '03C'), ('06', 'ank', '06C'), 
    ('07', 'ant', '07C'), ('10', 'bal', '10C'), ('16', 'bur', '16C'), 
    ('25', 'erz', '25C'), ('27', 'gzt', '27C'), ('31', 'hty', '31C'), 
    ('35', 'izm', '35C'), ('48', 'mug', '48C'), ('55', 'sam', '55C'), 
    ('58', 'siv', '58C'), ('61', 'tra', '61C'), ('63', 'urf', '63C'), 
    ('67', 'zon', '67C'), ('70', 'krm', '70C'), ('79', 'kls', '79C')
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache"
}

def fetch_image_from_urls(urls):
    last_err = ""
    session = requests.Session()
    
    for url in urls:
        time.sleep(random.uniform(0.2, 0.5))
        
        for attempt in range(2):
            try:
                res = session.get(url, headers=HEADERS, timeout=10, verify=False)
                if res.status_code == 200:
                    if len(res.content) > 5000:
                        img = Image.open(io.BytesIO(res.content))
                        img.load()
                        return img, None
                    else:
                        last_err = f"Boş/Bozuk Görsel ({len(res.content)}B)"
                else:
                    last_err = f"HTTP {res.status_code}"
            except requests.exceptions.Timeout:
                last_err = "Zaman Aşımı (Timeout)"
            except Exception as e:
                last_err = str(e)
            
            if attempt == 0 and "Timeout" in last_err:
                time.sleep(1)
                
    return None, last_err

def has_radar_echo(img, min_echo_pixels=10):
    if not REQUIRE_ECHO:
        return True

    rgb_img = img.convert("RGB")
    w, h = rgb_img.size
    crop_box = (int(w * 0.05), int(h * 0.10), int(w * 0.85), int(h * 0.90))
    cropped = rgb_img.crop(crop_box)
    
    np_img = np.array(cropped)
    r = np_img[:, :, 0].astype(float)
    g = np_img[:, :, 1].astype(float)
    b = np_img[:, :, 2].astype(float)
    
    max_c = np.maximum(np.maximum(r, g), b)
    min_c = np.minimum(np.minimum(r, g), b)
    
    with np.errstate(divide='ignore', invalid='ignore'):
        saturation = np.where(max_c == 0, 0, (max_c - min_c) / max_c)
    
    # VIL renk paleti için de mevcut doygunluk maskesi işlevseldir
    echo_mask = (saturation > 0.40) & (max_c > 70) & (max_c < 252)
    road_mask = (r > 200) & (g < 60) & (b < 60)
    valid_echo_mask = echo_mask & (~road_mask)
    
    echo_pixels = np.sum(valid_echo_mask)
    return echo_pixels >= min_echo_pixels

def convert_to_lossless_webp(img):
    buffer = io.BytesIO()
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGBA")
    else:
        img = img.convert("RGB")
    img.save(buffer, format="WEBP", lossless=True, quality=100)
    return buffer.getvalue()

def is_duplicate_in_r2(station, product, date_path, new_webp_bytes):
    prefix = f"{date_path}/{station}/{product}_"
    new_md5 = hashlib.md5(new_webp_bytes).hexdigest()
    
    try:
        response = s3.list_objects_v2(Bucket=BUCKET_NAME, Prefix=prefix)
        if 'Contents' in response and len(response['Contents']) > 0:
            latest_obj = sorted(response['Contents'], key=lambda x: x['Key'])[-1]
            last_etag = latest_obj['ETag'].strip('"')
            if last_etag == new_md5:
                return True
    except Exception:
        pass
        
    return False

def process_target(target, date_path, time_str):
    station, product, urls = target
    img, err = fetch_image_from_urls(urls)

    if img is None:
        return f" -> [ENGEL/HATA] {station} - {product}: {err}"

    if not has_radar_echo(img):
        return f" -> [ATLANDI - EKO YOK] {station} - {product}"

    webp_bytes = convert_to_lossless_webp(img)
    
    if is_duplicate_in_r2(station, product, date_path, webp_bytes):
        return f" -> [ATLANDI - DEĞİŞMEDİ] {station} - {product}"

    try:
        object_key = f"{date_path}/{station}/{product}_{time_str}.webp"
        s3.put_object(
            Bucket=BUCKET_NAME,
            Key=object_key,
            Body=webp_bytes,
            ContentType="image/webp"
        )
        return f" -> [R2 YÜKLENDİ] {object_key} ({len(webp_bytes)/1024:.1f} KB)"
    except Exception as e:
        return f" -> [YÜKLEME HATASI] {station} - {product}: {e}"

def main():
    now_tr = datetime.now(TURKEY_TZ)
    date_path = now_tr.strftime('%Y/%m/%d')
    time_str = now_tr.strftime('%H%M%S')

    # 1 Adet Birleştirilmiş VIL Görseli
    targets = [
        ('COMPOSITE', 'VIL', [
            "https://www.mgm.gov.tr/FTPDATA/uzal/radar/vil/vil_00.png",
            "https://www.mgm.gov.tr/FTPDATA/uzal/radar/vil/vil_00.jpg",
            "https://www.mgm.gov.tr/FTPDATA/uzal/radar/comp/compvil15.jpg"
        ])
    ]

    # 18 Adet Merkez Radar VIL Görselleri
    for plate, short_code, folder_tag in STATION_MAP:
        urls = [
            f"https://www.mgm.gov.tr/FTPDATA/uzal/radar/vil/vil_{folder_tag}.png",
            f"https://www.mgm.gov.tr/FTPDATA/uzal/radar/vil/vil_{plate}.png",
            f"https://www.mgm.gov.tr/FTPDATA/uzal/radar/vil/vil_{folder_tag}.jpg",
            f"https://www.mgm.gov.tr/FTPDATA/uzal/radar/vil/vil_{plate}.jpg",
            f"https://www.mgm.gov.tr/FTPDATA/uzal/radar/{short_code}/{short_code}vil15.jpg",
            f"https://www.mgm.gov.tr/FTPDATA/uzal/radar/{plate}/{plate}vil15.jpg"
        ]
        targets.append((folder_tag, 'VIL', urls))

    print(f"[{now_tr.strftime('%Y-%m-%d %H:%M:%S')}] VIL taraması başlatılıyor ({len(targets)} hedef)...")
    
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(process_target, t, date_path, time_str) for t in targets]
        for future in as_completed(futures):
            print(future.result())

    print("İşlem tamamlandı.")

if __name__ == "__main__":
    main()
