from __future__ import annotations

"""MGM rendered PPI/MAX -> per-scan, compact dBZ NetCDF -> Cloudflare R2.

The actual scan timestamp is read from the MGM right-hand information panel and
stored as UTC. Collection time is recorded separately and is NEVER substituted
for scan time. Source images are processed in memory and are not archived.
"""

import argparse
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import boto3
import numpy as np
import pytesseract
import requests
import xarray as xr
from PIL import Image, ImageEnhance, ImageOps
from botocore.config import Config
from botocore.exceptions import ClientError

# -------------------------- Fixed source configuration ----------------------
PRODUCTS = ("PPI", "MAX")
URL_SUFFIXES = ("15", "00", "05", "10", "")
REQUEST_TIMEOUT_SECONDS = 5.0
MAX_WORKERS = 6
MIN_IMAGE_BYTES = 5_000
OCR_SCALE = 3
RGB_MATCH_MAX_DIST = 30.0
CLASSIFIER_VERSION = "mgm-rendered-rgb-to-dbz-v1.0"
METADATA_PARSER_VERSION = "mgm-panel-ocr-v1.0"
NETCDF_SCHEMA_VERSION = "mgm-scan-dbz-v1.1"

# Values were extracted by the project owner from the MGM reflectivity colour scale.
# Keep the table explicit and versioned; do not silently infer a new palette.
LUT_RGB = np.array([
    [255, 0, 255], [221, 3, 156], [216, 6, 31], [248, 55, 0],
    [253, 123, 1], [255, 170, 1], [255, 208, 1], [255, 240, 13],
    [153, 255, 32], [18, 244, 22], [0, 214, 22], [0, 174, 47],
    [0, 141, 73], [0, 147, 131], [1, 184, 182], [0, 138, 223],
], dtype=np.uint8)
LUT_DBZ = np.array([
    69.0, 63.0, 57.5, 54.0, 51.5, 47.0, 41.5, 38.0,
    35.5, 31.0, 25.5, 22.0, 19.5, 15.0, 9.5, 4.5,
], dtype=np.float32)

# Coordinates are (latitude, longitude). These are station/radar coordinates,
# not the user's weather-station coordinates.
STATIONS: dict[str, dict[str, Any]] = {
    "03": {"name": "Afyonkarahisar", "short": "afy", "tag": "03C", "lat": 38.401667, "lon": 30.419167},
    "06": {"name": "Ankara",         "short": "ank", "tag": "06C", "lat": 39.798611, "lon": 32.971389},
    "07": {"name": "Antalya",        "short": "ant", "tag": "07C", "lat": 36.266389, "lon": 30.437500},
    "10": {"name": "Balıkesir",      "short": "blk", "tag": "10C", "lat": 39.740285, "lon": 27.618677},
    "16": {"name": "Bursa",          "short": "brs", "tag": "16C", "lat": 40.538333, "lon": 29.903333},
    "25": {"name": "Erzurum",        "short": "erz", "tag": "25C", "lat": 40.161286, "lon": 41.553013},
    "27": {"name": "Gaziantep",      "short": "gzt", "tag": "27C", "lat": 37.137222, "lon": 37.137222},
    "31": {"name": "Hatay",          "short": "hty", "tag": "31C", "lat": 36.318000, "lon": 35.788167},
    "34": {"name": "İstanbul",       "short": "ist", "tag": "34C", "lat": 41.341277, "lon": 28.356441},
    "35": {"name": "İzmir",          "short": "izm", "tag": "35C", "lat": 38.311389, "lon": 27.001139},
    "48": {"name": "Muğla",          "short": "mgl", "tag": "48C", "lat": 36.885889, "lon": 28.332500},
    "55": {"name": "Samsun",         "short": "smn", "tag": "55C", "lat": 41.314722, "lon": 36.036667},
    "58": {"name": "Sivas",          "short": "svs", "tag": "58C", "lat": 39.765053, "lon": 36.854780},
    "61": {"name": "Trabzon",        "short": "trb", "tag": "61C", "lat": 41.074722, "lon": 39.468333},
    "63": {"name": "Şanlıurfa",      "short": "srf", "tag": "63C", "lat": 37.715077, "lon": 39.828924},
    "67": {"name": "Zonguldak",      "short": "zng", "tag": "67C", "lat": 41.181154, "lon": 31.798572},
    "70": {"name": "Karaman",        "short": "krm", "tag": "70C", "lat": 37.391735, "lon": 33.138856},
    "79": {"name": "Kilis",          "short": "mob", "tag": "79C", "lat": 36.717222, "lon": 37.121944},
}

# Observed map-panel layouts from MGM screenshots. Max range is read from each
# image's right panel; it is not hard-coded as a range value.
PRODUCT_GEOMETRY = {
    "PPI": {"crop_box": (0, 0, 720, 720), "size": 720, "center": 359.5},
    "MAX": {"crop_box": (0, 60, 660, 720), "size": 660, "center": 329.5},
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache, no-store, max-age=0",
    "Pragma": "no-cache",
}
MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
          "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def generate_candidate_urls(station_id: str, product: str) -> list[str]:
    s = STATIONS[station_id]
    urls: list[str] = []
    for suffix in URL_SUFFIXES:
        for ext in ("jpg", "png"):
            urls.append(f"https://www.mgm.gov.tr/FTPDATA/uzal/radar/{s['short']}/{s['short']}{product.lower()}{suffix}.{ext}")
    # Existing documented MAX fallback endpoints only; no unverified PPI fallback invented.
    if product == "MAX":
        for tag in (s["tag"], station_id, s["short"]):
            for ext in ("png", "jpg"):
                urls.append(f"https://www.mgm.gov.tr/FTPDATA/uzal/radar/max/max_{tag}.{ext}")
    return list(dict.fromkeys(urls))


