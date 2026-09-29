"""Поиск Street View по опубликованным Chipoint heads/gallery; без непубличного reranker."""

import argparse
import json
import time
from pathlib import Path

import numpy as np
import open_clip
import torch
import torch.nn.functional as F
from huggingface_hub import hf_hub_download
from torch import nn

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / ".runtime" / "chipoint"
MANIFEST = json.loads(Path(__file__).with_name("manifest.json").read_text())
TOWERS = {
    "so400m256": ("ViT-SO400M-16-SigLIP2-256", 1152),
    "gopt": ("ViT-gopt-16-SigLIP2-256", 1536),
}


def artifact(name, local_only=True):
    return hf_hub_download(
        MANIFEST["model_repo"], name, revision=MANIFEST["revision"], local_files_only=local_only
    )


def encoder_weights(tag, local_only=True):
    config = MANIFEST["encoders"][tag]
    return hf_hub_download(
        config["repo"],
        "open_clip_model.safetensors",
        revision=config["revision"],
        cache_dir=str(RUNTIME / "encoders"),
        local_files_only=local_only,
    )


class Projection(nn.Module):
    def __init__(self, dimension):
        super().__init__()
        self.net = nn.Sequential(nn.Linear(dimension, 1024), nn.GELU(), nn.Dropout(0.1), nn.Linear(1024, 512))

    def forward(self, value):
        return self.net(value)


def download(towers):
    artifact("street_gallery/gallery_all_C_so400m256.npy", False)
    for tag in towers:
        print(f"Загрузка {tag}: галерея ~5 ГБ, энкодер и projection head…", flush=True)
        artifact(f"weights/proj_head_{tag}.pt", False)
        artifact(f"street_gallery/proj_head_gallery_512_{tag}.npy", False)
        # Здесь только загрузка; служба не должна обращаться в сеть во время inference.
        encoder_weights(tag, False)
    print("Все выбранные компоненты загружены", flush=True)


class StreetRetrieval:
    def __init__(self, towers=("so400m256",), device="cuda", chunk_rows=250_000):
        self.device, self.chunk_rows = device, chunk_rows
        if device == "cuda" and not torch.cuda.is_available():
            raise RuntimeError("CUDA недоступна; для CPU используйте --device cpu")
        self.dtype = torch.float16 if device == "cuda" else torch.float32
        self.coords = np.load(artifact("street_gallery/gallery_all_C_so400m256.npy"), mmap_mode="r")
        self.towers = {}
        for tag in towers:
            name, dimension = TOWERS[tag]
            print(f"Загрузка энкодера {tag}…", flush=True)
            model, _, preprocess = open_clip.create_model_and_transforms(
                name,
                pretrained=encoder_weights(tag),
                image_mean=(0.5, 0.5, 0.5),
                image_std=(0.5, 0.5, 0.5),
                image_interpolation="bicubic",
                image_resize_mode="squash",
            )
            model = model.to(device=device, dtype=self.dtype).eval()
            state = torch.load(artifact(f"weights/proj_head_{tag}.pt"), map_location="cpu", weights_only=True)
            head = Projection(dimension)
            head.load_state_dict({key: value for key, value in state.items() if key.startswith("net.")})
            head = head.to(device=device, dtype=self.dtype).eval()
            gallery = np.load(artifact(f"street_gallery/proj_head_gallery_512_{tag}.npy"), mmap_mode="r")
            if gallery.shape != (len(self.coords), 512) or self.coords.shape[1] != 2:
                raise ValueError("Галерея и координаты Chipoint несовместимы")
            self.towers[tag] = (model, preprocess, head, gallery)
        self.version = f"chipoint-2:{MANIFEST['revision'][:12]}:street_retrieval:" + "+".join(towers)

    @torch.inference_mode()
    def infer(self, image):
        started = time.perf_counter()
        combined, diagnostics = {}, {}
        for tag, (model, preprocess, head, gallery) in self.towers.items():
            pixels = preprocess(image.convert("RGB")).unsqueeze(0).to(device=self.device, dtype=self.dtype)
            embedding = F.normalize(model.encode_image(pixels).float(), dim=-1)
            query = F.normalize(head(embedding.to(self.dtype)).float(), dim=-1).to(self.dtype)
            if not torch.isfinite(query).all():
                raise ValueError(f"Неконечный embedding {tag}")
            best, ids = torch.empty(0, device=self.device), torch.empty(0, device=self.device, dtype=torch.long)
            for offset in range(0, len(gallery), self.chunk_rows):
                # Копируем только блок; галерея остаётся memory map на диске.
                block = torch.from_numpy(np.array(gallery[offset : offset + self.chunk_rows], copy=True)).to(
                    device=self.device, dtype=self.dtype
                )
                scores = (block @ query.T).flatten()
                values, indices = scores.topk(min(200, len(scores)))
                merged = torch.cat((best, values.float()))
                merged_ids = torch.cat((ids, indices + offset))
                best, order = merged.topk(min(200, len(merged)))
                ids = merged_ids[order]
                del block, scores
            sims, indices = best.cpu().numpy(), ids.cpu().numpy()
            normalized = (sims - sims.mean()) / (sims.std() + 1e-9)
            diagnostics[tag] = {"top_similarity": float(sims[0]), "pool_mean": float(sims.mean())}
            for index, value in zip(indices, normalized, strict=True):
                combined[int(index)] = combined.get(int(index), 0.0) + float(value)
        selected = sorted(combined, key=combined.get, reverse=True)[:64]
        return {
            "schema": "streetjev.chipoint.v1",
            "model": self.version,
            "mode": "street_retrieval",
            "reranker": False,
            "elapsed_ms": (time.perf_counter() - started) * 1000,
            "towers": diagnostics,
            "candidates": [
                {
                    "id": str(index),
                    "lat": float(self.coords[index, 0]),
                    "lon": float(self.coords[index, 1]),
                    "score": combined[index],
                }
                for index in selected
            ],
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--towers", nargs="+", choices=list(TOWERS), default=["so400m256"])
    args = parser.parse_args()
    if args.download:
        download(args.towers)
