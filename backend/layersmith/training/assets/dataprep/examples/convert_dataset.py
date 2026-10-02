#!/usr/bin/env python3
"""Convert a dataset between JSONL, CSV and Parquet. Formats follow the file extensions.

    python convert_dataset.py /datasets/train.jsonl /datasets/train.parquet
    python convert_dataset.py /datasets/table.csv   /datasets/table.jsonl

Nested fields (such as "messages") survive JSONL <-> Parquet. CSV is flat:
nested fields are written as JSON text, and a CSV is read back as plain
columns - prefer JSONL or Parquet for chat and preference data.
"""

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

READERS = {".jsonl": lambda p: pd.read_json(p, lines=True), ".csv": pd.read_csv, ".parquet": pd.read_parquet}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    args = parser.parse_args()
    reader = READERS.get(args.source.suffix.lower())
    if reader is None or args.target.suffix.lower() not in READERS:
        sys.exit("Use .jsonl, .csv or .parquet for both files")
    frame = reader(args.source)
    suffix = args.target.suffix.lower()
    args.target.parent.mkdir(parents=True, exist_ok=True)
    if suffix == ".jsonl":
        frame.to_json(args.target, orient="records", lines=True, force_ascii=False)
    elif suffix == ".parquet":
        frame.to_parquet(args.target, index=False)
    else:
        flat = frame.copy()
        for column in flat.columns:
            if flat[column].map(lambda v: isinstance(v, (list, dict))).any():
                flat[column] = flat[column].map(lambda v: json.dumps(v, ensure_ascii=False, default=str))
        flat.to_csv(args.target, index=False)
    print(f"{len(frame)} rows, columns {list(frame.columns)}: {args.source} -> {args.target}")


if __name__ == "__main__":
    main()
