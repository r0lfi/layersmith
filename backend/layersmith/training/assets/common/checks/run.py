#!/usr/bin/env python3
"""layersmith-check: verify a LayerSmith training image.

    layersmith-check deps       versions, dependency consistency and imports
    layersmith-check cpu        CPU smoke test: forward, backward, optimizer step, save and load
    layersmith-check offline    run the bundled example without network, on local resources
    layersmith-check gpu        GPU test - run this on the machine that will train
    layersmith-check model DIR  is everything a model needs for offline use present in DIR?
    layersmith-check all        deps, cpu and offline

LayerSmith runs deps, cpu and offline right after a build, with networking
disabled. It never runs gpu: GPU facts about the build host say nothing about
the machine that will train. Run gpu there; add --json to get a report that
LayerSmith can record against the build.

Every subcommand ends with one line:  LAYERSMITH_CHECK_RESULT {json}
The exit status is 0 only when every step passed.
"""

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from importlib import metadata
from pathlib import Path

ROOT = Path("/opt/layersmith")
EXAMPLES = ROOT / "examples"
DATA = EXAMPLES / "data"
RESULT_PREFIX = "LAYERSMITH_CHECK_RESULT "


def recipe():
    path = ROOT / "recipe.json"
    return json.loads(path.read_text()) if path.is_file() else {}


