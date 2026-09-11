# SN行定位: 从白底标签裁图里按水平投影切出每个文字行, 整行放大供rec直识(跳过det分裂)
import cv2
import numpy as np


def _to_gray(img):
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img


def _binarize_text(gray):
    # 暗字白底 -> 反相二值, 文字像素=255
    g = cv2.GaussianBlur(gray, (3, 3), 0)
    _, th = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    return th


def _row_bands(mask, ink_frac=0.05, gap=4, min_h=8):
    # 行墨水占比>阈值的行聚成带(容忍gap行内的小空白), 返回[(y0,y1)]
    h, w = mask.shape
    dens = mask.sum(axis=1) / 255.0 / max(w, 1)
    ink = dens > ink_frac
    bands = []
    i = 0
    while i < h:
        if not ink[i]:
            i += 1
            continue
        last = i
        blank = 0
        j = i
        while j < h and blank <= gap:
            if ink[j]:
                last = j
                blank = 0
            else:
                blank += 1
            j += 1
        if last - i + 1 >= min_h:
            bands.append((i, last))
        i = last + 1
    return bands


def _col_ink_frac(band_mask):
    h, w = band_mask.shape
    cols = band_mask.sum(axis=0) / 255.0 / max(h, 1)
    return float((cols > 0.15).mean())


def is_barcode_band(band_mask):
    # 条码带: 几乎每列都有墨(无字间空白); 文字行有空隙
    return _col_ink_frac(band_mask) > 0.9


def extract_lines(label_bgr, target_h=48, max_w=1600, pad=4, drop_barcode=True):
    gray = _to_gray(label_bgr)
    mask = _binarize_text(gray)
    H = gray.shape[0]
    lines = []
    for (y0, y1) in _row_bands(mask):
        a = max(0, y0 - pad)
        b = min(H, y1 + pad + 1)
        if drop_barcode and is_barcode_band(mask[a:b]):
            continue
        crop = label_bgr[a:b] if label_bgr.ndim == 3 else gray[a:b]
        ch, cw = crop.shape[:2]
        scale = target_h / max(ch, 1)
        nw = min(max(int(round(cw * scale)), 1), max_w)
        out = cv2.resize(crop, (nw, target_h), interpolation=cv2.INTER_CUBIC)
        lines.append({"y0": a, "y1": b, "img": out})
    return lines
