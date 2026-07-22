import os
import urllib.parse
from datetime import datetime
import requests
from bs4 import BeautifulSoup

# Çekilecek radar ürünleri
RADAR_SOURCES = {
    "MAX": "https://www.mgm.gov.tr/sondurum/radar.aspx?rG=img&rR=34C&rU=max",
    "PPI": "https://www.mgm.gov.tr/sondurum/radar.aspx?rG=img&rR=34C&rU=ppi"
}

SAVE_DIR = "images"
os.makedirs(SAVE_DIR, exist_ok=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://www.mgm.gov.tr/"
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

        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        # Örn: radar_34C_MAX_20260722_134500.png veya radar_34C_PPI_20260722_134500.png
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
