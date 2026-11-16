#!/usr/bin/env python3
import csv
import threading
import uuid
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from PIL import Image

from idIdentifier import idIdent
from objIdentifier import objIdent
from part_identifier import PartIdentifier, DATA_DIR
from main import (
    get_ocr_results,
    load_indexed_parts,
    rank_parts_from_ocr,
    make_white_background_photos,
    compare_results,
)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
UPLOAD_DIR = BASE_DIR / "uploads"
PROCESSED_DIR = BASE_DIR / "processed_casting_photos"
CHECKIN_LOG = BASE_DIR / "checkins.csv"

OCR_MIN_CONFIDENCE = 0.60

app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="")


_models = {}
_model_lock = threading.Lock()


_best_renders = {}


def get_models():
    with _model_lock:
        if not _models:
            print("Loading vision models (first run can take a few minutes)...")
            _models["segmenter"] = objIdent()
            _models["ocr"] = idIdent()
            _models["identifier"] = PartIdentifier(data_dir=Path(DATA_DIR))
            print("Models ready.")
        return _models


def save_upload(file_storage, folder, name):
    folder.mkdir(parents=True, exist_ok=True)
    suffix = Path(file_storage.filename or "").suffix or ".png"
    path = folder / f"{name}{suffix}"
    file_storage.save(path)
    return path


def render_from_candidate(candidate):
    """Find the render the CAD identifier picked as the best view, if it gave one."""
    for key in ("best_render", "render", "best_view", "image"):
        value = candidate.get(key)
        if isinstance(value, dict):
            value = value.get("image") or value.get("path")
        if value:
            path = Path(value)
            return path if path.is_absolute() else Path(DATA_DIR) / path
    return None


@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/cad-render/<part>")
def cad_render(part):
    """Rendered CAD image for a part. Uses the view that best matched the photos
    when available, otherwise the first render made during the build."""
    path = _best_renders.get(part)
    if path is None or not Path(path).exists():
        folder = Path(DATA_DIR) / "renders" / part
        renders = sorted(folder.glob("*.png")) if folder.exists() else []
        if not renders:
            return ("No render found for this part", 404)
        path = renders[0]
    path = Path(path)
    return send_from_directory(path.parent, path.name)


@app.route("/api/parts")
def api_parts():
    try:
        return jsonify({"parts": load_indexed_parts(DATA_DIR)})
    except FileNotFoundError as err:
        return jsonify({"parts": [], "error": str(err)}), 500


@app.route("/api/identify", methods=["POST"])
def api_identify():
    id_photo = request.files.get("id_photo")
    casting_photos = request.files.getlist("photos")

    if id_photo is None:
        return jsonify({"error": "The identification number photo is missing."}), 400
    if len(casting_photos) < 4:
        return jsonify({"error": "Four casting photos are required."}), 400

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6]
    run_upload_dir = UPLOAD_DIR / run_id

    try:
        models = get_models()

       
        id_path = save_upload(id_photo, run_upload_dir, "id_number")
        with Image.open(id_path) as image:
            models["ocr"].parseImg(image)
        ocr_results = get_ocr_results(models["ocr"], OCR_MIN_CONFIDENCE)

        
        photo_paths = [
            str(save_upload(photo, run_upload_dir, f"casting_{i}"))
            for i, photo in enumerate(casting_photos[:4], start=1)
        ]
        processed = make_white_background_photos(
            photo_paths, models["segmenter"], PROCESSED_DIR / run_id
        )

        
        indexed_parts = load_indexed_parts(DATA_DIR)
        reference_top_5 = rank_parts_from_ocr(ocr_results, indexed_parts, top_k=5)

        
        cad_result = models["identifier"].identify(processed)
        cad_top_5 = cad_result.get("candidates", [])[:5]
        cad_result["candidates"] = cad_top_5

        _best_renders.clear()
        for c in cad_top_5:
            render_path = render_from_candidate(c)
            if render_path is not None:
                _best_renders[c["part"]] = render_path

        
        comparison = compare_results(reference_top_5, cad_result)

        
        if comparison["systems_match"]:
            best_part = comparison["cad_part"]
            best_confidence = comparison["combined_confidence"]
        elif comparison["cad_part"]:
            best_part = comparison["cad_part"]
            best_confidence = comparison["cad_confidence"]
        else:
            best_part = comparison["reference_part"]
            best_confidence = comparison["reference_confidence"]

        return jsonify({
            "run_id": run_id,
            "ocr_results": ocr_results,
            "reference_top_5": reference_top_5,
            "cad_top_5": [
                {"part": c["part"], "score": round(float(c["score"]), 4)}
                for c in cad_top_5
            ],
            "cad_verdict": cad_result.get("verdict"),
            "comparison": comparison,
            "best_part": best_part,
            "best_confidence": round(float(best_confidence or 0), 4),
        })

    except Exception as err:  
        print(f"Identification failed: {err!r}")
        return jsonify({"error": str(err)}), 500


@app.route("/api/checkin", methods=["POST"])
def api_checkin():
    data = request.get_json(force=True)
    part = (data.get("part") or "").strip()
    if not part:
        return jsonify({"error": "No matched part was entered."}), 400

    recorded = datetime.now()
    new_file = not CHECKIN_LOG.exists()
    with CHECKIN_LOG.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if new_file:
            writer.writerow([
                "recorded", "delivery", "run_id", "matched_part",
                "system_part", "confidence", "decision", "corrected",
            ])
        writer.writerow([
            recorded.isoformat(timespec="seconds"),
            data.get("delivery", ""),
            data.get("run_id", ""),
            part,
            data.get("system_part", ""),
            data.get("confidence", ""),
            data.get("decision", ""),
            data.get("corrected", False),
        ])

    return jsonify({"recorded": recorded.strftime("%B %d, %Y %I:%M %p")})


if __name__ == "__main__":
    
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=False)