def fetch_image(urls: list[str], product: str) -> tuple[Image.Image | None, bytes | None, str | None, str | None, str | None, str | None]:
    headers = dict(HEADERS)
    headers["Referer"] = f"https://www.mgm.gov.tr/sondurum/radar.aspx?rG=img&rU={product.lower()}"
    last_error = "No candidate URL returned a valid radar image"
    with requests.Session() as session:
        session.headers.update(headers)
        for url in urls:
            try:
                response = session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)  # TLS verification enabled.
                if response.status_code != 200:
                    last_error = f"HTTP {response.status_code}: {url}"
                    continue
                if len(response.content) < MIN_IMAGE_BYTES:
                    last_error = f"Image response too small ({len(response.content)} bytes): {url}"
                    continue
                with Image.open(io.BytesIO(response.content)) as source:
                    source.load()
                    if source.format not in {"JPEG", "PNG", "WEBP"}:
                        last_error = f"Unsupported image format {source.format}: {url}"
                        continue
                    image = source.convert("RGB")
                return image, response.content, url, response.headers.get("Content-Type", ""), response.headers.get("Last-Modified", ""), None
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc} ({url})"
    return None, None, None, None, None, last_error


def _ocr_variants(image: Image.Image, box: tuple[int, int, int, int]) -> list[tuple[str, Image.Image]]:
    crop = image.convert("RGB").crop(box)
    crop = crop.resize((max(1, crop.width * OCR_SCALE), max(1, crop.height * OCR_SCALE)), Image.Resampling.LANCZOS)
    gray = ImageOps.grayscale(crop)
    gray = ImageEnhance.Contrast(gray).enhance(2.0)
    threshold = gray.point(lambda p: 255 if p > 115 else 0)
    return [("gray", gray), ("threshold", threshold)]


def _best_ocr(image: Image.Image, box: tuple[int, int, int, int], psm_values: tuple[int, ...]) -> tuple[str, str, float | None]:
    candidates: list[tuple[int, str, str, float | None]] = []
    for variant_name, variant in _ocr_variants(image, box):
        for psm in psm_values:
            try:
                text = pytesseract.image_to_string(variant, lang="eng", config=f"--oem 3 --psm {psm}").strip()
                data = pytesseract.image_to_data(variant, lang="eng", config=f"--oem 3 --psm {psm}", output_type=pytesseract.Output.DICT)
                vals = []
                for token, conf in zip(data.get("text", []), data.get("conf", [])):
                    try:
                        c = float(conf)
                        if token.strip() and c >= 0:
                            vals.append(c)
                    except (TypeError, ValueError):
                        pass
                confidence = float(np.mean(vals)) if vals else None
            except Exception:
                continue
            normalized = text.upper().replace("OCT.", "OCT")
            score = 0
            if re.search(r"\d{1,2}\s*:\s*\d{2}\s*:\s*\d{2}", normalized): score += 30
            if re.search(r"\d{1,3}\s+(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\s+20\d{2}", normalized): score += 30
            if re.search(r"\bUTC\b|\bUIC\b|\d{1,2}:\d{2}:\d{2}\s*Z\b", normalized): score += 15
            for marker in ("RANGE", "ELEVATION", "PRF", "TASK", "HGT", "HEIGHT"):
                if marker in normalized: score += 3
            score += min(len(text), 120) / 120
            if variant_name == "threshold": score += 0.25
            candidates.append((score, text, variant_name + f"_psm{psm}", confidence))
    if not candidates:
        return "", "none", None
    candidates.sort(key=lambda x: x[0], reverse=True)
    _, text, variant, confidence = candidates[0]
    return text, variant, confidence


def _parse_scan_time_utc(text: str) -> tuple[datetime | None, str | None, str | None, str]:
    upper = text.upper().replace("OCT.", "OCT")
    # OCR occasionally prepends a stray digit (e.g. 119:42:51Z or 116 OCT 2026).
    # Keep the complete raw OCR separately; normalization here only recovers the timestamp.
    time_tokens = re.findall(r"(\d{1,2})\s*:\s*(\d{2})\s*:\s*([0-9O]{2})", upper)
    date_matches = list(re.finditer(r"(\d{1,3})\s+(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\s+(20\d{2})", upper))
    time_raw = ":".join(time_tokens[0]) if time_tokens else None
    date_raw = date_matches[0].group(0) if date_matches else None
    if not time_tokens or not date_matches:
        return None, time_raw, date_raw, "Could not parse both HH:MM:SS and D MON YYYY from MGM panel OCR"

    # Require explicit evidence that MGM labelled the panel UTC. UIC is a common OCR
    # confusion for UTC and is retained in raw OCR for audit.
    utc_evidence = bool(re.search(r"\bUTC\b|\bUIC\b|\d{1,2}:\d{2}:\d{2}\s*Z\b", upper))
    if not utc_evidence:
        return None, time_raw, date_raw, "Timestamp found, but no UTC/Z marker was detected"

    try:
        h_txt, m_txt, s_txt = time_tokens[0]
        hour, minute, second = int(h_txt), int(m_txt), int(s_txt.replace("O", "0"))
        day_token, month_token, year_token = date_matches[0].groups()
        # If OCR adds a leading digit to the day (e.g. 116 OCT), use the final two
        # digits only when the three-digit token is not itself a valid calendar day.
        day = int(day_token)
        if day > 31 and len(day_token) == 3 and day_token.startswith("1"):
            day = int(day_token[-2:])
        month = MONTHS[month_token]
        dt = datetime(int(year_token), month, day, hour, minute, second, tzinfo=timezone.utc)
        return dt, time_raw, date_raw, "Timestamp parsed from MGM right panel; UTC marker present"
    except (ValueError, OverflowError):
        return None, time_raw, date_raw, "OCR timestamp tokens were not a valid calendar datetime"


def _first_float(pattern: str, text: str) -> float | None:
    m = re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "."))
    except ValueError:
        return None


