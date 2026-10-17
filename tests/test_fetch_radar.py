import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import fetch_radar as radar


def test_scan_time_parser_with_mgm_ocr_noise():
    text = "Istanbul\nPPI\nZ2_000_370\nTask: SURVEILLANC\nPRF: 400Hz\nElevation:0,3\nMax Range:370 km\n19:42:012Z\n16 OCT 2026 UTC"
    dt, _, _, note = radar._parse_scan_time_utc(text)
    assert dt is not None
    assert dt.strftime("%Y-%m-%dT%H:%M:%SZ") == "2026-10-16T19:42:01Z"
    assert "UTC" in note


def test_scan_time_parser_for_max_common_ocr_prefix_noise():
    text = "Istanbul\nMax with panels\nZ_00_15_250\nTask: VOL_A\nMax Range:250 km\n119:42:512Z\n116 OCT 2026 UIC"
    dt, _, _, _ = radar._parse_scan_time_utc(text)
    assert dt is not None
    assert dt.strftime("%Y-%m-%dT%H:%M:%SZ") == "2026-10-16T19:42:51Z"


def test_classification_exact_palette_and_unmatched_background():
    pixels = np.array([
        [radar.LUT_RGB[0], radar.LUT_RGB[15], [210, 210, 185]],
    ], dtype=np.uint8)
    dbz, idx, distance = radar.classify_rgb_to_dbz(pixels)
    assert dbz[0, 0] == radar.LUT_DBZ[0]
    assert dbz[0, 1] == radar.LUT_DBZ[15]
    assert idx[0, 0] == 0
    assert idx[0, 1] == 15
    assert np.isnan(dbz[0, 2])
    assert idx[0, 2] == 255
    assert np.isnan(distance[0, 2])


def test_station_product_selection():
    assert radar.select_stations("34") == ["34"]
    assert radar.select_products("all") == ["PPI", "MAX"]
    assert radar.select_products("MAX") == ["MAX"]


def test_r2_scan_key_uses_panel_utc_not_collection_time():
    from datetime import datetime, timezone
    meta = {"scan_time_utc": "2026-10-16T19:42:01Z"}
    collected = datetime(2026, 10, 17, 0, 5, 0, tzinfo=timezone.utc)
    key, basis = radar.r2_key_for_scan("34", "PPI", meta, "a" * 64, collected)
    assert key == "2026/10/16/34/PPI_20261016T194201Z.nc"
    assert basis == "scan_time_utc"


def test_unknown_time_goes_to_quarantine_not_a_fake_scan_time():
    from datetime import datetime, timezone
    meta = {"scan_time_utc": None}
    collected = datetime(2026, 10, 17, 0, 5, 0, tzinfo=timezone.utc)
    key, basis = radar.r2_key_for_scan("34", "MAX", meta, "a" * 64, collected)
    assert key == "2026/10/17/34/_quarantine/MAX_UNPARSED_aaaaaaaaaaaaaaaa.nc"
    assert basis == "quarantine_collection_date_only"

def test_make_dataset_masks_palette_pixels_outside_circular_range(tmp_path):
    from datetime import datetime, timezone
    from PIL import Image

    # PPI geometry is 720x720 with an inspected circular radar-range radius.
    image = Image.new("RGB", (720, 720), (0, 0, 0))
    pixels = image.load()
    pixels[360, 360] = tuple(int(v) for v in radar.LUT_RGB[15])  # valid in-range class
    pixels[10, 700] = tuple(int(v) for v in radar.LUT_RGB[6])   # bottom-left outside range

    meta = {
        "scan_time_utc": "2026-10-17T09:24:02Z",
        "scan_time_status": "parsed_utc",
        "archive_status": "scan_time_and_dbz_scale_verified",
        "max_range_km": 370.0,
        "colorbar_units_ocr": "dBZ",
        "colorbar_units_status": "verified",
    }
    ds = radar.make_dataset(
        image=image,
        station_id="34",
        product="PPI",
        meta=meta,
        source_sha="a" * 64,
        source_url="https://example.invalid/test.webp",
        content_type="image/webp",
        last_modified="",
        collected_at=datetime(2026, 10, 17, 9, 34, 11, tzinfo=timezone.utc),
    )

    assert {"range_mask", "overlay_mask", "echo_mask"}.issubset(ds.data_vars)
    assert int(ds["range_mask"].sel(time="2026-10-17T09:24:02").values[360, 360]) == 1
    assert int(ds["echo_mask"].sel(time="2026-10-17T09:24:02").values[360, 360]) == 1
    assert ds["dBZ"].sel(time="2026-10-17T09:24:02").values[360, 360] == radar.LUT_DBZ[15]

    assert int(ds["range_mask"].values[0, 700, 10]) == 0
    assert int(ds["overlay_mask"].values[0, 700, 10]) == 1
    assert int(ds["echo_mask"].values[0, 700, 10]) == 0
    assert np.isnan(ds["dBZ"].values[0, 700, 10])
    assert int(ds["palette_class_index"].values[0, 700, 10]) == 255
    assert ds.attrs["palette_pixels_outside_range_count"] == 1

    # Verify NetCDF4 writing where the optional backend is installed.
    import pytest
    pytest.importorskip("netCDF4")
    out_nc = tmp_path / "range_mask_smoke.nc"
    out_nc.write_bytes(radar.dataset_to_netcdf_bytes(ds))
    import xarray as xr
    with xr.open_dataset(out_nc, engine="netcdf4") as reopened:
        assert {"range_mask", "overlay_mask", "echo_mask"}.issubset(reopened.data_vars)
        assert int(reopened["overlay_mask"].values[0, 700, 10]) == 1
        assert np.isnan(reopened["dBZ"].values[0, 700, 10])
        assert reopened.attrs["netcdf_schema_version"] == "mgm-scan-dbz-v1.1"


def test_upload_same_source_and_versions_is_idempotent(monkeypatch):
    monkeypatch.setenv("R2_BUCKET_NAME", "test-bucket")
    source_sha = "a" * 64

    class FakeS3:
        def __init__(self):
            self.put_calls = 0
            self.head_calls = 0

        def head_object(self, *, Bucket, Key):
            self.head_calls += 1
            return {
                "Metadata": {
                    "source-image-sha256": source_sha,
                    "classifier-version": radar.CLASSIFIER_VERSION,
                    "metadata-parser-version": radar.METADATA_PARSER_VERSION,
                    "schema-version": radar.NETCDF_SCHEMA_VERSION,
                },
                "ContentLength": 123,
            }

        def put_object(self, **kwargs):
            self.put_calls += 1
            raise AssertionError("Idempotent kayit yeniden yuklenmemeli.")

    fake = FakeS3()
    result = radar.upload_netcdf(
        fake,
        "2026/10/17/34/PPI_20261017T101801Z.nc",
        b"test-payload",
        {
            "scan_time_utc": "2026-10-17T10:18:01Z",
            "station_id": "34",
            "product_code": "PPI",
        },
        source_sha,
    )

    assert result == "UNCHANGED"
    assert fake.put_calls == 0
    assert fake.head_calls == 1
