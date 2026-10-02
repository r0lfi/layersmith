"""LLM training profiles: recipes, conflicts, generation, checks, export."""

import itertools
import json
import tarfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

from layersmith.core import containerfile as cf
from layersmith.core.spec import InvalidSpec, validate_spec
from layersmith.models import Build, create_all
from layersmith.training import catalog, checks, docs, recipes, render
from tests.test_api import client, run_pending_build  # noqa: F401  (fixture)
from tests.test_builds import FakeBackend

IMAGE = "layersmith/llm:1.0.0"


def result_line(check, status="passed", image=IMAGE):
    return checks.RESULT_PREFIX + json.dumps({
        "check": check, "status": status, "image": image, "finished": "2026-10-02T12:00:00Z",
        "steps": [{"name": "step", "status": status, "detail": "ok" if status == "passed" else "broke"}],
        "info": {"devices": [{"index": 0, "name": "Test GPU", "capability": "8.6", "memory_gb": 16}],
                 "driver": "580.1"} if check == "gpu" else {}})


class CheckingBackend(FakeBackend):
    """A fake backend that can also run a container, answering like layersmith-check."""

    def __init__(self, failing=()):
        super().__init__()
        self.failing = set(failing)
        self.runs = []

    def run_container(self, image_ref, argv, on_log, timeout=1800, network="none"):
        self.runs.append((image_ref, tuple(argv), network))
        check = argv[-1]
        line = result_line(check, "failed" if check in self.failing else "passed", image_ref)
        on_log(line)
        return (1 if check in self.failing else 0), line


# ------------------------------------------------------------ recipes

def test_every_profile_resolves_with_its_defaults():
    for profile in catalog.PROFILES:
        recipe = recipes.resolve({"profile": profile["id"], "addons": profile["default_addons"]})
        assert recipe["base"]["digest"].startswith("sha256:")
        assert recipe["versions"], profile["id"]
        assert all(tool["version"] for tool in recipe["tools"] if tool["required"]), profile["id"]


def test_every_addon_combination_merges_without_version_conflicts():
    for profile in catalog.PROFILES:
        for stack in profile["stacks"]:
            offered = [a for a in profile["addons"] if stack in catalog.ADDONS[a].get("stacks", [stack])]
            for size in range(len(offered) + 1):
                for combo in itertools.combinations(offered, size):
                    recipes.merged_lock(stack, list(combo))  # raises on a double pin


def test_locks_are_fully_hashed():
    for stack in catalog.STACKS:
        for path in (recipes.STACK_DIR / stack).glob("*.lock"):
            for name, block in recipes.lock_blocks(stack, path.stem):
                assert "--hash=sha256:" in block, f"{path.name}: {name} has no hash"


def test_core_versions_are_the_locked_ones():
    versions = recipes.resolve({"profile": "hf-finetune"})["versions"]
    assert versions["torch"] == "2.13.0+cu129"
    assert versions["transformers"] == "5.17.0" and versions["trl"] == "1.13.0" and versions["peft"] == "0.21.0"
    llama = recipes.resolve({"profile": "llama-factory"})["versions"]
    assert llama["llamafactory"] == "0.9.5" and llama["torch"] == "2.6.0+cu124"


def test_deepspeed_switches_to_the_cuda_development_base_and_says_why():
    recipe = recipes.resolve({"profile": "pytorch-advanced", "addons": ["deepspeed"]})
    assert recipe["base"]["image"] == "docker.io/nvidia/cuda" and "devel" in recipe["base"]["tag"]
    assert "nvcc" in recipe["base"]["reason"]
    assert "deepspeed" in recipe["compiled"]
    plain = recipes.resolve({"profile": "pytorch-advanced"})
    assert plain["base"]["image"] == "docker.io/library/ubuntu"


