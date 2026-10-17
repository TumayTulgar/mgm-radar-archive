# Uygulama adımları — MGM PPI + MAX NetCDF arşivleyici v1.1

## Bu sürümdeki önemli değişiklik

- `range_mask`: PPI/MAX görüntüsündeki yaklaşık dairesel radar kapsama alanını işaretler.
- `overlay_mask`: LUT rengiyle eşleştiği hâlde kapsama dairesinin dışında kalan pikselleri işaretler. Bunlar `dBZ` rasterından çıkarılır.
- `echo_mask`: Daire içindeki LUT renk eşleşmelerini gösterir; meteorolojik ekoyu clutter'dan ayırdığı **iddia edilmez**.
- Şema sürümü `mgm-scan-dbz-v1.1` olarak artırıldı. Eski NetCDF'ler bu yeni değişkenleri içermez.

Bu geometrik maske MGM'nin görüntülenmiş ürün alanına göre yaklaşık bir maskedir; native radar-gate geometrisinin yerine geçmez. Daire içindeki olası arayüz bindirmeleri henüz otomatik tespit edilmiyor.

## A. Önce yerel dosyaları güncelle

VS Code / PowerShell:

`C:\Users\tumay\Documents\MGM-Radar-Archive`

Önce mevcut dosyayı yedekle:

```powershell
Copy-Item .\fetch_radar.py "$env:TEMP\fetch_radar_before_v1_1.py"
```

Paket içindeki `fetch_radar.py` dosyasını mevcut `fetch_radar.py` ile değiştir. `tests/test_fetch_radar.py` dosyasını da güncelle. README'yi depo köküne koyabilirsin. Bu aşamada R2'ye bağlantı veya GitHub workflow çalıştırma yok.

## B. Statik testleri çalıştır

```powershell
cd C:\Users\tumay\Documents\MGM-Radar-Archive
.\.venv\Scripts\python.exe -m py_compile fetch_radar.py
.\.venv\Scripts\python.exe -m pytest -q
```

Testler geçmezse canlı denemeye geçme; hata çıktısını incele.

## C. İstanbul yerel dry-run

Tesseract terminalde çalışıyor olmalı. Aynı PowerShell oturumunda:

```powershell
$env:Path = "C:\Program Files\Tesseract-OCR;$env:Path"
.\.venv\Scripts\python.exe fetch_radar.py `
    --station 34 `
    --product all `
    --dry-run `
    --output-dir output/dry-run-pilot-v1_1
```

Ardından dosyaları listele:

```powershell
Get-ChildItem .\output\dry-run-pilot-v1_1 -Recurse -File |
    Select-Object FullName, Length, LastWriteTime
```

Bu test R2'ye yükleme yapmamalı. Her iki çıktı için `range_mask`, `overlay_mask`, `echo_mask` değişkenlerinin yazıldığını ve menzil dışı dBZ değerlerinin eksik kaldığını doğrula. `palette_pixels_outside_range_count` ve `classified_pixel_count` özetlerini de kontrol et.

## D. Bundan sonra

Bu yerel dry-run kontrol edilmeden Git'e commit/push yapma ve GitHub Actions workflow'larını etkinleştirme. Sonraki adım, kaynağın aynı taramasına ait özgün MGM görüntüsüyle hızlı önizlemeyi karşılaştırmak ve daire içindeki olası arayüz bindirmelerini değerlendirmektir. Bu kontroller bitince İstanbul'da tek-istasyon R2 pilotu yapılabilir.