class Steps:
    """Run named steps, keep going after a failure, report all of them."""

    def __init__(self, check):
        self.check = check
        self.steps = []
        self.info = {}

    def run(self, name, function, *args, **kwargs):
        started = time.monotonic()
        print(f"--> {name}", flush=True)
        try:
            detail = function(*args, **kwargs)
            status = "passed"
            if isinstance(detail, tuple) and detail and detail[0] == "skipped":
                status, detail = "skipped", detail[1]
        except Exception as exc:  # report, then continue with the next step
            traceback.print_exc()
            status, detail = "failed", f"{type(exc).__name__}: {exc}"[:600]
        seconds = round(time.monotonic() - started, 1)
        print(f"    {status.upper()} ({seconds}s){': ' + str(detail) if detail else ''}", flush=True)
        self.steps.append({"name": name, "status": status, "detail": "" if detail is None else str(detail),
                           "seconds": seconds})
        return status == "passed"

    def finish(self, as_json=False):
        failed = [s for s in self.steps if s["status"] == "failed"]
        result = {"check": self.check, "status": "failed" if failed or not self.steps else "passed",
                  "steps": self.steps, "info": self.info,
                  "image": os.environ.get("LAYERSMITH_IMAGE"),
                  "recipe": {k: recipe().get(k) for k in ("profile", "profile_version", "stack", "addons")},
                  "finished": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        if as_json:
            print(json.dumps(result, indent=2))
        print(RESULT_PREFIX + json.dumps(result), flush=True)
        return 0 if result["status"] == "passed" else 1


def sh(argv, timeout=1800, env=None):
    """Run a command, stream its output, raise with the tail on failure."""
    print("    $ " + " ".join(map(str, argv)), flush=True)
    merged = {**os.environ, **(env or {})}
    done = subprocess.run(list(map(str, argv)), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                          timeout=timeout, env=merged)
    lines = [line for line in done.stdout.splitlines() if "it/s]" not in line and "examples/s]" not in line]
    for line in lines[-40:]:
        print("    | " + line)
    if done.returncode != 0:
        raise RuntimeError(f"{Path(str(argv[0])).name} exited with {done.returncode}: " + " / ".join(lines[-3:]))
    return done.stdout


# ------------------------------------------------------------------ deps

def check_versions(r):
    locked = r.get("locked") or {}
    wrong = []
    for name, expected in locked.items():
        try:
            installed = metadata.version(name)
        except metadata.PackageNotFoundError:
            wrong.append(f"{name} missing")
            continue
        # A wheel's local version label (+cu12torch2.8...) is not always in its metadata.
        if installed != expected and installed != expected.split("+", 1)[0]:
            wrong.append(f"{name} {installed} (locked {expected})")
    if wrong:
        raise RuntimeError("; ".join(wrong[:20]))
    return f"{len(locked)} locked packages at their locked versions"


def check_pip(r):
    sh([sys.executable, "-m", "pip", "check"], timeout=300)
    return "pip check: no broken requirements"


def check_import(module):
    __import__(module)
    return None


def torch_info(steps, r):
    import torch

    steps.info["torch"] = torch.__version__
    steps.info["torch_cuda"] = torch.version.cuda
    steps.info["cuda_arch_list"] = torch.cuda.get_arch_list() if torch.version.cuda else []
    expected = r.get("cuda")
    if expected and not (torch.version.cuda or "").startswith(expected):
        raise RuntimeError(f"PyTorch is built for CUDA {torch.version.cuda}, the recipe says {expected}")
    if expected is None and torch.version.cuda:
        raise RuntimeError(f"CPU recipe but PyTorch is a CUDA {torch.version.cuda} build")
    return f"torch {torch.__version__}, CUDA {torch.version.cuda or 'none'}, archs {','.join(steps.info['cuda_arch_list'])}"


def bitsandbytes_library():
    """bitsandbytes must ship a native library it will pick for PyTorch's CUDA version.

    Same rule as bitsandbytes' own loader: an exact match, else the highest
    packaged version of the same major below the runtime (12.9 -> 12.8), else
    the lowest above it. No cross-major fallback.
    """
    import importlib.util
    import re

    import torch

    folder = Path(importlib.util.find_spec("bitsandbytes").origin).parent
    major, minor = (int(x) for x in torch.version.cuda.split(".")[:2])
    shipped = []
    for path in folder.glob("libbitsandbytes_cuda*.so"):
        match = re.fullmatch(r"libbitsandbytes_cuda(\d+)(\d)\.so", path.name)
        if match and int(match.group(1)) == major:
            shipped.append((int(match.group(2)), path.name))
    if not shipped:
        raise RuntimeError(f"bitsandbytes has no CUDA {major}.x library")
    below = [s for s in shipped if s[0] <= minor]
    chosen = max(below) if below else min(shipped)
    exact = "exact match" if chosen[0] == minor else f"closest for CUDA {major}.{minor}"
    return f"{chosen[1]} ({exact})"


def deepspeed_info(steps):
    import deepspeed

    steps.info["deepspeed"] = deepspeed.__version__
    nvcc = shutil.which("nvcc") or "/usr/local/cuda/bin/nvcc"
    out = subprocess.run([nvcc, "--version"], capture_output=True, text=True, check=True).stdout
    release = [line for line in out.splitlines() if "release" in line]
    steps.info["nvcc"] = release[-1].strip() if release else out.strip()[-80:]
    return f"deepspeed {deepspeed.__version__}; {steps.info['nvcc']}; CUDA ops compile on first use (JIT)"


def flash_attn_info(steps):
    import flash_attn

    steps.info["flash_attn"] = flash_attn.__version__
    try:
        import flash_attn_2_cuda  # noqa: F401  the compiled extension
    except ImportError as exc:
        # Loading the extension needs the NVIDIA driver library, which only a
        # GPU host provides. That is expected here and checked by `gpu`.
        if "libcuda" in str(exc):
            return f"flash_attn {flash_attn.__version__}; CUDA extension needs a GPU host (checked by gpu)"
        raise
    return f"flash_attn {flash_attn.__version__}, CUDA extension loads"


def cmd_deps(args):
    r = recipe()
    steps = Steps("deps")
    steps.info["python"] = platform.python_version()
    steps.run("locked versions", check_versions, r)
    steps.run("pip check", check_pip, r)
    for module in r.get("imports") or []:
        steps.run(f"import {module}", check_import, module)
    if r.get("torch"):
        steps.run("pytorch build", torch_info, steps, r)
    if "bitsandbytes" in (r.get("locked") or {}) and r.get("cuda"):
        steps.run("bitsandbytes CUDA library", bitsandbytes_library)
    if "deepspeed" in (r.get("addons") or []):
        steps.run("deepspeed and nvcc", deepspeed_info, steps)
    if "flash-attn" in (r.get("addons") or []):
        steps.run("flash-attn", flash_attn_info, steps)
    if r.get("profile") == "llama-factory":
        steps.run("llamafactory-cli", lambda: sh(["llamafactory-cli", "version"], timeout=300) and None)
    return steps.finish(args.json)


# ------------------------------------------------------------------- cpu

def tiny_model(directory, tokenizer_only=False):
    argv = [sys.executable, EXAMPLES / "make_tiny_model.py", directory]
    if tokenizer_only:
        argv.append("--tokenizer-only")
    sh(argv, timeout=600)
    return f"made locally in {directory} (function test only)"


def torch_training_step(directory):
    """Forward, backward, optimizer step, save, load - on CPU, tiny model."""
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(directory, local_files_only=True)
    model = AutoModelForCausalLM.from_pretrained(directory, local_files_only=True)
    text = tokenizer.apply_chat_template(
        [{"role": "user", "content": "Reverse the word 'orbit'."}, {"role": "assistant", "content": "tibro"}],
        tokenize=False)
    batch = tokenizer([text], return_tensors="pt")
    optimizer = torch.optim.AdamW(model.parameters(), lr=1e-3)
    before = [p.detach().clone() for p in model.parameters()]
    output = model(**batch, labels=batch["input_ids"])
    if not torch.isfinite(output.loss):
        raise RuntimeError(f"loss is {output.loss}")
    output.loss.backward()
    missing = [n for n, p in model.named_parameters() if p.requires_grad and p.grad is None]
    if missing:
        raise RuntimeError(f"no gradient for {missing[:3]}")
    optimizer.step()
    changed = sum(not torch.equal(a, b.detach()) for a, b in zip(before, model.parameters()))
    if changed == 0:
        raise RuntimeError("optimizer step changed no parameter")
    with tempfile.TemporaryDirectory() as saved:
        model.save_pretrained(saved)
        reloaded = AutoModelForCausalLM.from_pretrained(saved, local_files_only=True)
        model.eval()
        reloaded.eval()
        with torch.no_grad():
            if not torch.allclose(model(**batch).logits, reloaded(**batch).logits, atol=1e-6):
                raise RuntimeError("reloaded model gives different logits")
    return f"loss {output.loss.item():.3f}; {changed} parameter tensors updated; save/load round trip identical"


def lora_step(directory):
    import torch
    from peft import LoraConfig, get_peft_model
    from transformers import AutoModelForCausalLM

    model = AutoModelForCausalLM.from_pretrained(directory, local_files_only=True)
    model = get_peft_model(model, LoraConfig(r=4, lora_alpha=8, target_modules="all-linear", task_type="CAUSAL_LM"))
    ids = torch.randint(10, 400, (2, 24))
    loss = model(input_ids=ids, labels=ids).loss
    loss.backward()
    trainable = [p for p in model.parameters() if p.requires_grad]
    frozen_with_grad = [n for n, p in model.named_parameters() if not p.requires_grad and p.grad is not None]
    if not trainable or frozen_with_grad:
        raise RuntimeError("LoRA did not freeze the base model as expected")
    return f"{sum(p.numel() for p in trainable):,} trainable LoRA parameters, base frozen"


def tokenizer_and_data(directory):
    """The data-preparation path: tokenizer with chat template, datasets, pandas, parquet."""
    import pandas as pd
    from datasets import load_dataset
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(directory, local_files_only=True)
    dataset = load_dataset("json", data_files=str(DATA / "sample_chat.jsonl"), split="train")
    lengths = [len(tokenizer(tokenizer.apply_chat_template(m, tokenize=False), add_special_tokens=False)
                   .input_ids) for m in dataset["messages"]]
    split = dataset.train_test_split(test_size=0.25, seed=1)
    with tempfile.TemporaryDirectory() as out:
        split["train"].to_parquet(f"{out}/train.parquet")
        frame = pd.read_parquet(f"{out}/train.parquet")
        frame.to_csv(f"{out}/train.csv", index=False)
    if len(frame) != len(split["train"]):
        raise RuntimeError("parquet round trip lost rows")
    return f"{len(dataset)} records, {min(lengths)}-{max(lengths)} tokens, split {len(split['train'])}/{len(split['test'])}"


def llamaboard_ui():
    from llamafactory.webui.interface import create_ui

    create_ui()  # builds the Gradio app without starting a server
    return "LLaMA Board interface builds"


def cmd_cpu(args):
    r = recipe()
    steps = Steps("cpu")
    work = Path(tempfile.mkdtemp(prefix="layersmith-cpu-"))
    try:
        if r.get("torch"):
            if steps.run("create tiny model", tiny_model, work / "tiny"):
                steps.run("forward, backward, optimizer step, save/load", torch_training_step, work / "tiny")
                if "peft" in (r.get("locked") or {}):
                    steps.run("LoRA adapter step", lora_step, work / "tiny")
        else:
            if steps.run("create tiny tokenizer", tiny_model, work / "tok", True):
                steps.run("tokenize, split, parquet/csv", tokenizer_and_data, work / "tok")
        if r.get("profile") == "llama-factory":
            steps.run("LLaMA Board interface", llamaboard_ui)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return steps.finish(args.json)


# --------------------------------------------------------------- offline

def offline_env():
    return {"HF_HUB_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}


def provenance_names(adapter, base):
    recorded = json.loads((adapter / "adapter_provenance.json").read_text())["base_model"]
    if recorded["name_or_path"] != str(base) or not recorded.get("file_sha256"):
        raise RuntimeError(f"adapter_provenance.json names {recorded['name_or_path']}, expected {base}")
    return "adapter_provenance.json records the base model and its file checksums"


def run_examples(steps, work, device_args):
    """The shipped examples, exactly as a user runs them, on local files only."""
    r = recipe()
    tiny, py, env = work / "tiny", sys.executable, offline_env()
    report = ["--report-to", "tensorboard" if "tensorboard" in (r.get("addons") or []) else "none",
              "--logging-dir", work / "logs"]
    small = ["--max-length", "128", "--grad-accum", "1", "--batch-size", "2"]
    if not steps.run("create tiny model", tiny_model, tiny):
        return
    profile = r.get("profile")
    if profile in ("hf-finetune", "pytorch-advanced"):
        sft = [py, EXAMPLES / "sft_lora.py", "--model", tiny, "--train", DATA / "sample_chat.jsonl",
               "--val", DATA / "sample_chat_val.jsonl", "--output", work / "sft", "--offline",
               "--save-steps", "1", *small, *report, *device_args]
        steps.run("SFT + LoRA example (2 steps)", lambda: sh([*sft, "--max-steps", "2"], env=env) and
                  f"adapter and checkpoints in {work / 'sft'}")
        steps.run("resume from checkpoint", lambda: sh([*sft, "--max-steps", "3", "--resume"], env=env) and
                  "continued from checkpoint-2 to step 3")
        steps.run("adapter provenance", provenance_names, work / "sft", tiny)
        steps.run("DPO + LoRA example (1 step)", lambda: sh(
            [py, EXAMPLES / "dpo_lora.py", "--model", tiny, "--train", DATA / "sample_preference.jsonl",
             "--output", work / "dpo", "--offline", "--max-steps", "1", *small, *report, *device_args], env=env)
            and "preference data trains")
        steps.run("compare base and adapter", lambda: sh(
            [py, EXAMPLES / "compare_adapter.py", "--adapter", work / "sft", "--prompts",
             DATA / "sample_chat_val.jsonl", "--limit", "2", "--max-new-tokens", "4", "--offline", *device_args],
            env=env) and "base and adapter generate")
    if profile == "pytorch-advanced":
        steps.run("full fine-tuning / continued pretraining example (1 step)", lambda: sh(
            [py, EXAMPLES / "train_causal_lm.py", "--model", tiny, "--train", DATA / "sample_text.jsonl",
             "--output", work / "cpt", "--offline", "--max-steps", "1", "--max-length", "64", "--grad-accum", "1",
             *report, *device_args], env=env) and "full-parameter training runs")
    if profile == "llama-factory":
        config = EXAMPLES / "llamafactory" / "lora_sft.yaml"
        overrides = [f"model_name_or_path={tiny}", f"output_dir={work / 'lf'}", "max_steps=2", "save_steps=1",
                     f"dataset_dir={EXAMPLES / 'llamafactory'}", "dataset=layersmith_sample", "template=chatml",
                     "cutoff_len=128", "per_device_train_batch_size=2", "gradient_accumulation_steps=1",
                     "report_to=none", "bf16=false", "trust_remote_code=false"]
        steps.run("LLaMA-Factory CLI: LoRA SFT (2 steps)", lambda: sh(
            ["llamafactory-cli", "train", config, *overrides], env={**env, "CUDA_VISIBLE_DEVICES": os.environ.get(
                "CUDA_VISIBLE_DEVICES", "")}) and "llamafactory-cli train completed")


def run_data_examples(steps, work):
    env, py = offline_env(), sys.executable
    if not steps.run("create tiny tokenizer", tiny_model, work / "tok", True):
        return
    steps.run("validate and split", lambda: sh(
        [py, EXAMPLES / "validate_dataset.py", DATA / "sample_chat.jsonl", "--split", "0.25", "--out",
         work / "prepared", "--tokenizer", work / "tok", "--max-length", "128"], env=env) and "train/validation written")
    steps.run("filter", lambda: sh(
        [py, EXAMPLES / "filter_dataset.py", work / "prepared" / "train.jsonl", "--out", work / "filtered.jsonl",
         "--tokenizer", work / "tok", "--max-tokens", "128", "--dedupe"], env=env) and None)
    steps.run("convert JSONL -> Parquet -> CSV", lambda: sh(
        [py, EXAMPLES / "convert_dataset.py", work / "filtered.jsonl", work / "filtered.parquet"], env=env) and sh(
        [py, EXAMPLES / "convert_dataset.py", DATA / "sample_text.jsonl", work / "text.csv"], env=env) and None)
    steps.run("token statistics", lambda: sh(
        [py, EXAMPLES / "token_stats.py", work / "filtered.parquet", "--tokenizer", work / "tok"], env=env) and None)


def cmd_offline(args):
    r = recipe()
    steps = Steps("offline")
    steps.info["network"] = "no network expected; HF_HUB_OFFLINE=1"
    work = Path(tempfile.mkdtemp(prefix="layersmith-offline-"))
    try:
        if r.get("torch"):
            run_examples(steps, work, ["--cpu"])
        else:
            run_data_examples(steps, work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return steps.finish(args.json)


# ------------------------------------------------------------------- gpu

def gpu_devices(steps):
    import torch

    if not torch.cuda.is_available():
        raise RuntimeError("PyTorch sees no CUDA device. Start the container with GPU access "
                           "(docker --gpus all, or --device nvidia.com/gpu=all with CDI).")
    devices = []
    for index in range(torch.cuda.device_count()):
        props = torch.cuda.get_device_properties(index)
        devices.append({"index": index, "name": props.name, "capability": f"{props.major}.{props.minor}",
                        "memory_gb": round(props.total_memory / 1024 ** 3, 1)})
    steps.info["devices"] = devices
    steps.info["torch"] = torch.__version__
    steps.info["torch_cuda"] = torch.version.cuda
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"],
                             capture_output=True, text=True, timeout=30).stdout.split()
        steps.info["driver"] = out[0] if out else None
    except (OSError, subprocess.SubprocessError):
        steps.info["driver"] = None
    return ", ".join(f"{d['name']} (sm {d['capability']}, {d['memory_gb']} GB)" for d in devices)


def gpu_arch(steps):
    import torch

    supported = torch.cuda.get_arch_list()
    problems = []
    for device in steps.info["devices"]:
        major, minor = device["capability"].split(".")
        sm = f"sm_{major}{minor}"
        ptx = [a for a in supported if a.startswith("compute_")]
        if sm not in supported and not ptx:
            problems.append(f"{device['name']} is {sm}; this PyTorch build has {', '.join(supported)}")
    if problems:
        raise RuntimeError("; ".join(problems))
    return "every GPU is supported by this PyTorch build"


def gpu_compute():
    import torch

    a, b = torch.randn(512, 512), torch.randn(512, 512)
    expected = a @ b
    got = (a.cuda() @ b.cuda()).cpu()
    if not torch.allclose(expected, got, atol=1e-2, rtol=1e-3):
        raise RuntimeError("GPU matrix product differs from the CPU result")
    torch.cuda.synchronize()
    return f"matmul matches CPU; bf16 supported: {torch.cuda.is_bf16_supported()}"


def flash_attention():
    import torch
    from flash_attn import flash_attn_func

    if torch.cuda.get_device_capability(0)[0] < 8:
        return "skipped", "FlashAttention 2 needs an Ampere (sm_80) or newer GPU"
    q = torch.randn(1, 128, 4, 64, device="cuda", dtype=torch.float16)
    out = flash_attn_func(q, q, q, causal=True)
    reference = torch.nn.functional.scaled_dot_product_attention(
        q.transpose(1, 2), q.transpose(1, 2), q.transpose(1, 2), is_causal=True).transpose(1, 2)
    if not torch.allclose(out, reference, atol=2e-2):
        raise RuntimeError("flash_attn_func differs from PyTorch SDPA")
    return "flash_attn_func matches PyTorch SDPA"


def deepspeed_ops(build_ops):
    sh(["ds_report"], timeout=600)
    if not build_ops:
        return "ds_report ran; CUDA ops not compiled (add --deepspeed-ops to JIT-build FusedAdam)"
    import torch
    from deepspeed.ops.adam import FusedAdam

    parameter = torch.nn.Parameter(torch.randn(16, device="cuda"))
    optimizer = FusedAdam([parameter], lr=1e-3)
    parameter.sum().backward()
    optimizer.step()
    return "FusedAdam compiled (JIT) and ran an optimizer step"


def cmd_gpu(args):
    r = recipe()
    steps = Steps("gpu")
    work = Path(tempfile.mkdtemp(prefix="layersmith-gpu-"))
    py = sys.executable
    try:
        if not steps.run("CUDA devices", gpu_devices, steps):
            return steps.finish(args.json)
        steps.run("GPU architecture supported", gpu_arch, steps)
        steps.run("GPU computation", gpu_compute)
        if not steps.run("create tiny model", tiny_model, work / "tiny"):
            return steps.finish(args.json)
        profile = r.get("profile")
        small = ["--max-length", "128", "--grad-accum", "1", "--batch-size", "2", "--report-to", "none",
                 "--save-steps", "100"]
        if profile in ("hf-finetune", "pytorch-advanced"):
            base = [py, EXAMPLES / "sft_lora.py", "--model", work / "tiny", "--train", DATA / "sample_chat.jsonl",
                    "--max-steps", "3", "--offline", *small]
            steps.run("LoRA SFT training on GPU (3 steps)",
                      lambda: sh([*base, "--output", work / "lora"], env=offline_env()) and "trained on GPU")
            steps.run("QLoRA (4-bit bitsandbytes) training on GPU (3 steps)",
                      lambda: sh([*base, "--qlora", "--output", work / "qlora"], env=offline_env())
                      and "4-bit base + LoRA trained on GPU")
            if len(steps.info["devices"]) > 1:
                steps.run("multi-GPU LoRA SFT (accelerate, 2 GPUs)", lambda: sh(
                    ["accelerate", "launch", "--config_file", EXAMPLES / "configs" / "multi_gpu.yaml",
                     "--num_processes", "2", *base[1:], "--output", work / "multi"], env=offline_env())
                    and "DDP over 2 GPUs")
        if profile == "pytorch-advanced":
            steps.run("full fine-tuning on GPU (2 steps)", lambda: sh(
                [py, EXAMPLES / "train_causal_lm.py", "--model", work / "tiny", "--train", DATA / "sample_text.jsonl",
                 "--output", work / "full", "--offline", "--max-steps", "2", "--max-length", "64",
                 "--grad-accum", "1", "--report-to", "none"], env=offline_env()) and "full-parameter step on GPU")
        if profile == "llama-factory":
            config = EXAMPLES / "llamafactory" / "lora_sft.yaml"
            common = [f"model_name_or_path={work / 'tiny'}", "max_steps=3", f"dataset_dir={EXAMPLES / 'llamafactory'}",
                      "dataset=layersmith_sample", "template=chatml", "cutoff_len=128", "report_to=none"]
            steps.run("LLaMA-Factory LoRA SFT on GPU", lambda: sh(
                ["llamafactory-cli", "train", config, *common, f"output_dir={work / 'lf'}"], env=offline_env())
                and "trained on GPU")
            steps.run("LLaMA-Factory QLoRA (4-bit) on GPU", lambda: sh(
                ["llamafactory-cli", "train", config, *common, "quantization_bit=4", f"output_dir={work / 'lfq'}"],
                env=offline_env()) and "4-bit base + LoRA trained on GPU")
        if "flash-attn" in (r.get("addons") or []):
            steps.run("FlashAttention kernel", flash_attention)
        if "deepspeed" in (r.get("addons") or []):
            steps.run("DeepSpeed", deepspeed_ops, args.deepspeed_ops)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return steps.finish(args.json)


# ----------------------------------------------------------------- model

def cmd_model(args):
    """Is everything there for offline use? Config, tokenizer, every shard."""
    steps = Steps("model")
    directory = Path(args.directory)

    def files():
        if not (directory / "config.json").is_file():
            raise RuntimeError(f"{directory}/config.json missing")
        index = directory / "model.safetensors.index.json"
        if index.is_file():
            shards = sorted(set(json.loads(index.read_text())["weight_map"].values()))
            missing = [s for s in shards if not (directory / s).is_file()]
            if missing:
                raise RuntimeError(f"{len(missing)} of {len(shards)} shards missing, e.g. {missing[0]}")
            return f"config.json and all {len(shards)} shards present"
        weights = list(directory.glob("*.safetensors")) + list(directory.glob("pytorch_model*.bin"))
        if not weights:
            raise RuntimeError("no weight files (*.safetensors or pytorch_model*.bin)")
        return f"config.json and {len(weights)} weight file(s) present"

    def tokenizer():
        from transformers import AutoTokenizer

        loaded = AutoTokenizer.from_pretrained(directory, local_files_only=True)
        template = "with" if loaded.chat_template else "WITHOUT"
        return f"{type(loaded).__name__} loads offline, {template} a chat template"

    def config():
        from transformers import AutoConfig

        loaded = AutoConfig.from_pretrained(directory, local_files_only=True)
        if getattr(loaded, "auto_map", None):
            return f"{loaded.model_type}: needs trust_remote_code - its code files must be in the directory too"
        return f"{loaded.model_type} loads offline"

    os.environ.update(offline_env())
    steps.run("weight files", files)
    steps.run("model config", config)
    steps.run("tokenizer", tokenizer)
    return steps.finish(args.json)


def main():
    parser = argparse.ArgumentParser(prog="layersmith-check", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", action="store_true", help="also print the result as indented JSON")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("deps", "cpu", "offline", "all"):
        sub.add_parser(name)
    gpu = sub.add_parser("gpu")
    gpu.add_argument("--deepspeed-ops", action="store_true", help="also JIT-compile a DeepSpeed CUDA op")
    model = sub.add_parser("model")
    model.add_argument("directory")
    args = parser.parse_args()
    if args.command == "all":
        codes = [cmd_deps(args), cmd_cpu(args), cmd_offline(args)]
        sys.exit(max(codes))
    sys.exit({"deps": cmd_deps, "cpu": cmd_cpu, "offline": cmd_offline, "gpu": cmd_gpu,
              "model": cmd_model}[args.command](args))


if __name__ == "__main__":
    main()
