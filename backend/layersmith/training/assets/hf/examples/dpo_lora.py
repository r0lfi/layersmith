#!/usr/bin/env python3
"""Preference tuning with DPO, training a LoRA adapter.

    python /opt/layersmith/examples/dpo_lora.py \
        --model /models/my-sft-model --train /datasets/preferences.jsonl --output /outputs/dpo-run1

Data: JSONL with one preference example per line - the same prompt with a
preferred and a less wanted answer (the conversational format TRL documents):

  {"prompt":   [{"role": "user", "content": "..."}],
   "chosen":   [{"role": "assistant", "content": "the better answer"}],
   "rejected": [{"role": "assistant", "content": "the worse answer"}]}

DPO is usually run after SFT, on a model that already follows instructions.
With a LoRA adapter the frozen base model doubles as the reference model, so
no second copy of the model is loaded.

Add --qlora to load the base in 4-bit (NVIDIA GPU only), --offline to refuse
network access, --resume to continue from the newest checkpoint.
"""

import argparse
import os
import sys

from common import describe_model, device_settings, last_checkpoint, load_split, set_offline, write_provenance


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", required=True)
    parser.add_argument("--revision")
    parser.add_argument("--train", required=True)
    parser.add_argument("--val")
    parser.add_argument("--output", required=True)
    parser.add_argument("--qlora", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--cpu", action="store_true")
    parser.add_argument("--beta", type=float, default=0.1, help="how far the model may move from the reference")
    parser.add_argument("--epochs", type=float, default=1.0)
    parser.add_argument("--max-steps", type=int, default=-1)
    parser.add_argument("--lr", type=float, default=5e-6)
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--grad-accum", type=int, default=8)
    parser.add_argument("--max-length", type=int, default=1024)
    parser.add_argument("--lora-r", type=int, default=16)
    parser.add_argument("--save-steps", type=int, default=100)
    parser.add_argument("--report-to", default="tensorboard")
    parser.add_argument("--logging-dir", default="/logs")
    args = parser.parse_args()
    set_offline(args.offline)
    run_name = os.path.basename(os.path.normpath(args.output))
    os.environ.setdefault("TENSORBOARD_LOGGING_DIR", os.path.join(args.logging_dir, run_name))

    import torch
    from peft import LoraConfig, prepare_model_for_kbit_training
    from transformers import AutoConfig, AutoModelForCausalLM, AutoTokenizer
    from trl import DPOConfig, DPOTrainer

    use_cpu, bf16, fp16 = device_settings(args.cpu)
    if args.qlora and use_cpu:
        sys.exit("QLoRA needs an NVIDIA GPU. Drop --qlora for a CPU function test.")
    load = {"revision": args.revision, "local_files_only": args.offline or os.environ.get("HF_HUB_OFFLINE") == "1"}
    config = AutoConfig.from_pretrained(args.model, **load)
    tokenizer = AutoTokenizer.from_pretrained(args.model, **load)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    model_kwargs = dict(load)
    if args.qlora:
        from transformers import BitsAndBytesConfig

        model_kwargs["quantization_config"] = BitsAndBytesConfig(
            load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_use_double_quant=True,
            bnb_4bit_compute_dtype=torch.bfloat16 if bf16 else torch.float16)
    elif not use_cpu:
        model_kwargs["dtype"] = torch.bfloat16 if bf16 else torch.float16
    model = AutoModelForCausalLM.from_pretrained(args.model, **model_kwargs)
    if args.qlora:
        model = prepare_model_for_kbit_training(model)

    train, validation = load_split(args.train), load_split(args.val)
    training_args = DPOConfig(
        output_dir=args.output, run_name=run_name, beta=args.beta,
        num_train_epochs=args.epochs, max_steps=args.max_steps, learning_rate=args.lr,
        per_device_train_batch_size=args.batch_size, gradient_accumulation_steps=args.grad_accum,
        max_length=args.max_length, logging_steps=1, save_steps=args.save_steps, save_total_limit=3,
        eval_strategy="steps" if validation is not None else "no", eval_steps=args.save_steps,
        report_to=[] if args.report_to == "none" else [args.report_to],
        use_cpu=use_cpu, bf16=bf16, fp16=fp16,
    )
    trainer = DPOTrainer(model=model, args=training_args, train_dataset=train, eval_dataset=validation,
                         processing_class=tokenizer,
                         peft_config=LoraConfig(r=args.lora_r, lora_alpha=2 * args.lora_r,
                                                target_modules="all-linear", task_type="CAUSAL_LM"))
    trainer.train(resume_from_checkpoint=last_checkpoint(args.output) if args.resume else None)
    trainer.save_model(args.output)
    tokenizer.save_pretrained(args.output)
    write_provenance(args.output, base=describe_model(args.model, args.revision, config),
                     method="qlora" if args.qlora else "lora", task="dpo",
                     data={"train": args.train, "validation": args.val}, args=vars(args))
    print(f"DPO adapter saved to {args.output}")


if __name__ == "__main__":
    main()
