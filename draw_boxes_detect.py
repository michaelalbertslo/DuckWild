"""
A script to run species net, which calls megadetector, and format the outputs.
Also optionally writes annotated images (boxes + species ID) to a directory.

Usage:
  uv run python detect.py /path/to/images_or_single_image.jpg

Writes to:
  - speciesnet_results.json (raw results)
  - predictions.csv         (compact summary: filename,species,confidence)
  - (optional) annotated images directory with boxes + species labels
"""

import argparse, csv, json, os, shlex, subprocess, tempfile, sys
from pathlib import Path

DEFAULT_COUNTRY = "USA"
DEFAULT_ADMIN1 = "CA"
DEFAULT_MIN_CONF = 0.50
DEFAULT_JSON = "speciesnet_results.json"
DEFAULT_CSV  = "predictions.csv"

def run_speciesnet_cli(images_dir: Path, predictions_json: Path,
                       country: str, admin1: str) -> None:
    cmd = [
        sys.executable, "-m", "speciesnet.scripts.run_model",
        "--folders", str(images_dir),
        "--predictions_json", str(predictions_json),
        "--country", country, "--admin1_region", admin1,
    ]
    print("\n[SpeciesNet] Running:\n ", " ".join(shlex.quote(c) for c in cmd), "\n", flush=True)
    ret = subprocess.call(cmd)
    if ret != 0:
        raise SystemExit(f"SpeciesNet exited with status {ret}")

def _rewrite_tmp_paths_in_json(predictions_json: Path, tmp_dir: Path, real_file: Path) -> None:
    if not predictions_json.exists():
        return
    with predictions_json.open("r", encoding="utf-8") as f:
        data = json.load(f)
    preds = data.get("predictions", [])
    changed = False
    for p in preds:
        fp = Path(p.get("filepath", ""))
        try:
            _ = fp.resolve().relative_to(tmp_dir.resolve())
        except Exception:
            continue
        p["filepath"] = str(real_file.resolve())
        changed = True
    if changed:
        with predictions_json.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

def json_to_csv(predictions_json: Path, out_csv: Path, min_conf: float) -> int:
    if not predictions_json.exists():
        print(f"[WARN] Missing JSON: {predictions_json}")
        return 0
    with predictions_json.open("r", encoding="utf-8") as f:
        data = json.load(f)
    preds = data.get("predictions", [])
    by_path = {}
    for p in preds:
        path = p.get("filepath")
        if not path:
            continue
        score = p.get("prediction_score")
        try:
            conf = float(score) if score is not None else None
        except Exception:
            conf = None
        if conf is None or conf < min_conf:
            continue
        by_path[path] = {
            "filename": Path(path).name,
            "species": p.get("prediction", ""),
            "confidence": f"{conf:.4f}",
        }
    rows = list(by_path.values())
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["filename", "species", "confidence"])
        w.writeheader()
        w.writerows(rows)
    return len(rows)

