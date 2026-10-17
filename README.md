# MGM Radar PPI + MAX compact dBZ archive

## Policy

- Collect the 18 configured stations for PPI and MAX separately.
- Parse the actual UTC scan timestamp from the right-hand MGM panel. Collection time is separately recorded and is never substituted for scan time.
- Downloaded JPG/PNG bytes are processed in memory only; source images are not uploaded or retained as the radar archive.
- Persist one compressed NetCDF file per scan under `YYYY/MM/DD/<station_id>/`.
- Persist the image-derived `dBZ` field, the palette class index, and the right-panel/legend metadata inside the same NetCDF file.
- Do not persist a rainfall-rate field: all Z-R conversion methods stay in analysis code so future calibration experiments can be repeated.
- If scan UTC or dBZ legend verification fails, store the raster as a quarantined NetCDF under `_quarantine/`, clearly marked as not a verified timestamp/scale. Collector time is only used to organize this quarantine path, not as scan time.
- An identical source image for the same scan is skipped. If the same scan time returns different source-image bytes, a hash-suffixed conflict NetCDF is preserved rather than silently overwriting the first result.
- Each R2 upload carries the NetCDF SHA-256 and source-image SHA-256 in object metadata; an immediate HEAD check verifies file size and SHA metadata.

## R2 key layout

Verified scan:

```text
YYYY/MM/DD/<station_id>/PPI_YYYYMMDDTHHMMSSZ.nc
YYYY/MM/DD/<station_id>/MAX_YYYYMMDDTHHMMSSZ.nc
```

Timestamp/scale not verified:

```text
YYYY/MM/DD/<station_id>/_quarantine/PPI_UNPARSED_<source-hash>.nc
YYYY/MM/DD/<station_id>/_quarantine/MAX_UNPARSED_<source-hash>.nc
```

Time is the UTC timestamp read from the MGM image, not the download time. For verified scans, the NetCDF `time` coordinate has one element. Quarantined scans intentionally do not pretend that a collection timestamp is a scan timestamp.

## NetCDF content

- `dBZ(time,y,x)` or `dBZ(y,x)` for quarantined time-unparsed frames. This is derived from the displayed MGM RGB palette, not native raw radar moment data.
- `palette_class_index` (0-15; 255 is unclassified or masked) provides a compact trace to the lookup-table class.
- `range_mask(time,y,x)` marks the inspected circular coverage area. `overlay_mask` flags palette-coloured pixels outside that circle as display artifacts; `echo_mask` marks the remaining in-range palette-classified pixels. These are approximate display masks, not native radar-gate masks, and in-range overlays/clutter are not yet automatically detected. Pixels outside the range circle are set to missing in `dBZ`.
- `dBZ` is packed into an unsigned byte at a 0.5 dBZ scale factor. The current palette levels can be represented exactly; 255 is reserved for missing data. Xarray/netCDF4 automatically decodes the scale factor when loading the variable.
- Only 1-D `x_m(x)` and `y_m(y)` local azimuthal-equidistant axes are stored when Max Range was parsed. Reconstruct geographic latitude/longitude later from the radar station coordinates and `crs_proj4`. This avoids duplicating two large 2-D coordinate arrays in every scan. The pixel scale is approximate and derived from the visible range and inspected crop geometry; it is not native radar gate geometry.
- Global attributes include the raw OCR strings, structured `metadata_json`, source image SHA-256, download UTC time, source URL/HTTP headers, product/task/configuration/PRF/elevation/heights/range where parsed, colour legend text, the RGB and dBZ lookup tables, classifier version, and pixel-classification quality summaries.
- There is no Z-R or precipitation-rate variable. These are computed during analysis.

## GitHub Actions

`.github/workflows/radar_archiver.yml` intentionally has **no GitHub cron**. It retains `workflow_dispatch` and the existing `repository_dispatch` type `radar_trigger` for compatibility with the external scheduler.

Manual input defaults are `station=all`, `product=all`, `dry_run=false`. For the first test, select `station=34`, `product=all`, `dry_run=true`. This writes local NC files as a short-lived Actions artifact and does not access R2. Do not re-enable the old schedule while testing.

For a subsequent upload pilot, manually run again with `station=34`, `product=all`, `dry_run=false`. That run requires the four R2 secrets configured in the GitHub repository.

## Tests

```powershell
python -m pip install pytest
python -m pytest -q
```

## Local PowerShell dry run

Requires Windows Tesseract OCR installed and on `PATH`, as well as the Python packages in `requirements.txt`.

```powershell
$env:RADAR_DRY_RUN = "true"
$env:RADAR_STATION = "34"
$env:RADAR_PRODUCT = "all"
python fetch_radar.py
```

Output goes to `output/dry-run/` and can be inspected before any upload.

## Migration from old WebP daily batch

The old `batch_processor.py` expects R2 WebP objects and creates a daily NetCDF. It is incompatible with the new direct per-scan NetCDF layout and must not be run against this new layout. The local R2 archive manager should download/verify each per-scan `.nc` object and then the local consolidation step can combine those scans into daily NetCDF. Keep the old daily workflow disabled and remove `.github/workflows/radar_daily_batch.yml` from the production branch after the new collector passes its pilot test.

## Safety notes

- Never commit R2 credentials. Only GitHub Actions secrets or the local `R2_Secrets/.env` file may supply them.
- Do not delete R2 objects until the local archive manager has verified SHA-256 and the local copy can be opened as NetCDF.
- Every new timestamp is preserved, even if the dBZ field is identical to a previous scan at a different timestamp.
- A different image under an already-used scan timestamp is stored as a content-addressed conflict copy and reported in the logs, not silently overwritten.


Current package schema version: `mgm-scan-dbz-v1.1`. The circular `range_mask` is approximate display geometry; `overlay_mask` only detects palette-coloured pixels outside this circle. It does not yet classify in-range GUI overlays or distinguish meteorological echo from clutter.
