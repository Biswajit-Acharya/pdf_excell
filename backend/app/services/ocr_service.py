"""
Production OCR engine using Tesseract plus OpenCV preprocessing.

The parser depends on word-level boxes, so this module returns reconstructed
lines, blocks, confidence, and per-word coordinates in PDF page space.
"""

import io
import logging
import re

import fitz
import numpy as np
import pytesseract
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

try:
    import cv2
except Exception:  # pragma: no cover - Pillow fallback still works
    cv2 = None

pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

logger = logging.getLogger(__name__)


def _to_grayscale(img: Image.Image) -> Image.Image:
    return img.convert("L")


def _enhance_standard(img: Image.Image) -> Image.Image:
    img = _to_grayscale(img)
    img = ImageOps.autocontrast(img, cutoff=1)
    img = ImageEnhance.Contrast(img).enhance(1.8)
    img = ImageEnhance.Sharpness(img).enhance(1.5)
    return img


def _enhance_aggressive(img: Image.Image) -> Image.Image:
    img = _to_grayscale(img)
    img = ImageOps.autocontrast(img, cutoff=2)
    img = ImageEnhance.Contrast(img).enhance(2.6)
    img = img.filter(ImageFilter.SHARPEN)
    return img.point(lambda p: 255 if p > 140 else 0)


def _enhance_denoise(img: Image.Image) -> Image.Image:
    img = _to_grayscale(img)
    img = img.filter(ImageFilter.MedianFilter(size=3))
    img = ImageEnhance.Contrast(img).enhance(2.0)
    return img.point(lambda p: 255 if p > 150 else 0)


def _pil_from_cv(gray: np.ndarray) -> Image.Image:
    return Image.fromarray(gray.astype("uint8"), mode="L")


def _opencv_preprocess_variants(img: Image.Image) -> list[tuple[str, Image.Image, float]]:
    variants: list[tuple[str, Image.Image, float]] = [
        ("pillow_standard", _enhance_standard(img), 1.0),
        ("pillow_aggressive", _enhance_aggressive(img), 1.0),
        ("pillow_denoise", _enhance_denoise(img), 1.0),
    ]
    if cv2 is None:
        return variants

    gray = np.array(img.convert("L"))
    gray = cv2.fastNlMeansDenoising(gray, None, 12, 7, 21)
    gray = cv2.createCLAHE(clipLimit=2.4, tileGridSize=(8, 8)).apply(gray)

    scale = 2.0
    up = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    blur = cv2.GaussianBlur(up, (3, 3), 0)
    adaptive = cv2.adaptiveThreshold(
        blur, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY, 31, 11,
    )
    otsu = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    inv = cv2.bitwise_not(adaptive)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
    repaired = cv2.bitwise_not(cv2.morphologyEx(inv, cv2.MORPH_CLOSE, kernel, iterations=1))

    variants.extend([
        ("opencv_clahe_up", _pil_from_cv(up), scale),
        ("opencv_adaptive", _pil_from_cv(adaptive), scale),
        ("opencv_otsu", _pil_from_cv(otsu), scale),
        ("opencv_repaired", _pil_from_cv(repaired), scale),
    ])
    return variants


def _orientation_variants(img: Image.Image) -> list[tuple[str, Image.Image]]:
    return [
        ("rot0", img),
        ("rot90", img.rotate(90, expand=True)),
        ("rot270", img.rotate(270, expand=True)),
    ]


def _fast_preprocess_variants(img: Image.Image) -> list[tuple[str, Image.Image, float]]:
    return [
        ("pillow_standard", _enhance_standard(img), 1.0),
        ("pillow_denoise", _enhance_denoise(img), 1.0),
    ]


def _run_tesseract(
    img: Image.Image,
    lang: str = "eng",
    config: str = "--psm 6 --oem 1",
) -> dict | None:
    for attempt_lang in ([f"{lang}+hin", lang] if lang == "eng" else [lang]):
        try:
            return pytesseract.image_to_data(
                img,
                lang=attempt_lang,
                output_type=pytesseract.Output.DICT,
                config=config,
            )
        except Exception as exc:
            logger.warning(f"Tesseract attempt ({attempt_lang}) failed: {exc}")
    return None


def _safe_conf(value) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return -1


