"""
A script to run species net, which calls megadetector, and format the outputs.
Formatting of the output can be changes here for ClusterDuck packet
compatability. 

Basically just parses the args
Usage:
  uv run python detect.py /path/to/images_or_single_image.jpg

Writes to:
  - speciesnet_results.json (raw results)
  - predictions.csv         (compact summary: filename,species,confidence)

Requirements:
make a directory on the pi
uv pip install speciesnet - will need uv and pip and python and such installed
Run once on some test files before edge node deployment to install weights, etc.
"""

import argparse, csv, json, os, shlex, subprocess, tempfile
from pathlib import Path

#defaults for CLI run

DEFAULT_COUNTRY = "USA"
DEFAULT_ADMIN1 = "CA"
DEFAULT_MIN_CONF = 0.50
DEFAULT_NUM_WORK = 1
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

def main():
    ap = argparse.ArgumentParser(description="Run SpeciesNet with defaults and export CSV")
    ap.add_argument("path", help="Image folder OR single image file")
    ap.add_argument("--country", default=DEFAULT_COUNTRY)
    ap.add_argument("--admin1_region", default=DEFAULT_ADMIN1)
    ap.add_argument("--min_conf", type=float, default=DEFAULT_MIN_CONF)
    ap.add_argument("--json", default=DEFAULT_JSON, help="Predictions JSON path")
    ap.add_argument("--out_csv", default=DEFAULT_CSV, help="Output CSV path")
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

if __name__ == "__main__":
    main()