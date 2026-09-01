import os
import io
import hashlib
import requests
import boto3
import numpy as np
from PIL import Image
from datetime import datetime, timezone, timedelta

# ==========================================
# AYARLAR
# ==========================================
REQUIRE_ECHO = True # True: Sadece dBZ > 2 (yağış) olanları yükler
TURKEY_TZ = timezone(timedelta(hours=3)) # Türkiye Saati (UTC+3)

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

STATION_MAP = [
    ('03', 'afy', '03C'), ('06', 'ank', '06C'), ('07', 'ant', '07C'),
    ('10', 'bal', '10C'), ('16', 'bur', '16C'), ('25', 'erz', '25C'),
    ('27', 'gzt', '27C'), ('31', 'hty', '31C'), ('35', 'izm', '35C'),
    ('48', 'mug', '48C'), ('55', 'sam', '55C'), ('58', 'siv', '58C'),
    ('61', 'tra', '61C'), ('63', 'urf', '63C'), ('67', 'zon', '67C'),
    ('70', 'krm', '70C'), ('79', 'kls', '79C')
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.mgm.gov.tr/sondurum/radar.aspx",
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"
}

def fetch_image_from_urls(urls):
    for url in urls:
        try:
            res = requests.get(url, headers=HEADERS, timeout=8)
            if res.status_code == 200 and len(res.content) > 5000:
                img = Image.open(io.BytesIO(res.content))
                img.load()
                print(f" -> [İNDİRİLDİ] {url} ({len(res.content)/1024:.1f} KB)")
                return img
        except Exception:
            continue
    return None

def has_radar_echo(img, min_echo_pixels=20):
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
    saturation = np.where(max_c == 0, 0, (max_c - min_c) / max_c)
    
    echo_mask = (saturation > 0.40) & (max_c > 70) & (max_c < 252)
    road_mask = (r > 200) & (g < 60) & (b < 60)
    valid_echo_mask = echo_mask & (~road_mask)
    
    echo_pixels = np.sum(valid_echo_mask)
    print(f"   [EKO ANALİZİ] Eko piksel sayısı: {echo_pixels}")
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
    """
    R2 üzerindeki en son dosyanın MD5 hash'i ile yeni görselinkini karşılaştırır.
    Birebir aynıysa True döndürerek tekrar yüklemeyi engeller.
    """
    prefix = f"{date_path}/{station}/{product}_"
    new_md5 = hashlib.md5(new_webp_bytes).hexdigest()
    
    try:
        response = s3.list_objects_v2(Bucket=BUCKET_NAME, Prefix=prefix)
        if 'Contents' in response and len(response['Contents']) > 0:
            # En son yüklenen dosyayı bul
            latest_obj = sorted(response['Contents'], key=lambda x: x['Key'])[-1]
            last_etag = latest_obj['ETag'].strip('"')
            
            if last_etag == new_md5:
                return True
    except Exception as e:
        print(f"   [ÇAKIŞMA KONTROL UYARISI] {e}")
        
    return False

def main():
    now_tr = datetime.now(TURKEY_TZ)
    date_path = now_tr.strftime('%Y/%m/%d')
    time_str = now_tr.strftime('%H%M%S')

    targets = [
        ('COMPOSITE', 'PPI', [
            "https://www.mgm.gov.tr/FTPDATA/uzal/radar/ppi/ppi_00.png",
            "https://www.mgm.gov.tr/FTPDATA/uzal/radar/ppi/ppi_00.jpg",
            "https://www.mgm.gov.tr/FTPDATA/uzal/radar/comp/compppi15.jpg"
        ]),
        ('34C', 'PPI', [
            "https://www.mgm.gov.tr/FTPDATA/uzal/radar/ppi/ppi_34C.png",
            "https://www.mgm.gov.tr/FTPDATA/uzal/radar/ppi/ppi_34C.jpg",
            "https://www.mgm.gov.tr/FTPDATA/uzal/radar/ist/istppi15.jpg"
        ]),
        ('34C', 'MAX', [
            "https://www.mgm.gov.tr/FTPDATA/uzal/radar/max/max_34C.png",
            "https://www.mgm.gov.tr/FTPDATA/uzal/radar/max/max_34C.jpg",
            "https://www.mgm.gov.tr/FTPDATA/uzal/radar/ist/istmax15.jpg"
        ])
    ]

    for plate, short_code, folder_tag in STATION_MAP:
        urls = [
            f"https://www.mgm.gov.tr/FTPDATA/uzal/radar/max/max_{folder_tag}.png",
            f"https://www.mgm.gov.tr/FTPDATA/uzal/radar/max/max_{plate}.png",
            f"https://www.mgm.gov.tr/FTPDATA/uzal/radar/max/max_{folder_tag}.jpg",
            f"https://www.mgm.gov.tr/FTPDATA/uzal/radar/max/max_{plate}.jpg",
            f"https://www.mgm.gov.tr/FTPDATA/uzal/radar/{short_code}/{short_code}max15.jpg",
            f"https://www.mgm.gov.tr/FTPDATA/uzal/radar/{plate}/{plate}max15.jpg"
        ]
        targets.append((folder_tag, 'MAX', urls))

    uploaded_count = 0
    skipped_count = 0

    for station, product, urls in targets:
        print(f"\nİşleniyor: {station} - {product}")
        img = fetch_image_from_urls(urls)

        if img is not None:
            if has_radar_echo(img):
                webp_bytes = convert_to_lossless_webp(img)
                
                # MGM görseli güncellemediyse (Aynı görselse) atla
                if is_duplicate_in_r2(station, product, date_path, webp_bytes):
                    print(f" -> [ATLANDI] {station} - {product}: MGM görseli henüz yenilemedi (Birebir aynı dosya).")
                    skipped_count += 1
                    continue

                try:
                    object_key = f"{date_path}/{station}/{product}_{time_str}.webp"
                    s3.put_object(
                        Bucket=BUCKET_NAME,
                        Key=object_key,
                        Body=webp_bytes,
                        ContentType="image/webp"
                    )
                    print(f" -> [R2 YÜKLENDİ] {object_key} ({len(webp_bytes)/1024:.1f} KB)")
                    uploaded_count += 1
                except Exception as e:
                    print(f" -> [HATA] R2 yükleme hatası ({station}_{product}): {e}")
            else:
                print(f" -> [ATLANDI] {station} - {product}: Yağış ekosu yok.")
                skipped_count += 1
        else:
            print(f" -> [UYARI] {station} - {product}: Kaynak görsel çekilemedi.")

    print(f"\nİşlem tamamlandı. {uploaded_count} yeni dosya R2'ye yüklendi, {skipped_count} atlandı.")

if __name__ == "__main__":
    main()
