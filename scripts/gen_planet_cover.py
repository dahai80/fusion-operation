import logging
import os
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from douyin_tools.config import IMAGE_DIR, H, W

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

PLANET_COVER = os.path.join(IMAGE_DIR, "_planet_cover.png")


def gen_planet_cover(out_path=PLANET_COVER, w=W, h=H):
    bg = np.zeros((h, w, 3), dtype=np.uint8)
    rng = np.random.default_rng(42)
    for _ in range(220):
        sx = int(rng.integers(0, w))
        sy = int(rng.integers(0, int(h * 0.55)))
        br = int(rng.integers(120, 255))
        bg[sy, sx] = (br, br, br)
    cx, cy = w // 2, int(h * 0.42)
    r = int(min(w, h) * 0.30)
    yy, xx = np.ogrid[:h, :w]
    dist = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
    t = np.clip(dist / r, 0, 1)
    ocean = np.array([20, 70, 150], dtype=np.float32)
    land = np.array([60, 120, 70], dtype=np.float32)
    polar = np.array([230, 235, 240], dtype=np.float32)
    band = np.clip((yy - cy) / r, -1, 1)
    base = ocean * (1 - np.abs(band))[..., None] + land * np.abs(band)[..., None]
    base = np.where(np.abs(band)[..., None] > 0.75, polar, base)
    sphere = base * (1 - t[..., None] * 0.55)
    inside = dist <= r
    disk = np.where(inside[..., None], sphere, 0.0)
    halo_r = int(r * 1.18)
    halo_t = np.clip((dist - r) / max(halo_r - r, 1), 0, 1)
    halo = (1 - halo_t) * 90
    halo_band = np.where((dist > r) & (dist <= halo_r), halo, 0.0)
    halo_layer = halo_band[..., None]
    img = bg.astype(np.float32) + disk + halo_layer
    img = np.clip(img, 0, 255).astype(np.uint8)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    cv2.imwrite(out_path, cv2.cvtColor(img, cv2.COLOR_RGB2BGR))
    mean = float(np.mean(img))
    logger.info("planet_cover wrote %s mean=%.1f", out_path, mean)
    return out_path


if __name__ == "__main__":
    gen_planet_cover()