def _assemble_lines_and_blocks(data: dict, zoom: float, image_scale: float = 1.0) -> tuple[list, list]:
    lines_dict: dict = {}
    blocks_dict: dict = {}
    z = (zoom if zoom > 0 else 1.0) * (image_scale if image_scale > 0 else 1.0)

    for i, raw_word in enumerate(data.get("text", [])):
        word = (raw_word or "").strip()
        conf = _safe_conf(data.get("conf", [])[i])
        if conf < 0 or not word:
            continue

        blk = data["block_num"][i]
        ln = data["line_num"][i]
        lft = data["left"][i]
        top = data["top"][i]
        wid = data["width"][i]
        hgt = data["height"][i]

        key = (blk, ln)
        if key not in lines_dict:
            lines_dict[key] = {
                "words": [], "confs": [], "children": [],
                "x": lft, "y": top, "x2": lft + wid, "y2": top + hgt,
                "block_num": blk, "line_num": ln,
            }
        entry = lines_dict[key]
        entry["words"].append(word)
        entry["confs"].append(conf)
        entry["children"].append({
            "text": word,
            "confidence": conf,
            "x0": lft,
            "y0": top,
            "x1": lft + wid,
            "y1": top + hgt,
        })
        entry["x"] = min(entry["x"], lft)
        entry["y"] = min(entry["y"], top)
        entry["x2"] = max(entry["x2"], lft + wid)
        entry["y2"] = max(entry["y2"], top + hgt)

        if blk not in blocks_dict:
            blocks_dict[blk] = {
                "words": [], "confs": [],
                "x": lft, "y": top, "x2": lft + wid, "y2": top + hgt,
                "block_num": blk,
            }
        block = blocks_dict[blk]
        block["words"].append(word)
        block["confs"].append(conf)
        block["x"] = min(block["x"], lft)
        block["y"] = min(block["y"], top)
        block["x2"] = max(block["x2"], lft + wid)
        block["y2"] = max(block["y2"], top + hgt)

    def finalise_line(v: dict) -> dict:
        return {
            "text": " ".join(v["words"]),
            "confidence": sum(v["confs"]) / len(v["confs"]),
            "x": int(v["x"] / z),
            "y": int(v["y"] / z),
            "width": int((v["x2"] - v["x"]) / z),
            "height": int((v["y2"] - v["y"]) / z),
            "block_num": v["block_num"],
            "line_num": v["line_num"],
            "children": [
                {
                    "text": c["text"],
                    "confidence": c["confidence"],
                    "x0": c["x0"] / z,
                    "y0": c["y0"] / z,
                    "x1": c["x1"] / z,
                    "y1": c["y1"] / z,
                }
                for c in v.get("children", [])
            ],
        }

    def finalise_block(v: dict) -> dict:
        return {
            "text": " ".join(v["words"]),
            "confidence": sum(v["confs"]) / len(v["confs"]),
            "x": int(v["x"] / z),
            "y": int(v["y"] / z),
            "width": int((v["x2"] - v["x"]) / z),
            "height": int((v["y2"] - v["y"]) / z),
            "block_num": v["block_num"],
        }

    lines = [finalise_line(v) for v in lines_dict.values()]
    blocks = [finalise_block(v) for v in blocks_dict.values()]
    lines.sort(key=lambda line: (line["y"], line["x"]))
    blocks.sort(key=lambda block: (block["y"], block["x"]))
    return lines, blocks


def _ocr_candidate_score(lines: list[dict]) -> float:
    if not lines:
        return 0.0
    text = "\n".join(line.get("text", "") for line in lines)
    upper = text.upper()
    avg_conf = sum(line.get("confidence", 0) for line in lines) / len(lines)
    alnum = sum(ch.isalnum() for ch in text)
    line_count = len(lines)
    score = avg_conf + min(line_count, 42) * 0.9 + min(alnum / 25, 28)
    if line_count > 90:
        score -= (line_count - 90) * 0.9
    for token, weight in [
        ("SCHOOL", 10), ("CERTIFICATE", 8), ("EXAMINATION", 8),
        ("SUBJECT", 10), ("FLO", 7), ("SLE", 7), ("TLS", 7),
        ("MTH", 7), ("GSC", 7), ("SSC", 7), ("MARKS", 7),
        ("ADDRESS", 16), ("DOB", 10), ("MALE", 8), ("FEMALE", 8),
        ("GOVERNMENT", 8), ("INDIA", 6), ("AADHAAR", 8), ("UIDAI", 8),
        ("ISSUE DATE", 10), ("PRINT DATE", 10),
    ]:
        if token in upper:
            score += weight
    return score


def _dedupe_lines(primary: list, secondary: list, y_tolerance: int = 8, iou_threshold: float = 0.5) -> list:
    merged = list(primary)
    for sline in secondary:
        sx1, sy1 = sline["x"], sline["y"]
        sx2 = sx1 + sline["width"]
        duplicate = False
        for pline in merged:
            px1, py1 = pline["x"], pline["y"]
            px2 = px1 + pline["width"]
            if abs(sy1 - py1) <= y_tolerance:
                overlap_x = max(0, min(sx2, px2) - max(sx1, px1))
                union_x = max(sx2, px2) - min(sx1, px1)
                if union_x > 0 and overlap_x / union_x >= iou_threshold:
                    duplicate = True
                    break
        if not duplicate:
            merged.append(sline)
    merged.sort(key=lambda line: (line["y"], line["x"]))
    return merged


