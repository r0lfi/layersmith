"""AI / ML / GPU templates: recipes, Containerfiles, compatibility, tests, Slurm and export."""

import base64
import json
import os
import re
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import pytest

from layersmith.aiml import catalog, checks, compat, export, lint, recipes, render, slurm
from layersmith.aiml.catalog import AimlError
from layersmith.core import containerfile as cf
from layersmith.core.spec import DIGEST_RE, InvalidSpec, validate_spec
from tests.test_api import client, run_pending_build  # noqa: F401  (fixture)
from tests.test_builds import FakeBackend

IMAGE = "localhost/layersmith-pytorch-cuda13.4:1.0.0"
CHECK = render.CHECK_SCRIPT


def basic_line(image, status="passed", packages=None, archs=("sm_80", "sm_90", "sm_100", "sm_120", "compute_120")):
    return checks.RESULT_PREFIX + json.dumps({
        "check": "basic", "status": status, "image": image, "finished": "2026-10-27T12:00:00Z",
        "steps": [{"name": "python", "status": "passed", "detail": "Python 3.12.3"},
                  {"name": "framework import", "status": status, "detail": "torch"}],
        "info": {"python": "3.12.3", "torch": "2.14.0a0+b2c75dd", "torch_cuda": "13.4",
                 "compiled_archs": list(archs), "os": "Ubuntu 24.04.3 LTS",
                 "env": {"CUDA_VERSION": "13.4.1.012", "CUDNN_VERSION": "9.26.0.51", "NCCL_VERSION": "2.31.2"},
                 "packages": packages or {"torch": "2.14.0a0+b2c75dd", "transformers": "5.17.0"}}})


def gpu_line(image, check="gpu", status="passed", host="gpu-worker-01", name="NVIDIA H200 NVL", capability="9.0"):
    return checks.RESULT_PREFIX + json.dumps({
        "check": check, "status": status, "image": image, "host": host, "finished": "2026-10-27T13:00:00Z",
        "steps": [{"name": "nvidia-smi", "status": "passed", "detail": "1 GPU"},
                  {"name": "PyTorch matrix multiplication", "status": status, "detail": "ok"}],
        "info": {"driver": "615.71.09", "devices": [{"index": "0", "name": name, "uuid": "GPU-1",
                                                     "memory_gb": 140.0, "capability": capability}],
                 **({"slurm": {"SLURM_JOB_ID": "42", "LAYERSMITH_SLURM_ISOLATION": "enforced"}}
                    if check == "slurm" else {})}})


class AimlBackend(FakeBackend):
    """A fake backend that runs the inline basic test like a real runtime would print it."""

    def __init__(self, status="passed"):
        super().__init__()
        self.status = status
        self.runs = []

    def build(self, context_dir, image_ref, architecture, on_log):
        # The context is removed after the build; keep a list of what it held.
        self.context_files = sorted(str(p.relative_to(context_dir)) for p in Path(context_dir).rglob("*")
                                    if p.is_file())
        return super().build(context_dir, image_ref, architecture, on_log)

    def run_container(self, image_ref, argv, on_log, timeout=1800, network="none", entrypoint=None):
        self.runs.append({"image": image_ref, "argv": argv, "network": network, "entrypoint": entrypoint})
        line = basic_line(image_ref, self.status)
        on_log(line)
        return (0 if self.status == "passed" else 1), line


# ------------------------------------------------------------ catalog

def test_every_verified_version_resolves_with_a_digest():
    for template_id, template in catalog.TEMPLATES.items():
        for version, entry in template["versions"].items():
            recipe = recipes.resolve({"template": template_id, "version": version, "mode": "import"})
            assert DIGEST_RE.match(entry["digest"]), (template_id, version)
            assert recipe["base"]["reference"] == f"{entry['image']}:{entry['tag']}@{entry['digest']}"
            assert recipe["base"]["verified"] and recipe["cuda"]


def test_pytorch_default_is_the_requested_ngc_release():
    recipe = recipes.resolve({})
    assert recipe["template"] == "nvidia-pytorch" and recipe["version"] == "26.09-py3"
    assert recipe["base"]["display"] == "nvcr.io/nvidia/pytorch:26.09-py3"
    assert recipe["cuda"] == "13.4.1" and recipe["framework_version"].startswith("2.14.0a0")
    assert recipe["included_packages_known"]
    assert recipe["included"]["jupyterlab"] == "4.6.3" and recipe["included"]["flash-attn-4"] == "4.0.0b24"


def test_unknown_versions_are_never_guessed():
    with pytest.raises(AimlError, match="not a verified version"):
        recipes.resolve({"template": "nvidia-pytorch", "version": "26.10-py3"})
    with pytest.raises(AimlError, match="Unknown AI/ML template"):
        recipes.resolve({"template": "jax"})