@pytest.mark.parametrize("request_, alternative", [
    ({"profile": "pytorch-advanced", "addons": ["flash-attn"]}, {"stack": "fa2-cu128"}),
    ({"profile": "hf-finetune", "target": "cpu"}, {"profile": "dataset-prep", "target": "cpu"}),
    ({"profile": "dataset-prep", "target": "nvidia-cuda"}, {"profile": "hf-finetune", "target": "nvidia-cuda"}),
    ({"profile": "hf-finetune", "addons": ["deepspeed"]}, {"addons": []}),
    ({"profile": "hf-finetune", "extra_python": ["torch==2.5.0"]}, {"extra_python": []}),
])
def test_conflicts_explain_and_offer_a_working_alternative(request_, alternative):
    with pytest.raises(recipes.TrainingConflict) as raised:
        recipes.resolve(request_)
    assert raised.value.alternative == alternative
    fixed = {**request_, **alternative}
    recipes.resolve(fixed)  # the alternative really works


def test_architecture_without_a_recipe_is_refused():
    with pytest.raises(recipes.TrainingConflict) as raised:
        recipes.resolve({"profile": "hf-finetune"}, architecture="arm64")
    assert raised.value.alternative == {"architecture": "amd64"}


def test_extra_python_must_be_pinned():
    with pytest.raises(recipes.TrainingConflict):
        recipes.resolve({"profile": "hf-finetune", "extra_python": ["einops"]})
    recipe = recipes.resolve({"profile": "hf-finetune", "extra_python": ["einops==0.8.2"]})
    assert recipe["extra_python"] == ["einops==0.8.2"]
    assert any("do not apply" in note for note in recipe["notes"])


def test_recommendation_follows_the_stated_purpose():
    assert recipes.recommend({"task": "data-prep"})["profile"] == "dataset-prep"
    assert recipes.recommend({"task": "continued-pretraining"})["profile"] == "pytorch-advanced"
    assert recipes.recommend({"task": "sft", "method": "qlora"})["profile"] == "hf-finetune"


# --------------------------------------------------------------- spec

def test_spec_takes_base_and_architecture_from_the_recipe():
    spec = validate_spec({"training": {"profile": "hf-finetune"}, "base": {"distribution": "Fedora", "version": "44"}})
    assert spec["base"]["source"].startswith("docker.io/library/ubuntu@sha256:")
    assert spec["architecture"] == "amd64"
    assert spec["training"]["stack"] == "hf-cu129"


def test_spec_refuses_a_custom_user_for_training_images():
    with pytest.raises(InvalidSpec):
        validate_spec({"training": {"profile": "hf-finetune"}, "user": {"name": "bob"}})


def test_customization_is_detected():
    assert not recipes.is_customized(validate_spec({"training": {"profile": "hf-finetune"}}))
    assert recipes.is_customized(validate_spec({"training": {"profile": "hf-finetune"}, "extra_packages": ["htop"]}))
    assert recipes.is_customized(validate_spec({"training": {"profile": "hf-finetune",
                                                             "extra_python": ["einops==0.8.2"]}}))


# -------------------------------------------------------- containerfile

def test_containerfile_installs_locked_packages_and_runs_unprivileged():
    spec = validate_spec({"training": {"profile": "hf-finetune", "addons": ["tensorboard", "jupyter"]}})
    text, packages, _ = cf.generate({"name": "llm"}, spec, base_digest="sha256:" + "d" * 64, image_ref=IMAGE)
    assert "FROM docker.io/library/ubuntu@sha256:" + "d" * 64 in text
    assert "--require-hashes --no-deps --only-binary=:all:" in text
    assert "pip check" in text
    assert "USER trainer" in text and "WORKDIR /workspace" in text
    assert "EXPOSE 6006 8888" in text
    assert text.rstrip().endswith(f'ENV LAYERSMITH_IMAGE="{IMAGE}"')
    assert 'HF_HUB_DISABLE_TELEMETRY="1"' in text and 'WANDB_MODE="disabled"' in text
    assert "python3-venv" in packages and "gcc" in packages
    assert "TOKEN" not in text.replace("HF_HUB_DISABLE_TELEMETRY", "")


def test_source_only_packages_are_built_in_their_own_step():
    spec = validate_spec({"training": {"profile": "pytorch-advanced", "addons": ["deepspeed"]}})
    text, _, _ = cf.generate({"name": "llm"}, spec)
    assert "--no-build-isolation -r /opt/layersmith/requirements-source.lock" in text
    assert 'DS_BUILD_OPS="0"' in text


