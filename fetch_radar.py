import os
import io
import requests
import boto3
import numpy as np
from PIL import Image
from datetime import datetime

# GitHub Secrets üzerinden gelen R2 bilgileri
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

# MGM Radar İstasyon Listesi (Görseldeki 18 İl + Kodları)
STATIONS = [
    ('03C', 'Afyonkarahisar'),
    ('06C', 'Ankara'),
    ('07C', 'Antalya'),
    ('10C', 'Balikesir'),
    ('16C', 'Bursa'),
    ('25C', 'Erzurum'),
    ('27C', 'Gaziantep'),
    ('31C', 'Hatay'),
    ('35C', 'Izmir'),
    ('48C', 'Mugla'),
    ('55C', 'Samsun'),
    ('58C', 'Sivas'),
    ('61C', 'Trabzon'),
    ('63C', 'Sanliurfa'),
    ('67C', 'Zonguldak'),
    ('70C', 'Karaman'),
    ('79C', 'Kilis')
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.mgm.gov.tr/sondurum/radar.aspx",
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"
}

def fetch_image_from_urls(urls):
    """Verilen alternatif URL'leri sırayla dener, doğrulanan ilk görseli döndürür."""
    for url in urls:
        try:
            res = requests.get(url, headers=HEADERS, timeout=10)
            if res.status_code == 200 and len(res.content) > 5000:
                img = Image.open(io.BytesIO(res.content))
                img.load()
                print(f" -> [İNDİRİLDİ] {url} ({len(res.content)/1024:.1f} KB)")
                return img
        except Exception:
            continue
    return None

def has_radar_echo(img, min_echo_pixels=25):
    """
    Görselde dBZ > 2 yağış ekosu (renkli radar kütlesi) olup olmadığını analiz eder.
    Haritanın zemin rengini, karayolu/il sınırlarını ve lejantı eleyerek radar ekolarını tespit eder.
    """
    rgb_img = img.convert("RGB")
    w, h = rgb_img.size
    
    # Sağdaki lejantı ve üst/alt başlıkları elemek için harita merkez alanını kırp
    crop_box = (int(w * 0.05), int(h * 0.10), int(w * 0.85), int(h * 0.90))
    cropped = rgb_img.crop(crop_box)
    
    np_img = np.array(cropped)
    r = np_img[:, :, 0].astype(float)
    g = np_img[:, :, 1].astype(float)
    b = np_img[:, :, 2].astype(float)
    
    # Renk doygunluğu (HSV Saturation hesaplaması)
    max_c = np.maximum(np.maximum(r, g), b)
    min_c = np.minimum(np.minimum(r, g), b)
    saturation = np.where(max_c == 0, 0, (max_c - min_c) / max_c)
    
    # MGM Radar Eko Renk Kriteri (Mavi, Yeşil, Sarı, Turuncu, Kırmızı, Mor eko pikselleri)
    # Doygunluğu yüksek ve nötr gri/beyaz olmayan pikseller
    echo_mask = (saturation > 0.42) & (max_c > 75) & (max_c < 252)
    
    # Harita üzerindeki kırmızı il/yol sınır hatlarını elemek için filtre
    road_mask = (r > 200) & (g < 60) & (b < 60)
    valid_echo_mask = echo_mask & (~road_mask)
    
    echo_pixels = np.sum(valid_echo_mask)
    print(f"   [EKO ANALİZİ] Tespit edilen eko piksel sayısı: {echo_pixels}")
    
    return echo_pixels >= min_echo_pixels

def convert_to_lossless_webp(img):
    """Görseli Kayıpsız (Lossless) WebP formatına dönüştürür."""
    buffer = io.BytesIO()
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGBA")
    else:
        img = img.convert("RGB")
    img.save(buffer, format="WEBP", lossless=True, quality=100)
    return buffer.getvalue()

def main():
    now = datetime.now()
    date_path = now.strftime('%Y/%m/%d')
    time_str = now.strftime('%H%M%S')

    # Target yapısı: (İstasyon Klasörü, Ürün Adı, URL Alternatifleri)
    targets = [
        # 1. Türkiye Birleştirilmiş Görüntü (PPI)
        ('COMPOSITE', 'PPI', [
            "https://www.mgm.gov.tr/FTPDATA/uzal/radar/ppi/ppi_00.png",
            "https://www.mgm.gov.tr/FTPDATA/uzal/radar/ppi/ppi_00.jpg",
            "https://www.mgm.gov.tr/FTPDATA/uzal/radar/comp/compppi15.jpg"
        ]),
        # 2. İstanbul (Hem MAX hem PPI)
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

    # 3. Görseldeki Diğer Tüm İller (MAX Ürünleri)
    for code, name in STATIONS:
        code_num = code.replace('C', '')
        targets.append((code, 'MAX', [
            f"https://www.mgm.gov.tr/FTPDATA/uzal/radar/max/max_{code}.png",
            f"https://www.mgm.gov.tr/FTPDATA/uzal/radar/max/max_{code_num}.png",
            f"https://www.mgm.gov.tr/FTPDATA/uzal/radar/max/max_{code}.jpg",
            f"https://www.mgm.gov.tr/FTPDATA/uzal/radar/{code_num}/{code_num}max15.jpg"
        ]))

    uploaded_count = 0
    skipped_count = 0

    for station, product, urls in targets:
        print(f"\nİşleniyor: {station} - {product}")
        img = fetch_image_from_urls(urls)

        if img is not None:
            # dBZ > 2 Eko Kontrolü
            if has_radar_echo(img):
                try:
                    webp_bytes = convert_to_lossless_webp(img)
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
                    print(f" -> [HATA] R2'ye yüklenirken hata oluştu ({station}_{product}): {e}")
            else:
                print(f" -> [ATLANDI] {station} - {product} üzerinde dBZ > 2 eko bulunamadı (Açık hava).")
                skipped_count += 1
        else:
            print(f" -> [UYARI] {station} - {product} için kaynak görsel çekilemedi.")

    print(f"\nİşlem tamamlandı: {uploaded_count} adet radarda eko tespit edilip R2'ye aktarıldı, {skipped_count} adet boş görsel atlandı.")

if __name__ == "__main__":
    main()
