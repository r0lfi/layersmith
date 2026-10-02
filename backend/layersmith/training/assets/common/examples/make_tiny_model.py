#!/usr/bin/env python3
"""Create a tiny, randomly initialised model and tokenizer - for FUNCTION TESTS ONLY.

    python make_tiny_model.py /tmp/tiny-model
    python make_tiny_model.py /tmp/tiny-tokenizer --tokenizer-only   # no PyTorch needed

Everything is made locally, without network access: a byte-level BPE tokenizer
trained on the bundled synthetic samples, with a ChatML-style chat template,
and (unless --tokenizer-only) a Llama-architecture model with about 0.14M
parameters. It lets every example and check run end to end offline.

A model made this way produces nonsense. Training it shows that the tooling
works - data loads, loss is computed, gradients flow, checkpoints save and
load - and nothing about model quality.
"""

import argparse
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPECIAL = ["<|pad|>", "<|bos|>", "<|eos|>", "<|im_start|>", "<|im_end|>"]

# ChatML-style template. {% generation %} marks the assistant text, so TRL's
# assistant_only_loss option works with it too.
CHAT_TEMPLATE = (
    "{% for message in messages %}"
    "{{ '<|im_start|>' + message['role'] + '\n' }}"
    "{% if message['role'] == 'assistant' %}{% generation %}{{ message['content'] + '<|im_end|>' }}"
    "{% endgeneration %}{% else %}{{ message['content'] + '<|im_end|>' }}{% endif %}"
    "{{ '\n' }}"
    "{% endfor %}"
    "{% if add_generation_prompt %}{{ '<|im_start|>assistant\n' }}{% endif %}"
)


def corpus():
    """Text from the bundled samples, so common words become single tokens."""
    for path in sorted((HERE / "data").glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            for key in ("messages", "prompt", "chosen", "rejected"):
                for message in record.get(key) or []:
                    yield message["content"]
            if "text" in record:
                yield record["text"]
    yield "abcdefghijklmnopqrstuvwxyz ABCDEFGHIJKLMNOPQRSTUVWXYZ 0123456789 .,;:!?'\"-()"


def make_tokenizer(output: Path):
    from tokenizers import Tokenizer, decoders, models, pre_tokenizers, trainers
    from transformers import PreTrainedTokenizerFast

    tokenizer = Tokenizer(models.BPE())
    tokenizer.pre_tokenizer = pre_tokenizers.ByteLevel(add_prefix_space=False)
    tokenizer.decoder = decoders.ByteLevel()
    trainer = trainers.BpeTrainer(vocab_size=512, special_tokens=SPECIAL,
                                  initial_alphabet=pre_tokenizers.ByteLevel.alphabet())
    tokenizer.train_from_iterator(corpus(), trainer=trainer)

    wrapped = PreTrainedTokenizerFast(
        tokenizer_object=tokenizer, pad_token="<|pad|>", bos_token="<|bos|>", eos_token="<|im_end|>",
        additional_special_tokens=["<|im_start|>", "<|eos|>"], model_max_length=256,
    )
    wrapped.chat_template = CHAT_TEMPLATE
    wrapped.save_pretrained(output)
    return wrapped


def make_model(output: Path, tokenizer, seed: int):
    import torch
    from transformers import LlamaConfig, LlamaForCausalLM

    torch.manual_seed(seed)
    config = LlamaConfig(
        vocab_size=len(tokenizer), hidden_size=64, intermediate_size=128, num_hidden_layers=2,
        num_attention_heads=4, num_key_value_heads=2, max_position_embeddings=256,
        bos_token_id=tokenizer.bos_token_id, eos_token_id=tokenizer.eos_token_id,
        pad_token_id=tokenizer.pad_token_id, tie_word_embeddings=False,
    )
    model = LlamaForCausalLM(config)
    model.generation_config.pad_token_id = tokenizer.pad_token_id
    model.save_pretrained(output, safe_serialization=True)
    return model


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("output", type=Path)
    parser.add_argument("--tokenizer-only", action="store_true", help="only the tokenizer (no PyTorch needed)")
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()

    args.output.mkdir(parents=True, exist_ok=True)
    tokenizer = make_tokenizer(args.output)
    if not args.tokenizer_only:
        model = make_model(args.output, tokenizer, args.seed)
        parameters = sum(p.numel() for p in model.parameters())
        print(f"Tiny model ({parameters:,} parameters) and tokenizer written to {args.output}")
    else:
        print(f"Tiny tokenizer written to {args.output}")
    (args.output / "FUNCTION-TEST-ONLY.txt").write_text(
        "Randomly initialised model/tokenizer made by LayerSmith's make_tiny_model.py.\n"
        "For function tests only: it produces nonsense and says nothing about model quality.\n")


if __name__ == "__main__":
    main()