def test_cpu_profile_has_no_compiler_and_no_torch():
    spec = validate_spec({"training": {"profile": "dataset-prep"}})
    _, packages, _ = cf.generate({"name": "data"}, spec)
    assert "gcc" not in packages
    assert "torch" not in recipes.resolve(spec["training"])["versions"]


def test_context_contains_locks_recipe_examples_and_checks(tmp_path):
    recipe = recipes.resolve({"profile": "pytorch-advanced", "addons": ["deepspeed"]})
    written = render.write_context(tmp_path, recipe, "readme")
    opt = tmp_path / "training" / "opt"
    assert (tmp_path / "training" / "requirements.lock").read_text().count("--hash=") > 50
    assert "deepspeed==" in (tmp_path / "training" / "requirements-source.lock").read_text()
    assert json.loads((opt / "recipe.json").read_text())["locked"]["torch"] == "2.13.0+cu129"
    for name in ("bin/layersmith-check", "checks/run.py", "examples/sft_lora.py", "examples/train_causal_lm.py",
                 "examples/configs/fsdp.yaml", "examples/data/sample_chat.jsonl"):
        assert (opt / name).is_file(), name
    assert not any("__pycache__" in path for path in written)


# ------------------------------------------------------------- checks

def test_gpu_status_is_not_run_until_reported():
    recipe = recipes.resolve({"profile": "hf-finetune"})
    rows = checks.summarise("ready", recipe, {"results": {}}, IMAGE, False)
    assert [r["id"] for r in rows] == ["image", "deps", "cpu", "gpu", "offline"]
    gpu = next(r for r in rows if r["id"] == "gpu")
    assert gpu["label"] == "GPU test not run" and IMAGE in gpu["command"]["docker"]


def test_cpu_profile_has_no_gpu_row():
    recipe = recipes.resolve({"profile": "dataset-prep"})
    assert "gpu" not in [r["id"] for r in checks.summarise("ready", recipe, None, IMAGE, False)]


def test_gpu_report_must_come_from_the_same_image():
    with pytest.raises(checks.InvalidReport):
        checks.validate_gpu_report(result_line("gpu", image="other/image:1.0.0"), IMAGE)
    with pytest.raises(checks.InvalidReport):
        checks.validate_gpu_report(result_line("cpu"), IMAGE)
    recorded = checks.validate_gpu_report("noise before\n" + result_line("gpu"), IMAGE)
    assert recorded["status"] == "passed" and recorded["source"] == "reported"
    assert recorded["info"]["devices"][0]["name"] == "Test GPU"


# ---------------------------------------------------- build service + API

def create_training_project(client, training, **extra):  # noqa: F811
    response = client.post("/api/projects", json={"name": "llm", "template": "LLM", "spec": {"training": training,
                                                                                             **extra}})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_training_build_runs_checks_offline_and_documents_the_export(client):  # noqa: F811
    backend = CheckingBackend(failing={"offline"})
    client.state["build_service"].backend = backend
    project = create_training_project(client, {"profile": "hf-finetune"})
    build = client.post(f"/api/projects/{project}/builds", json={}).json()
    run_pending_build(client, build["id"])

    assert [run[1][-1] for run in backend.runs] == ["deps", "cpu", "offline"]
    assert all(run[2] == "none" for run in backend.runs)
    detail = client.get(f"/api/builds/{build['id']}").json()
    assert detail["status"] == "ready"  # a failed check is a result, not a failed build
    labels = {row["id"]: row["label"] for row in detail["training"]["checks"]}
    assert labels == {"image": "Image built", "deps": "Dependencies checked", "cpu": "CPU smoke test passed",
                      "gpu": "GPU test not run", "offline": "Offline example failed"}
    assert detail["manifest"]["training"]["checks"]["deps"] == "passed"
    sections = {s["id"]: s for s in detail["training"]["getting_started"]["sections"]}
    assert detail["image_ref"] in sections["shell"]["docker"] and "--gpus all" in sections["shell"]["docker"]
    assert "nvidia.com/gpu=all" in sections["shell"]["podman"] and "--userns=keep-id" in sections["shell"]["podman"]
    assert "127.0.0.1:6006:6006" in sections["tensorboard"]["docker"]

    client.post(f"/api/builds/{build['id']}/airgap")
    with client.state["factory"]() if "factory" in client.state else client.state["session_factory"]() as session:
        bundle = Path(session.get(Build, build["id"]).airgap_path)
    with tarfile.open(bundle) as archive:
        names = archive.getnames()
        readme = archive.extractfile(next(n for n in names if n.endswith("training/README.md"))).read().decode()
    for expected in ("training/recipe.json", "training/requirements.lock", "training/checks.json",
                     "training/EXTERNAL-FILES.md", "training/examples/sft_lora.py"):
        assert any(n.endswith(expected) for n in names), expected
    assert detail["image_ref"] in readme and "Model weights and training data must be transferred separately" in readme
    assert "not scanned" in readme.lower()


