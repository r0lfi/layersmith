# Sample data - function tests only

These files are **synthetic** and tiny. They exist so the examples and the
LayerSmith checks can run end to end without network access. Training on them
shows that the tooling works (data loads, the loss is computed, gradients
flow, checkpoints save and load); it says nothing about model quality.

| File | Format | Used by |
| --- | --- | --- |
| `sample_chat.jsonl` | `{"messages": [{"role", "content"}, ...]}` per line | SFT (training split) |
| `sample_chat_val.jsonl` | same | SFT (validation split, kept separate) |
| `sample_preference.jsonl` | `{"prompt": [...], "chosen": [...], "rejected": [...]}` | DPO |
| `sample_text.jsonl` | `{"text": "..."}` | continued pretraining |

Replace them with your own files under `/datasets` when you run the container.
