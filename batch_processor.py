import os
import io
import boto3
import numpy as np
import pandas as pd
import xarray as xr
from PIL import Image
from datetime import datetime, timedelta, timezone
from pyproj import CRS, Transformer

# ============================================================
# SABİTLER VE FİZİKSEL PARAMETRELER (Jupyter Notebook'tan Birebir)
# ============================================================
RADAR_INFO = {
    "Ankara":         {"coords": (39.798611, 32.971389), "id": "06"},
    "İstanbul":       {"coords": (41.341277, 28.356441), "id": "34"},
    "Balıkesir":      {"coords": (39.740285, 27.618677), "id": "10"},
    "Zonguldak":      {"coords": (41.181154, 31.798572), "id": "67"},
    "İzmir":          {"coords": (38.311389, 27.001139), "id": "35"},
    "Muğla":          {"coords": (36.885889, 28.332500), "id": "48"},
    "Antalya":        {"coords": (36.266389, 30.437500), "id": "07"},
    "Hatay":          {"coords": (36.318000, 35.788167), "id": "31"},
    "Samsun":         {"coords": (41.314722, 36.036667), "id": "55"},
    "Trabzon":        {"coords": (41.074722, 39.468333), "id": "61"},
    "Afyonkarahisar": {"coords": (38.401667, 30.419167), "id": "03"},
    "Bursa":          {"coords": (40.538333, 29.903333), "id": "16"},
    "Karaman":        {"coords": (37.391735, 33.138856), "id": "70"},
    "Gaziantep":      {"coords": (37.137222, 37.137222), "id": "27"},
    "Sivas":          {"coords": (39.765053, 36.854780), "id": "58"},
    "Erzurum":        {"coords": (40.161286, 41.553013), "id": "25"},
    "Şanlıurfa":      {"coords": (37.715077, 39.828924), "id": "63"},
    "Kilis":          {"coords": (36.717222, 37.121944), "id": "79"},
}

PLAN_X0, PLAN_X1 = 0, 659
PLAN_Y0, PLAN_Y1 = 60, 719
PLAN_BOX = (PLAN_X0, PLAN_Y0, PLAN_X1 + 1, PLAN_Y1 + 1)
PLAN_SIZE = 660

CENTER_X_LOCAL = 329.5
CENTER_Y_LOCAL = 329.5
KM_PER_PIXEL = 0.758725341
MAX_RANGE_KM = CENTER_X_LOCAL * KM_PER_PIXEL

LUT_RGB = np.array([
    [255, 0, 255], [221, 3, 156], [216, 6, 31], [248, 55, 0],
    [253, 123, 1], [255, 170, 1], [255, 208, 1], [255, 240, 13],
    [153, 255, 32], [18, 244, 22], [0, 214, 22], [0, 174, 47],
    [0, 141, 73], [0, 147, 131], [1, 184, 182], [0, 138, 223],
], dtype=np.float32)

LUT_DBZ = np.array([
    69.0, 63.0, 57.5, 54.0, 51.5, 47.0, 41.5, 38.0,
    35.5, 31.0, 25.5, 22.0, 19.5, 15.0, 9.5, 4.5,
], dtype=np.float32)

RGB_MATCH_MAX_DIST = 30.0
ZR_A = 200.0
ZR_B = 1.6
MAX_PHYSICAL_RATE_MMH = 300.0

# ============================================================
# BULUT BAĞLANTISI (R2)
# ============================================================
ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID")
ACCESS_KEY = os.environ.get("R2_ACCESS_KEY_ID")
SECRET_KEY = os.environ.get("R2_SECRET_ACCESS_KEY")
BUCKET_NAME = os.environ.get("R2_BUCKET_NAME")

s3 = boto3.client(
    service_name="s3",
    endpoint_url=f"https://{ACCOUNT_ID}.r2.cloudflarestorage.com",
    aws_access_key_id=ACCESS_KEY,
    aws_secret_access_key=SECRET_KEY,
    region_name="auto"
)

# ============================================================
# MATEMATİK VE DÖNÜŞÜM FONKSİYONLARI
# ============================================================
def classify_rgb_to_dbz(rgb_array):
    arr = np.asarray(rgb_array, dtype=np.float32)
    diff = arr[:, :, np.newaxis, :] - LUT_RGB[np.newaxis, np.newaxis, :, :]
    dist = np.sqrt(np.sum(diff ** 2, axis=-1))
    nearest_idx = np.argmin(dist, axis=-1)
    nearest_dist = np.min(dist, axis=-1)
    dbz = LUT_DBZ[nearest_idx].copy()
    dbz[nearest_dist >= RGB_MATCH_MAX_DIST] = np.nan
    return dbz

def dbz_to_rate_mmh(dbz):
    dbz = np.asarray(dbz, dtype=np.float32)
    valid = ~np.isnan(dbz)
    Z = np.zeros_like(dbz, dtype=np.float64)
    Z[valid] = 10.0 ** (dbz[valid] / 10.0)
    R = np.zeros_like(dbz, dtype=np.float64)
    R[valid] = (Z[valid] / ZR_A) ** (1.0 / ZR_B)
    R = np.minimum(R, MAX_PHYSICAL_RATE_MMH)
    return R.astype(np.float32)

def get_range_mask():
    yy, xx = np.mgrid[0:PLAN_SIZE, 0:PLAN_SIZE]
    dist_px = np.sqrt((xx - CENTER_X_LOCAL) ** 2 + (yy - CENTER_Y_LOCAL) ** 2)
    return dist_px <= CENTER_X_LOCAL

