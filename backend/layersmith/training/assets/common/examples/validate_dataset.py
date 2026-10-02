#!/usr/bin/env python3
"""Validate a local JSONL training file, and optionally split it.

    python validate_dataset.py /datasets/train.jsonl
    python validate_dataset.py /datasets/all.jsonl --split 0.1 --out /datasets/prepared
    python validate_dataset.py /datasets/train.jsonl --tokenizer /models/my-model --max-length 2048

Expected fields, one JSON object per line (the formats TRL documents):

  chat (SFT)        {"messages": [{"role": "system|user|assistant", "content": "..."}, ...]}
  preference (DPO)  {"prompt": [...messages...], "chosen": [...], "rejected": [...]}
                    (plain strings instead of message lists are accepted too)
  plain text        {"text": "..."}       continued pretraining
  prompt/completion {"prompt": "...", "completion": "..."}

The format is detected from the first record and every record must match it.
With --split, the validation part is written to a separate file and never
mixed into training: the split is made once, reproducibly (--seed), before any
training starts.

Only local files are read; no network access is needed.
"""

import argparse
import json
import random
import sys
from collections import Counter
from pathlib import Path

ROLES = {"system", "user", "assistant", "tool"}


def detect(record):
    if "messages" in record:
        return "chat"
    if {"prompt", "chosen", "rejected"} <= record.keys():
        return "preference"
    if {"prompt", "completion"} <= record.keys():
        return "prompt_completion"
    if "text" in record:
        return "text"
    return None


def check_messages(messages, where, problems, final_role=None):
    if not isinstance(messages, list) or not messages:
        problems.append(f"{where}: must be a non-empty list of messages")
        return
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or set(message) - {"role", "content", "name", "tool_calls"}:
            problems.append(f"{where}[{index}]: expected {{'role', 'content'}}")
            continue
        if message.get("role") not in ROLES:
            problems.append(f"{where}[{index}]: unknown role {message.get('role')!r}")
        if not isinstance(message.get("content"), str) or not message["content"].strip():
            problems.append(f"{where}[{index}]: empty or non-text content")
    if final_role and messages and isinstance(messages[-1], dict) and messages[-1].get("role") != final_role:
        problems.append(f"{where}: last message should be from the {final_role}")


def check_record(record, kind, where):
    problems = []
    if kind == "chat":
        check_messages(record.get("messages"), f"{where}.messages", problems, final_role="assistant")
    elif kind == "preference":
        for key in ("prompt", "chosen", "rejected"):
            value = record.get(key)
            if isinstance(value, str):
                if not value.strip():
                    problems.append(f"{where}.{key}: empty")
            else:
                check_messages(value, f"{where}.{key}", problems)
        if record.get("chosen") == record.get("rejected"):
            problems.append(f"{where}: chosen and rejected are identical")
    elif kind == "prompt_completion":
        for key in ("prompt", "completion"):
            if not isinstance(record.get(key), str) or not record[key].strip():
                problems.append(f"{where}.{key}: empty or not text")
    elif kind == "text":
        if not isinstance(record.get("text"), str) or not record["text"].strip():
            problems.append(f"{where}.text: empty or not text")
    return problems


def render(record, kind, tokenizer):
    """The text the model will see, for token counting."""
    if kind == "chat":
        return tokenizer.apply_chat_template(record["messages"], tokenize=False)
    if kind == "preference" and isinstance(record["prompt"], list):
        return tokenizer.apply_chat_template(record["prompt"] + record["chosen"], tokenize=False)
    if kind == "preference":
        return record["prompt"] + record["chosen"]
    if kind == "prompt_completion":
        return record["prompt"] + record["completion"]
    return record["text"]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("path", type=Path)
    parser.add_argument("--split", type=float, default=0.0, help="fraction for a validation split, e.g. 0.1")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=Path, help="directory for train.jsonl / validation.jsonl")
    parser.add_argument("--tokenizer", help="local tokenizer directory, to count tokens")
    parser.add_argument("--max-length", type=int, default=0, help="report records longer than this many tokens")
    parser.add_argument("--json", action="store_true", help="print the summary as JSON")
    args = parser.parse_args()

    records, problems, kind = [], [], None
    with open(args.path, encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            where = f"line {number}"
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                problems.append(f"{where}: not valid JSON ({exc.msg})")
                continue
            if not isinstance(record, dict):
                problems.append(f"{where}: not a JSON object")
                continue
            this_kind = detect(record)
            kind = kind or this_kind
            if this_kind is None or this_kind != kind:
                problems.append(f"{where}: format {this_kind or 'unknown'} does not match {kind}")
                continue
            problems += check_record(record, kind, where)
            records.append(record)

    duplicates = sum(count - 1 for count in Counter(json.dumps(r, sort_keys=True) for r in records).values())
    summary = {"file": str(args.path), "format": kind, "records": len(records), "duplicates": duplicates,
               "problems": len(problems)}

    if args.tokenizer and records:
        from transformers import AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(args.tokenizer, local_files_only=True)
        lengths = sorted(len(tokenizer(render(r, kind, tokenizer), add_special_tokens=False).input_ids)
                         for r in records)
        summary["tokens"] = {"min": lengths[0], "median": lengths[len(lengths) // 2], "max": lengths[-1],
                             "total": sum(lengths)}
        if args.max_length:
            summary["tokens"]["over_max_length"] = sum(1 for n in lengths if n > args.max_length)

    if args.split and records and not problems:
        rng = random.Random(args.seed)
        shuffled = records[:]
        rng.shuffle(shuffled)
        cut = max(1, int(len(shuffled) * args.split))
        validation, train = shuffled[:cut], shuffled[cut:]
        out = args.out or args.path.parent
        out.mkdir(parents=True, exist_ok=True)
        for name, part in (("train.jsonl", train), ("validation.jsonl", validation)):
            with open(out / name, "w", encoding="utf-8") as handle:
                for record in part:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        summary["split"] = {"train": len(train), "validation": len(validation), "seed": args.seed,
                            "directory": str(out)}

    if args.json:
        print(json.dumps({**summary, "problem_list": problems[:50]}, indent=2))
    else:
        print(f"{args.path}: {len(records)} records, format {kind}, {duplicates} duplicates")
        for problem in problems[:50]:
            print(f"  problem: {problem}")
        if len(problems) > 50:
            print(f"  ... and {len(problems) - 50} more")
        if "tokens" in summary:
            print(f"  tokens: {summary['tokens']}")
        if "split" in summary:
            print(f"  split: {summary['split']}")
    sys.exit(1 if problems or not records else 0)


if __name__ == "__main__":
    main()