# ------------------------------------------------------------ tools and pip

def test_tools_already_in_the_base_are_not_installed_again():
    recipe = recipes.resolve({"tools": ["jupyterlab", "pandas", "transformers", "peft"]})
    status = {tool["id"]: tool["status"] for tool in recipe["tools"]}
    assert status["jupyterlab"] == "included" and status["pandas"] == "included"
    assert status["transformers"] == "install" and status["peft"] == "install"
    assert recipe["install"] == ["transformers", "peft"]


def test_framework_packages_are_protected():
    with pytest.raises(AimlError, match="protected"):
        recipes.resolve({"pip": ["torch==2.9.0"]})
    with pytest.raises(AimlError, match="protected"):
        recipes.resolve({"pip": ["nvidia-cudnn-cu13"]})
    with pytest.raises(AimlError, match="protected"):
        recipes.resolve({"pip": ["numpy>=2.3"]})  # NVIDIA built 26.09 with numpy<=2.1
    # numpy is only held for the NVIDIA PyTorch image
    assert recipes.resolve({"template": "vllm", "pip": ["numpy>=2.3"]})["install"] == ["numpy>=2.3"]
    with pytest.raises(AimlError, match="not offered"):
        recipes.resolve({"template": "vllm", "tools": ["flash-attn"]})


@pytest.mark.parametrize("value", [
    "https://evil.example/pkg.whl", "pkg @ https://x", "-e .", "--index-url=http://x", "pkg; os_name=='nt'",
    "git+https://github.com/x/y", "../pkg", "pkg==1.0 ; x", "pkg==$(id)", "pk g", "pkg[extra=1]",
])
def test_pip_input_is_a_strict_subset(value):
    with pytest.raises(AimlError):
        catalog.parse_requirement(value)


def test_pip_requirements_accepted_and_flagged():
    recipe = recipes.resolve({"pip": ["mlflow==3.4.0", "optuna>=4,<5", "einops[torch]"]})
    assert recipe["install"] == ["mlflow==3.4.0", "optuna>=4,<5", "einops[torch]"]
    assert any("optuna>=4,<5 is not pinned" in w for w in recipe["warnings"])
    with pytest.raises(AimlError, match="twice"):
        recipes.resolve({"tools": ["mlflow"], "pip": ["mlflow==3.4.0"]})


def test_import_keeps_upstream_unchanged_and_cuda_has_no_python():
    with pytest.raises(AimlError, match="unchanged"):
        recipes.resolve({"mode": "import", "tools": ["peft"]})
    with pytest.raises(AimlError, match="no Python"):
        recipes.resolve({"template": "nvidia-cuda", "tools": ["pandas"]})


def test_custom_base_needs_a_tag_and_reports_its_origin():
    with pytest.raises(AimlError, match="tag or a digest"):
        recipes.resolve({"template": "custom", "base": "nvcr.io/nvidia/pytorch"})
    trusted = recipes.resolve({"template": "custom", "base": "nvcr.io/nvidia/pytorch:26.10-py3"})
    assert trusted["base"]["image"] == "nvcr.io/nvidia/pytorch" and not trusted["base"]["verified"]
    assert not any("not from a vendor" in w for w in trusted["warnings"])
    other = recipes.resolve({"template": "custom", "base": "quay.io/someone/torch:1"})
    assert any("not from a vendor" in w for w in other["warnings"])
    plain = recipes.resolve({"template": "custom", "base": "ubuntu:24.04"})
    assert plain["base"]["image"] == "docker.io/library/ubuntu"


def test_index_url_refuses_credentials():
    with pytest.raises(AimlError):
        recipes.resolve({"pip_index_url": "https://user:secret@mirror.example/simple"})
    recipe = recipes.resolve({"pip_index_url": "http://mirror.example/simple"})
    assert any("plain HTTP" in w for w in recipe["warnings"])


# ------------------------------------------------------------ spec and Containerfile

def test_spec_pins_the_base_by_digest_and_refuses_unrelated_fields():
    spec = validate_spec({"aiml": {"tools": ["peft"]}})
    assert spec["base"]["source"].startswith("nvcr.io/nvidia/pytorch:26.09-py3@sha256:")
    assert spec["architecture"] == "amd64" and spec["aiml"]["version"] == "26.09-py3"
    with pytest.raises(InvalidSpec, match="custom env"):
        validate_spec({"aiml": {"mode": "custom"}, "env": [{"name": "A", "value": "b"}]})
    with pytest.raises(InvalidSpec, match="either"):
        validate_spec({"aiml": {"mode": "custom"}, "training": {"profile": "hf-finetune"}})


