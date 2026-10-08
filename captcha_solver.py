"""Unified Captcha Solver for ByteDance / Dola Verifycenter.

Supports:
1. Slider Puzzle Captcha: OpenCV Canny edge template matching with Gaussian blur & humanized drag.
2. 3D Object Match Captcha: YOLOv5 deep learning detection from api-solve-captcha.
"""
import asyncio
import os
import pathlib
import random
import re
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

import aiohttp
import cv2
import numpy as np
import torch

if os.name == "nt":
    pathlib.PosixPath = pathlib.WindowsPath

import config
from gap import find_gap_x

# Import 3D captcha solver from api-solve-captcha
API_SOLVE_DIR = Path(__file__).parent / "api-solve-captcha" / "api-solve-captcha"
if str(API_SOLVE_DIR) not in sys.path:
    sys.path.insert(0, str(API_SOLVE_DIR))

try:
    from tiktok_captcha.tiktok_3d_captcha import solve_3d_captcha_tiktok
except ImportError:
    solve_3d_captcha_tiktok = None


def find_captcha_frame(page):
    """Finds the active verifycenter / captcha iframe on the page."""
    for f in page.frames:
        if "bdcaptcha.html" in f.url or "verifycenter" in f.url:
            return f
    return None


async def _fetch_bytes(url: str) -> bytes:
    """Downloads image bytes directly."""
    async with aiohttp.ClientSession() as s:
        async with s.get(url, proxy=config.PROXY or None) as r:
            return await r.read()


def _gen_human_track(distance: float) -> list[tuple[float, float, int]]:
    """Generates natural human-like mouse movement with acceleration, deceleration, and micro-jitter."""
    steps = random.randint(45, 65)
    overshoot = random.uniform(3, 8)
    pts = []
    for i in range(1, steps + 1):
        t = i / steps
        # Ease-in-out polynomial curve
        s = 10 * t**3 - 15 * t**4 + 6 * t**5
        x = (distance + overshoot) * s
        y = random.uniform(-1.5, 1.5) if 0.1 < t < 0.95 else 0
        pts.append((x, y, random.randint(8, 20)))
    # Micro correction for overshoot
    correction_steps = random.randint(3, 5)
    for i in range(1, correction_steps + 1):
        pts.append((distance + overshoot * (1 - i / correction_steps), random.uniform(-0.8, 0.8), random.randint(15, 28)))
    return pts


async def detect_captcha_type(frame) -> str:
    """Determines whether captcha is a slider puzzle, 3D object match, or other."""
    try:
        # Check for slider handle button
        btn = frame.locator(".captcha-slider-btn, .secsdk-captcha-drag-icon, [class*='slider-btn'], [class*='drag-icon']").first
        if await btn.count() and await btn.is_visible():
            return "slider"

        # Check prompt text or classnames inside frame
        text_content = await frame.evaluate(r"""() => {
            return (document.body ? document.body.innerText : "") + " " +
                   Array.from(document.querySelectorAll('*')).map(e => e.className || "").join(" ");
        }""")

        is_3d = bool(re.search(
            r"same shape|same object|identical|cùng hình dạng|相同形状|同じ形|3d|select 2|chọn 2|两个相同",
            text_content,
            re.IGNORECASE
        ))
        if is_3d:
            return "3d"

        # Check image count: 2 images usually slider (bg + piece), 1 image usually 3D
        img_count = await frame.evaluate("() => document.images.length")
        if img_count >= 2:
            return "slider"
        elif img_count == 1:
            return "3d"
    except Exception:
        pass
    return "slider"


async def solve_slider_captcha(page, frame, attempt: int) -> bool:
    """Solves ByteDance slider puzzle captcha."""
    # 1. Wait for slider to be reset to start position
    try:
        await frame.wait_for_selector(".captcha-slider-btn, [class*='slider-btn']", timeout=12000)
    except Exception:
        pass

    # Wait for images to load
    await frame.evaluate("""async () => {
        const t0 = Date.now();
        while (Date.now() - t0 < 10000) {
            const imgs = [...document.images];
            if (imgs.length >= 2 && imgs.every(im => im.complete && im.naturalWidth > 0)) return;
            await new Promise(r => setTimeout(r, 200));
        }
        throw new Error("captcha images load timeout");
    }""")
    await page.wait_for_timeout(600)

    imgs = await frame.evaluate("""() => [...document.images].map(im => ({
        src: im.src, w: im.naturalWidth, h: im.naturalHeight,
        bw: im.getBoundingClientRect().width,
        left: im.getBoundingClientRect().left,
    }))""")

    bg = next((i for i in imgs if ".jpeg" in i["src"] or "-2." in i["src"] or i["w"] > 200), None)
    piece = next((i for i in imgs if i is not bg and (".png" in i["src"] or "-1." in i["src"] or i["w"] < 200)), None)
    if not bg or not piece:
        print("  ✗ Slider background or puzzle piece image not found", flush=True)
        return False

    bg_bytes = await _fetch_bytes(bg["src"])
    piece_bytes = await _fetch_bytes(piece["src"])
    Path("dbg_bg.jpg").write_bytes(bg_bytes)
    Path("dbg_piece.png").write_bytes(piece_bytes)

    gap_x, conf = find_gap_x(bg_bytes, piece_bytes)
    scale = bg["bw"] / bg["w"] if bg["w"] else 340 / 552

    # Calculate actual slider distance needed
    # Target notch screen x relative to background left edge
    target_x = gap_x * scale

    # Starting position of piece relative to background left edge
    initial_offset = max(0, piece["left"] - bg["left"])
    if initial_offset > 50:
        # Piece might be lingering from previous drag, clamp to 0
        initial_offset = 0

    distance = target_x - initial_offset
    print(f"  [slider#{attempt}] gap_x={gap_x} (conf={conf:.3f}, scale={scale:.2f}, distance={distance:.1f}px)", flush=True)

    btn = frame.locator(".captcha-slider-btn, .secsdk-captcha-drag-icon, [class*='slider-btn']").first
    bb = await btn.bounding_box()
    if not bb:
        print("  ✗ Drag handle .captcha-slider-btn not found", flush=True)
        return False

    sx, sy = bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2
    await page.mouse.move(sx, sy)
    await page.wait_for_timeout(random.randint(150, 300))
    await page.mouse.down()
    await page.wait_for_timeout(random.randint(80, 160))

    for dx, dy, dt in _gen_human_track(distance):
        await page.mouse.move(sx + dx, sy + dy)
        await asyncio.sleep(dt / 1000)

    await page.wait_for_timeout(random.randint(120, 240))
    await page.mouse.up()

    # Wait to verify if captcha frame disappears
    for _ in range(10):
        await page.wait_for_timeout(600)
        if not find_captcha_frame(page):
            return True
    return False


