#!/usr/bin/env python3
"""
Evaluate CV predictions against directory-structured ground truth.

CSV format (tab or comma delimited both work):
    filename,species,confidence
    PICT0254_sd6h-rendered.JPG,ba76...;mammalia;carnivora;felidae;lynx;rufus;bobcat,0.9729
    PICT0254_sd6h.JPG,ba76...;mammalia;carnivora;felidae;lynx;rufus;bobcat,0.9349

Assumptions:
- Ground truth label is the FIRST directory under --images-root (e.g., ".../DatasetSamples/<LABEL>/.../file.jpg").
- Predicted label is the LAST non-empty token in the semicolon-separated "species" column (e.g., "...;bobcat").
- Filenames in CSV contain just the filename, not a path.
- Matching is by filename only (case-insensitive). If the same filename exists under multiple species folders,
  it is considered ambiguous and skipped (reported).

Usage:
    python evaluate_predictions.py --csv path/to/results.csv --images-root path/to/DatasetSamples

Optional knobs:
    --species-token-index -1     # where to take label from in the semicolon list (default: last)
    --label-synonyms synonyms.json # optional JSON mapping alias->canonical label
    --extensions .jpg,.jpeg,.png,.bmp,.tif,.tiff  # which files to index
    --delimiter auto             # 'auto', 'comma', or 'tab' (auto sniffs)
"""

import argparse
import csv
import json
import os
import re
import sys
from collections import defaultdict, Counter
from typing import Dict, List, Optional, Set, Tuple

# -------- Normalization helpers --------

def normalize_label(s: str) -> str:
    """Normalize labels for robust comparison (case, punctuation, underscores, spaces)."""
    s = s.strip().lower()
    # replace common separators with space
    s = s.replace("_", " ").replace("-", " ").replace("/", " ")
    # collapse non-letters to single spaces
    s = re.sub(r"[^a-z]+", " ", s)
    # collapse whitespace
    s = re.sub(r"\s+", " ", s).strip()
    return s

# Built-in synonyms: treat Deer, DeerBuck, DeerBuckHead, DeerDoe all as Deer
BASE_SYNONYMS: Dict[str, str] = {
    normalize_label("DeerBuck"):      normalize_label("Deer"),
    normalize_label("DeerBuckHead"):  normalize_label("Deer"),
    normalize_label("DeerDoe"):       normalize_label("Deer"),
    # normalize_label("Deer"):       normalize_label("Deer"),  # not strictly needed
}

def load_synonyms(path: Optional[str]) -> Dict[str, str]:
    """
    Load optional synonyms mapping from JSON: {"grey fox":"gray fox", "lynx rufus":"bobcat"}.
    Keys and values will be normalized the same way as labels.

    Also inject built-in synonyms so DeerBuck / DeerBuckHead / DeerDoe are treated as Deer.
    """
    # start with built-in synonyms
    syn: Dict[str, str] = dict(BASE_SYNONYMS)

    if not path:
        return syn

    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    for k, v in raw.items():
        syn[normalize_label(k)] = normalize_label(v)
    return syn

def apply_synonyms(label: str, syn: Dict[str, str]) -> str:
    return syn.get(label, label)