def test_import_containerfile_adds_labels_only():
    spec = validate_spec({"aiml": {"mode": "import"}})
    text, packages, _ = cf.generate({"name": "pt"}, spec, base_digest="sha256:" + "a" * 64, image_ref=IMAGE)
    instructions = [line.split()[0] for line in text.splitlines() if line and not line.startswith((" ", "#"))]
    assert instructions == ["FROM", "LABEL"] and packages == []
    assert "io.layersmith.aiml.mode=\"import\"" in text


def test_custom_containerfile_protects_the_base_and_sets_offline_defaults():
    spec = validate_spec({"aiml": {"tools": ["transformers", "mlflow"], "pip": ["optuna==4.5.0"]}})
    text, _, warnings = cf.generate({"name": "pt"}, spec, image_ref=IMAGE)
    assert "layersmith-aiml-pip install" in text and "LAYERSMITH_AIML_PROTECTED=" in text
    assert "torch" in text.split("LAYERSMITH_AIML_PROTECTED=")[1].split('"')[1].split()
    assert 'HF_HUB_OFFLINE="1"' in text and f'LAYERSMITH_IMAGE="{IMAGE}"' in text
    assert "apt-get" not in text and "--privileged" not in text
    assert "LAYERSMITH_AIML_EXPECT=\"transformers mlflow optuna\"" in text


def test_build_context_holds_requirements_and_scripts(tmp_path):
    recipe = recipes.resolve({"tools": ["peft"], "pip": ["optuna==4.5.0"]})
    written = render.write_context(tmp_path, recipe)
    assert written == ["aiml/layersmith-aiml-check", "aiml/layersmith-aiml-pip", "aiml/requirements.txt"]
    assert (tmp_path / "aiml/requirements.txt").read_text() == "peft\noptuna==4.5.0\n"


# ------------------------------------------------------------ compatibility

def rows_by_id(rows):
    return {row["id"]: row for row in rows}


def test_target_driver_runs_cuda_13_4_natively():
    rows = rows_by_id(compat.check(recipes.resolve({})))
    assert rows["driver"]["status"] == "pass" and "R615" in rows["driver"]["detail"]
    assert rows["gpu:h200-nvl"]["status"] == "pass" and rows["gpu:h200-nvl"]["evidence"] == "documented"


def test_older_drivers_are_graded_not_assumed():
    recipe = recipes.resolve({})
    assert compat.driver_row(recipe["cuda"], "580.95.05")["status"] == "warn"  # minor version compatibility
    assert compat.driver_row(recipe["cuda"], "570.172.08")["status"] == "fail"
    assert compat.driver_row("12.3.0", "615.71.09")["status"] == "pass"
    with pytest.raises(AimlError):
        compat.driver_row("13.4.1", "latest")


def test_tensorflow_on_blackwell_is_a_warning():
    rows = rows_by_id(compat.check(recipes.resolve({"template": "tensorflow"})))
    assert rows["gpu:rtx-pro-6000-bse"]["status"] == "warn" and "PTX" in rows["gpu:rtx-pro-6000-bse"]["detail"]
    assert rows["gpu:h200-nvl"]["status"] == "warn"  # sm_89 is the newest native code in TF 2.21
    # What the real TF 2.21.0-gpu image reported in its basic test (2026-10-27):
    observed = {"compiled_archs": ["compute_90", "sm_60", "sm_70", "sm_80", "sm_89"], "tf_cuda": "12.5.1",
                "tensorflow": "2.21.0", "env": {"CUDA_VERSION": "12.3.0"}}
    rows = rows_by_id(compat.check(recipes.resolve({"template": "tensorflow"}), basic=observed))
    assert rows["gpu:h200-nvl"]["status"] == "warn" and rows["gpu:h200-nvl"]["evidence"] == "image"
    assert rows["framework-cuda"]["status"] == "pass" and "minor version compatible" in rows["framework-cuda"]["detail"]


def test_compiled_architectures_from_the_image_decide():
    recipe = recipes.resolve({})
    native = rows_by_id(compat.check(recipe, basic={"compiled_archs": ["sm_90", "sm_120"]}))
    assert native["gpu:rtx-pro-6000-bse"]["status"] == "pass" and native["gpu:rtx-pro-6000-bse"]["evidence"] == "image"
    ptx = rows_by_id(compat.check(recipe, basic={"compiled_archs": ["sm_80", "compute_90"]}))
    assert ptx["gpu:rtx-pro-6000-bse"]["status"] == "warn" and ptx["gpu:h200-nvl"]["status"] == "warn"
    no_cuda = rows_by_id(compat.check(recipes.resolve({"template": "custom", "base": "python:3.12-slim"}),
                                      basic={"python": "3.12.16"}))
    assert no_cuda["driver"]["status"] == "fail" and "no CUDA" in no_cuda["driver"]["detail"]
    none = rows_by_id(compat.check(recipe, basic={"compiled_archs": ["sm_80", "sm_86"]}))
    assert none["gpu:h200-nvl"]["status"] == "fail"
    arch_specific = rows_by_id(compat.check(recipe, basic={"compiled_archs": ["sm_80", "compute_90a"]}))
    assert arch_specific["gpu:rtx-pro-6000-bse"]["status"] == "fail"  # compute_90a PTX runs only on sm_90a


