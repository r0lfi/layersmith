#!/usr/bin/env python3
"""Supervised fine-tuning (SFT) with LoRA - or QLoRA with --qlora.

    # LoRA on one GPU, model and data from the mounted directories
    python /opt/layersmith/examples/sft_lora.py \
        --model /models/my-model --train /datasets/train.jsonl --val /datasets/validation.jsonl \
        --output /outputs/sft-lora-run1

    # QLoRA: the base model is loaded in 4-bit (needs an NVIDIA GPU)
    python /opt/layersmith/examples/sft_lora.py --qlora --model /models/my-model ... --output /outputs/sft-qlora-run1

    # Resume an interrupted run from its newest checkpoint
    python /opt/layersmith/examples/sft_lora.py ... --output /outputs/sft-lora-run1 --resume

    # Several GPUs in one server
    accelerate launch --config_file /opt/layersmith/examples/configs/multi_gpu.yaml \
        --num_processes 2 /opt/layersmith/examples/sft_lora.py ...

Data: JSONL with {"messages": [{"role": ..., "content": ...}, ...]} per line.
The model's own chat template turns each conversation into training text, so
use the tokenizer that belongs to the model. Validation data comes from its
own file (--val) and is only used for evaluation, never for training.

The result in --output is a LoRA adapter (adapter_model.safetensors) plus
adapter_provenance.json, which records the base model and revision the adapter
belongs to. Checkpoints are written to --output/checkpoint-*.

--offline (or HF_HUB_OFFLINE=1) refuses all network access: the model, its
tokenizer and the data must then be local.
"""

import argparse
import os
import sys

from common import describe_model, device_settings, last_checkpoint, load_split, set_offline, write_provenance


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", required=True, help="local directory (/models/...) or Hugging Face model id")
    parser.add_argument("--revision", help="model revision (commit, tag or branch) when using a Hub id")
    parser.add_argument("--train", required=True, help="training file (.jsonl, .csv or .parquet)")
    parser.add_argument("--val", help="separate validation file")
    parser.add_argument("--output", required=True, help="where the adapter and checkpoints go, e.g. /outputs/run1")
    parser.add_argument("--qlora", action="store_true", help="load the base model in 4-bit (bitsandbytes)")
    parser.add_argument("--resume", action="store_true", help="continue from the newest checkpoint in --output")
    parser.add_argument("--offline", action="store_true", help="no network access; everything must be local")
    parser.add_argument("--cpu", action="store_true", help="force CPU (function tests)")
    parser.add_argument("--epochs", type=float, default=1.0)
    parser.add_argument("--max-steps", type=int, default=-1, help="stop after this many steps (overrides epochs)")
    parser.add_argument("--lr", type=float, default=2e-4)
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--grad-accum", type=int, default=8)
    parser.add_argument("--max-length", type=int, default=1024, help="tokens per example; memory grows with it")
    parser.add_argument("--lora-r", type=int, default=16)
    parser.add_argument("--lora-alpha", type=int, default=32)
    parser.add_argument("--lora-dropout", type=float, default=0.05)
    parser.add_argument("--target-modules", default="all-linear", help="'all-linear' or a comma-separated list")
    parser.add_argument("--save-steps", type=int, default=100)
    parser.add_argument("--attn", default="sdpa", help="sdpa (default), eager or flash_attention_2")
    parser.add_argument("--gradient-checkpointing", action="store_true", help="less memory, slower steps")
    parser.add_argument("--report-to", default="tensorboard", help="tensorboard or none")
    parser.add_argument("--logging-dir", default="/logs", help="TensorBoard log directory")
    parser.add_argument("--seed", type=int, default=42)
    return parser.parse_args()


def main():
    args = parse_args()
    set_offline(args.offline)
    run_name = os.path.basename(os.path.normpath(args.output))
    os.environ.setdefault("TENSORBOARD_LOGGING_DIR", os.path.join(args.logging_dir, run_name))

    import torch
    from peft import LoraConfig, prepare_model_for_kbit_training
    from transformers import AutoConfig, AutoModelForCausalLM, AutoTokenizer
    from trl import SFTConfig, SFTTrainer

    use_cpu, bf16, fp16 = device_settings(args.cpu)
    if args.qlora and use_cpu:
        sys.exit("QLoRA needs an NVIDIA GPU: 4-bit training in bitsandbytes runs on CUDA. "
                 "Use plain LoRA (without --qlora) for a CPU function test.")

    local_only = args.offline or os.environ.get("HF_HUB_OFFLINE") == "1"
    load = {"revision": args.revision, "local_files_only": local_only}
    config = AutoConfig.from_pretrained(args.model, **load)
    tokenizer = AutoTokenizer.from_pretrained(args.model, **load)
    if tokenizer.chat_template is None:
        sys.exit(f"{args.model} has no chat template. Use an instruction/chat model's tokenizer, "
                 "or train on plain text with train_causal_lm.py.")
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    model_kwargs = {"attn_implementation": args.attn, **load}
    if args.qlora:
        from transformers import BitsAndBytesConfig

        # NF4 base weights, frozen; computation in bf16 where the GPU supports it.
        model_kwargs["quantization_config"] = BitsAndBytesConfig(
            load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_use_double_quant=True,
            bnb_4bit_compute_dtype=torch.bfloat16 if bf16 else torch.float16)
    elif not use_cpu:
        model_kwargs["dtype"] = torch.bfloat16 if bf16 else torch.float16
    model = AutoModelForCausalLM.from_pretrained(args.model, **model_kwargs)
    if args.qlora:
        model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=args.gradient_checkpointing)

    train = load_split(args.train)
    validation = load_split(args.val)

    peft_config = LoraConfig(
        r=args.lora_r, lora_alpha=args.lora_alpha, lora_dropout=args.lora_dropout, task_type="CAUSAL_LM",
        target_modules="all-linear" if args.target_modules == "all-linear" else args.target_modules.split(","),
    )
    training_args = SFTConfig(
        output_dir=args.output, run_name=run_name, seed=args.seed,
        num_train_epochs=args.epochs, max_steps=args.max_steps, learning_rate=args.lr,
        per_device_train_batch_size=args.batch_size, gradient_accumulation_steps=args.grad_accum,
        max_length=args.max_length, gradient_checkpointing=args.gradient_checkpointing,
        logging_steps=1, save_steps=args.save_steps, save_total_limit=3,
        eval_strategy="steps" if validation is not None else "no", eval_steps=args.save_steps,
        report_to=[] if args.report_to == "none" else [args.report_to],
        use_cpu=use_cpu, bf16=bf16, fp16=fp16,
    )
    trainer = SFTTrainer(model=model, args=training_args, train_dataset=train, eval_dataset=validation,
                         processing_class=tokenizer, peft_config=peft_config)
    trainer.model.print_trainable_parameters()

    resume = last_checkpoint(args.output) if args.resume else None
    if args.resume and not resume:
        print(f"No checkpoint in {args.output} yet; starting from the beginning.")
    trainer.train(resume_from_checkpoint=resume)
    if validation is not None:
        print("validation:", trainer.evaluate())

    trainer.save_model(args.output)  # the adapter, not the full model
    tokenizer.save_pretrained(args.output)
    provenance = write_provenance(
        args.output, base=describe_model(args.model, args.revision, config),
        method="qlora" if args.qlora else "lora", task="sft",
        data={"train": args.train, "validation": args.val}, args=vars(args))
    print(f"Adapter saved to {args.output} ({provenance.name} records its base model)")


if __name__ == "__main__":
    main()