def parse_panel_metadata(image: Image.Image, product: str) -> dict[str, Any]:
    width, height = image.size
    # Observed MGM layout: right-hand info panel at x ~= image_width - 165.
    panel_x = max(0, width - 165)
    header_box = (panel_x, min(75, height), width, min(340, height))
    legend_box = (panel_x, min(345, height), width, min(660, height))
    header_text, header_variant, header_conf = _best_ocr(image, header_box, (6, 11))
    legend_text, legend_variant, legend_conf = _best_ocr(image, legend_box, (12, 6, 11))
    # Preserve a rotated rendition as a secondary OCR attempt for vertical legend labels.
    try:
        legend_crop = image.crop(legend_box).rotate(90, expand=True)
        rotated_text, rotated_variant, rotated_conf = _best_ocr(
            legend_crop, (0, 0, legend_crop.width, legend_crop.height), (6, 11)
        )
        if rotated_text and len(rotated_text) > len(legend_text):
            legend_text = legend_text + "\n[ROTATED OCR]\n" + rotated_text
            legend_variant += "+" + rotated_variant
            if rotated_conf is not None:
                legend_conf = rotated_conf if legend_conf is None else float((legend_conf + rotated_conf) / 2)
    except Exception:
        pass

    scan_dt, raw_time, raw_date, time_note = _parse_scan_time_utc(header_text)
    joined_upper = header_text.upper().replace(",", ".")
    config_match = re.search(r"\b([A-Z0-9]{1,8}_[A-Z0-9_]+)\b", joined_upper)
    config_raw = config_match.group(1) if config_match else None
    task_match = re.search(r"TASK\s*[:;?]?\s*([A-Z0-9_./-]+)", joined_upper)
    prf_match = re.search(r"PRF\s*[:;=]?\s*(\d+(?:[.,]\d+)?)\s*(KHZ|HZ)", joined_upper)
    prf_hz = None
    if prf_match:
        value = float(prf_match.group(1).replace(",", "."))
        prf_hz = value * (1000.0 if prf_match.group(2).upper() == "KHZ" else 1.0)

    elevation = _first_float(r"ELEVATION\s*[:;=]?\s*(-?\d+(?:[.,]\d+)?)", header_text)
    min_height = _first_float(r"MIN\s*(?:HGT|HET|HEIGHT)\s*[:;=]?\s*(-?\d+(?:[.,]\d+)?)\s*KM", header_text)
    max_height = _first_float(r"MAX\s*(?:HGT|HET|HEIGHT)\s*[:;=]?\s*(-?\d+(?:[.,]\d+)?)\s*KM", header_text)
    max_range = _first_float(r"MAX\s*RANGE[^\d\n]{0,15}(\d+(?:[.,]\d+)?)\s*KM", header_text)
    if max_range is not None and not (20 <= max_range <= 500):
        max_range = None

    legend_lower = legend_text.casefold()
    if "dbz" in legend_lower.replace(" ", "") or "reflectivity" in legend_lower:
        units = "dBZ"
        unit_status = "reflectivity_scale_seen_in_legend_ocr"
    elif "vil" in legend_lower and "mm" in legend_lower:
        units = "mm (VIL)"
        unit_status = "non_dbz_product_detected"
    elif "km/h" in legend_lower:
        units = "km/h"
        unit_status = "non_dbz_product_detected"
    else:
        units = None
        unit_status = "legend_units_not_resolved_by_ocr"

    lines = [line.strip() for line in header_text.splitlines() if line.strip()]
    station_label = lines[0] if lines else None
    return {
        "product_code": product,
        "station_label_ocr": station_label,
        "configuration_code_ocr": config_raw,
        "task_ocr": task_match.group(1) if task_match else None,
        "prf_hz": prf_hz,
        "elevation_deg": elevation,
        "min_height_km": min_height,
        "max_height_km": max_height,
        "max_range_km": max_range,
        "scan_time_utc": scan_dt.strftime("%Y-%m-%dT%H:%M:%SZ") if scan_dt else None,
        "scan_time_raw_ocr": raw_time,
        "scan_date_raw_ocr": raw_date,
        "scan_time_status": "parsed_from_mgm_panel_utc" if scan_dt else "unparsed_ocr",
        "scan_time_parse_note": time_note,
        "utc_marker_evidence": bool(re.search(r"\bUTC\b|\bUIC\b|\d{1,2}:\d{2}:\d{2}\s*Z\b", header_text.upper())),
        "header_ocr_raw": header_text,
        "colorbar_ocr_raw": legend_text,
        "header_ocr_variant": header_variant,
        "colorbar_ocr_variant": legend_variant,
        "header_ocr_confidence_mean": header_conf,
        "colorbar_ocr_confidence_mean": legend_conf,
        "colorbar_units_ocr": units,
        "colorbar_units_status": unit_status,
        "image_width_px": int(width),
        "image_height_px": int(height),
        "header_panel_crop_xyxy": list(header_box),
        "colorbar_crop_xyxy": list(legend_box),
        "ocr_engine": f"Tesseract {pytesseract.get_tesseract_version()}",
    }


