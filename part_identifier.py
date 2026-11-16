#!/usr/bin/env python3
"""
part_identifier.py - identify which CAD part a casting is from a few photos.

Pipeline
  1. build:    STEP -> mesh -> Open3D renders (42 viewpoints x 2 lights) -> DINOv2
               embeddings of every render (x4 in-plane rotations) -> FAISS index.
               Everything is cached by STEP file hash; re-running only processes
               new or changed CAD files.
  2. query:    photos (background already removed, part on white) -> DINOv2 global
               embedding shortlists the closest render views of each part -> DINOv2
               patch features are matched photo-vs-render (mutual nearest neighbours +
               ratio test) and checked for geometric consistency (RANSAC affine).
               A part's score = fraction of the photo's part area with verified
               matches, averaged over all photos. Each photo also votes.
               Verdict is CONFIDENT when the winner leads the runner-up by
               >= MIN_MARGIN, otherwise UNCERTAIN (have a person confirm).

Command line
  python part_identifier.py build  [--cad-dir DIR]
  python part_identifier.py query photo1.png photo2.png photo3.png photo4.png
         [--parts 2299_61 2299_71]   # limit to the parts expected in this delivery
         [--json result.json]

Python
  from part_identifier import PartIdentifier
  result = PartIdentifier().identify(["c1.png", "c2.png", "c3.png", "c4.png"])
  result["part"], result["verdict"], result["margin"], result["candidates"]

Photo guidance: 4 photos, one from each corner of the casting, from about chest height
looking down (~30-60 deg), whole casting in frame and filling most of it.

Requires: numpy, pillow, trimesh, cadquery, open3d, torch, torchvision, faiss-cpu
"""

import argparse
import hashlib
import json
import math
import os
import sys
import time
import warnings
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

warnings.filterwarnings("ignore", message="xFormers is not available")
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "part_identifier_data"          # renders, caches, index
STEP_EXTS = {".stp", ".step"}

MODEL_NAME = "dinov2_vitb14_reg"
N_VIEWS = 42                    # viewpoints per part, evenly spread over a sphere
LIGHTS = [(0.5, 0.6, 0.8), (-0.6, 0.3, 0.7)]   # camera-relative (right, up, toward camera)
ROTATIONS = 4                   # in-plane rotations of every render (0/90/180/270 deg)
RENDER_PX = 512
WHITE_THRESH = 240              # pixel value below this (any channel) = part
MAX_FACES = 400_000             # decimate only very large meshes

PATCH_RES = 448                 # patch-feature input size -> 32 x 32 patches of 14 px
PATCH_GRID = PATCH_RES // 14
TOP_K_VIEWS = 3                 # global score = mean of a part's best k views
VERIFY_VIEWS = 8                # best views per part checked with patch matching
VERIFY_PARTS = 5                # parts (by global score) that get patch verification
RANSAC_ITERS = 1500
RANSAC_THRESH = 2.0             # inlier distance in patch units
MIN_PATCH_SIM = 0.15            # ignore mutual matches weaker than this cosine similarity
RATIO_TEST = 0.95               # match must clearly beat the 2nd-best candidate
MIN_MARGIN = 0.02               # top-1 minus top-2 score needed for CONFIDENT
                                # (calibrated on test photos; re-check as data grows)
PIPELINE_VERSION = "p1"         # bump to invalidate all caches after changing the method


# ---------------------------------------------------------------------------
# Image helpers
# ---------------------------------------------------------------------------
def _to_rgb(img):
    """RGB on white. Transparent pixels are composited onto white (a plain RGB
    conversion would expose whatever colour is stored under them)."""
    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        return Image.alpha_composite(Image.new("RGBA", rgba.size, (255, 255, 255, 255)),
                                     rgba).convert("RGB")
    return img.convert("RGB")


def _foreground(img):
    return (np.asarray(_to_rgb(img)) < WHITE_THRESH).any(axis=2)


