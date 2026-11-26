#!/usr/bin/env python3
"""Issue #28 P1: process artifacts under out/ grow unbounded (1.0G at audit time).

Deletes videos/audio/images/compare files older than N days (default 14).
NEVER touches out/ops (ops evidence), out/queue (queue authority + projections),
out/fallback_images (deterministic cover fallback used by ffmpeg_compose).
Run from repo root: python scripts/clean_artifacts.py [--days 14] [--dry-run]
"""
import argparse
import logging
import os
import sys
import time

logger = logging.getLogger("clean_artifacts")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Regenerable process artifact dirs only; ops/queue/fallback are evidence or runtime deps.
CLEAN_DIRS = ["out/videos", "out/audio", "out/images", "out/compare"]
MEDIA_EXTS = {".mp4", ".mp3", ".wav", ".png", ".jpg", ".jpeg", ".npy"}


def clean_dir(path, cutoff, dry_run):
    removed, freed = 0, 0
    if not os.path.isdir(path):
        return removed, freed
    for root, _dirs, files in os.walk(path):
        for fn in files:
            if os.path.splitext(fn)[1].lower() not in MEDIA_EXTS:
                continue
            fp = os.path.join(root, fn)
            try:
                if os.path.getmtime(fp) >= cutoff:
                    continue
                size = os.path.getsize(fp)
            except OSError as e:
                logger.warning("stat failed %s: %s", fp, e)
                continue
            if dry_run:
                logger.info("would remove %s (%.1fM)", fp, size / 1048576)
            else:
                try:
                    os.remove(fp)
                except OSError as e:
                    logger.error("remove failed %s: %s", fp, e)
                    continue
            removed += 1
            freed += size
    return removed, freed


def main():
    parser = argparse.ArgumentParser(description="Remove stale out/ process artifacts (issue #28)")
    parser.add_argument("--days", type=int, default=14, help="delete media files older than N days (default 14)")
    parser.add_argument("--dry-run", action="store_true", help="log only, delete nothing")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.days < 1:
        logger.error("--days must be >= 1 (refusing to wipe fresh artifacts)")
        sys.exit(2)
    cutoff = time.time() - args.days * 86400
    total_removed, total_freed = 0, 0
    for rel in CLEAN_DIRS:
        removed, freed = clean_dir(os.path.join(REPO_ROOT, rel), cutoff, args.dry_run)
        logger.info("%s: removed=%d freed=%.1fM", rel, removed, freed / 1048576)
        total_removed += removed
        total_freed += freed
    logger.info("done removed=%d freed=%.1fM dry_run=%s", total_removed, total_freed / 1048576, args.dry_run)


if __name__ == "__main__":
    main()