def is_uuid_like(tok: str) -> bool:
    """Heuristic: treat the leading GUID in your example as non-label."""
    tok = tok.strip().lower()
    return bool(re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", tok))

def pick_taxon_label(field: str, token_index: int) -> str:
    """
    From a semicolon-separated field, pick the token at token_index (default last).
    Skips empty tokens and GUID-like tokens.
    """
    toks = [t for t in (x.strip() for x in field.split(";")) if t and not is_uuid_like(t)]
    if not toks:
        return ""
    if token_index < 0:
        idx = len(toks) + token_index
    else:
        idx = token_index
    idx = max(0, min(idx, len(toks) - 1))
    return toks[idx]

# -------- Filesystem indexing --------

def index_images(images_root: str, allowed_exts: Set[str]) -> Tuple[Dict[str, Set[str]], Dict[str, List[str]]]:
    """
    Walk images_root and build:
      - filename_to_labels:  filename (lowercase) -> set of ground-truth labels (normalized)
      - filename_to_paths:   filename (lowercase) -> list of full paths (for reporting)
    Ground-truth label is the first path component in the relative path.
    """
    filename_to_labels: Dict[str, Set[str]] = defaultdict(set)
    filename_to_paths: Dict[str, List[str]] = defaultdict(list)

    root = os.path.abspath(images_root)
    for dirpath, _, files in os.walk(root):
        for fn in files:
            ext = os.path.splitext(fn)[1].lower()
            if allowed_exts and ext not in allowed_exts:
                continue
            rel = os.path.relpath(os.path.join(dirpath, fn), root)
            parts = rel.split(os.sep)
            if len(parts) < 2:
                # file directly under root; skip because we can't infer label
                continue
            label_raw = parts[0]
            label = normalize_label(label_raw)
            key = fn.lower()
            filename_to_labels[key].add(label)
            filename_to_paths[key].append(os.path.join(dirpath, fn))
    return filename_to_labels, filename_to_paths

# -------- CSV reading --------

def sniff_delimiter(csv_path: str) -> str:
    """Return delimiter character: ',' or '\\t'."""
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        sample = f.read(4096)
    if sample.count("\t") > sample.count(","):
        return "\t"
    return ","

def read_predictions(csv_path: str,
                     delimiter: str,
                     species_token_index: int) -> Dict[str, Tuple[str, float, str]]:
    """
    Read CSV and return mapping:
      filename_lower -> (pred_label_norm, confidence_float, original_species_string)

    If duplicates for the same filename exist, we keep the row with the highest confidence.
    """
    if delimiter == "auto":
        delimiter = sniff_delimiter(csv_path)

    best: Dict[str, Tuple[str, float, str]] = {}
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f, delimiter=delimiter)
        required = {"filename", "normalized_species", "confidence"}
        missing = [c for c in required if c not in reader.fieldnames]
        if missing:
            raise ValueError(f"CSV is missing required columns: {missing}. Found: {reader.fieldnames}")

        for row in reader:
            fn = (row["filename"] or "").strip()
            if not fn:
                continue
            key = fn.lower()
            species_field = row["normalized_species"] or ""
            pred_raw = pick_taxon_label(species_field, species_token_index)
            pred_norm = normalize_label(pred_raw)
            try:
                conf = float(row.get("confidence", "") or "0")
            except ValueError:
                conf = 0.0

            # Keep the highest-confidence entry per filename
            if key not in best or conf > best[key][1]:
                best[key] = (pred_norm, conf, species_field)
    return best

# -------- Evaluation --------

def evaluate(preds: Dict[str, Tuple[str, float, str]],
             filename_to_labels: Dict[str, Set[str]],
             filename_to_paths: Dict[str, List[str]],
             synonyms: Dict[str, str]) -> Dict[str, object]:
    """
    Returns a report dict with counts and details.
    """
    correct = 0
    incorrect = 0
    skipped_missing = 0
    skipped_ambiguous = 0

    per_label_counts = Counter()
    confusion = Counter()  # (gt, pred) -> count
    mistakes: List[Dict[str, str]] = []

    for fname, (pred_label, conf, species_str) in preds.items():
        if fname not in filename_to_labels:
            skipped_missing += 1
            continue

        gt_labels = filename_to_labels[fname]
        if len(gt_labels) != 1:
            # ambiguous filename appears under multiple species folders
            skipped_ambiguous += 1
            continue

        (gt_label,) = tuple(gt_labels)

        # apply synonyms/canonicalization (this is where DeerBuck etc. become Deer)
        pred_canon = apply_synonyms(pred_label, synonyms)
        gt_canon = apply_synonyms(gt_label, synonyms)

        per_label_counts[gt_canon] += 1
        confusion[(gt_canon, pred_canon)] += 1

        if pred_canon == gt_canon:
            correct += 1
        else:
            incorrect += 1
            mistakes.append({
                "filename": fname,
                "gt_label": gt_label,
                "pred_label": pred_label,
                "pred_label_canon": pred_canon,
                "confidence": f"{conf:.4f}",
                "paths": " | ".join(filename_to_paths.get(fname, [])),
                "species_field": species_str
            })

    evaluated = correct + incorrect
    accuracy = (correct / evaluated) if evaluated > 0 else 0.0

    return {
        "accuracy": accuracy,
        "evaluated": evaluated,
        "correct": correct,
        "incorrect": incorrect,
        "skipped_missing_files": skipped_missing,
        "skipped_ambiguous_filenames": skipped_ambiguous,
        "per_label_counts": per_label_counts,
        "confusion": confusion,
        "mistakes": mistakes,
    }

