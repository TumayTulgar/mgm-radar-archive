import os
import urllib.parse
from datetime import datetime
import requests
from bs4 import BeautifulSoup

RADAR_SOURCES = {
    "MAX": "https://www.mgm.gov.tr/sondurum/radar.aspx?rG=img&rR=34C&rU=max",
    "PPI": "https://www.mgm.gov.tr/sondurum/radar.aspx?rG=img&rR=34C&rU=ppi"
}

SAVE_DIR = "images"
os.makedirs(SAVE_DIR, exist_ok=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://www.mgm.gov.tr/",
    "Cache-Control": "no-cache"
}

def download_radar_product(product_code, page_url):
    try:
        response = requests.get(page_url, headers=HEADERS, timeout=15)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        img_tag = soup.find("img", id="radarImg") or soup.find(
            "img", src=lambda s: s and ("radar" in s.lower() or "34C" in s)
        )

        if not img_tag or not img_tag.get("src"):
            print(f"[{product_code}] Radar görseli bulunamadı.")
            return

        full_img_url = urllib.parse.urljoin(page_url, img_tag["src"])
        img_res = requests.get(full_img_url, headers=HEADERS, timeout=15)
        img_res.raise_for_status()

        # Sunucudaki resmin orijinal oluşturulma anını al
        last_modified = img_res.headers.get('Last-Modified')
        timestamp = None
        
        if last_modified:
            try:
                from email.utils import parsedate_to_datetime
                dt = parsedate_to_datetime(last_modified)
                timestamp = dt.strftime("%Y%m%d_%H%M%S")
            except Exception:
                pass
        
        # Eğer sunucu tarih vermezse (hata olursa) güvenli liman olarak kodun çalıştığı anı kullan
        if not timestamp:
            timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")

        filename = f"radar_34C_{product_code}_{timestamp}.png"
        filepath = os.path.join(SAVE_DIR, filename)

        with open(filepath, "wb") as f:
            f.write(img_res.content)

        print(f"[{product_code}] Başarıyla kaydedildi: {filepath}")

    except Exception as e:
        print(f"[{product_code}] Hata oluştu: {e}")

if __name__ == "__main__":
    for code, url in RADAR_SOURCES.items():
        download_radar_product(code, url)