def test_gpu_report_is_recorded_for_the_build(client):  # noqa: F811
    client.state["build_service"].backend = CheckingBackend()
    project = create_training_project(client, {"profile": "hf-finetune"})
    build = client.post(f"/api/projects/{project}/builds", json={}).json()
    run_pending_build(client, build["id"])
    response = client.post(f"/api/builds/{build['id']}/gpu-report",
                           json={"report": result_line("gpu", image=build["image_ref"])})
    assert response.status_code == 200, response.text
    gpu = next(row for row in response.json()["checks"] if row["id"] == "gpu")
    assert gpu["label"] == "GPU training test passed" and "Test GPU" in gpu["summary"]
    wrong = client.post(f"/api/builds/{build['id']}/gpu-report", json={"report": result_line("gpu", image="x:1")})
    assert wrong.status_code == 422


def test_resolve_endpoint_answers_conflicts_with_an_alternative(client):  # noqa: F811
    response = client.post("/api/training/resolve", json={"training": {"profile": "pytorch-advanced",
                                                                       "addons": ["flash-attn"]}}).json()
    assert response["ok"] is False and response["alternative"] == {"stack": "fa2-cu128"}
    ok = client.post("/api/training/resolve", json={"training": {"profile": "hf-finetune"}}).json()
    assert ok["ok"] and ok["recipe"]["profile"] == "hf-finetune"
    catalog_view = client.get("/api/training/catalog").json()
    assert [p["id"] for p in catalog_view["profiles"]] == ["hf-finetune", "pytorch-advanced", "llama-factory",
                                                           "dataset-prep"]


def test_backend_without_container_runs_records_checks_as_not_run(client):  # noqa: F811
    project = create_training_project(client, {"profile": "dataset-prep"})
    build = client.post(f"/api/projects/{project}/builds", json={}).json()
    run_pending_build(client, build["id"])
    rows = client.get(f"/api/builds/{build['id']}").json()["training"]["checks"]
    assert {r["id"]: r["status"] for r in rows} == {"image": "passed", "deps": "not_run", "cpu": "not_run",
                                                     "offline": "not_run"}


def test_old_databases_get_the_new_column(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/old.db")
    create_all(engine)
    with engine.begin() as connection:  # a database from before the column existed
        connection.execute(text("ALTER TABLE builds DROP COLUMN checks"))
    assert "checks" not in {c["name"] for c in inspect(engine).get_columns("builds")}
    create_all(engine)
    assert "checks" in {c["name"] for c in inspect(engine).get_columns("builds")}


def test_docs_never_suggest_publishing_web_uis_beyond_localhost():
    for profile in catalog.PROFILES:
        recipe = recipes.resolve({"profile": profile["id"], "addons": profile["addons"][:3]
                                  if profile["id"] != "pytorch-advanced" else ["tensorboard", "jupyter"]})
        text = docs.markdown(recipe, IMAGE)
        for line in text.splitlines():
            if "-p " in line and "mkdir" not in line:
                assert "127.0.0.1:" in line, line
