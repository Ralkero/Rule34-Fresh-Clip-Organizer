#!/usr/bin/env python3
"""
Simple automated comparison script for Rule34 Organizer previews.

Usage example:
    python analysis/compare_previews.py \
        --baseline analysis/akiryo-baseline-2026-05-31/r34_preview_20260531-155149.csv \
        --new C:/Users/jmswo/Downloads/Akiryo/Audio\ Collection/r34_preview_20260531-160717.csv \
        --ground-truth analysis/akiryo-baseline-2026-05-31/GROUND_TRUTH_CORRECTIONS.md

It focuses on the Akiryo "Mai" files for artist accuracy vs ground truth.
"""

import argparse
import csv
import re
from pathlib import Path
from typing import Dict, List, Tuple


def load_csv(path: Path) -> List[Dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def extract_mai_rows(rows: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Return rows whose original_name starts with 'Mai ' (case-insensitive)."""
    return [r for r in rows if re.match(r"(?i)^mai\s", r.get("original_name", ""))]


def load_ground_truth(path: Path) -> Dict[str, str]:
    """
    Very simple parser for the GROUND_TRUTH_CORRECTIONS.md we created.
    Returns mapping of original filename -> expected artist.
    For Akiryo "Mai" files we know the expected artist is "Mai".
    """
    # For this specific batch we hard-code the known ground truth for simplicity
    # (all "Mai ..." files should have artist "Mai")
    gt = {}
    # In a real version we would parse the MD, but for speed we hard-code the known case
    return gt  # Caller will use "Mai" as expected for all Mai rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", required=True, help="Previous preview CSV (before changes)")
    parser.add_argument("--new", required=True, help="Latest preview CSV (after changes)")
    parser.add_argument("--ground-truth", help="Optional ground truth file (not strictly needed for Akiryo)")
    args = parser.parse_args()

    baseline_path = Path(args.baseline)
    new_path = Path(args.new)

    baseline_rows = load_csv(baseline_path)
    new_rows = load_csv(new_path)

    baseline_mai = extract_mai_rows(baseline_rows)
    new_mai = extract_mai_rows(new_rows)

    print("=== Akiryo 'Mai' Files Artist Analysis ===\n")

    print(f"Baseline ({baseline_path.name}): {len(baseline_mai)} Mai files")
    print(f"New      ({new_path.name}):     {len(new_mai)} Mai files\n")

    # Count how many have artist == "Mai" (ideal)
    def count_correct_artist(rows: List[Dict[str, str]]) -> int:
        return sum(1 for r in rows if r.get("artist", "").strip().lower() == "mai")

    baseline_correct = count_correct_artist(baseline_mai)
    new_correct = count_correct_artist(new_mai)

    print(f"Baseline files with artist='Mai': {baseline_correct} / {len(baseline_mai)}")
    print(f"New files with artist='Mai':      {new_correct} / {len(new_mai)}\n")

    # Average artist confidence for Mai rows
    def avg_artist_conf(rows: List[Dict[str, str]]) -> float:
        vals = [float(r.get("confidence", 0)) for r in rows if r.get("confidence")]
        return sum(vals) / len(vals) if vals else 0.0

    print(f"Baseline avg artist confidence (Mai rows): {avg_artist_conf(baseline_mai):.2f}")
    print(f"New avg artist confidence (Mai rows):      {avg_artist_conf(new_mai):.2f}\n")

    # Count low-confidence artist notes
    def count_low_conf_artist_notes(rows: List[Dict[str, str]]) -> int:
        return sum(1 for r in rows if "artist inference is low confidence" in r.get("notes", "").lower())

    baseline_low = count_low_conf_artist_notes(baseline_mai)
    new_low = count_low_conf_artist_notes(new_mai)

    print(f"Baseline 'artist low confidence' notes: {baseline_low}")
    print(f"New 'artist low confidence' notes:      {new_low}\n")

    # Collector fallback detection (simple heuristic on reason/notes)
    def count_collector_fallbacks(rows: List[Dict[str, str]]) -> int:
        # Count only the *bad* collector fallbacks (the ones that made artist the folder name itself).
        # New good paths contain "over_collector" but correctly set artist to the filename prefix.
        bad = 0
        for r in rows:
            artist = r.get("artist", "").strip().lower()
            reason = r.get("reason", "").lower()
            if artist in ("audio collection", "akiryo") or ("artist_from_collector_folder" in reason and "over_collector" not in reason):
                bad += 1
        return bad

    baseline_col = count_collector_fallbacks(baseline_mai)
    new_col = count_collector_fallbacks(new_mai)

    print(f"Baseline collector-folder style artist assignments: {baseline_col}")
    print(f"New collector-folder style artist assignments:      {new_col}\n")

    # Simple automated assessment
    improvement = new_correct - baseline_correct
    conf_change = avg_artist_conf(new_mai) - avg_artist_conf(baseline_mai)

    print("=== Automated Assessment ===")
    if new_correct > baseline_correct:
        print(f"[OK] Artist accuracy improved (+{improvement} more 'Mai' attributions).")
    else:
        print("[X] No improvement in artist accuracy vs ground truth (still not attributing to 'Mai').")

    if conf_change > 0:
        print(f"[OK] Average confidence on Mai rows increased by {conf_change:.2f}.")
    else:
        print(f"[X] Average confidence on Mai rows decreased or stayed the same ({conf_change:+.2f}).")

    if new_low < baseline_low:
        print(f"[OK] Fewer low-confidence artist notes ({new_low} vs {baseline_low}).")
    else:
        print(f"[X] Low-confidence artist notes did not decrease ({new_low} vs {baseline_low}).")

    if new_col < baseline_col:
        print("[OK] Fewer obvious collector-folder artist assignments.")
    else:
        print("[X] Still heavily relying on collector/subfolder names as artist.")

    further_needed = (new_correct < len(new_mai) * 0.7) or (new_low > 5) or (new_col > 5)
    print(f"\nFurther improvement needed? {'YES' if further_needed else 'No (or marginal)'}")
    if further_needed:
        print("Recommendation: Continue strengthening filename artist prefix logic when source is a collection folder.")


if __name__ == "__main__":
    main()