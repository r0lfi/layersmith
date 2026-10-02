"""Shared helpers for the LayerSmith training examples.

Kept small and explicit so each example still reads top to bottom:
  * offline mode (HF_HUB_OFFLINE / local_files_only),
  * loading local JSONL/CSV/Parquet files with train and validation kept apart,
  * choosing a device and precision,
  * writing a provenance file that says which base model an adapter belongs to.
"""

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path


def set_offline(offline: bool) -> None:
    """Make every Hugging Face library refuse network access, before it is imported."""
    if offline:
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["HF_DATASETS_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"


def load_split(path: str | None):
    """A local file as a datasets.Dataset, or None. The format follows the extension."""
    if not path:
        return None
    from datasets import load_dataset

    suffix = Path(path).suffix.lower()
    kind = {".jsonl": "json", ".json": "json", ".csv": "csv", ".parquet": "parquet"}.get(suffix)
    if kind is None:
        sys.exit(f"{path}: use .jsonl, .json, .csv or .parquet")
    return load_dataset(kind, data_files=path, split="train")


def device_settings(force_cpu: bool):
    """(use_cpu, bf16, fp16) for this machine."""
    import torch

    if force_cpu or not torch.cuda.is_available():
        return True, False, False
    bf16 = torch.cuda.is_bf16_supported()
    return False, bf16, not bf16


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def describe_model(model_name: str, revision: str | None, config) -> dict:
    """Where the base model came from, precisely enough to pair an adapter with it again."""
    local = Path(model_name).is_dir()
    entry = {"name_or_path": model_name, "local": local, "requested_revision": revision,
             "resolved_commit": getattr(config, "_commit_hash", None), "architecture": config.architectures}
    if local:
        fingerprint = {}
        for name in ("config.json", "model.safetensors.index.json", "tokenizer.json", "tokenizer_config.json"):
            if (Path(model_name) / name).is_file():
                fingerprint[name] = _sha256(Path(model_name) / name)
        if "model.safetensors.index.json" not in fingerprint and (Path(model_name) / "model.safetensors").is_file():
            fingerprint["model.safetensors"] = _sha256(Path(model_name) / "model.safetensors")
        entry["file_sha256"] = fingerprint
    return entry


def write_provenance(output_dir: str, *, base: dict, method: str, task: str, data: dict, args: dict) -> Path:
    """adapter_provenance.json next to the adapter: base model, data and versions."""
    versions = {}
    for package in ("torch", "transformers", "peft", "trl", "datasets", "accelerate", "bitsandbytes"):
        try:
            versions[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            pass
    record = {
        "created": datetime.now(timezone.utc).isoformat(),
        "image": os.environ.get("LAYERSMITH_IMAGE"),
        "task": task, "method": method,
        "base_model": base,
        "data": {key: {"path": value, "sha256": _sha256(Path(value))} for key, value in data.items() if value},
        "versions": versions,
        "arguments": args,
        "note": "An adapter only works with the base model (and revision) recorded here.",
    }
    path = Path(output_dir) / "adapter_provenance.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2) + "\n")
    return path


def last_checkpoint(output_dir: str) -> str | None:
    from transformers.trainer_utils import get_last_checkpoint

    return get_last_checkpoint(output_dir) if Path(output_dir).is_dir() else None