def annotate_images_from_json(predictions_json: Path, annotated_dir: Path,
                             box_min_conf: float = 0.20, top1_only: bool = False) -> int:
    """
    Draws MegaDetector bboxes + SpeciesNet ensemble prediction on the original images.

    SpeciesNet JSON format includes:
      - detections: [{label/conf/bbox=[xmin,ymin,w,h] normalized}, ...]
      - prediction / prediction_score (ensemble output)
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception as e:
        raise SystemExit(
            "Pillow is required for --annotated_dir. Install with: uv pip install pillow\n"
            f"Import error: {e}"
        )

    if not predictions_json.exists():
        print(f"[WARN] Missing JSON: {predictions_json}")
        return 0

    with predictions_json.open("r", encoding="utf-8") as f:
        data = json.load(f)

    preds = data.get("predictions", [])
    annotated_dir.mkdir(parents=True, exist_ok=True)

    # Try to load a default font; fall back safely
    try:
        font = ImageFont.load_default()
    except Exception:
        font = None

    wrote = 0
    for p in preds:
        img_path = p.get("filepath")
        if not img_path:
            continue
        img_path = Path(img_path)
        if not img_path.exists():
            continue

        detections = p.get("detections") or []
        if not detections:
            # still save an image with "blank" prediction if you want; here we skip
            continue

        species = p.get("prediction", "")
        species_score = p.get("prediction_score", None)

        try:
            im = Image.open(img_path).convert("RGB")
        except Exception:
            continue

        W, H = im.size
        draw = ImageDraw.Draw(im)

        # sort detections by conf desc (should already be, but be safe)
        detections = sorted(detections, key=lambda d: float(d.get("conf", 0.0)), reverse=True)
        if top1_only:
            detections = detections[:1]

        for d in detections:
            conf = float(d.get("conf", 0.0))
            if conf < box_min_conf:
                continue

            label = d.get("label", d.get("category", ""))
            bbox = d.get("bbox", None)
            if not bbox or len(bbox) != 4:
                continue

            xmin, ymin, bw, bh = bbox
            x1 = int(xmin * W)
            y1 = int(ymin * H)
            x2 = int((xmin + bw) * W)
            y2 = int((ymin + bh) * H)

            # Box
            draw.rectangle([x1, y1, x2, y2], outline=(255, 0, 0), width=3)

            # Text
            sp_txt = ""
            if species:
                if species_score is not None:
                    try:
                        sp_txt = f" | {species} {float(species_score):.2f}"
                    except Exception:
                        sp_txt = f" | {species}"
                else:
                    sp_txt = f" | {species}"

            text = f"{label} {conf:.2f}{sp_txt}"

            # Simple background for readability
            text_x, text_y = x1, max(0, y1 - 14)
            draw.rectangle([text_x, text_y, min(W, text_x + 600), text_y + 14], fill=(0, 0, 0))
            draw.text((text_x + 2, text_y), text, fill=(255, 255, 255), font=font)

        out_path = annotated_dir / img_path.name
        im.save(out_path, quality=95)
        wrote += 1

    return wrote

def main():
    ap = argparse.ArgumentParser(description="Run SpeciesNet with defaults and export CSV (+ optional annotated images)")
    ap.add_argument("path", help="Image folder OR single image file")
    ap.add_argument("--country", default=DEFAULT_COUNTRY)
    ap.add_argument("--admin1_region", default=DEFAULT_ADMIN1)
    ap.add_argument("--min_conf", type=float, default=DEFAULT_MIN_CONF)
    ap.add_argument("--json", default=DEFAULT_JSON, help="Predictions JSON path")
    ap.add_argument("--out_csv", default=DEFAULT_CSV, help="Output CSV path")

    # NEW: annotation options
    ap.add_argument("--annotated_dir", default=None,
                    help="If set, write annotated images (boxes + species) into this directory")
    ap.add_argument("--box_min_conf", type=float, default=0.20,
                    help="Only draw detection boxes with conf >= this threshold")
    ap.add_argument("--top1_only", action="store_true",
                    help="Only draw the highest-confidence detection per image")

    args = ap.parse_args()

    # keep BLAS threads modest on Pi
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

    target = Path(args.path).expanduser().resolve()
    predictions_json = Path(args.json).expanduser().resolve()
    out_csv = Path(args.out_csv).expanduser().resolve()
    if not target.exists():
        raise SystemExit(f"Path not found: {target}")

    # file vs folder handling
    tmp_dir = None
    images_dir = target
    if target.is_file():
        tmp = tempfile.TemporaryDirectory(prefix="speciesnet_one_")
        tmp_dir = Path(tmp.name)
        link = tmp_dir / target.name
        try:
            link.symlink_to(target)
        except (NotImplementedError, OSError):
            import shutil; shutil.copy2(target, link)
        images_dir = tmp_dir

    run_speciesnet_cli(images_dir, predictions_json, args.country, args.admin1_region)

    if tmp_dir:
        _rewrite_tmp_paths_in_json(predictions_json, tmp_dir, target)

    count = json_to_csv(predictions_json, out_csv, args.min_conf)
    print(f"[OK] Wrote {count} rows -> {out_csv}")

    if args.annotated_dir:
        annotated_dir = Path(args.annotated_dir).expanduser().resolve()
        wrote = annotate_images_from_json(
            predictions_json,
            annotated_dir,
            box_min_conf=args.box_min_conf,
            top1_only=args.top1_only
        )
        print(f"[OK] Wrote {wrote} annotated images -> {annotated_dir}")

if __name__ == "__main__":
    main()