def test_a_reported_gpu_test_is_hardware_evidence():
    recipe = recipes.resolve({})
    report = checks.validate_report(gpu_line(IMAGE), IMAGE)
    rows = rows_by_id(compat.check(recipe, reported=report))
    assert rows["gpu:h200-nvl"]["evidence"] == "hardware" and rows["gpu:h200-nvl"]["status"] == "pass"
    assert rows["gpu:rtx-pro-6000-bse"]["evidence"] != "hardware"


# ------------------------------------------------------------ reports

def test_gpu_report_is_recorded_for_its_image_only():
    result = checks.validate_report(f"noise\n{gpu_line(IMAGE)}\n", IMAGE)
    assert result["check"] == "gpu" and result["status"] == "passed" and result["source"] == "reported"
    with pytest.raises(checks.InvalidReport, match="from localhost/other:1.0.0, not"):
        checks.validate_report(gpu_line("localhost/other:1.0.0"), IMAGE)
    with pytest.raises(checks.InvalidReport, match="basic"):
        checks.validate_report(basic_line(IMAGE), IMAGE)
    with pytest.raises(checks.InvalidReport, match="No LayerSmith test result"):
        checks.validate_report("hello", IMAGE)


def test_slurm_job_output_with_two_nodes_and_exit_codes():
    output = "\n".join([
        "LayerSmith: node=gpu-01 job=42 gpus=GPU-1 isolation=enforced",
        gpu_line(IMAGE, "slurm", host="gpu-01"),
        checks.EXIT_PREFIX + json.dumps({"node": "gpu-01", "job": "42", "exit_code": 0}),
        gpu_line(IMAGE, "slurm", host="gpu-02", status="failed"),
        checks.EXIT_PREFIX + json.dumps({"node": "gpu-02", "job": "42", "exit_code": 1}),
    ])
    result = checks.validate_report(output, IMAGE)
    assert result["check"] == "slurm" and result["status"] == "failed" and result["exit_code"] == 1
    assert [n["host"] for n in result["nodes"]] == ["gpu-01", "gpu-02"]
    assert any(step["name"].startswith("gpu-02:") for step in result["steps"])


def test_missing_prerequisite_is_recorded_as_a_failed_slurm_test():
    output = ("LAYERSMITH_PREREQ_MISSING node=gpu-01: GPU device isolation is not configured\n"
              + checks.EXIT_PREFIX + '{"node": "gpu-01", "job": "7", "exit_code": 97}')
    result = checks.validate_report(output, IMAGE)
    assert result["status"] == "failed" and result["check"] == "slurm"
    assert "isolation" in result["steps"][0]["detail"]


# ------------------------------------------------------------ Slurm script

def test_default_script_uses_gres_and_never_all_gpus():
    generated = slurm.generate({}, IMAGE)
    script = generated["script"]
    code = "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("#"))
    assert "#SBATCH --gres=gpu:1" in script and "srun --ntasks-per-node=1 --gres=gpu:1" in script
    assert "nvidia.com/gpu=all" not in code and "--privileged" not in code
    assert not re.search(r"nvidia\.com/gpu=\d", code)  # never a hard-coded index
    assert "ConstrainDevices=yes" in script and "container_use_devices" in script
    assert "label=disable" in script  # only reachable when chosen explicitly
    subprocess.run(["bash", "-n"], input=script, text=True, check=True)


def test_script_options_and_validation():
    generated = slurm.generate({"nodes": 2, "gpus_per_node": 4, "gpu_type": "h200", "partition": "h200-nvl",
                                "mem": "512G", "time": "1-00:00:00", "network": "host",
                                "command": "torchrun --nproc-per-node=4 train.py", "kind": "job"}, IMAGE)
    assert "#SBATCH --gres=gpu:h200:4" in generated["script"] and "#SBATCH --nodes=2" in generated["script"]
    assert "export LS_COMMAND='torchrun --nproc-per-node=4 train.py'" in generated["script"]
    subprocess.run(["bash", "-n"], input=generated["script"], text=True, check=True)
    for bad in ({"partition": "gpu;rm -rf /"}, {"command": "a\nb"}, {"mem": "lots"}, {"time": "soon"},
                {"mounts": [{"source": "/etc", "target": "/etc2"}]},
                {"mounts": [{"source": "/run/user/1000/podman/podman.sock", "target": "/s"}]},
                {"mounts": [{"source": "/data/../etc", "target": "/x"}]},
                {"selinux": "disabled"}, {"gpus_per_node": 0}, {"output": "$(id).out"}, {"bogus": 1}):
        with pytest.raises(AimlError):
            slurm.generate(bad, IMAGE)
    assert slurm.generate({"nodes": 2}, IMAGE)["warnings"]  # network none cannot do multi-node


