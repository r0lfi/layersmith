#!/usr/bin/env python3
"""Full fine-tuning and continued pretraining of a causal language model.

Every parameter is trained, so this needs much more GPU memory than LoRA for
the same model. Typical uses:

    # Continued pretraining on your own plain text ({"text": ...} per line), one GPU
    python /opt/layersmith/examples/train_causal_lm.py \
        --model /models/my-model --train /datasets/corpus.jsonl --output /outputs/cpt-run1

    # The same on several GPUs in one server, with FSDP
    accelerate launch --config_file /opt/layersmith/examples/configs/fsdp.yaml --num_processes 4 \
        /opt/layersmith/examples/train_causal_lm.py --model /models/my-model ...

    # With DeepSpeed ZeRO-3 and CPU offload (DeepSpeed add-on)
    accelerate launch --config_file /opt/layersmith/examples/configs/deepspeed.yaml --num_processes 4 \
        /opt/layersmith/examples/train_causal_lm.py --model /models/my-model ...

    # Pretraining from scratch: a FUNCTION TEST / learning exercise, not a way to make a useful model
    python /opt/layersmith/examples/train_causal_lm.py --from-scratch --model /models/tiny ...

Data: {"text": "..."} records are concatenated and cut into --max-length
blocks (the usual pretraining layout). {"messages": [...]} records are
rendered with the tokenizer's chat template instead (full-parameter SFT).
Validation data (--val) stays separate and is only evaluated.

Checkpoints go to --output/checkpoint-*; --resume continues from the newest.
"""

import argparse
import os
from itertools import chain

from common import describe_model, device_settings, last_checkpoint, load_split, set_offline, write_provenance


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", required=True, help="local directory or Hub id (with --from-scratch: config only)")
    parser.add_argument("--revision")
    parser.add_argument("--train", required=True)
    parser.add_argument("--val")
    parser.add_argument("--output", required=True)
    parser.add_argument("--from-scratch", action="store_true", help="random weights from the model's config")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--cpu", action="store_true")
    parser.add_argument("--epochs", type=float, default=1.0)
    parser.add_argument("--max-steps", type=int, default=-1)
    parser.add_argument("--lr", type=float, default=1e-5)
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--grad-accum", type=int, default=16)
    parser.add_argument("--max-length", type=int, default=2048)
    parser.add_argument("--save-steps", type=int, default=200)
    parser.add_argument("--attn", default="sdpa", help="sdpa, eager or flash_attention_2 (FlashAttention add-on)")
    parser.add_argument("--gradient-checkpointing", action="store_true")
    parser.add_argument("--deepspeed", help="DeepSpeed JSON config, when not launched through an Accelerate config")
    parser.add_argument("--report-to", default="tensorboard")
    parser.add_argument("--logging-dir", default="/logs")
    args = parser.parse_args()
    set_offline(args.offline)
    run_name = os.path.basename(os.path.normpath(args.output))
    os.environ.setdefault("TENSORBOARD_LOGGING_DIR", os.path.join(args.logging_dir, run_name))

    import torch
    from transformers import (AutoConfig, AutoModelForCausalLM, AutoTokenizer, DataCollatorForLanguageModeling,
                              Trainer, TrainingArguments)

    use_cpu, bf16, fp16 = device_settings(args.cpu)
    load = {"revision": args.revision, "local_files_only": args.offline or os.environ.get("HF_HUB_OFFLINE") == "1"}
    config = AutoConfig.from_pretrained(args.model, **load)
    tokenizer = AutoTokenizer.from_pretrained(args.model, **load)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    if args.from_scratch:
        print("Pretraining from scratch: random weights. Treat the result as a function test.")
        model = AutoModelForCausalLM.from_config(config, attn_implementation=args.attn)
    else:
        model = AutoModelForCausalLM.from_pretrained(
            args.model, attn_implementation=args.attn, **load,
            **({} if use_cpu else {"dtype": torch.bfloat16 if bf16 else torch.float32}))

    def tokenize(batch):
        if "messages" in batch:
            texts = [tokenizer.apply_chat_template(m, tokenize=False) for m in batch["messages"]]
        else:
            texts = [t + tokenizer.eos_token for t in batch["text"]]
        return tokenizer(texts, add_special_tokens=False)

    def blocks(batch):
        # Concatenate, then cut into equal blocks: no padding wasted on text data.
        joined = {k: list(chain.from_iterable(batch[k])) for k in ("input_ids", "attention_mask")}
        size = (len(joined["input_ids"]) // args.max_length) * args.max_length
        return {k: [v[i:i + args.max_length] for i in range(0, size, args.max_length)] for k, v in joined.items()}

    def prepare(dataset):
        if dataset is None:
            return None
        tokenized = dataset.map(tokenize, batched=True, remove_columns=dataset.column_names)
        if "messages" in dataset.column_names:
            return tokenized.filter(lambda r: len(r["input_ids"]) <= args.max_length)
        grouped = tokenized.map(blocks, batched=True)
        if len(grouped) == 0:
            raise SystemExit(f"Not enough text for one block of {args.max_length} tokens; lower --max-length.")
        return grouped

    train, validation = prepare(load_split(args.train)), prepare(load_split(args.val))
    training_args = TrainingArguments(
        output_dir=args.output, run_name=run_name,
        num_train_epochs=args.epochs, max_steps=args.max_steps, learning_rate=args.lr,
        per_device_train_batch_size=args.batch_size, gradient_accumulation_steps=args.grad_accum,
        gradient_checkpointing=args.gradient_checkpointing, deepspeed=args.deepspeed,
        logging_steps=1, save_steps=args.save_steps, save_total_limit=2,
        eval_strategy="steps" if validation is not None else "no", eval_steps=args.save_steps,
        report_to=[] if args.report_to == "none" else [args.report_to],
        use_cpu=use_cpu, bf16=bf16, fp16=fp16,
    )
    trainer = Trainer(model=model, args=training_args, train_dataset=train, eval_dataset=validation,
                      data_collator=DataCollatorForLanguageModeling(tokenizer, mlm=False))
    trainer.train(resume_from_checkpoint=last_checkpoint(args.output) if args.resume else None)
    trainer.save_model(args.output)
    tokenizer.save_pretrained(args.output)
    write_provenance(args.output, base=describe_model(args.model, args.revision, config),
                     method="from-scratch" if args.from_scratch else "full", task="causal-lm",
                     data={"train": args.train, "validation": args.val}, args=vars(args))
    print(f"Model saved to {args.output}")


if __name__ == "__main__":
    main()
