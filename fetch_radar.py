import os
import io
import time
import hashlib
import requests
import boto3
import numpy as np
from PIL import Image
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ==========================================
# AYARLAR
# ==========================================
REQUIRE_ECHO = True 
TURKEY_TZ = timezone(timedelta(hours=3))
TOTAL_TIMEOUT_SECONDS = 50
MAX_WORKERS = 6

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

# Plaka, Kısa Kod ve Klasör Etiketi eşleşmesi
STATION_MAP = [
    ('03', 'afy', '03C'), ('06', 'ank', '06C'), ('07', 'ant', '07C'),
    ('10', 'bal', '10C'), ('16', 'bur', '16C'), ('25', 'erz', '25C'),
    ('27', 'gzt', '27C'), ('31', 'hty', '31C'), ('34', 'ist', '34C'),
    ('35', 'izm', '35C'), ('48', 'mug', '48C'), ('55', 'sam', '55C'),
    ('58', 'siv', '58C'), ('61', 'tra', '61C'), ('63', 'urf', '63C'),
    ('67', 'zon', '67C'), ('70', 'krm', '70C'), ('79', 'kls', '79C')
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://www.mgm.gov.tr/sondurum/radar.aspx",
    "Cache-Control": "no-cache"
}

def fetch_image_from_urls(urls):
    session = requests.Session()
    session.headers.update(HEADERS)
    last_err = "Bilinmeyen Hata"
    
    for url in urls:
        try:
            res = session.get(url, timeout=2.5, verify=False)
            # MGM'nin 'Görsel Yok' placeholder resmi ~2876 bayttır
            if res.status_code == 200 and len(res.content) > 5000:
                img = Image.open(io.BytesIO(res.content))
                img.load()
                return img, None
            else:
                last_err = f"Resim Bulunamadı ({len(res.content)} B)"
        except Exception as e:
            last_err = str(e)
            
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
    
    echo_mask = (saturation > 0.40) & (max_c > 70) & (max_c < 252)
    road_mask = (r > 200) & (g < 60) & (b < 60)
    valid_echo_mask = echo_mask & (~road_mask)
    
    return np.sum(valid_echo_mask) >= min_echo_pixels

def convert_to_lossless_webp(img):
    buffer = io.BytesIO()
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGBA")
    else:
        img = img.convert("RGB")
    img.save(buffer, format="WEBP", lossless=True, quality=100)
    return buffer.getvalue()

def is_duplicate_in_r2(plate, date_path, new_webp_bytes):
    prefix = f"{date_path}/{plate}/VIL_"
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

def generate_candidate_urls(plate, short_code, folder_tag):
    urls = []
    time_suffixes = ["15", "00", "05", "10", ""]
    
    # 1. Klasör bazı: /radar/ist/istvil15.jpg veya /radar/ist/istvil.jpg
    for suffix in time_suffixes:
        urls.append(f"https://www.mgm.gov.tr/FTPDATA/uzal/radar/{short_code}/{short_code}vil{suffix}.jpg")
        urls.append(f"https://www.mgm.gov.tr/FTPDATA/uzal/radar/{short_code}/{short_code}vil{suffix}.png")
    
    # 2. VIL ortak klasörü: /radar/vil/vil_34C.png veya /radar/vil/vil_34.jpg
    for tag in [folder_tag, plate, short_code]:
        urls.append(f"https://www.mgm.gov.tr/FTPDATA/uzal/radar/vil/vil_{tag}.png")
        urls.append(f"https://www.mgm.gov.tr/FTPDATA/uzal/radar/vil/vil_{tag}.jpg")
    
    # Mükerrer URL'leri temizle
    seen = set()
    deduped = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            deduped.append(u)
    return deduped

def process_station(station_info, date_path, time_str):
    plate, short_code, folder_tag = station_info
    urls = generate_candidate_urls(plate, short_code, folder_tag)
    
    img, err = fetch_image_from_urls(urls)
    if img is None:
        return f" -> [HATA/ATLANDI] İstasyon {plate}: {err}"

    if not has_radar_echo(img):
        return f" -> [ATLANDI - EKO YOK] İstasyon {plate}"

    webp_bytes = convert_to_lossless_webp(img)

    if is_duplicate_in_r2(plate, date_path, webp_bytes):
        return f" -> [ÇÖPE ATILDI - ZAMAN DAMGASI DEĞİŞMEDİ] İstasyon {plate}"

    object_key = f"{date_path}/{plate}/VIL_{time_str}.webp"

    try:
        s3.put_object(
            Bucket=BUCKET_NAME,
            Key=object_key,
            Body=webp_bytes,
            ContentType="image/webp"
        )
        return f" -> [R2 YÜKLENDİ] {object_key} ({len(webp_bytes)/1024:.1f} KB)"
    except Exception as e:
        return f" -> [YÜKLEME HATASI] İstasyon {plate}: {e}"

def main():
    now_tr = datetime.now(TURKEY_TZ)
    date_path = now_tr.strftime('%Y/%m/%d')
    time_str = now_tr.strftime('%H%M%S')

    print(f"[{now_tr.strftime('%Y-%m-%d %H:%M:%S')}] 18 İstasyon için VIL Taraması Başlatılıyor...", flush=True)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(process_station, st, date_path, time_str): st[0] for st in STATION_MAP}
        
        try:
            for future in as_completed(futures, timeout=TOTAL_TIMEOUT_SECONDS):
                print(future.result(), flush=True)
        except TimeoutError:
            print("[ZAMAN AŞIMI] İşlem süresi doldu!", flush=True)

    print("İşlem tamamlandı.", flush=True)

if __name__ == "__main__":
    main()
