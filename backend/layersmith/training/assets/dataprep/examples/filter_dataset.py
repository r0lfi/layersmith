#!/usr/bin/env python3
"""Filter a JSONL training file: drop empty, over-long and duplicate records.

    python filter_dataset.py /datasets/train.jsonl --out /datasets/train.filtered.jsonl \
        --tokenizer /models/my-model --max-tokens 2048 --dedupe

Records are kept in their original order. A summary says how many were
dropped and why, so nothing disappears silently. Token lengths use the chat
template of the tokenizer you pass - use the tokenizer of the model you will
train, because different tokenizers split the same text very differently.
"""

import argparse
import json
from pathlib import Path


def text_of(record, tokenizer):
    if "messages" in record:
        return tokenizer.apply_chat_template(record["messages"], tokenize=False) if tokenizer else json.dumps(record)
    if "prompt" in record and "chosen" in record:
        prompt, chosen = record["prompt"], record["chosen"]
        if isinstance(prompt, list) and tokenizer:
            return tokenizer.apply_chat_template(prompt + chosen, tokenize=False)
        return json.dumps(prompt) + json.dumps(chosen)
    if "prompt" in record and "completion" in record:
        return record["prompt"] + record["completion"]
    return record.get("text", "")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("path", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--tokenizer", help="local tokenizer directory")
    parser.add_argument("--max-tokens", type=int, default=0)
    parser.add_argument("--min-chars", type=int, default=1)
    parser.add_argument("--dedupe", action="store_true", help="drop exact duplicate records")
    args = parser.parse_args()

    tokenizer = None
    if args.tokenizer:
        from transformers import AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(args.tokenizer, local_files_only=True)
    if args.max_tokens and not tokenizer:
        parser.error("--max-tokens needs --tokenizer")

    seen, kept, dropped = set(), 0, {"empty": 0, "too_long": 0, "duplicate": 0}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.path, encoding="utf-8") as source, open(args.out, "w", encoding="utf-8") as target:
        for line in source:
            if not line.strip():
                continue
            record = json.loads(line)
            text = text_of(record, tokenizer)
            if len(text.strip()) < args.min_chars:
                dropped["empty"] += 1
                continue
            if args.max_tokens and len(tokenizer(text, add_special_tokens=False).input_ids) > args.max_tokens:
                dropped["too_long"] += 1
                continue
            key = json.dumps(record, sort_keys=True)
            if args.dedupe and key in seen:
                dropped["duplicate"] += 1
                continue
            seen.add(key)
            target.write(json.dumps(record, ensure_ascii=False) + "\n")
            kept += 1
    print(f"kept {kept}, dropped {sum(dropped.values())} {dropped} -> {args.out}")


if __name__ == "__main__":
    main()
