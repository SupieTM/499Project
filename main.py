#!/usr/bin/env python3

import argparse
import json
import re
from difflib import SequenceMatcher
from pathlib import Path

from PIL import Image

from idIdentifier import idIdent
from objIdentifier import objIdent
from part_identifier import PartIdentifier, DATA_DIR


def normalize_part_number(value):
    """Normalize OCR text and filenames so values such as 2299-61 and 2299_61 match."""
    return re.sub(r"[^A-Z0-9]", "", str(value).upper())


def get_ocr_results(ocr, min_confidence):
    """Read OCR text and confidence without changing idIdentifier.py."""
    results = []
    for data in ocr.dataSet:
        texts = data.get("res", {}).get("rec_texts", [])
        scores = data.get("res", {}).get("rec_scores", [])
        for text, score in zip(texts, scores):
            score = float(score)
            if score >= min_confidence:
                results.append({"text": str(text), "confidence": round(score, 4)})
    return sorted(results, key=lambda item: item["confidence"], reverse=True)


def load_indexed_parts(data_dir):
    """Get the part names that are actually present in the CAD index."""
    meta_path = Path(data_dir) / "index_meta.json"
    if not meta_path.exists():
        raise FileNotFoundError(
            f"CAD index metadata was not found at {meta_path}. "
            "Run part_identifier.py build first."
        )

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    return sorted(meta.get("parts", {}).keys())


def rank_parts_from_ocr(ocr_results, part_names, top_k=5):
    """Return the five indexed filenames that best match the OCR number."""
    ranked = []

    for part_name in part_names:
        normalized_part = normalize_part_number(part_name)
        best = None

        for item in ocr_results:
            normalized_text = normalize_part_number(item["text"])
            if not normalized_text:
                continue

            text_match = SequenceMatcher(None, normalized_text, normalized_part).ratio()

            # Give an exact/contained casting number a full filename-match score.
            if normalized_text == normalized_part:
                text_match = 1.0
            elif normalized_text in normalized_part or normalized_part in normalized_text:
                text_match = max(text_match, 0.95)

            # This score includes both OCR certainty and filename similarity.
            reference_confidence = float(item["confidence"]) * text_match
            candidate = {
                "part": part_name,
                "ocr_text": item["text"],
                "ocr_confidence": round(float(item["confidence"]), 4),
                "filename_match": round(text_match, 4),
                "reference_confidence": round(reference_confidence, 4),
            }

            if best is None or candidate["reference_confidence"] > best["reference_confidence"]:
                best = candidate

        if best is not None:
            ranked.append(best)

    ranked.sort(key=lambda item: item["reference_confidence"], reverse=True)
    return ranked[:top_k]


def make_white_background_photos(photo_paths, segmenter, output_dir):
    """Use objIdentifier.py for background removal without modifying that file."""
    output_dir.mkdir(parents=True, exist_ok=True)
    processed_paths = []

    for index, photo_path in enumerate(photo_paths, start=1):
        with Image.open(photo_path) as image:
            image = image.convert("RGB")
            mask, _ = segmenter.proccessImg(image)

            # Call the existing helpers through the class because they do not take self.
            cutout = objIdent.parseMask(objIdent, image, mask)
            white = objIdent.addBg(objIdent, cutout, image.size, (255, 255, 255, 255)).convert("RGB")

        output_path = output_dir / f"casting_{index}_white.png"
        white.save(output_path)
        processed_paths.append(str(output_path))

    return processed_paths


def compare_results(reference_candidates, cad_result):
    reference_top = reference_candidates[0] if reference_candidates else None
    cad_candidates = cad_result.get("candidates", [])[:5]
    cad_top = cad_candidates[0] if cad_candidates else None

    match = bool(
        reference_top
        and cad_top
        and normalize_part_number(reference_top["part"])
        == normalize_part_number(cad_top["part"])
    )

    reference_score = reference_top["reference_confidence"] if reference_top else 0.0
    cad_score = float(cad_top["score"]) if cad_top else 0.0

    # Only combine the scores when both systems chose the same part.
    combined_confidence = (reference_score + cad_score) / 2.0 if match else 0.0

    return {
        "systems_match": match,
        "reference_part": reference_top["part"] if reference_top else None,
        "reference_confidence": round(reference_score, 4),
        "cad_part": cad_top["part"] if cad_top else None,
        "cad_confidence": round(cad_score, 4),
        "combined_confidence": round(combined_confidence, 4),
        "decision": "MATCH" if match else "REVIEW",
    }


def run(args):
    data_dir = Path(args.data_dir)

    print("[1/4] Loading vision models...")
    segmenter = objIdent() 
    ocr = idIdent()

    print("[2/4] Reading the casting number...")
    ocr_image_path = args.ocr_image or args.photos[0]
    with Image.open(ocr_image_path) as image:
        ocr.parseImg(image)
    ocr_results = get_ocr_results(ocr, args.ocr_min_confidence)

    if not ocr_results:
        raise RuntimeError(
            "No casting number met the OCR confidence threshold. "
            "Try a clearer close-up or lower --ocr-min-confidence."
        )

    indexed_parts = load_indexed_parts(data_dir)
    reference_candidates = rank_parts_from_ocr(ocr_results, indexed_parts, top_k=5)

    print("[3/4] Removing backgrounds and running CAD identification...")
    processed_photos = make_white_background_photos(
        args.photos,
        segmenter,
        Path(args.processed_dir),
    )

    identifier = PartIdentifier(data_dir=data_dir)
    cad_result = identifier.identify(processed_photos)
    cad_result["candidates"] = cad_result.get("candidates", [])[:5]

    print("[4/4] Comparing both systems...")
    comparison = compare_results(reference_candidates, cad_result)

    result = {
        "ocr_results": ocr_results,
        "reference_top_5": reference_candidates,
        "cad_top_5": cad_result["candidates"],
        "cad_verdict": cad_result.get("verdict"),
        "cad_margin": cad_result.get("margin"),
        "comparison": comparison,
        "processed_photos": processed_photos,
    }

    print("\nOCR/file reference top 5:")
    for index, candidate in enumerate(reference_candidates, start=1):
        print(
            f"  {index}. {candidate['part']}  "
            f"confidence={candidate['reference_confidence']:.4f}"
        )

    print("\nCAD image-match top 5:")
    for index, candidate in enumerate(cad_result["candidates"], start=1):
        print(f"  {index}. {candidate['part']}  confidence={candidate['score']:.4f}")

    print("\nFinal comparison:")
    print(f"  Reference: {comparison['reference_part']} ({comparison['reference_confidence']:.4f})")
    print(f"  CAD:       {comparison['cad_part']} ({comparison['cad_confidence']:.4f})")
    print(f"  Decision:  {comparison['decision']}")
    if comparison["systems_match"]:
        print(f"  Combined confidence: {comparison['combined_confidence']:.4f}")
    else:
        print("  The two systems disagree, so the casting should be reviewed.")

    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"\nSaved full result to {args.json}")

    return result


def main():
    parser = argparse.ArgumentParser(
        description="Combine casting-number OCR with CAD image identification."
    )
    parser.add_argument("photos", nargs="+", help="Photos of one casting")
    parser.add_argument(
        "--ocr-image",
        help="Optional close-up image of the casting number. Defaults to the first photo.",
    )
    parser.add_argument("--data-dir", default=str(DATA_DIR))
    parser.add_argument("--processed-dir", default="processed_casting_photos")
    parser.add_argument("--ocr-min-confidence", type=float, default=0.60)
    parser.add_argument("--json", default="combined_result.json")
    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()
