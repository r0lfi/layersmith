#!/usr/bin/env python3
"""Compare the base model with base model + adapter on the same prompts.

    python /opt/layersmith/examples/compare_adapter.py \
        --adapter /outputs/sft-lora-run1 --prompts /datasets/validation.jsonl --limit 5

The base model is taken from the adapter's adapter_provenance.json (or
--model), so an adapter is never paired with the wrong base by accident; a
mismatch in the recorded file checksums is reported. Generation is greedy, so
two runs print the same text.

This is a quick look, not an evaluation. For benchmark numbers use
lm-evaluation-harness (the lm-eval add-on) on both models.
"""

import argparse
import json
import os
import sys
from pathlib import Path

from common import describe_model, set_offline


def prompts_from(path: str, limit: int):
    found = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if "messages" in record:
            # Everything up to the last assistant turn is the prompt.
            messages = record["messages"]
            cut = max(i for i, m in enumerate(messages) if m["role"] == "assistant") if any(
                m["role"] == "assistant" for m in messages) else len(messages)
            found.append(messages[:cut])
        elif "prompt" in record:
            prompt = record["prompt"]
            found.append(prompt if isinstance(prompt, list) else [{"role": "user", "content": prompt}])
        if len(found) >= limit:
            break
    return found


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--adapter", required=True)
    parser.add_argument("--model", help="base model; defaults to the one in adapter_provenance.json")
    parser.add_argument("--prompts", required=True, help="JSONL with messages or prompt records")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--max-new-tokens", type=int, default=64)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--cpu", action="store_true")
    args = parser.parse_args()
    set_offline(args.offline)

    import torch
    from peft import PeftModel
    from transformers import AutoConfig, AutoModelForCausalLM, AutoTokenizer

    provenance_path = Path(args.adapter) / "adapter_provenance.json"
    provenance = json.loads(provenance_path.read_text()) if provenance_path.is_file() else {}
    model_name = args.model or provenance.get("base_model", {}).get("name_or_path")
    if not model_name:
        sys.exit("No base model: pass --model, or use an adapter with adapter_provenance.json")
    revision = provenance.get("base_model", {}).get("requested_revision")
    local_only = args.offline or os.environ.get("HF_HUB_OFFLINE") == "1"

    config = AutoConfig.from_pretrained(model_name, revision=revision, local_files_only=local_only)
    recorded = provenance.get("base_model", {}).get("file_sha256")
    if recorded:
        current = describe_model(model_name, revision, config).get("file_sha256", {})
        changed = [name for name, digest in recorded.items() if current.get(name) != digest]
        print("Base model matches the adapter's provenance." if not changed
              else f"WARNING: base model files differ from training time: {', '.join(changed)}")

    device = "cuda" if torch.cuda.is_available() and not args.cpu else "cpu"
    tokenizer = AutoTokenizer.from_pretrained(args.adapter, local_files_only=True)
    base = AutoModelForCausalLM.from_pretrained(model_name, revision=revision, local_files_only=local_only,
                                                dtype=torch.bfloat16 if device == "cuda" else torch.float32)
    base.to(device).eval()

    def generate(model, messages):
        inputs = tokenizer.apply_chat_template(messages, add_generation_prompt=True, return_tensors="pt",
                                               return_dict=True).to(device)
        with torch.no_grad():
            output = model.generate(**inputs, max_new_tokens=args.max_new_tokens, do_sample=False,
                                    pad_token_id=tokenizer.pad_token_id)
        return tokenizer.decode(output[0, inputs["input_ids"].shape[1]:], skip_special_tokens=True).strip()

    prompts = prompts_from(args.prompts, args.limit)
    base_answers = [generate(base, p) for p in prompts]
    tuned = PeftModel.from_pretrained(base, args.adapter).eval()
    tuned_answers = [generate(tuned, p) for p in prompts]

    changed = 0
    for messages, before, after in zip(prompts, base_answers, tuned_answers):
        changed += before != after
        print("=" * 72)
        print("PROMPT:", messages[-1]["content"])
        print("BASE:   ", before[:400])
        print("ADAPTER:", after[:400])
    print("=" * 72)
    print(f"{changed} of {len(prompts)} answers differ between the base model and the adapter.")


if __name__ == "__main__":
    main()