def classify_rgb_to_dbz(rgb_array: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return dBZ float field (NaN when unmatched), palette index (255 when unmatched),
    and a distance field used only to compute a scalar QA summary (not stored per pixel).
    Iterating over the 16 colours avoids a huge HxWx16x3 temporary tensor.
    """
    arr = np.asarray(rgb_array, dtype=np.int16)
    if arr.ndim != 3 or arr.shape[-1] != 3:
        raise ValueError(f"RGB array must have shape (y,x,3), got {arr.shape}")
    shape = arr.shape[:2]
    best_distance = np.full(shape, np.inf, dtype=np.float32)
    best_index = np.full(shape, 255, dtype=np.uint8)
    for idx, color in enumerate(LUT_RGB.astype(np.int16)):
        diff = (arr - color).astype(np.int32)
        distance = np.sqrt(np.sum(diff * diff, axis=-1, dtype=np.int32)).astype(np.float32)
        take = distance < best_distance
        best_distance[take] = distance[take]
        best_index[take] = np.uint8(idx)
    unmatched = best_distance >= RGB_MATCH_MAX_DIST
    best_index[unmatched] = 255
    dbz = np.full(shape, np.nan, dtype=np.float32)
    matched = ~unmatched
    dbz[matched] = LUT_DBZ[best_index[matched]]
    best_distance[unmatched] = np.nan
    return dbz, best_index, best_distance


def build_local_xy_axes(size: int, center: float, range_km: float) -> tuple[np.ndarray, np.ndarray]:
    """Return 1-D local AEQD x/y axes in metres, avoiding duplicate 2-D lat/lon grids."""
    km_per_pixel = range_km / center
    x_m = (np.arange(size, dtype=np.float64) - center) * km_per_pixel * 1000.0
    y_m = (center - np.arange(size, dtype=np.float64)) * km_per_pixel * 1000.0
    return x_m.astype(np.float32), y_m.astype(np.float32)


def _clean_attr(value: Any) -> str | float | int:
    if value is None:
        return ""
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value) if np.isfinite(value) else ""
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    return str(value)


def make_dataset(
    image: Image.Image,
    station_id: str,
    product: str,
    meta: dict[str, Any],
    source_sha: str,
    source_url: str,
    content_type: str,
    last_modified: str,
    collected_at: datetime,
) -> xr.Dataset:
    station = STATIONS[station_id]
    geom = PRODUCT_GEOMETRY[product]
    crop_box = geom["crop_box"]
    size = int(geom["size"])
    center = float(geom["center"])
    if image.width < crop_box[2] or image.height < crop_box[3]:
        raise ValueError(f"Image dimensions {image.size} are too small for {product} crop {crop_box}")

    crop = np.asarray(image.crop(crop_box), dtype=np.uint8)
    if crop.shape != (size, size, 3):
        raise ValueError(f"Unexpected {product} map crop shape {crop.shape}; expected {(size, size, 3)}")

    # First classify palette-like RGB pixels, then constrain them to the inspected
    # circular radar coverage. Palette colours beyond that circle cannot be echoes;
    # preserve a separate overlay/artifact mask for auditability before removing them
    # from dBZ/class values. This is a geometric mask, not native radar-gate geometry.
    dbz_raw, class_index_raw, match_distance = classify_rgb_to_dbz(crop)
    raw_palette_match = np.isfinite(dbz_raw)
    yy, xx = np.ogrid[:size, :size]
    range_mask = ((xx - center) ** 2 + (yy - center) ** 2) <= center ** 2
    overlay_mask = raw_palette_match & ~range_mask
    echo_mask = raw_palette_match & range_mask & ~overlay_mask

    dbz = dbz_raw.copy()
    class_index = class_index_raw.copy()
    dbz[~echo_mask] = np.nan
    class_index[~echo_mask] = np.uint8(255)

    classified = np.isfinite(dbz)
    classified_count = int(classified.sum())
    raw_classified_count = int(raw_palette_match.sum())
    overlay_count = int(overlay_mask.sum())
    range_count = int(range_mask.sum())
    total_count = int(dbz.size)
    matched_dist = match_distance[echo_mask]
    class_counts = {str(float(v)): int(np.count_nonzero(dbz == v)) for v in LUT_DBZ}

    valid_scan_time = meta.get("scan_time_utc")
    ds_coords: dict[str, Any] = {"y": np.arange(size, dtype=np.int32), "x": np.arange(size, dtype=np.int32)}
    dims_2d = ("y", "x")
    data_vars: dict[str, Any] = {
        "dBZ": (dims_2d, dbz),
        "palette_class_index": (dims_2d, class_index),
        "range_mask": (dims_2d, range_mask.astype(np.uint8)),
        "overlay_mask": (dims_2d, overlay_mask.astype(np.uint8)),
        "echo_mask": (dims_2d, echo_mask.astype(np.uint8)),
    }
    if valid_scan_time:
        scan_dt = datetime.strptime(valid_scan_time, "%Y-%m-%dT%H:%M:%SZ")
        ds_coords["time"] = np.array([np.datetime64(scan_dt, "s")])
        # Put single scan fields along time dimension to make scan files concat-friendly.
        data_vars = {
            "dBZ": (("time", "y", "x"), dbz[np.newaxis, ...]),
            "palette_class_index": (("time", "y", "x"), class_index[np.newaxis, ...]),
            "range_mask": (("time", "y", "x"), range_mask[np.newaxis, ...].astype(np.uint8)),
            "overlay_mask": (("time", "y", "x"), overlay_mask[np.newaxis, ...].astype(np.uint8)),
            "echo_mask": (("time", "y", "x"), echo_mask[np.newaxis, ...].astype(np.uint8)),
        }
    if meta.get("max_range_km") is not None:
        x_m, y_m = build_local_xy_axes(size, center, float(meta["max_range_km"]))
        ds_coords["x_m"] = (("x",), x_m)
        ds_coords["y_m"] = (("y",), y_m)
    ds = xr.Dataset(data_vars=data_vars, coords=ds_coords)

    # A full structured metadata snapshot is kept INSIDE the same NetCDF object.
    # Do not put the source image or its RGB array into persistent storage.
    metadata = {
        **meta,
        "station_id": station_id,
        "station_name_configured": station["name"],
        "radar_latitude": station["lat"],
        "radar_longitude": station["lon"],
        "product": product,
        "scan_time_utc": valid_scan_time,
        "collected_at_utc": collected_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_url": source_url,
        "source_content_type": content_type,
        "source_http_last_modified": last_modified,
        "source_image_sha256": source_sha,
        "image_is_archived": False,
        "classified_pixel_count": classified_count,
        "unclassified_pixel_count": total_count - classified_count,
        "classified_pixel_percent": round(100.0 * classified_count / total_count, 6),
        "raw_palette_matched_pixel_count": raw_classified_count,
        "palette_pixels_outside_range_count": overlay_count,
        "range_mask_pixel_count": range_count,
        "range_mask_pixel_percent": round(100.0 * range_count / total_count, 6),
        "echo_mask_pixel_count": classified_count,
        "overlay_mask_note": "Palette-matched pixels outside the inspected circular range are flagged as overlays/artifacts; this does not yet detect any in-range UI overlays.",
        "mean_rgb_match_distance": float(np.mean(matched_dist)) if matched_dist.size else None,
        "p95_rgb_match_distance": float(np.percentile(matched_dist, 95)) if matched_dist.size else None,
        "palette_class_counts": class_counts,
        "palette_rgb_lut": LUT_RGB.tolist(),
        "palette_dbz_lut": LUT_DBZ.tolist(),
        "rgb_match_max_distance": RGB_MATCH_MAX_DIST,
        "classifier_version": CLASSIFIER_VERSION,
        "metadata_parser_version": METADATA_PARSER_VERSION,
        "netcdf_schema_version": NETCDF_SCHEMA_VERSION,
        "product_geometry_crop_box_xyxy": list(crop_box),
        "product_geometry_center_pixel": center,
        "georeference_method": (
            "1-D local AEQD x_m/y_m axes; pixel scale derived from per-image MGM Max Range and inspected crop radius; use radar lat/lon to transform to geographic coordinates"
            if meta.get("max_range_km") is not None else "not available; use image x/y only"
        ),
        "crs_proj4": (
            f"+proj=aeqd +lat_0={station['lat']} +lon_0={station['lon']} +R=6371008.8 +units=m +no_defs"
            if meta.get("max_range_km") is not None else ""
        ),
        "km_per_pixel": (
            float(meta["max_range_km"]) / center
            if meta.get("max_range_km") is not None else -1.0
        ),
    }

    # NetCDF attributes must be scalar strings/numbers; store raw OCR/structured fields
    # as JSON plus a small set of commonly used attributes.
    ds.attrs.update({
        "Conventions": "CF-1.10",
        "title": f"MGM rendered-image-derived {product} reflectivity class",
        "station": station["name"],
        "station_id": station_id,
        "radar_latitude": float(station["lat"]),
        "radar_longitude": float(station["lon"]),
        "product": product,
        "time_standard": "UTC",
        "scan_time_utc": valid_scan_time or "",
        "scan_time_status": meta.get("scan_time_status", "unparsed_ocr"),
        "archive_status": meta.get("archive_status", "unknown"),
        "quarantine_reason": meta.get("quarantine_reason", ""),
        "collected_at_utc": collected_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_url": source_url,
        "source_content_type": content_type or "",
        "source_http_last_modified": last_modified or "",
        "source_image_sha256": source_sha,
        "source_image_stored": "false",
        "metadata_json": json.dumps(metadata, ensure_ascii=False, separators=(",", ":"), default=str),
        "header_ocr_raw": str(meta.get("header_ocr_raw", "")),
        "colorbar_ocr_raw": str(meta.get("colorbar_ocr_raw", "")),
        "colorbar_units_ocr": str(meta.get("colorbar_units_ocr") or ""),
        "colorbar_units_status": str(meta.get("colorbar_units_status", "unknown")),
        "configuration_code_ocr": str(meta.get("configuration_code_ocr") or ""),
        "task_ocr": str(meta.get("task_ocr") or ""),
        "prf_hz": float(meta["prf_hz"]) if meta.get("prf_hz") is not None else -1.0,
        "elevation_deg": float(meta["elevation_deg"]) if meta.get("elevation_deg") is not None else -999.0,
        "min_height_km": float(meta["min_height_km"]) if meta.get("min_height_km") is not None else -999.0,
        "max_height_km": float(meta["max_height_km"]) if meta.get("max_height_km") is not None else -999.0,
        "max_range_km": float(meta["max_range_km"]) if meta.get("max_range_km") is not None else -1.0,
        "classifier_version": CLASSIFIER_VERSION,
        "metadata_parser_version": METADATA_PARSER_VERSION,
        "netcdf_schema_version": NETCDF_SCHEMA_VERSION,
        "palette_rgb_lut_json": json.dumps(LUT_RGB.tolist(), separators=(",", ":")),
        "palette_dbz_lut_json": json.dumps(LUT_DBZ.tolist(), separators=(",", ":")),
        "rgb_match_max_distance": float(RGB_MATCH_MAX_DIST),
        "classified_pixel_count": classified_count,
        "unclassified_pixel_count": total_count - classified_count,
        "classified_pixel_percent": float(100.0 * classified_count / total_count),
        "raw_palette_matched_pixel_count": raw_classified_count,
        "palette_pixels_outside_range_count": overlay_count,
        "range_mask_pixel_count": range_count,
        "range_mask_pixel_percent": float(100.0 * range_count / total_count),
        "echo_mask_pixel_count": classified_count,
        "overlay_mask_note": "Palette-matched pixels outside the inspected circular range are flagged as overlays/artifacts; this does not yet detect any in-range UI overlays.",
        "mask_method": "circular geometry from inspected product crop center/radius; not native radar-gate geometry",
        "mean_rgb_match_distance": float(np.mean(matched_dist)) if matched_dist.size else -1.0,
        "p95_rgb_match_distance": float(np.percentile(matched_dist, 95)) if matched_dist.size else -1.0,
        "palette_class_counts_json": json.dumps(class_counts, separators=(",", ":")),
        "palette_note": "dBZ is reconstructed from the project-owned RGB lookup table applied to the rendered MGM image; not native radar-volume data",
        "rain_rate_note": "No Z-R conversion is stored. Derive precipitation_rate in analysis to test multiple equations.",
        "georeference_note": metadata["georeference_method"],
        "crs_proj4": metadata["crs_proj4"],
        "km_per_pixel": float(metadata["km_per_pixel"]),
    })

    ds["dBZ"].attrs.update({
        "long_name": "Reflectivity class inferred from MGM rendered RGB palette",
        "units": "dBZ",
        "source_note": "Image-derived, not a native/raw radar moment",
        "palette_mapping": "palette_dbz_lut_json global attribute; class indices preserved in palette_class_index",
        "missing_value_note": "Unmatched/background image pixels remain missing; not interpreted as zero rainfall",
    })
    ds["palette_class_index"].attrs.update({
        "long_name": "Index of nearest project-owned RGB palette colour; 255 is unclassified or excluded by masks",
        "flag_values": np.array(list(range(len(LUT_RGB))) + [255], dtype=np.uint8),
        "flag_meanings": "class_0 class_1 class_2 class_3 class_4 class_5 class_6 class_7 class_8 class_9 class_10 class_11 class_12 class_13 class_14 class_15 unclassified_or_masked",
    })
    for mask_name, long_name, comment in (
        ("range_mask", "Pixel inside inspected circular radar coverage", "1 means inside approximate valid radar display circle; this is display geometry, not native gate coverage."),
        ("overlay_mask", "Palette-matched pixel outside radar range circle", "1 means a palette-like pixel was found outside the circle and excluded from dBZ; not a detector for all possible overlays."),
        ("echo_mask", "In-range palette-classified echo candidate", "1 means palette matched and inside the approximate range circle; this does not distinguish meteorological echo from in-range clutter."),
    ):
        ds[mask_name].attrs.update({
            "long_name": long_name,
            "flag_values": np.array([0, 1], dtype=np.uint8),
            "flag_meanings": "false true",
            "comment": comment,
        })
    if "time" in ds.coords:
        ds["time"].attrs.update({"standard_name": "time", "long_name": "MGM scan timestamp parsed from right-side panel", "comment": "UTC; not the collection/download timestamp"})
    if "x_m" in ds.coords and "y_m" in ds.coords:
        ds["x_m"].attrs.update({"long_name": "local azimuthal-equidistant easting from radar center", "units": "m", "standard_name": "projection_x_coordinate"})
        ds["y_m"].attrs.update({"long_name": "local azimuthal-equidistant northing from radar center", "units": "m", "standard_name": "projection_y_coordinate"})
    return ds


def _serialize_netcdf_to_bytes(
    ds: xr.Dataset,
    encoding: dict[str, dict[str, Any]] | None = None,
) -> bytes:
    """Serialize as NETCDF4 via a temporary file, then return its bytes.

    xarray's path=None in-memory writer does not support the netcdf4 engine.
    Writing to a real temporary path also preserves NETCDF4 compression options.
    The temporary file is removed before this function returns.
    """
    fd, temp_name = tempfile.mkstemp(suffix=".nc")
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        ds.to_netcdf(
            path=str(temp_path),
            engine="netcdf4",
            format="NETCDF4",
            encoding=encoding,
        )
        return temp_path.read_bytes()
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def dataset_to_netcdf_bytes(ds: xr.Dataset) -> bytes:
    encoding: dict[str, dict[str, Any]] = {
        # Xarray writes the logical dBZ float values packed into uint8 using scale=0.5.
        # All palette levels are exact multiples of 0.5 dBZ; 255 is reserved as fill.
        "dBZ": {"dtype": "uint8", "scale_factor": 0.5, "add_offset": 0.0,
                "_FillValue": np.uint8(255), "zlib": True, "complevel": 4, "shuffle": True},
        "palette_class_index": {"dtype": "uint8", "_FillValue": np.uint8(255),
                                "zlib": True, "complevel": 4, "shuffle": True},
        "range_mask": {"dtype": "uint8", "_FillValue": None, "zlib": True, "complevel": 4, "shuffle": True},
        "overlay_mask": {"dtype": "uint8", "_FillValue": None, "zlib": True, "complevel": 4, "shuffle": True},
        "echo_mask": {"dtype": "uint8", "_FillValue": None, "zlib": True, "complevel": 4, "shuffle": True},
    }
    if "time" in ds.coords:
        encoding["time"] = {"units": "seconds since 1970-01-01 00:00:00 UTC", "calendar": "proleptic_gregorian"}
    return _serialize_netcdf_to_bytes(ds, encoding=encoding)


def create_s3_client():
    required = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET_NAME")
    missing = [key for key in required if not os.environ.get(key)]
    if missing:
        raise RuntimeError("Eksik R2 secrets/environment: " + ", ".join(missing))
    account_id = os.environ["R2_ACCOUNT_ID"].strip()
    return boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"].strip(),
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"].strip(),
        region_name="auto",
        config=Config(signature_version="s3v4", connect_timeout=15, read_timeout=60,
                      retries={"max_attempts": 5, "mode": "standard"}),
    )


def object_exists(s3, key: str) -> dict[str, Any] | None:
    try:
        return s3.head_object(Bucket=os.environ["R2_BUCKET_NAME"].strip(), Key=key)
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code in {"404", "NoSuchKey", "NotFound"} or status == 404:
            return None
        raise


def r2_key_for_scan(station_id: str, product: str, meta: dict[str, Any], source_sha: str, collected_at: datetime) -> tuple[str, str]:
    scan_text = meta.get("scan_time_utc")
    if scan_text:
        scan_dt = datetime.strptime(scan_text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        date_path = scan_dt.strftime("%Y/%m/%d")
        stamp = scan_dt.strftime("%Y%m%dT%H%M%SZ")
        return f"{date_path}/{station_id}/{product}_{stamp}.nc", "scan_time_utc"
    # Unknown scan time is kept in quarantine; collector time is used only to organize
    # the quarantine directory and is explicitly NOT written as scan_time_utc.
    date_path = collected_at.strftime("%Y/%m/%d")
    return f"{date_path}/{station_id}/_quarantine/{product}_UNPARSED_{source_sha[:16]}.nc", "quarantine_collection_date_only"


def upload_netcdf(s3, key: str, netcdf_bytes: bytes, meta: dict[str, Any], source_sha: str) -> str:
    bucket = os.environ["R2_BUCKET_NAME"].strip()
    content_sha = sha256_hex(netcdf_bytes)
    head = object_exists(s3, key)
    if head is not None:
        remote_meta = {k.lower(): v for k, v in head.get("Metadata", {}).items()}
        same_source = remote_meta.get("source-image-sha256") == source_sha
        same_processing = (
            remote_meta.get("classifier-version") == CLASSIFIER_VERSION
            and remote_meta.get("metadata-parser-version") == METADATA_PARSER_VERSION
            and remote_meta.get("schema-version") == NETCDF_SCHEMA_VERSION
        )
        # The NetCDF contains the collection time, so its body hash changes between polls.
        # Source image + processing versions are the idempotency contract.
        if same_source and same_processing:
            return "UNCHANGED"
        # Do not overwrite an existing scan/quarantine record. Preserve a conflicting
        # source image or reprocessed schema as an explicit, versioned object.
        stem, ext = key.rsplit(".", 1)
        revision_tag = re.sub(r"[^A-Za-z0-9_-]+", "_", CLASSIFIER_VERSION + "_" + METADATA_PARSER_VERSION + "_" + NETCDF_SCHEMA_VERSION)
        key = f"{stem}_conflict_{source_sha[:12]}_{revision_tag}.{ext}"
        conflict_head = object_exists(s3, key)
        if conflict_head is not None:
            remote_meta = {k.lower(): v for k, v in conflict_head.get("Metadata", {}).items()}
            same_conflict = (
                remote_meta.get("source-image-sha256") == source_sha
                and remote_meta.get("classifier-version") == CLASSIFIER_VERSION
                and remote_meta.get("metadata-parser-version") == METADATA_PARSER_VERSION
                and remote_meta.get("schema-version") == NETCDF_SCHEMA_VERSION
            )
            if same_conflict:
                return "UNCHANGED_CONFLICT_COPY"
            raise IOError(f"Versioned conflict key collision with different metadata: {key}")

    stamp = meta.get("scan_time_utc") or "unparsed"
    r2_meta = {
        "file-sha256": content_sha,
        "source-image-sha256": source_sha,
        "station-id": str(meta.get("station_id", "unknown")),
        "product": str(meta.get("product_code", "unknown")).lower(),
        "scan-time-utc": stamp,
        "scan-time-status": str(meta.get("scan_time_status", "unparsed_ocr"))[:100],
        "classifier-version": CLASSIFIER_VERSION,
        "metadata-parser-version": METADATA_PARSER_VERSION,
        "schema-version": NETCDF_SCHEMA_VERSION,
    }
    s3.put_object(
        Bucket=bucket, Key=key, Body=netcdf_bytes,
        ContentType="application/x-netcdf", Metadata=r2_meta,
    )
    # Verify R2 metadata after PUT. Local archive will independently verify body SHA-256.
    uploaded_head = s3.head_object(Bucket=bucket, Key=key)
    uploaded_meta = {k.lower(): v for k, v in uploaded_head.get("Metadata", {}).items()}
    if int(uploaded_head.get("ContentLength", -1)) != len(netcdf_bytes):
        raise IOError(f"R2 ContentLength check failed for {key}")
    if uploaded_meta.get("file-sha256") != content_sha:
        raise IOError(f"R2 file SHA-256 metadata check failed for {key}")
    return "UPLOADED"


def select_stations(selector: str) -> list[str]:
    selector = (selector or "all").strip()
    if selector.casefold() == "all":
        return list(STATIONS.keys())
    requested = [part.strip() for part in selector.split(",") if part.strip()]
    selected = []
    for item in requested:
        if item in STATIONS:
            selected.append(item)
            continue
        matches = [sid for sid, cfg in STATIONS.items() if cfg["name"].casefold() == item.casefold()]
        if not matches:
            raise ValueError(f"Unknown station selector {item!r}; use 'all' or one/more radar IDs: {','.join(STATIONS)}")
        selected.extend(matches)
    return list(dict.fromkeys(selected))


def select_products(selector: str) -> list[str]:
    selector = (selector or "all").strip().upper()
    if selector == "ALL":
        return list(PRODUCTS)
    requested = [part.strip().upper() for part in selector.split(",") if part.strip()]
    bad = sorted(set(requested) - set(PRODUCTS))
    if bad:
        raise ValueError(f"Unknown product(s): {bad}; use all, PPI or MAX")
    return list(dict.fromkeys(requested))



def make_metadata_only_netcdf_bytes(station_id: str, product: str, meta: dict[str, Any]) -> bytes:
    """Persist metadata for an explicitly non-dBZ response without mislabeling its pixels."""
    station = STATIONS[station_id]
    payload = {
        **meta,
        "station_id": station_id,
        "station_name_configured": station["name"],
        "radar_latitude": station["lat"],
        "radar_longitude": station["lon"],
        "product": product,
        "image_is_archived": False,
        "classifier_version": CLASSIFIER_VERSION,
        "metadata_parser_version": METADATA_PARSER_VERSION,
        "netcdf_schema_version": NETCDF_SCHEMA_VERSION,
    }
    ds = xr.Dataset()
    ds.attrs.update({
        "Conventions": "CF-1.10",
        "title": "MGM radar metadata quarantine; not a dBZ raster",
        "station": station["name"],
        "station_id": station_id,
        "product": product,
        "archive_status": "quarantined_non_dbz_product",
        "scan_time_utc": str(meta.get("scan_time_utc") or ""),
        "scan_time_status": str(meta.get("scan_time_status") or "unparsed_ocr"),
        "collected_at_utc": str(meta.get("collected_at_utc") or ""),
        "source_image_sha256": str(meta.get("source_image_sha256") or ""),
        "source_url": str(meta.get("source_url") or ""),
        "header_ocr_raw": str(meta.get("header_ocr_raw") or ""),
        "colorbar_ocr_raw": str(meta.get("colorbar_ocr_raw") or ""),
        "colorbar_units_ocr": str(meta.get("colorbar_units_ocr") or ""),
        "metadata_json": json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str),
        "source_image_stored": "false",
        "classifier_version": CLASSIFIER_VERSION,
        "metadata_parser_version": METADATA_PARSER_VERSION,
        "netcdf_schema_version": NETCDF_SCHEMA_VERSION,
    })
    return _serialize_netcdf_to_bytes(ds)


def store_metadata_quarantine(station_id: str, product: str, meta: dict[str, Any], source_sha: str,
                              collected_at: datetime, dry_run: bool, output_dir: Path, s3=None) -> str:
    nc_bytes = make_metadata_only_netcdf_bytes(station_id, product, meta)
    scan_text = meta.get("scan_time_utc")
    if scan_text:
        dt = datetime.strptime(scan_text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        date_path = dt.strftime("%Y/%m/%d")
        stamp = dt.strftime("%Y%m%dT%H%M%SZ")
        leaf = f"{product}_NON_DBZ_{stamp}_{source_sha[:12]}.nc"
    else:
        date_path = collected_at.strftime("%Y/%m/%d")
        leaf = f"{product}_NON_DBZ_UNPARSED_{source_sha[:12]}.nc"
    key = f"{date_path}/{station_id}/_quarantine/{leaf}"
    if dry_run:
        out_path = output_dir / station_id / product / leaf
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(nc_bytes)
        with xr.open_dataset(out_path, engine="netcdf4") as check:
            if "dBZ" in check.data_vars or check.attrs.get("archive_status") != "quarantined_non_dbz_product":
                raise IOError(f"Metadata-only quarantine validation failed: {out_path}")
        return f"[DRY RUN NON-DBZ METADATA] {key} ({len(nc_bytes):,} B) local={out_path}"
    if s3 is None:
        s3 = create_s3_client()
    status = upload_netcdf(s3, key, nc_bytes, meta, source_sha)
    return f"[{status} NON-DBZ METADATA] {key} ({len(nc_bytes):,} B)"


def process_one(station_id: str, product: str, dry_run: bool, output_dir: Path, s3=None) -> str:
    collected_at = datetime.now(timezone.utc)
    urls = generate_candidate_urls(station_id, product)
    image, source_bytes, source_url, content_type, last_modified, error = fetch_image(urls, product)
    if image is None or source_bytes is None or source_url is None:
        # Treat missing source imagery as a failed task, not a successful skip.
        # main() catches this exception, increments its error counter, and exits
        # with a non-zero status so GitHub Actions cannot silently pass partial runs.
        raise RuntimeError(
            f"No valid radar image for station={station_id} "
            f"product={product}: {error}"
        )

    source_sha = sha256_hex(source_bytes)
    meta = parse_panel_metadata(image, product)
    meta["station_id"] = station_id
    meta["station_name_configured"] = STATIONS[station_id]["name"]
    meta["product_code"] = product
    meta["source_image_sha256"] = source_sha
    meta["collected_at_utc"] = collected_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    meta["source_url"] = source_url
    meta["source_content_type"] = content_type or ""
    meta["source_http_last_modified"] = last_modified or ""
    try:
        with Image.open(io.BytesIO(source_bytes)) as src_image:
            meta["image_format_source"] = src_image.format or "unknown"
    except Exception:
        meta["image_format_source"] = "unknown"

    # If OCR explicitly identifies a non-dBZ legend, do not mislabel the palette as dBZ.
    # If the legend itself could not be read, preserve a quarantined NetCDF with the raw OCR.
    explicitly_non_dbz = meta.get("colorbar_units_status") == "non_dbz_product_detected"
    valid_scan_time = bool(meta.get("scan_time_utc"))
    unit_confirmed = meta.get("colorbar_units_ocr") == "dBZ"
    quarantine_reason = None
    if not valid_scan_time:
        quarantine_reason = "scan timestamp not parsed with explicit UTC evidence"
    if explicitly_non_dbz:
        # Do not label a known different physical product as dBZ. Preserve only its
        # metadata for diagnosis; source image bytes are still never archived.
        meta["archive_status"] = "quarantined_non_dbz_product"
        meta["quarantine_reason"] = "legend OCR indicates a non-dBZ product"
        return store_metadata_quarantine(station_id, product, meta, source_sha, collected_at, dry_run, output_dir, s3)
    elif not unit_confirmed:
        quarantine_reason = quarantine_reason or "dBZ scale not confirmed from legend OCR"

    # Unknown/invalid timestamp or scale goes to quarantine and is not mistaken for a
    # time-valid dBZ scan. We still preserve the resulting raster and raw metadata.
    if quarantine_reason:
        meta["archive_status"] = "quarantined"
        meta["quarantine_reason"] = quarantine_reason
    else:
        meta["archive_status"] = "scan_time_and_dbz_scale_verified"
        meta["quarantine_reason"] = ""

    ds = make_dataset(image, station_id, product, meta, source_sha, source_url, content_type or "", last_modified or "", collected_at)
    nc_bytes = dataset_to_netcdf_bytes(ds)
    nc_sha = sha256_hex(nc_bytes)
    meta["netcdf_sha256"] = nc_sha
    key, time_basis = r2_key_for_scan(station_id, product, meta, source_sha, collected_at)
    if quarantine_reason and not valid_scan_time:
        # Collection date only organizes quarantine data; not used as scan timestamp.
        pass
    elif quarantine_reason:
        # Timestamp exists but product scale is unclear/non-dBZ: store under quarantine.
        base = key.rsplit("/", 1)[0]
        key = f"{base}/_quarantine/{product}_SCALE_UNVERIFIED_{meta['scan_time_utc'].replace(':','').replace('-','')}_{source_sha[:12]}.nc"

    if dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)
        fname = key.rsplit("/", 1)[-1]
        out_path = output_dir / station_id / product / fname
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(nc_bytes)
        # Re-open for structural validation before reporting a successful dry run.
        with xr.open_dataset(out_path, engine="netcdf4") as check:
            if "dBZ" not in check.data_vars or check.attrs.get("source_image_sha256") != source_sha:
                raise IOError(f"Dry-run NetCDF validation failed: {out_path}")
            if valid_scan_time and "time" not in check.coords:
                raise IOError(f"Valid scan missing time coordinate: {out_path}")
        return (
            f"[DRY RUN {meta['archive_status']}] {station_id} {STATIONS[station_id]['name']} {product} "
            f"scan={meta.get('scan_time_utc') or 'UNPARSED'} units={meta.get('colorbar_units_ocr') or 'UNKNOWN'} "
            f"classified={ds.attrs.get('classified_pixel_percent', 0):.2f}% "
            f"size={len(nc_bytes):,}B file={out_path} sha256={nc_sha}"
        )

    if s3 is None:
        s3 = create_s3_client()
    status = upload_netcdf(s3, key, nc_bytes, meta, source_sha)
    return (
        f"[{status}] {key} | scan_utc={meta.get('scan_time_utc') or 'UNPARSED'} "
        f"status={meta['archive_status']} units={meta.get('colorbar_units_ocr') or 'UNKNOWN'} "
        f"classified={ds.attrs.get('classified_pixel_percent', 0):.2f}% bytes={len(nc_bytes):,} "
        f"file_sha256={nc_sha}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MGM PPI/MAX -> dBZ NetCDF collector (no image archival)")
    parser.add_argument("--station", default=os.getenv("RADAR_STATION", "all"), help="all, station ID (34), station name, or comma list")
    parser.add_argument("--product", default=os.getenv("RADAR_PRODUCT", "all"), help="all, PPI, MAX, or comma list")
    parser.add_argument("--dry-run", action="store_true", default=os.getenv("RADAR_DRY_RUN", "false").casefold() in {"1", "true", "yes", "on"}, help="Download/process images and write local NC files, but do not access R2")
    parser.add_argument("--output-dir", default=os.getenv("RADAR_DRY_RUN_DIR", "output/dry-run"), help="dry-run output directory")
    args = parser.parse_args(argv)
    try:
        pytesseract.get_tesseract_version()
    except Exception as exc:
        print("ERROR: Tesseract executable is not available. Install Tesseract OCR and retry.", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        return 2

    station_ids = select_stations(args.station)
    products = select_products(args.product)
    dry_run = bool(args.dry_run)
    print(f"Start UTC: {datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}")
    print(f"Stations: {', '.join(station_ids)} | products: {', '.join(products)} | dry_run={dry_run}")
    print("Stored image policy: source WebP/JPEG/PNG is held in memory only; archive target is per-scan compressed NetCDF.")
    if not dry_run:
        # Fail before doing any network collection if credentials are missing.
        s3 = create_s3_client()
    else:
        s3 = None

    results: list[str] = []
    failures = 0
    tasks = [(sid, product) for sid in station_ids for product in products]
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(1, len(tasks)))) as executor:
        future_map = {
            executor.submit(process_one, sid, product, dry_run, Path(args.output_dir), s3): (sid, product)
            for sid, product in tasks
        }
        for future in as_completed(future_map):
            sid, product = future_map[future]
            try:
                result = future.result()
            except Exception as exc:
                failures += 1
                result = f"[ERROR] {sid} {product}: {type(exc).__name__}: {exc}"
            print(result, flush=True)
            results.append(result)

    print(f"Finished UTC: {datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}")
    print(f"Tasks={len(tasks)} successful_or_skipped={len(tasks)-failures} errors={failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