def prepare(img):
    """Crop to the part, pad to a white square, grayscale + autocontrast. Applied
    identically to photos and renders so both look alike to the model."""
    img = _to_rgb(img)
    mask = _foreground(img)
    rows = np.nonzero(mask.sum(axis=1) >= 2)[0]
    cols = np.nonzero(mask.sum(axis=0) >= 2)[0]
    if len(rows) and len(cols):
        y0, y1, x0, x1 = rows[0], rows[-1] + 1, cols[0], cols[-1] + 1
        pad = int(0.08 * max(y1 - y0, x1 - x0))
        img = img.crop((max(0, x0 - pad), max(0, y0 - pad),
                        min(img.width, x1 + pad), min(img.height, y1 + pad)))
    s = max(img.size)
    sq = Image.new("RGB", (s, s), (255, 255, 255))
    sq.paste(img, ((s - img.width) // 2, (s - img.height) // 2))
    return ImageOps.autocontrast(sq.convert("L"), cutoff=1).convert("RGB")


def _rotated(prepared, angle):
    if not angle:
        return prepared
    return prepare(prepared.rotate(angle, resample=Image.BICUBIC, expand=True,
                                   fillcolor=(255, 255, 255)))


def load_photo(path_or_image):
    if isinstance(path_or_image, Image.Image):
        return _to_rgb(path_or_image)
    with Image.open(path_or_image) as im:
        return _to_rgb(ImageOps.exif_transpose(im))   # phones store rotation in EXIF


# ---------------------------------------------------------------------------
# CAD -> mesh -> renders
# ---------------------------------------------------------------------------
def _file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sphere_views(n):
    golden = math.pi * (3 - math.sqrt(5))
    return [(round(math.degrees(math.asin(1 - 2 * (i + 0.5) / n)), 2),
             round(math.degrees(golden * i) % 360, 2)) for i in range(n)]


def _load_mesh(step_path, stl_path):
    import cadquery as cq
    import trimesh
    shape = cq.importers.importStep(str(step_path))
    if shape is None or not shape.solids().vals():
        raise RuntimeError("STEP import produced no solids")
    cq.exporters.export(shape, str(stl_path), exportType="STL",
                        tolerance=0.1, angularTolerance=0.1)
    mesh = trimesh.load(str(stl_path), force="mesh")
    if isinstance(mesh, trimesh.Scene):
        mesh = trimesh.util.concatenate([g for g in mesh.geometry.values()
                                         if isinstance(g, trimesh.Trimesh)])
    if mesh.is_empty or len(mesh.faces) == 0:
        raise RuntimeError("mesh is empty")
    extents = [float(x) for x in mesh.extents]
    if len(mesh.faces) > MAX_FACES:
        try:
            mesh = mesh.simplify_quadric_decimation(face_count=MAX_FACES)
        except Exception:
            pass
    mesh = mesh.copy()
    mesh.apply_translation(-mesh.bounding_box.centroid)
    mesh.apply_scale(2.0 / max(float(np.max(mesh.extents)), 1e-9))
    return mesh, extents


def _camera_dir(elev, azim):
    e, a = math.radians(elev), math.radians(azim)
    return np.array([math.cos(e) * math.cos(a), math.cos(e) * math.sin(a), math.sin(e)])


def _camera_light(elev, azim, light_cam):
    f = _camera_dir(elev, azim)
    r = np.cross([0.0, 0.0, 1.0], f)
    r = r / np.linalg.norm(r) if np.linalg.norm(r) > 1e-6 else np.array([0.0, 1.0, 0.0])
    u = np.cross(f, r)
    l = light_cam[0] * r + light_cam[1] * u + light_cam[2] * f
    return l / np.linalg.norm(l)


_RENDERERS = {}


class _Renderer:
    """Open3D offscreen renderer. The depth buffer decides which pixels are part, so
    the background is pure white and incompletely drawn frames are detected and
    re-rendered."""
    SUPERSAMPLE, FOV, MAX_ATTEMPTS = 2, 30.0, 5

    def __init__(self, mesh):
        import open3d as o3d
        from open3d.visualization import rendering
        self.rendering = rendering
        px = RENDER_PX * self.SUPERSAMPLE
        if px not in _RENDERERS:              # one per process; re-creating can crash Filament
            _RENDERERS[px] = rendering.OffscreenRenderer(px, px)
        self.r = _RENDERERS[px]
        scene = self.r.scene
        scene.clear_geometry()
        scene.set_background([1.0, 1.0, 1.0, 1.0])
        tri = np.ascontiguousarray(np.array(mesh.triangles, dtype=np.float64).reshape(-1, 3))
        faces = np.arange(len(tri), dtype=np.int32).reshape(-1, 3)   # flat shading
        tm = o3d.geometry.TriangleMesh(o3d.utility.Vector3dVector(tri),
                                       o3d.utility.Vector3iVector(faces))
        tm.compute_vertex_normals()
        mat = rendering.MaterialRecord()
        mat.shader, mat.base_color = "defaultLit", [0.6, 0.6, 0.6, 1.0]
        mat.base_roughness, mat.base_metallic = 0.75, 0.0
        scene.add_geometry("part", tm, mat)
        radius = float(np.linalg.norm(tri, axis=1).max())
        self.dist = radius / math.sin(math.radians(self.FOV / 2)) * 1.02

    def render(self, out_path, elev, azim, light_cam):
        f = _camera_dir(elev, azim)
        up = [0.0, 0.0, 1.0] if abs(f[2]) < 0.98 else [0.0, 1.0, 0.0]
        self.r.setup_camera(self.FOV, [0.0, 0.0, 0.0], (f * self.dist).tolist(), up)
        sun = (-_camera_light(elev, azim, light_cam)).tolist()
        scene = self.r.scene
        try:
            scene.set_lighting(self.rendering.Open3DScene.LightingProfile.SOFT_SHADOWS, sun)
        except Exception:
            scene.scene.set_sun_light(sun, [1.0, 1.0, 1.0], 100000)
            scene.scene.enable_sun_light(True)
        best, best_score = None, -1.0
        for _ in range(self.MAX_ATTEMPTS):
            img = np.asarray(self.r.render_to_image())[..., :3].astype(np.int16)
            part = np.asarray(self.r.render_to_depth_image()) < 0.9999
            if part.mean() < 0.001:
                continue
            bg = np.median(img[~part], axis=0) if (~part).any() else np.full(3, 255)
            score = float(((np.abs(img - bg).max(axis=2) > 10) & part).sum()) / part.sum()
            if score > best_score:
                best_score, best = score, np.where(part[..., None], img, 255).astype(np.uint8)
            if score > 0.85:
                break
        if best is None:
            raise RuntimeError(f"Open3D could not render view elev={elev} azim={azim}")
        Image.fromarray(best).resize((RENDER_PX, RENDER_PX), Image.LANCZOS).save(out_path)


def _render_part(step_path_str, step_hash, data_dir_str):
    """Worker process: one STEP file -> N_VIEWS x len(LIGHTS) renders."""
    step_path, data_dir = Path(step_path_str), Path(data_dir_str)
    part = step_path.stem
    out_dir = data_dir / "renders" / part
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.png"):
        old.unlink()
    t0 = time.time()
    mesh, extents = _load_mesh(step_path, data_dir / "meshes" / f"{part}.stl")
    renderer = _Renderer(mesh)
    renders = []
    for vi, (elev, azim) in enumerate(_sphere_views(N_VIEWS)):
        for li, light in enumerate(LIGHTS):
            path = out_dir / f"v{vi:02d}_l{li}.png"
            renderer.render(path, elev, azim, light)
            renders.append(str(path.relative_to(data_dir)))
    return {"part": part, "step": step_path.name, "hash": step_hash, "extents": extents,
            "renders": renders, "version": PIPELINE_VERSION,
            "seconds": round(time.time() - t0, 1)}


# ---------------------------------------------------------------------------
# DINOv2
# ---------------------------------------------------------------------------
class _Model:
    def __init__(self, batch_size=32):
        import torch
        from torchvision import transforms as T
        self.torch, self.batch_size = torch, batch_size
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        print(f"[model] loading {MODEL_NAME} on {self.device}")
        try:
            self.model = torch.hub.load("facebookresearch/dinov2", MODEL_NAME, verbose=False)
        except RuntimeError as exc:
            if "central directory" not in str(exc) and "zip archive" not in str(exc):
                raise
            ckpt = (Path(torch.hub.get_dir()) / "checkpoints" /
                    (MODEL_NAME.replace("_reg", "_reg4") + "_pretrain.pth"))
            print(f"[model] cached weights corrupted, re-downloading ({ckpt})")
            ckpt.unlink(missing_ok=True)
            self.model = torch.hub.load("facebookresearch/dinov2", MODEL_NAME, verbose=False)
        self.model = self.model.to(self.device).eval()
        norm = T.Normalize((0.485, 0.456, 0.406), (0.229, 0.224, 0.225))
        self.tf_global = T.Compose([T.Resize((224, 224), interpolation=T.InterpolationMode.BICUBIC),
                                    T.ToTensor(), norm])
        self.tf_patch = T.Compose([T.Resize((PATCH_RES, PATCH_RES),
                                            interpolation=T.InterpolationMode.BICUBIC),
                                   T.ToTensor(), norm])

    def _run(self, images, tf, fn, batch):
        torch, out = self.torch, []
        for i in range(0, len(images), batch):
            x = torch.stack([tf(im) for im in images[i:i + batch]]).to(self.device)
            with torch.inference_mode():
                v = fn(x).float()
                v = v / v.norm(dim=-1, keepdim=True).clamp(min=1e-12)
            out.append(v.cpu().numpy().astype("float32"))
        return np.concatenate(out)

    def embed(self, images):
        return self._run(images, self.tf_global, self.model, self.batch_size)

    def patches(self, images):
        """(n, PATCH_GRID**2, D) normalized patch features, row-major patch order."""
        return self._run(images, self.tf_patch,
                         lambda x: self.model.forward_features(x)["x_norm_patchtokens"], 8)


# ---------------------------------------------------------------------------
# Geometric verification
# ---------------------------------------------------------------------------
_COORDS = np.stack([np.arange(PATCH_GRID ** 2) % PATCH_GRID,
                    np.arange(PATCH_GRID ** 2) // PATCH_GRID], axis=1).astype(np.float64) + 0.5


def _patch_mask(prepared):
    g = PATCH_GRID
    m = _foreground(prepared.resize((g * 4, g * 4), Image.BILINEAR))
    return m.reshape(g, 4, g, 4).mean(axis=(1, 3)).ravel() > 0.25


def _mutual_matches(fq, fr):
    S = fq @ fr.T
    a, b = S.argmax(axis=1), S.argmax(axis=0)
    qi = np.nonzero(b[a] == np.arange(len(fq)))[0]
    ri = a[qi]
    sims = S[qi, ri]
    keep = sims >= MIN_PATCH_SIM
    if S.shape[1] > 1:
        second = np.partition(S[qi], -2, axis=1)[:, -2]
        keep &= (np.sqrt(np.maximum(2 - 2 * sims, 0))
                 < RATIO_TEST * np.sqrt(np.maximum(2 - 2 * second, 1e-12)))
    return qi[keep], ri[keep], sims[keep]


def _ransac_affine(src, dst, rng):
    """Inliers of the best plausible affine map (no mirroring, scale 1/3-3x, limited
    shear) between photo and render patch positions."""
    n = len(src)
    if n < 6:
        return np.zeros(n, dtype=bool)
    P = np.hstack([src, np.ones((n, 1))])
    samples = rng.random((RANSAC_ITERS, n)).argsort(axis=1)[:, :3]
    M = P[samples]
    ok = np.abs(np.linalg.det(M)) > 1e-3
    if not ok.any():
        return np.zeros(n, dtype=bool)
    A = np.linalg.solve(M[ok], dst[samples[ok]])
    L = A[:, :2, :]
    det = np.linalg.det(L)
    sv = np.linalg.svd(L, compute_uv=False)
    scale = np.sqrt(np.abs(det))
    A = A[(det > 0) & (scale > 1 / 3) & (scale < 3) & (sv[:, 0] < 3 * np.maximum(sv[:, 1], 1e-9))]
    if not len(A):
        return np.zeros(n, dtype=bool)
    inl = np.linalg.norm(np.einsum("nj,kjc->knc", P, A) - dst[None], axis=2) < RANSAC_THRESH
    mask = inl[inl.sum(axis=1).argmax()]
    if mask.sum() >= 3:
        A2, *_ = np.linalg.lstsq(P[mask], dst[mask], rcond=None)
        if np.linalg.det(A2[:2, :]) > 0:
            mask2 = np.linalg.norm(P @ A2 - dst, axis=1) < RANSAC_THRESH
            if mask2.sum() >= mask.sum():
                mask = mask2
    return mask


def _verify(qf, qmask, rf, rmask, rng):
    """(score, quality): fraction of the photo's part patches with geometrically
    consistent matches; quality is the similarity-weighted version (tie-breaker)."""
    qidx, ridx = np.nonzero(qmask)[0], np.nonzero(rmask)[0]
    if len(qidx) < 6 or len(ridx) < 6:
        return 0.0, 0.0
    qi, ri, sims = _mutual_matches(qf[qidx], rf[ridx])
    inl = _ransac_affine(_COORDS[qidx[qi]], _COORDS[ridx[ri]], rng)
    return float(inl.sum()) / len(qidx), float(sims[inl].sum()) / len(qidx)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
class PartIdentifier:
    def __init__(self, data_dir=DATA_DIR, batch_size=32):
        self.data_dir = Path(data_dir)
        self.batch_size = batch_size
        self._model = None
        self._index = None

    # -- paths -------------------------------------------------------------
    @property
    def _manifest_path(self):
        return self.data_dir / "manifest.json"

    @property
    def _index_path(self):
        return self.data_dir / "index.faiss"

    @property
    def _meta_path(self):
        return self.data_dir / "index_meta.json"

    @property
    def model(self):
        if self._model is None:
            self._model = _Model(self.batch_size)
        return self._model

    # -- indexing ----------------------------------------------------------
    def build_index(self, cad_dir, workers=None):
        """Render + embed every STEP file in cad_dir (cached) and write the index."""
        import faiss
        cad_dir = Path(cad_dir)
        steps = sorted(p for p in cad_dir.iterdir() if p.suffix.lower() in STEP_EXTS)
        if not steps:
            raise FileNotFoundError(f"no .stp/.step files in {cad_dir}")
        for sub in ("renders", "meshes", "cache"):
            (self.data_dir / sub).mkdir(parents=True, exist_ok=True)
        manifest = (json.loads(self._manifest_path.read_text(encoding="utf-8"))
                    if self._manifest_path.exists() else {})

        todo, entries = [], {}
        for p in steps:
            h = _file_hash(p)
            e = manifest.get(p.stem)
            if (e and e["hash"] == h and e.get("version") == PIPELINE_VERSION
                    and all((self.data_dir / r).exists() for r in e["renders"])):
                entries[p.stem] = e
            else:
                todo.append((p, h))
        print(f"[index] {len(steps)} CAD file(s): {len(entries)} cached, {len(todo)} to render")

        failures = []
        if todo:
            import multiprocessing as mp
            workers = workers or max(1, min(4, (os.cpu_count() or 2) - 1))
            try:
                pool = ProcessPoolExecutor(workers, mp_context=mp.get_context("spawn"),
                                           max_tasks_per_child=1)
            except TypeError:                                  # Python < 3.11
                pool = ProcessPoolExecutor(workers)
            with pool:
                futs = {pool.submit(_render_part, str(p), h, str(self.data_dir)): p
                        for p, h in todo}
                for fut in as_completed(futs):
                    p = futs[fut]
                    try:
                        e = fut.result()
                        entries[e["part"]] = manifest[e["part"]] = e
                        print(f"[index] rendered {p.name} in {e['seconds']}s")
                    except Exception as exc:
                        failures.append((p.name, str(exc)))
                        print(f"[index] FAILED {p.name}: {exc}")
            self._manifest_path.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
        if not entries:
            raise RuntimeError(f"no CAD file could be rendered: {failures}")

        vecs, meta, parts = [], [], {}
        for part in sorted(entries):
            e = entries[part]
            v, m = self._embed_part(e)
            vecs.append(v)
            meta.extend(m)
            parts[part] = {"step": e["step"], "hash": e["hash"], "extents": e["extents"]}
            print(f"[index] {part}: {len(v)} vectors")
        matrix = np.concatenate(vecs).astype("float32")
        index = faiss.IndexFlatIP(matrix.shape[1])
        index.add(matrix)
        faiss.write_index(index, str(self._index_path))
        self._meta_path.write_text(json.dumps({"model": MODEL_NAME, "version": PIPELINE_VERSION,
                                               "parts": parts, "vectors": meta}),
                                   encoding="utf-8")
        self._index = None
        print(f"[index] done: {index.ntotal} vectors, {len(parts)} parts"
              + (f", {len(failures)} failed" if failures else ""))
        return {"parts": sorted(parts), "failed": failures}

    def _embed_part(self, e):
        key = f"{MODEL_NAME}_{e['part']}_{e['hash'][:12]}_{PIPELINE_VERSION}_r{ROTATIONS}"
        path = self.data_dir / "cache" / f"emb_{key}.npz"
        if path.exists():
            d = np.load(path, allow_pickle=False)
            return d["vecs"], json.loads(str(d["meta"]))
        images, meta = [], []
        for rel in e["renders"]:
            with Image.open(self.data_dir / rel) as im:
                if not _foreground(im).any():
                    continue
                base = prepare(im)
            for k in range(ROTATIONS):
                angle = 360.0 * k / ROTATIONS
                images.append(_rotated(base, angle))
                meta.append({"part": e["part"], "image": rel, "rot": angle})
        vecs = self.model.embed(images)
        np.savez(path, vecs=vecs, meta=np.array(json.dumps(meta)))
        return vecs, meta

    # -- identification ------------------------------------------------------
    def _load_index(self):
        if self._index is None:
            import faiss
            if not self._index_path.exists():
                raise FileNotFoundError("no index found - run `part_identifier.py build` first")
            meta = json.loads(self._meta_path.read_text(encoding="utf-8"))
            parts = sorted(meta["parts"])
            pos = {p: i for i, p in enumerate(parts)}
            self._index = {"faiss": faiss.read_index(str(self._index_path)), "meta": meta,
                           "parts": parts,
                           "vec_part": np.array([pos[m["part"]] for m in meta["vectors"]])}
        return self._index

    def _render_patches(self, vids):
        idx = self._index
        cache = self.data_dir / "cache" / "patches"
        cache.mkdir(parents=True, exist_ok=True)
        out, missing = {}, []
        for vid in vids:
            m = idx["meta"]["vectors"][vid]
            h = idx["meta"]["parts"][m["part"]]["hash"][:12]
            key = hashlib.sha1(f"{MODEL_NAME}|{PIPELINE_VERSION}|{h}|{m['image']}|{m['rot']}|"
                               f"{PATCH_RES}".encode()).hexdigest()[:24]
            path = cache / f"{key}.npz"
            if path.exists():
                d = np.load(path)
                out[vid] = (d["f"].astype("float32"), d["mask"])
            else:
                missing.append((vid, m, path))
        if missing:
            imgs = []
            for _, m, _ in missing:
                with Image.open(self.data_dir / m["image"]) as im:
                    imgs.append(_rotated(prepare(im), m["rot"]))
            feats = self.model.patches(imgs)
            for (vid, _, path), img, f in zip(missing, imgs, feats):
                mask = _patch_mask(img)
                np.savez(path, f=f.astype("float16"), mask=mask)
                out[vid] = (f, mask)
        return out

    def identify(self, photos, parts=None, min_margin=MIN_MARGIN):
        """photos: file paths or PIL images of ONE casting (background removed).
        parts: optional list of part names / name prefixes expected in this delivery.

        Returns {"part", "verdict", "margin", "n_photos", "candidates": [
                 {"part", "score", "votes", "per_photo", "best_render", "extents"}, ...]}"""
        idx = self._load_index()
        allowed = list(range(len(idx["parts"])))
        if parts:
            allowed = [i for i, p in enumerate(idx["parts"])
                       if any(p == x or p.startswith(x) for x in parts)]
            if not allowed:
                raise ValueError(f"none of {parts} is in the index ({idx['parts']})")

        prepared = [prepare(load_photo(p)) for p in photos]
        if not prepared:
            raise ValueError("no photos given")
        q = self.model.embed(prepared)
        n = idx["faiss"].ntotal
        scores, ids = idx["faiss"].search(q, n)
        S = np.empty((len(q), n), dtype="float32")
        for i in range(len(q)):
            S[i, ids[i]] = scores[i]

        cols = {pi: np.nonzero(idx["vec_part"] == pi)[0] for pi in allowed}
        glob = {pi: float(np.sort(S[:, c], axis=1)[:, -min(TOP_K_VIEWS, len(c)):].mean())
                for pi, c in cols.items()}
        short = sorted(allowed, key=lambda pi: glob[pi], reverse=True)[:VERIFY_PARTS]

        wanted = {(i, pi): cols[pi][np.argsort(-S[i, cols[pi]])[:VERIFY_VIEWS]]
                  for i in range(len(q)) for pi in short}
        rfeats = self._render_patches(sorted({int(v) for t in wanted.values() for v in t}))
        qfeats = self.model.patches(prepared)
        qmasks = [_patch_mask(p) for p in prepared]
        rng = np.random.default_rng(0)
        local, quality, best_vid = {}, {}, {}
        for (i, pi), top in wanted.items():
            best = (-1.0, -1.0, None)
            for vid in top:
                rf, rmask = rfeats[int(vid)]
                sc, qual = _verify(qfeats[i], qmasks[i], rf, rmask, rng)
                if (sc, qual) > best[:2]:
                    best = (sc, qual, int(vid))
            local[i, pi], quality[i, pi], best_vid[i, pi] = best

        rank_key = {pi: (float(np.mean([local[i, pi] for i in range(len(q))])),
                         float(np.mean([quality[i, pi] for i in range(len(q))])), glob[pi])
                    for pi in short}
        votes = {pi: 0 for pi in short}
        for i in range(len(q)):
            votes[max(short, key=lambda pi: (local[i, pi], quality[i, pi], glob[pi]))] += 1

        ranked = sorted(short, key=lambda pi: rank_key[pi], reverse=True)
        candidates = []
        for pi in ranked:
            per_photo = [round(local[i, pi], 4) for i in range(len(q))]
            best_i = int(np.argmax(per_photo))
            render = idx["meta"]["vectors"][best_vid[best_i, pi]]["image"]
            candidates.append({"part": idx["parts"][pi], "score": round(rank_key[pi][0], 4),
                               "votes": votes[pi], "per_photo": per_photo,
                               "best_render": str(self.data_dir / render),
                               "extents": idx["meta"]["parts"][idx["parts"][pi]]["extents"]})
        margin = (candidates[0]["score"] - candidates[1]["score"]) if len(candidates) > 1 else 1.0
        return {"part": candidates[0]["part"],
                "verdict": "CONFIDENT" if margin >= min_margin else "UNCERTAIN",
                "margin": round(margin, 4), "n_photos": len(q), "candidates": candidates}


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------
def _print_result(res):
    print(f"\nPart:    {res['part']}")
    print(f"Verdict: {res['verdict']}  (margin {res['margin']:.4f}, {res['n_photos']} photos)")
    for k, c in enumerate(res["candidates"][:3], start=1):
        print(f"  {k}. {c['part']:<28} score={c['score']:.4f}  votes={c['votes']}/{res['n_photos']}")
    if res["verdict"] == "UNCERTAIN":
        print("Have the user confirm between the top candidates (best_render shows each one).")


def main():
    ap = argparse.ArgumentParser(description="Identify a casting's CAD part from photos.")
    ap.add_argument("--data-dir", type=Path, default=DATA_DIR,
                    help="where renders, caches and the index are stored")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pi = sub.add_parser("build", help="render and index all STEP files")
    pi.add_argument("--cad-dir", type=Path, default=ROOT)
    pi.add_argument("--workers", type=int, default=None)
    pq = sub.add_parser("query", help="identify the part in one or more photos")
    pq.add_argument("photos", nargs="+")
    pq.add_argument("--parts", nargs="+", default=None,
                    help="only consider these parts (names or prefixes)")
    pq.add_argument("--min-margin", type=float, default=MIN_MARGIN)
    pq.add_argument("--json", type=Path, default=None, help="also write the result here")
    args = ap.parse_args()

    ident = PartIdentifier(args.data_dir)
    if args.cmd == "build":
        ident.build_index(args.cad_dir, workers=args.workers)
    else:
        res = ident.identify(args.photos, parts=args.parts, min_margin=args.min_margin)
        _print_result(res)
        if args.json:
            args.json.write_text(json.dumps(res, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