def build_lat_lon_grid(radar_lat, radar_lon):
    aeqd_crs = CRS.from_proj4(f"+proj=aeqd +lat_0={radar_lat} +lon_0={radar_lon} +R=6371008.8 +units=m +no_defs")
    wgs84 = CRS.from_epsg(4326)
    to_wgs84 = Transformer.from_crs(aeqd_crs, wgs84, always_xy=True)
    
    cols, rows = np.meshgrid(np.arange(PLAN_SIZE, dtype=np.float64), np.arange(PLAN_SIZE, dtype=np.float64))
    x_m = (cols - CENTER_X_LOCAL) * KM_PER_PIXEL * 1000.0
    y_m = (CENTER_Y_LOCAL - rows) * KM_PER_PIXEL * 1000.0
    lon_grid, lat_grid = to_wgs84.transform(x_m, y_m)
    return lat_grid, lon_grid

# ============================================================
# BATCH İŞLEMİ
# ============================================================
def process_station_day(station_name, target_date):
    station_id = RADAR_INFO[station_name]["id"]
    radar_lat, radar_lon = RADAR_INFO[station_name]["coords"]
    date_path = target_date.strftime("%Y/%m/%d")
    prefix = f"{date_path}/{station_id}/"
    
    # 1. WebP dosyalarını R2'den listele
    paginator = s3.get_paginator("list_objects_v2")
    objects = []
    for page in paginator.paginate(Bucket=BUCKET_NAME, Prefix=prefix):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith(".webp"):
                objects.append(obj["Key"])
                
    if not objects:
        print(f"[{station_name}] {date_path} için veri bulunamadı.")
        return
        
    objects.sort()
    frames_dbz = []
    frames_rate = []
    times = []
    range_mask = get_range_mask()
    
    print(f"[{station_name}] {len(objects)} kare işleniyor...")
    
    # 2. Her kareyi indir, fiziksel dönüşümü yap
    for key in objects:
        time_str = key.split("MAX_")[-1].replace(".webp", "")
        # Saat damgasını parse et (Örn: 154000)
        dt = datetime.strptime(f"{date_path} {time_str}", "%Y/%m/%d %H%M%S")
        times.append(dt)
        
        # Dosyayı RAM'e al
        obj = s3.get_object(Bucket=BUCKET_NAME, Key=key)
        img = Image.open(io.BytesIO(obj["Body"].read())).convert("RGB")
        cropped = np.array(img.crop(PLAN_BOX), dtype=np.uint8)
        
        # Dönüşüm
        dbz = classify_rgb_to_dbz(cropped)
        rate = dbz_to_rate_mmh(dbz)
        
        # Maskeleme
        dbz[~range_mask] = np.nan
        rate[~range_mask] = np.nan
        
        frames_dbz.append(dbz)
        frames_rate.append(rate)
        
    cube_dbz = np.array(frames_dbz, dtype=np.float32)
    cube_rate = np.array(frames_rate, dtype=np.float32)
    lat_grid, lon_grid = build_lat_lon_grid(radar_lat, radar_lon)
    
    # 3. NetCDF oluştur
    ds = xr.Dataset(
        data_vars={
            "dBZ": (("time", "y", "x"), cube_dbz),
            "precipitation_rate": (("time", "y", "x"), cube_rate),
        },
        coords={
            "time": times,
            "y": np.arange(PLAN_SIZE),
            "x": np.arange(PLAN_SIZE),
            "lat": (("y", "x"), lat_grid),
            "lon": (("y", "x"), lon_grid)
        },
        attrs={
            "station": station_name,
            "radar_lat": radar_lat,
            "radar_lon": radar_lon,
            "crs": f"+proj=aeqd +lat_0={radar_lat} +lon_0={radar_lon} +R=6371008.8 +units=m +no_defs",
            "zr_a": ZR_A,
            "zr_b": ZR_B
        }
    )
    
    nc_filename = f"{target_date.strftime('%Y%m%d')}_{station_id}_MAX.nc"
    nc_path = f"/tmp/{nc_filename}"
    
    # zlib kompresyonu ile kaydet
    encoding = {var: {"zlib": True, "complevel": 4} for var in ["dBZ", "precipitation_rate"]}
    ds.to_netcdf(nc_path, encoding=encoding)
    
    # 4. NetCDF'i R2'ye yükle (Günün klasörüne .nc olarak)
    r2_nc_key = f"{date_path}/{nc_filename}"
    s3.upload_file(nc_path, BUCKET_NAME, r2_nc_key)
    print(f"[{station_name}] {r2_nc_key} yüklendi. Temizlik yapılıyor...")
    
    # 5. Başarılıysa, eski WebP dosyalarını sil (Kotayı temizle)
    delete_keys = [{'Key': key} for key in objects]
    # boto3 delete_objects max 1000 obje kabul eder, batchleyerek siliyoruz
    for i in range(0, len(delete_keys), 1000):
        s3.delete_objects(Bucket=BUCKET_NAME, Delete={'Objects': delete_keys[i:i+1000]})
        
    os.remove(nc_path)
    print(f"[{station_name}] Tamamlandı.")

if __name__ == "__main__":
    # GitHub Action gece 00:30 UTC'de çalıştığında bir önceki günü (dün) hedef almalı
    target_date = datetime.now(timezone.utc) - timedelta(days=1)
    
    print(f"Batch Processing Başlıyor... Hedef Tarih: {target_date.strftime('%Y-%m-%d')}")
    
    for station_name in RADAR_INFO.keys():
        try:
            process_station_day(station_name, target_date)
        except Exception as e:
            print(f"[{station_name}] Kritik Hata: {e}")
