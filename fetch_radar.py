import os
import io
import requests
import boto3
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

# MGM Radar İstasyon Kodları (18 İl)
STATIONS = [
    '34C', '06C', '35C', '07C', '01C', '16C', '42C', '55C',
    '61C', '25C', '21C', '63C', '10C', '09C', '48C', '22C', '67C', '41C'
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
                print(f" -> [BAŞARILI] {url} ({len(res.content)/1024:.1f} KB)")
                return img
        except Exception:
            continue
    return None

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

    # Yapı: (İstasyon Klasörü, Ürün Adı, URL Listesi)
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

    # Diğer 17 il için MAX ürünlerini hedeflere ekle
    for st in STATIONS:
        if st != '34C':
            targets.append((st, 'MAX', [
                f"https://www.mgm.gov.tr/FTPDATA/uzal/radar/max/max_{st}.png",
                f"https://www.mgm.gov.tr/FTPDATA/uzal/radar/max/max_{st}.jpg",
                f"https://www.mgm.gov.tr/FTPDATA/uzal/radar/{st.lower()[:3]}/{st.lower()[:3]}max15.jpg"
            ]))

    uploaded_count = 0

    for station, product, urls in targets:
        print(f"\nİşleniyor: {station} - {product}")
        img = fetch_image_from_urls(urls)

        if img is not None:
            try:
                webp_bytes = convert_to_lossless_webp(img)
                # Hiyerarşik klasör yapısı: YIL/AY/GÜN/İSTASYON/ÜRÜN_SAAT.webp
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
            print(f" -> [ATLANDI] {station} - {product} için geçerli görsel bulunamadı.")

    print(f"\nİşlem tamamlandı. Toplam {uploaded_count} adet radar görseli Cloudflare R2'ye aktarıldı.")

if __name__ == "__main__":
    main()