def test_test_script_carries_the_check():
    script = slurm.generate({"kind": "test"}, IMAGE)["script"]
    encoded = re.search(r"export LS_CHECK_B64='?([A-Za-z0-9+/=]+)", script).group(1)
    assert base64.b64decode(encoded) == CHECK.read_bytes()
    assert "gpu --expect-gpus" in script
    vllm = slurm.generate({"kind": "test"}, IMAGE, entrypoint_wraps=False)["script"]
    assert "export LS_ENTRYPOINT_WRAPS=0" in vllm


@pytest.fixture
def fake_node(tmp_path):
    """A GPU node in miniature: two GPUs, a CDI spec, Slurm env, and podman/srun that record their arguments."""
    if os.geteuid() == 0:
        pytest.skip("root can open any file, so cgroup denial cannot be simulated")
    proc, dev, cdi, bin_dir = tmp_path / "proc", tmp_path / "dev", tmp_path / "cdi", tmp_path / "bin"
    for index, uuid in enumerate(("GPU-aaaa", "GPU-bbbb")):
        gpu = proc / "gpus" / f"0000:0{index}:00.0"
        gpu.mkdir(parents=True)
        (gpu / "information").write_text(f"Model: \t\t NVIDIA H200 NVL\nGPU UUID: \t {uuid}\nDevice Minor: \t {index}\n")
    dev.mkdir()
    (dev / "nvidia0").write_text("")
    (dev / "nvidia1").write_text("")
    cdi.mkdir()
    (cdi / "nvidia.yaml").write_text("devices:\n- name: GPU-aaaa\n- name: GPU-bbbb\n")
    bin_dir.mkdir()
    log = tmp_path / "podman.log"
    (bin_dir / "podman").write_text(f'#!/bin/bash\necho "$*" >> {log}\n'
                                    'if [ "$1" = run ]; then cat > /dev/null; echo ran; fi\nexit 0\n')
    (bin_dir / "srun").write_text('#!/bin/bash\nwhile [ "$1" != bash ]; do shift; done\nexec "$@"\n')
    (bin_dir / "getenforce").write_text("#!/bin/sh\necho Enforcing\n")
    (bin_dir / "getsebool").write_text("#!/bin/sh\necho 'container_use_devices --> on'\n")
    for tool in bin_dir.iterdir():
        tool.chmod(0o755)
    data = tmp_path / "models"
    data.mkdir()
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "SLURM_JOB_ID": "42", "SLURM_STEP_GPUS": "0",
           "LS_PROC_NVIDIA": str(proc), "LS_DEV": str(dev), "LS_CDI_DIRS": str(cdi)}

    def run(params=None, deny=(1,)):
        for index in (0, 1):
            (dev / f"nvidia{index}").chmod(0o000 if index in deny else 0o644)
        params = {"mounts": [{"source": str(data), "target": "/models", "mode": "ro"}], **(params or {})}
        script = slurm.generate(params, IMAGE)["script"]
        log.unlink(missing_ok=True)
        done = subprocess.run(["bash", "-c", script], env=env, text=True, capture_output=True, timeout=60)
        return done, (log.read_text() if log.exists() else "")
    return run


def test_node_gets_exactly_the_gpus_its_cgroup_allows(fake_node):
    done, podman = fake_node()
    assert done.returncode == 0, done.stderr
    assert "--device nvidia.com/gpu=GPU-aaaa" in podman and "GPU-bbbb" not in podman
    assert "--cap-drop=ALL" in podman and "--security-opt=no-new-privileges" in podman
    assert "label=disable" not in podman and f"-v {Path(podman.split('-v ')[1].split(':')[0])}" in podman
    assert 'LAYERSMITH_SLURM_EXIT {"node":' in done.stdout and '"exit_code": 0' in done.stdout


def test_node_without_device_isolation_stops(fake_node):
    done, podman = fake_node(deny=())
    assert done.returncode != 0 and "run " not in podman
    assert "LAYERSMITH_PREREQ_MISSING" in done.stdout and "ConstrainDevices=yes" in done.stdout


