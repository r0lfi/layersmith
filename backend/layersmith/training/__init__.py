"""LLM training and fine-tuning environments.

A training profile is a purpose ("fine-tune an existing model with LoRA"),
a hardware target ("NVIDIA GPU, Linux x86_64") and a software stack whose
Python packages are locked with hashes. LayerSmith builds and documents the
environment; the training itself runs in the finished image on the user's
own machine.

  catalog     profiles, tools, concepts, targets, stacks, add-ons (data)
  recipes     resolve a request into one concrete, validated recipe
  render      the Containerfile section and build-context files
  checks      what is verified after a build, and how results are recorded
  docs        Getting started and the documentation in an export
"""
