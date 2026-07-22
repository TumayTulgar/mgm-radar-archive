import os
import urllib.parse
from datetime import datetime
import requests
from bs4 import BeautifulSoup

PAGE_URL = "https://www.mgm.gov.tr/sondurum/radar.aspx?rG=img&rR=34C&rU=max"
SAVE_DIR = "images"
os.makedirs(SAVE_DIR, exist_ok=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://www.mgm.gov.tr/"
}

def download_radar():
    try:
        response = requests.get(PAGE_URL, headers=HEADERS, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        img_tag = soup.find("img", id="radarImg") or soup.find(
            "img", src=lambda s: s and ("radar" in s.lower() or "34C" in s)
        )

        if not img_tag or not img_tag.get("src"):
            print("Radar görseli bulunamadı.")
            return

        full_img_url = urllib.parse.urljoin(PAGE_URL, img_tag["src"])
        img_res = requests.get(full_img_url, headers=HEADERS, timeout=15)
        img_res.raise_for_status()

        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        filepath = os.path.join(SAVE_DIR, f"radar_34C_{timestamp}.png")

        with open(filepath, "wb") as f:
            f.write(img_res.content)

        print(f"Başarıyla kaydedildi: {filepath}")

    except Exception as e:
        print(f"Hata oluştu: {e}")

if __name__ == "__main__":
    download_radar()