async def solve_3d_captcha(page, frame, attempt: int) -> bool:
    """Solves ByteDance 3D Object Match Captcha using YOLOv5."""
    if solve_3d_captcha_tiktok is None:
        print("  ✗ YOLOv5 3D captcha model not available", flush=True)
        return False

    # Wait for the main captcha image
    img_el = None
    for sel in [
        "#captcha-verify-image",
        "img[class*='captcha']",
        "img.captcha_verify_img",
        "img[class*='verify']",
        "img",
    ]:
        cand = frame.locator(sel).first
        if await cand.count() and await cand.is_visible():
            img_el = cand
            break

    if not img_el:
        print("  ✗ 3D captcha image element not found", flush=True)
        return False

    # Wait until image is loaded
    await frame.wait_for_timeout(1000)
    img_box = await img_el.bounding_box()
    if not img_box or img_box["width"] <= 0 or img_box["height"] <= 0:
        print("  ✗ 3D captcha image bounding box invalid", flush=True)
        return False

    # Save screenshot of the 3D captcha image
    img_path = Path("dbg_3d_captcha.png")
    try:
        await img_el.screenshot(path=str(img_path))
    except Exception as ex:
        print(f"  ✗ Failed to screenshot 3D captcha: {ex}", flush=True)
        return False

    # Run YOLOv5 to find identical 3D objects
    coords = solve_3d_captcha_tiktok(str(img_path))
    if not coords:
        print(f"  [3d#{attempt}] No matching object pair detected by YOLOv5", flush=True)
        return False

    center1, center2 = coords
    print(f"  [3d#{attempt}] Detected 3D pair: pt1=({center1[0]:.3f}, {center1[1]:.3f}), pt2=({center2[0]:.3f}, {center2[1]:.3f})", flush=True)

    # Click first object
    cx1 = img_box["x"] + center1[0] * img_box["width"]
    cy1 = img_box["y"] + center1[1] * img_box["height"]
    await page.mouse.move(cx1, cy1)
    await page.wait_for_timeout(random.randint(150, 300))
    await page.mouse.click(cx1, cy1)
    await page.wait_for_timeout(random.randint(300, 600))

    # Click second object
    cx2 = img_box["x"] + center2[0] * img_box["width"]
    cy2 = img_box["y"] + center2[1] * img_box["height"]
    await page.mouse.move(cx2, cy2)
    await page.wait_for_timeout(random.randint(150, 300))
    await page.mouse.click(cx2, cy2)
    await page.wait_for_timeout(random.randint(400, 700))

    # Click Confirm / Verify button
    submit_btn = None
    for b_sel in [
        ".verify-captcha-submit-button",
        "button[class*='submit']",
        "div[class*='submit']",
        "button:has-text('Confirm')",
        "button:has-text('Xác nhận')",
        "button:has-text('确认')",
        "button:has-text('確認')",
        "[role='button']:has-text('Confirm')",
        "[role='button']:has-text('Xác nhận')",
    ]:
        btn_cand = frame.locator(b_sel).first
        if await btn_cand.count() and await btn_cand.is_visible():
            submit_btn = btn_cand
            break

    if submit_btn:
        await submit_btn.click()
    else:
        print("  (Submit button not explicitly found, checking auto-submit)", flush=True)

    # Wait to verify if captcha frame disappears
    for _ in range(12):
        await page.wait_for_timeout(600)
        if not find_captcha_frame(page):
            return True
    return False


async def solve_any_captcha(page, frame, attempt: int) -> bool:
    """Dispatches to the correct solver based on detected captcha type."""
    ctype = await detect_captcha_type(frame)
    print(f"  [captcha#{attempt}] Detected type: {ctype.upper()}", flush=True)

    if ctype == "3d":
        ok = await solve_3d_captcha(page, frame, attempt)
        if ok:
            return True
        # If 3D solver failed, fallback to slider if slider button became visible
        if await frame.locator(".captcha-slider-btn").count():
            return await solve_slider_captcha(page, frame, attempt)
        return False
    else:
        ok = await solve_slider_captcha(page, frame, attempt)
        if ok:
            return True
        # If slider failed, check if it transformed into 3D
        if await detect_captcha_type(frame) == "3d":
            return await solve_3d_captcha(page, frame, attempt)
        return False
