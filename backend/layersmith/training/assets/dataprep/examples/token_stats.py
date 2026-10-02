#!/usr/bin/env python3
"""Token length statistics for a dataset, with the tokenizer of the model you will train.

    python token_stats.py /datasets/train.jsonl --tokenizer /models/my-model
    python token_stats.py /datasets/train.parquet --tokenizer /models/my-model --max-length 2048

The numbers help choose --max-length for training: memory use grows with it,
and examples longer than it are truncated or dropped by the trainer.
"""

import argparse
from pathlib import Path

import numpy as np
from datasets import load_dataset
from transformers import AutoTokenizer


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("path", type=Path)
    parser.add_argument("--tokenizer", required=True)
    parser.add_argument("--max-length", type=int, default=0)
    args = parser.parse_args()

    kind = {".jsonl": "json", ".json": "json", ".csv": "csv", ".parquet": "parquet"}[args.path.suffix.lower()]
    dataset = load_dataset(kind, data_files=str(args.path), split="train")
    tokenizer = AutoTokenizer.from_pretrained(args.tokenizer, local_files_only=True)

    def length(record):
        if record.get("messages"):
            text = tokenizer.apply_chat_template(record["messages"], tokenize=False)
            return len(tokenizer(text, add_special_tokens=False).input_ids)
        if record.get("text"):
            return len(tokenizer(record["text"]).input_ids)
        return len(tokenizer(str(record)).input_ids)

    lengths = np.array([length(record) for record in dataset])
    print(f"{len(lengths)} records, {int(lengths.sum()):,} tokens in total")
    for name, value in (("min", lengths.min()), ("p50", np.percentile(lengths, 50)),
                        ("p90", np.percentile(lengths, 90)), ("p99", np.percentile(lengths, 99)),
                        ("max", lengths.max())):
        print(f"  {name:>4}: {int(value)}")
    if args.max_length:
        over = int((lengths > args.max_length).sum())
        print(f"  {over} records ({over / len(lengths):.1%}) are longer than {args.max_length} tokens")


if __name__ == "__main__":
    main()