# -------- CLI --------

def parse_args():
    p = argparse.ArgumentParser(description="Evaluate model predictions against folder-based ground truth.")
    p.add_argument("--csv", required=True, help="Path to predictions CSV.")
    p.add_argument("--images-root", required=True, help="Root directory containing species subfolders.")
    p.add_argument("--species-token-index", type=int, default=-1,
                   help="Index into semicolon-separated 'normalized_species' column to use as prediction label (default: -1 for last).")
    p.add_argument("--label-synonyms", default=None,
                   help="Optional JSON file mapping aliases to canonical labels (keys/values normalized).")
    p.add_argument("--extensions", default=".jpg,.jpeg,.png,.bmp,.tif,.tiff",
                   help="Comma-separated list of image extensions to index (lowercased).")
    p.add_argument("--delimiter", choices=["auto", "comma", "tab"], default="auto",
                   help="CSV delimiter handling. 'auto' sniffs; else force comma or tab.")
    p.add_argument("--show-mistakes", action="store_true", help="Print a table of misclassified files.")
    return p.parse_args()

def main():
    args = parse_args()

    allowed_exts = set(e.strip().lower() for e in args.extensions.split(",") if e.strip())
    allowed_exts = {e if e.startswith(".") else "." + e for e in allowed_exts}

    # 1) Index filesystem
    filename_to_labels, filename_to_paths = index_images(args.images_root, allowed_exts)
    if not filename_to_labels:
        print(f"[ERROR] No images found under {args.images_root} with extensions {sorted(allowed_exts)}", file=sys.stderr)
        sys.exit(2)

    # 2) Read predictions
    delimiter = {"auto": "auto", "comma": ",", "tab": "\t"}[args.delimiter]
    preds = read_predictions(args.csv, delimiter=delimiter, species_token_index=args.species_token_index)
    if not preds:
        print(f"[ERROR] No prediction rows found in {args.csv}", file=sys.stderr)
        sys.exit(2)

    # 3) Synonyms (built-in deer synonyms + optional JSON)
    synonyms = load_synonyms(args.label_synonyms)

    # 4) Evaluate
    report = evaluate(preds, filename_to_labels, filename_to_paths, synonyms)

    # 5) Print summary
    print("\n=== Evaluation Summary ===")
    print(f"Images root:           {os.path.abspath(args.images_root)}")
    print(f"CSV:                   {os.path.abspath(args.csv)}")
    print(f"Evaluated rows:        {report['evaluated']}")
    print(f"Correct:               {report['correct']}")
    print(f"Incorrect:             {report['incorrect']}")
    print(f"Skipped (missing):     {report['skipped_missing_files']}")
    print(f"Skipped (ambiguous):   {report['skipped_ambiguous_filenames']}")
    print(f"Accuracy:              {report['accuracy']:.4f}")

    if args.show_mistakes and report["mistakes"]:
        print("\n--- Misclassifications ---")
        # header
        print("filename\tgt\tpred\tpred_canon\tconfidence\tpath(s)")
        for m in report["mistakes"]:
            print(f"{m['filename']}\t{m['gt_label']}\t{m['pred_label']}\t{m['pred_label_canon']}\t{m['confidence']}\t{m['paths']}")

if __name__ == "__main__":
    main()