def _dedupe_many_line_sets(line_sets: list[list]) -> list:
    merged: list = []
    for lines in line_sets:
        merged = _dedupe_lines(merged, lines, y_tolerance=5, iou_threshold=0.45) if merged else list(lines)
    merged.sort(key=lambda line: (line["y"], line["x"]))
    return merged


def perform_ocr_on_page(page: fitz.Page, page_num: int, zoom: float = 4.0) -> dict:
    try:
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        img = Image.open(io.BytesIO(pix.tobytes("png")))

        width, height = img.size
        logger.info(f"Page {page_num}: rendered {width}x{height}px (zoom={zoom}x)")

        configs = [
            ("psm6_block", "--psm 6 --oem 1 -c preserve_interword_spaces=1"),
            ("psm4_columns", "--psm 4 --oem 1 -c preserve_interword_spaces=1"),
        ]

        candidates = []
        for orientation_name, oriented_img in _orientation_variants(img):
            preprocessors = (
                _opencv_preprocess_variants(oriented_img)
                if orientation_name == "rot0"
                else _fast_preprocess_variants(oriented_img)
            )
            orientation_configs = configs if orientation_name == "rot0" else configs[:1]
            for variant_name, variant_img, image_scale in preprocessors:
                for config_name, config in orientation_configs:
                    data = _run_tesseract(variant_img, config=config)
                    if data is None:
                        continue
                    lines, blocks = _assemble_lines_and_blocks(data, zoom, image_scale)
                    if not lines:
                        continue
                    avg_conf = sum(line["confidence"] for line in lines) / len(lines)
                    score = _ocr_candidate_score(lines)
                    if variant_name == "pillow_standard":
                        score += 18
                    elif variant_name == "pillow_aggressive":
                        score -= 18
                    elif variant_name in {"opencv_adaptive", "opencv_repaired"}:
                        score -= 22
                    if orientation_name != "rot0":
                        score -= 4
                    candidates.append({
                        "name": f"{orientation_name}/{variant_name}/{config_name}",
                        "lines": lines,
                        "blocks": blocks,
                        "avg_confidence": avg_conf,
                        "score": score,
                    })
                    logger.info(
                        f"Page {page_num}: OCR {orientation_name}/{variant_name}/{config_name} "
                        f"lines={len(lines)} avg_conf={avg_conf:.1f} score={score:.1f}"
                    )

        if not candidates:
            return {
                "page": page_num, "text": "", "lines": [], "blocks": [],
                "avg_confidence": 0.0, "error": "OCR_ENGINE_ERROR",
            }

        candidates.sort(key=lambda item: item["score"], reverse=True)
        best = candidates[0]
        final_lines = best["lines"]
        final_blocks = best["blocks"]

        label_re = re.compile(
            r"\b(Address|Issue\s*Date|Print\s*Date|DOB|Male|Female)\b|\b\d{4}\s+\d{4}\s+\d{4}\b",
            re.IGNORECASE,
        )
        supplemental = []
        for item in candidates[1:]:
            if item["name"].split("/", 1)[0] == best["name"].split("/", 1)[0]:
                continue
            for line in item["lines"]:
                text = line.get("text", "")
                if line.get("confidence", 0) >= 45 and label_re.search(text):
                    supplemental.append(line)
        if supplemental:
            final_lines = _dedupe_lines(final_lines, supplemental, y_tolerance=5, iou_threshold=0.45)

        if not final_lines:
            return {
                "page": page_num, "text": "", "lines": [], "blocks": [],
                "avg_confidence": 0.0, "error": "OCR_RETURNED_EMPTY_TEXT",
            }

        full_text = "\n".join(line["text"] for line in final_lines)
        avg_conf = sum(line["confidence"] for line in final_lines) / len(final_lines)
        logger.info(
            f"Page {page_num}: selected {best['name']} "
            f"final_lines={len(final_lines)} avg_conf={avg_conf:.1f}"
        )

        return {
            "page": page_num,
            "text": full_text,
            "lines": final_lines,
            "blocks": final_blocks,
            "avg_confidence": round(avg_conf, 2),
            "error": None,
        }

    except Exception as exc:
        logger.error(f"OCR failed on page {page_num}: {exc}", exc_info=True)
        return {
            "page": page_num, "text": "", "lines": [], "blocks": [],
            "avg_confidence": 0.0, "error": f"PAGE_RENDER_FAILED: {exc}",
        }