def test_node_without_uuid_cdi_names_stops(fake_node, tmp_path):
    (tmp_path / "cdi" / "nvidia.yaml").write_text("devices:\n- name: '0'\n- name: '1'\n")
    done, podman = fake_node()
    assert "--device-name-strategy=uuid" in done.stdout and "run " not in podman


def test_label_disable_is_per_container_and_explicit(fake_node):
    done, podman = fake_node({"selinux": "label_disable"})
    assert done.returncode == 0 and "--security-opt label=disable" in podman


# ------------------------------------------------------------ in-image scripts

def test_check_script_runs_inline_and_reports_one_result(tmp_path):
    """The same heredoc LayerSmith passes to `podman run --entrypoint /bin/sh`, run on this machine."""
    recipe = recipes.resolve({"tools": ["peft"]})
    entrypoint, argv = render.inline_check(recipe, IMAGE)
    env = {k: v for k, v in os.environ.items() if not k.startswith("LAYERSMITH")}
    env.update({"CUDA_VERSION": "13.4.1", "LAYERSMITH_AIML_STATE_DIR": str(tmp_path),
                "PATH": f"{Path(sys.executable).parent}:{os.environ['PATH']}"})
    done = subprocess.run([entrypoint, *argv], env=env, text=True, capture_output=True, timeout=300)
    result = checks.parse_output(done.stdout)
    assert result["check"] == "basic" and result["image"] == IMAGE
    names = {step["name"]: step["status"] for step in result["steps"]}
    # This test environment has no torch or peft: both are reported, not crashed on.
    assert names["python"] == "passed" and names["installed libraries"] == "failed"
    assert names["framework import"] == "failed" and result["status"] == "failed"
    assert result["info"]["packages"]["pytest"]


def test_shell_fallback_for_images_without_python(tmp_path):
    recipe = recipes.resolve({"template": "nvidia-cuda", "mode": "import"})
    entrypoint, argv = render.inline_check(recipe, IMAGE)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("tail", "tr", "printf"):
        found = subprocess.run(["sh", "-c", f"command -v {tool}"], text=True, capture_output=True).stdout.strip()
        if found.startswith("/"):
            (bin_dir / tool).symlink_to(found)
    done = subprocess.run(["/bin/sh", *argv], env={"PATH": str(bin_dir), "CUDA_VERSION": "13.4.1"},
                          text=True, capture_output=True, timeout=60)
    result = checks.parse_output(done.stdout)
    assert result and result["status"] == "passed" and result["info"]["env"]["CUDA_VERSION"] == "13.4.1"


def test_pip_helper_records_state_without_installing(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("")
    env = {**os.environ, "LAYERSMITH_AIML_STATE_DIR": str(tmp_path / "state"),
           "LAYERSMITH_AIML_PROTECTED": "pytest", "LAYERSMITH_AIML_PROTECTED_PREFIXES": "nvidia-"}
    done = subprocess.run([sys.executable, str(render.PIP_SCRIPT), "install", str(requirements)], env=env,
                          text=True, capture_output=True, timeout=300)
    assert done.returncode == 0, done.stdout + done.stderr
    constraints = (tmp_path / "state/protected-constraints.txt").read_text()
    assert re.search(r"^pytest==", constraints, re.M)
    assert "pytest==" in (tmp_path / "state/requirements.lock").read_text()


# ------------------------------------------------------------ lint

def test_lint_refuses_credentials_and_isolation_escapes():
    result = lint.lint("FROM nvcr.io/nvidia/pytorch:26.09-py3\n"
                       "ENV HF_TOKEN=hf_" + "a" * 34 + "\n"
                       "RUN pip install --index-url https://user:pw@mirror.example/simple x\n"
                       "RUN curl -fsSL https://x.example/install.sh | sh\n"
                       "RUN setenforce 0\n")
    text = " ".join(result["errors"])
    assert "HF_TOKEN" in text and "credential" in text and "isolation" in text
    assert any("pipes a download" in w for w in result["warnings"])
    assert any("not pinned by digest" in w for w in result["warnings"])
    assert lint.lint("RUN echo\n")["errors"] == ["The Containerfile has no FROM line"]


# ------------------------------------------------------------ API, end to end

@pytest.fixture
def aiml_client(client, monkeypatch):  # noqa: F811
    monkeypatch.setenv("LAYERSMITH_GPU_MIN_FREE_GB", "0")  # the real images need ~60 GB free
    client.state["build_service"].backend = AimlBackend()
    return client


def create_and_build(client, spec, name="pytorch-cuda134"):
    project = client.post("/api/projects", json={
        "name": name, "repository": "localhost/layersmith-pytorch-cuda13.4", "template": "NVIDIA PyTorch",
        "spec": spec}).json()
    build = client.post(f"/api/projects/{project['id']}/builds", json={}).json()
    run_pending_build(client, build["id"])
    return project, build["id"]


def test_catalog_and_resolve_endpoints(aiml_client):
    view = aiml_client.get("/api/aiml/catalog").json()
    assert set(view["templates"]) == set(catalog.TEMPLATES)
    pytorch = view["templates"]["nvidia-pytorch"]["versions"]["26.09-py3"]
    assert pytorch["cuda"] == "13.4.1" and pytorch["included"]["torch"].startswith("2.14.0a0")
    resolved = aiml_client.post("/api/aiml/resolve", json={"aiml": {"tools": ["peft"]}}).json()
    assert resolved["ok"] and resolved["names"]["repository"] == "localhost/layersmith-pytorch-cuda13.4"
    assert {row["id"] for row in resolved["compatibility"]} >= {"driver", "gpu:h200-nvl"}
    refused = aiml_client.post("/api/aiml/resolve", json={"aiml": {"pip": ["torch"]}}).json()
    assert not refused["ok"] and "protected" in refused["error"]


def test_build_runs_the_basic_test_and_shows_metadata(aiml_client):
    _, build_id = create_and_build(aiml_client, {"aiml": {"tools": ["transformers"]}})
    backend = aiml_client.state["build_service"].backend
    assert backend.runs[0]["entrypoint"] == "/bin/sh" and backend.runs[0]["network"] == "none"
    assert "layersmith-aiml-pip" in backend.containerfile and "aiml/requirements.txt" in backend.context_files
    detail = aiml_client.get(f"/api/builds/{build_id}").json()
    assert detail["status"] == "ready" and detail["aiml_template"] == "nvidia-pytorch"
    meta = detail["aiml"]["metadata"]
    assert meta["torch"] == "2.14.0a0+b2c75dd" and meta["python"] == "3.12.3" and meta["cuda"] == "13.4.1.012"
    tests = {t["id"]: t for t in detail["aiml"]["tests"]}
    assert tests["basic"]["status"] == "passed" and tests["gpu"]["status"] == "not_run"
    assert tests["gpu"]["where"] == "GPU worker"
    compat_rows = {r["id"]: r for r in detail["aiml"]["compatibility"]}
    assert compat_rows["gpu:rtx-pro-6000-bse"]["evidence"] == "image"
    assert detail["manifest"]["aiml"]["basic_test"] == "passed"
    assert "Base pinned by digest, verified by LayerSmith" in detail["log"]


def test_build_stops_before_pulling_without_disk_space(aiml_client, monkeypatch):
    monkeypatch.setenv("LAYERSMITH_GPU_MIN_FREE_GB", "999999")
    _, build_id = create_and_build(aiml_client, {"aiml": {"mode": "import"}}, name="pytorch-import")
    detail = aiml_client.get(f"/api/builds/{build_id}").json()
    assert detail["status"] == "failed" and "Not enough disk space" in detail["error"]
    assert not any(call[0] == "resolve_base" for call in aiml_client.state["build_service"].backend.calls)


def test_import_build_is_tested_inline(aiml_client):
    _, build_id = create_and_build(aiml_client, {"aiml": {"mode": "import"}}, name="pytorch-import")
    backend = aiml_client.state["build_service"].backend
    assert "RUN" not in backend.containerfile and not any(f.startswith("aiml/") for f in backend.context_files)
    assert backend.runs[-1]["argv"][0] == "-c" and "LAYERSMITH_AIML_CHECK_EOF" in backend.runs[-1]["argv"][1]
    assert aiml_client.get(f"/api/builds/{build_id}").json()["aiml"]["tests"][0]["status"] == "passed"


def test_airgap_bundle_has_sbom_packages_and_offline_readme(aiml_client):
    _, build_id = create_and_build(aiml_client, {"aiml": {"tools": ["transformers"]}})
    bundle = aiml_client.post(f"/api/builds/{build_id}/airgap", json={}).json()
    path = Path(aiml_client.state["settings"].image_dir) / bundle["filename"]
    with tarfile.open(path) as archive:
        names = archive.getnames()
        stem = names[0].split("/")[0]
        readme = archive.extractfile(f"{stem}/aiml/README.md").read().decode()
        sbom = json.loads(archive.extractfile(f"{stem}/aiml/sbom.cdx.json").read())
        sums = archive.extractfile(f"{stem}/SHA256SUMS").read().decode()
    for expected in ("aiml/packages.json", "aiml/requirements.lock", "aiml/layersmith-aiml-check",
                     "image/layersmith-pytorch-cuda13.4-1.0.0.tar.sha256", "source/Containerfile"):
        assert f"{stem}/{expected}" in names
    assert "sha256sum -c layersmith-pytorch-cuda13.4-1.0.0.tar.sha256" in readme
    assert "podman load -i layersmith-pytorch-cuda13.4-1.0.0.tar" in readme and "cd image" in readme
    assert sbom["bomFormat"] == "CycloneDX" and any(c.get("purl") == "pkg:pypi/transformers@5.17.0"
                                                    for c in sbom["components"])
    assert "aiml/sbom.cdx.json" in sums


def test_files_reports_and_slurm_endpoints(aiml_client):
    _, build_id = create_and_build(aiml_client, {"aiml": {"tools": ["transformers"]}})
    lock = aiml_client.get(f"/api/builds/{build_id}/aiml/files/requirements.lock")
    assert lock.status_code == 200 and "transformers==5.17.0" in lock.text
    assert aiml_client.get(f"/api/builds/{build_id}/aiml/files/../../etc/passwd").status_code == 404
    assert aiml_client.get(f"/api/builds/{build_id}/aiml/files/layersmith-aiml-check").text == CHECK.read_text()

    script = aiml_client.post(f"/api/builds/{build_id}/aiml/slurm",
                              json={"params": {"kind": "test", "gpus_per_node": 2}}).json()
    assert script["filename"] == "layersmith-gpu.sbatch" and "--gres=gpu:2" in script["script"]
    assert IMAGE in script["script"]
    bad = aiml_client.post(f"/api/builds/{build_id}/aiml/slurm", json={"params": {"partition": "a b"}})
    assert bad.status_code == 422

    wrong = aiml_client.post(f"/api/builds/{build_id}/aiml/report", json={"report": gpu_line("x/y:1.0.0")})
    assert wrong.status_code == 422
    recorded = aiml_client.post(f"/api/builds/{build_id}/aiml/report",
                                json={"report": gpu_line(IMAGE, "slurm") + "\n" + checks.EXIT_PREFIX
                                      + '{"node": "gpu-01", "job": "42", "exit_code": 0}'}).json()
    tests = {t["id"]: t for t in recorded["tests"]}
    assert tests["slurm"]["status"] == "passed" and tests["slurm"]["exit_code"] == 0
    rows = {r["id"]: r for r in recorded["compatibility"]}
    assert rows["gpu:h200-nvl"]["evidence"] == "hardware"


def test_docker_archive_export_and_delete(aiml_client):
    _, build_id = create_and_build(aiml_client, {"aiml": {"mode": "import"}}, name="pytorch-import")
    started = aiml_client.post(f"/api/builds/{build_id}/aiml/export", json={"format": "docker-archive"})
    assert started.status_code == 202
    for _ in range(100):
        exports = aiml_client.get(f"/api/builds/{build_id}").json()["aiml"]["exports"]
        if exports.get("docker-archive", {}).get("status") == "ready":
            break
        time.sleep(0.05)
    entry = exports["docker-archive"]
    assert entry["archive"] == "layersmith-pytorch-cuda13.4-1.0.0.docker.tar" and entry["available"]
    checksum = aiml_client.get(f"/api/builds/{build_id}/aiml/files/docker-archive.sha256").text
    assert checksum == f"{entry['sha256']}  {entry['archive']}\n"
    assert aiml_client.post(f"/api/builds/{build_id}/aiml/export",
                            json={"format": "oci-archive"}).status_code == 409  # already the primary format
    path = Path(aiml_client.state["settings"].image_dir) / entry["archive"]
    assert path.is_file()
    assert aiml_client.delete(f"/api/builds/{build_id}").status_code == 200
    assert not path.exists()


def test_advanced_mode_ai_project_is_linted(aiml_client):
    response = aiml_client.post("/api/projects", json={
        "name": "pt-advanced", "template": "NVIDIA PyTorch", "mode": "advanced",
        "containerfile": "FROM nvcr.io/nvidia/pytorch:26.09-py3\nENV API_KEY=abc\n"})
    assert response.status_code == 422 and "API_KEY" in response.json()["detail"]
    lint_view = aiml_client.post("/api/aiml/lint", json={"containerfile": "FROM x:latest\n"}).json()
    assert lint_view["errors"] == [] and lint_view["warnings"]


def test_export_helpers_without_a_basic_test():
    class Row:
        id, checks, image_ref, image_digest, image_id, base_digest, finished_at = "b", None, IMAGE, None, None, None, None
    recipe = recipes.resolve({"mode": "import"})
    assert export.lock(Row) == ""
    assert export.packages(Row, recipe)["source"].startswith("not available")
    assert export.sbom(Row, recipe)["components"][0]["type"] == "container"
