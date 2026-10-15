"""GPU and host discovery through the host driver's NVML, plus a live sampler.

Nothing here requires a GPU. Every value a GPU or driver does not support is
recorded as None and shown as N/A; an unsupported feature is not a failure.
"""

from __future__ import annotations

import collections
import datetime as dt
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

GPU_BENCH_HOME = Path(os.environ.get("GPU_BENCH_HOME", "/opt/gpu-bench"))
RECIPE_FILE = GPU_BENCH_HOME / "recipe.json"

# CUDA user-space version -> minimum Linux driver (NVIDIA CUDA release notes).
DRIVER_MINIMUMS = {"13.0": "580.65.06", "12.9": "575.51.03", "12.8": "570.26.00"}

TOPOLOGY_LEVELS = {0: "SELF", 10: "PIX", 20: "PXB", 30: "PHB", 40: "NODE", 50: "SYS"}


def _text(value) -> str:
    return value.decode(errors="replace") if isinstance(value, bytes) else str(value)


def run(args: list[str], timeout: int = 20) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(args, 127, "", str(exc))


def _try(call, default=None):
    try:
        return call()
    except Exception:
        return default


def recipe() -> dict:
    try:
        return json.loads(RECIPE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


# ------------------------------------------------------------------ NVML

def _nvml():
    import pynvml

    pynvml.nvmlInit()
    return pynvml


def _gpu_row(pynvml, index: int) -> dict:
    handle = pynvml.nvmlDeviceGetHandleByIndex(index)
    memory = pynvml.nvmlDeviceGetMemoryInfo(handle)
    row = {
        "index": index,
        "name": _text(pynvml.nvmlDeviceGetName(handle)),
        "uuid": _try(lambda: _text(pynvml.nvmlDeviceGetUUID(handle))),
        "memory_total_bytes": int(memory.total),
        "memory_used_bytes": int(memory.used),
        "memory_free_bytes": int(memory.free),
        "pci_bus_id": _try(lambda: _text(pynvml.nvmlDeviceGetPciInfo(handle).busId)),
        "compute_capability": _try(lambda: ".".join(map(str, pynvml.nvmlDeviceGetCudaComputeCapability(handle)))),
        "temperature_c": _try(lambda: pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)),
        "power_watts": _try(lambda: pynvml.nvmlDeviceGetPowerUsage(handle) / 1000),
        "power_limit_watts": _try(lambda: pynvml.nvmlDeviceGetEnforcedPowerLimit(handle) / 1000),
        "utilization_percent": _try(lambda: pynvml.nvmlDeviceGetUtilizationRates(handle).gpu),
        "memory_utilization_percent": _try(lambda: pynvml.nvmlDeviceGetUtilizationRates(handle).memory),
        "sm_clock_mhz": _try(lambda: pynvml.nvmlDeviceGetClockInfo(handle, pynvml.NVML_CLOCK_SM)),
        "sm_clock_max_mhz": _try(lambda: pynvml.nvmlDeviceGetMaxClockInfo(handle, pynvml.NVML_CLOCK_SM)),
        "memory_clock_mhz": _try(lambda: pynvml.nvmlDeviceGetClockInfo(handle, pynvml.NVML_CLOCK_MEM)),
        "persistence_mode": _try(lambda: bool(pynvml.nvmlDeviceGetPersistenceMode(handle))),
        "fan_percent": _try(lambda: pynvml.nvmlDeviceGetFanSpeed(handle)),
    }
    link = {"generation": _try(lambda: pynvml.nvmlDeviceGetCurrPcieLinkGeneration(handle)),
            "width": _try(lambda: pynvml.nvmlDeviceGetCurrPcieLinkWidth(handle)),
            "max_generation": _try(lambda: pynvml.nvmlDeviceGetMaxPcieLinkGeneration(handle)),
            "max_width": _try(lambda: pynvml.nvmlDeviceGetMaxPcieLinkWidth(handle))}
    row["pcie_link"] = link if any(value is not None for value in link.values()) else None
    mig = _try(lambda: pynvml.nvmlDeviceGetMigMode(handle))
    row["mig_mode"] = None if mig is None else {"current": "Enabled" if mig[0] else "Disabled",
                                               "pending": "Enabled" if mig[1] else "Disabled"}
    ecc = _try(lambda: pynvml.nvmlDeviceGetEccMode(handle))
    if ecc is None:
        row["ecc"] = None
    else:
        row["ecc"] = {
            "mode": "Enabled" if ecc[0] else "Disabled",
            "volatile_uncorrected": _try(lambda: pynvml.nvmlDeviceGetTotalEccErrors(
                handle, pynvml.NVML_MEMORY_ERROR_TYPE_UNCORRECTED, pynvml.NVML_VOLATILE_ECC)),
            "volatile_corrected": _try(lambda: pynvml.nvmlDeviceGetTotalEccErrors(
                handle, pynvml.NVML_MEMORY_ERROR_TYPE_CORRECTED, pynvml.NVML_VOLATILE_ECC)),
            "aggregate_uncorrected": _try(lambda: pynvml.nvmlDeviceGetTotalEccErrors(
                handle, pynvml.NVML_MEMORY_ERROR_TYPE_UNCORRECTED, pynvml.NVML_AGGREGATE_ECC)),
        }
    links = []
    for number in range(18):
        state = _try(lambda: pynvml.nvmlDeviceGetNvLinkState(handle, number))
        if state is None:
            break
        if state:
            remote = _try(lambda: _text(pynvml.nvmlDeviceGetNvLinkRemotePciInfo_v2(handle, number).busId))
            links.append({"link": number, "remote_pci_bus_id": remote})
    row["nvlink"] = links
    return row


def _bus_key(bus_id: str | None) -> str:
    """NVML pads PCI domains differently in different calls; compare the tail."""
    return (bus_id or "").lower()[-12:]


def _nvml_topology(pynvml, rows: list[dict]) -> dict | None:
    """Pairwise GPU connection, nvidia-smi topo style: NV<n>, PIX, PXB, PHB, NODE, SYS."""
    count = len(rows)
    if count < 2:
        return None
    handles = [pynvml.nvmlDeviceGetHandleByIndex(index) for index in range(count)]
    bus = {_bus_key(row.get("pci_bus_id")): row["index"] for row in rows}
    matrix, p2p = [], []
    for i in range(count):
        line, p2p_line = [], []
        for j in range(count):
            if i == j:
                line.append("X")
                p2p_line.append("X")
                continue
            nvlinks = sum(1 for link in rows[i].get("nvlink") or []
                          if bus.get(_bus_key(link.get("remote_pci_bus_id"))) == j)
            if nvlinks:
                line.append(f"NV{nvlinks}")
            else:
                level = _try(lambda: pynvml.nvmlDeviceGetTopologyCommonAncestor(handles[i], handles[j]))
                line.append(TOPOLOGY_LEVELS.get(int(level), str(level)) if level is not None else "N/A")
            status = _try(lambda: pynvml.nvmlDeviceGetP2PStatus(handles[i], handles[j],
                                                                pynvml.NVML_P2P_CAPS_INDEX_READ))
            p2p_line.append("N/A" if status is None else ("OK" if status == 0 else "NS"))
        matrix.append(line)
        p2p.append(p2p_line)
    return {"matrix": matrix, "p2p_read": p2p,
            "legend": "NV#=NVLink count, PIX=same PCIe switch, PXB=multiple switches, PHB=host bridge, "
                      "NODE=same NUMA node, SYS=across NUMA/SMP interconnect. P2P: OK or NS (not supported)."}


def _nvml_rows() -> tuple[list[dict], dict]:
    extra: dict = {"driver_version": None, "cuda_driver_version": None, "nvml_error": None, "topology": None}
    try:
        pynvml = _nvml()
    except Exception as exc:
        extra["nvml_error"] = str(exc) or type(exc).__name__
        return [], extra
    try:
        extra["driver_version"] = _try(lambda: _text(pynvml.nvmlSystemGetDriverVersion()))
        cuda = _try(lambda: pynvml.nvmlSystemGetCudaDriverVersion())
        if cuda:
            extra["cuda_driver_version"] = f"{cuda // 1000}.{(cuda % 1000) // 10}"
        rows = [_gpu_row(pynvml, index) for index in range(pynvml.nvmlDeviceGetCount())]
        extra["topology"] = _try(lambda: _nvml_topology(pynvml, rows))
        return rows, extra
    except Exception as exc:
        extra["nvml_error"] = str(exc)
        return [], extra
    finally:
        _try(pynvml.nvmlShutdown)


# ------------------------------------------------------------ host facts

def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def rdma_devices() -> list[dict]:
    devices = []
    root = Path("/sys/class/infiniband")
    if not root.is_dir():
        return devices
    for device in sorted(root.iterdir()):
        ports = []
        for port in sorted((device / "ports").glob("*")):
            ports.append({"port": port.name, "state": _read(port / "state"), "rate": _read(port / "rate"),
                          "link_layer": _read(port / "link_layer"),
                          "physical_state": _read(port / "phys_state")})
        netdevs = sorted(path.name for path in (device / "device" / "net").glob("*"))
        devices.append({"name": device.name, "ports": ports, "netdevs": netdevs,
                        "firmware": _read(device / "fw_ver"),
                        "uverbs_visible": any(Path("/dev/infiniband").glob("uverbs*")),
                        "transport": "RoCE" if any(p["link_layer"] == "Ethernet" for p in ports)
                        else ("InfiniBand" if ports else None)})
    return devices


def network_interfaces() -> list[dict]:
    rows = []
    root = Path("/sys/class/net")
    if not root.is_dir():
        return rows
    for interface in sorted(root.iterdir()):
        if interface.name == "lo":
            continue
        speed = _read(interface / "speed")
        rows.append({"name": interface.name, "state": _read(interface / "operstate"),
                     "mtu": _read(interface / "mtu"),
                     "speed_mbps": int(speed) if speed and speed.lstrip("-").isdigit() and int(speed) > 0 else None})
    return rows


def host_facts() -> dict:
    cpu = None
    match = re.search(r"^model name\s*:\s*(.+)$", _read(Path("/proc/cpuinfo")) or "", re.M)
    if match:
        cpu = match.group(1).strip()
    memory = None
    match = re.search(r"^MemTotal:\s*(\d+) kB", _read(Path("/proc/meminfo")) or "", re.M)
    if match:
        memory = int(match.group(1)) * 1024
    return {"cpu_model": cpu, "cpu_count": os.cpu_count(), "memory_total_bytes": memory,
            "shm_bytes": _try(lambda: shutil.disk_usage("/dev/shm").total), "kernel": platform.release(),
            "in_container": Path("/run/.containerenv").exists() or Path("/.dockerenv").exists()}


def collect_system_info(include_torch: bool = True) -> dict:
    """Facts about this host and its GPUs. Works without any GPU."""
    rows, extra = _nvml_rows()
    info = {
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "host": host_facts(),
        "driver_version": extra["driver_version"],
        "cuda_driver_version": extra["cuda_driver_version"],
        "cuda_version": None,
        "nvml_error": extra["nvml_error"],
        "gpu_count": len(rows),
        "gpus": rows,
        "topology": extra["topology"],
        "gpu_topology": None,
        "nvlink_status": None,
        "torch_cuda_available": False,
        "rdma_devices": rdma_devices(),
        "network_interfaces": network_interfaces(),
        "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"),
        "image": recipe(),
        "packages": {},
    }
    smi = shutil.which("nvidia-smi")
    if smi and rows:
        topology = run([smi, "topo", "-m"], timeout=10)
        if topology.returncode == 0:
            info["gpu_topology"] = topology.stdout.strip()
        nvlink = run([smi, "nvlink", "--status"], timeout=10)
        if nvlink.returncode == 0 and nvlink.stdout.strip():
            info["nvlink_status"] = nvlink.stdout.strip()
    if include_torch:
        try:
            import torch

            info["torch_version"] = torch.__version__
            info["torch_cuda_version"] = torch.version.cuda
            info["cuda_version"] = torch.version.cuda
            info["torch_cuda_available"] = bool(torch.cuda.is_available())
            if info["torch_cuda_available"]:
                info["nccl_version"] = _try(lambda: ".".join(map(str, torch.cuda.nccl.version())))
                info["torch_device_count"] = torch.cuda.device_count()
                info["torch_arch_list"] = _try(torch.cuda.get_arch_list)
        except Exception as exc:
            info["torch_error"] = str(exc)
    from importlib import metadata

    for package in ("torch", "vllm", "transformers", "nvidia-ml-py", "textual"):
        info["packages"][package] = _try(lambda: metadata.version(package))
    return info


def _version_tuple(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", value))


def health_checks(info: dict | None = None) -> dict:
    """PASS/FAIL for what benchmarks need; N/A for optional hardware features."""
    info = info or collect_system_info()
    checks = []

    def add(name, status, detail):
        checks.append({"name": name, "status": status, "detail": detail})

    gpus = info.get("gpus") or []
    add("NVIDIA GPU visible", "PASS" if gpus else "FAIL",
        f"{len(gpus)} GPU(s): {', '.join(dict.fromkeys(g['name'] for g in gpus))}" if gpus else
        (f"NVML: {info.get('nvml_error')}. " if info.get("nvml_error") else "")
        + "No NVIDIA GPU is visible. Run with --device nvidia.com/gpu=all (CDI) and check nvidia-ctk cdi list.")
    add("NVIDIA driver", "PASS" if info.get("driver_version") else "FAIL",
        f"{info.get('driver_version')} (supports CUDA {info.get('cuda_driver_version') or 'N/A'})"
        if info.get("driver_version") else "The host driver could not be read through NVML.")
    cuda = str(info.get("torch_cuda_version") or "")
    required = next((minimum for prefix, minimum in DRIVER_MINIMUMS.items() if cuda.startswith(prefix)), None)
    driver = str(info.get("driver_version") or "")
    if required and re.match(r"^\d+(\.\d+)+$", driver):
        ok = _version_tuple(driver) >= _version_tuple(required)
        add("CUDA driver compatibility", "PASS" if ok else "FAIL",
            f"Driver {driver} {'meets' if ok else 'is below'} the CUDA {cuda} minimum {required}."
            + ("" if ok else " Upgrade the host driver; the container cannot."))
    else:
        add("CUDA driver compatibility", "N/A", "Needs both a readable driver version and PyTorch CUDA.")
    if info.get("torch_error"):
        add("PyTorch CUDA", "FAIL", f"PyTorch cannot be imported: {info['torch_error']}")
    else:
        add("PyTorch CUDA", "PASS" if info.get("torch_cuda_available") else "FAIL",
            f"PyTorch {info.get('torch_version')} CUDA {info.get('torch_cuda_version')}: "
            + ("available" if info.get("torch_cuda_available") else "no usable CUDA device"))
    arch_list = info.get("torch_arch_list") or []
    for gpu in gpus:
        capability = gpu.get("compute_capability")
        if capability and arch_list:
            sm = int(capability.replace(".", ""))
            native = f"sm_{sm}" in arch_list
            ptx = any(a.startswith("compute_") and a[8:].isdigit() and int(a[8:]) <= sm for a in arch_list)
            add(f"GPU {gpu['index']} architecture", "PASS" if native or ptx else "FAIL",
                f"{gpu['name']} is sm_{sm}; PyTorch was built for {', '.join(arch_list)}")
    for gpu in gpus:
        ecc = gpu.get("ecc") or {}
        if ecc.get("volatile_uncorrected"):
            add(f"GPU {gpu['index']} ECC", "WARN", f"{ecc['volatile_uncorrected']} volatile uncorrected ECC errors")
        if (gpu.get("mig_mode") or {}).get("current") == "Enabled":
            add(f"GPU {gpu['index']} MIG", "WARN", "MIG is enabled; benchmarks see MIG instances, not the full GPU.")
        temperature = gpu.get("temperature_c")
        if isinstance(temperature, (int, float)) and temperature >= 85:
            add(f"GPU {gpu['index']} temperature", "WARN", f"{temperature} C before any benchmark ran")
    add("NCCL", "PASS" if info.get("nccl_version") else "N/A",
        f"NCCL {info['nccl_version']}" if info.get("nccl_version") else "No CUDA device, so NCCL was not initialised.")
    shm = (info.get("host") or {}).get("shm_bytes")
    if isinstance(shm, int):
        add("Shared memory (/dev/shm)", "PASS" if shm >= 1 << 30 else "WARN",
            f"{shm / 2**30:.1f} GiB" + ("" if shm >= 1 << 30 else "; pass --shm-size=16g for NCCL and vLLM"))
    rdma = info.get("rdma_devices") or []
    add("RDMA devices", "PASS" if rdma else "N/A",
        ", ".join(f"{d['name']} ({d.get('transport') or 'unknown'})" for d in rdma) if rdma
        else "No RDMA device exposed (only needed for multi-node).")
    models = Path(os.environ.get("GPU_BENCH_MODELS", "/models"))
    has_models = models.is_dir() and any(models.iterdir())
    add("Local models", "PASS" if has_models else "N/A",
        f"{models} is mounted" if has_models
        else f"Nothing under {models}; LLM tests need -v /models:/models:ro. Compute, memory and NCCL still work.")
    results = Path(os.environ.get("GPU_BENCH_RESULTS", "/results"))
    writable = results.is_dir() and os.access(results, os.W_OK)
    add("Results directory", "PASS" if writable else "WARN",
        f"{results} is writable" if writable else f"{results} is missing or read-only; mount -v /opt/gpu-results:/results:Z")
    status = "FAIL" if any(check["status"] == "FAIL" for check in checks) else "PASS"
    return {"status": status, "checks": checks}


# ------------------------------------------------------------- sampling

class GpuMonitor:
    """Polls NVML in a thread; keeps the latest snapshot and a short history.

    The TUI uses it for the live view; benchmarks use it as a sampler to
    attach GPU utilisation, memory and power to each result.
    """

    def __init__(self, interval: float = 1.0, history: int = 120, indices: list[int] | None = None):
        self.interval = interval
        self.indices = indices
        self.latest: list[dict] = []
        self.history: collections.deque = collections.deque(maxlen=history)
        self.samples: list[dict] = []
        self.error: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._record = False

    def start(self, record: bool = False) -> "GpuMonitor":
        self._record = record
        self._thread = threading.Thread(target=self._loop, name="gpu-monitor", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.interval + 2)

    def __enter__(self):
        return self.start(record=True)

    def __exit__(self, *_):
        self.stop()

    def _loop(self) -> None:
        try:
            pynvml = _nvml()
        except Exception as exc:
            self.error = str(exc) or type(exc).__name__
            return
        try:
            count = pynvml.nvmlDeviceGetCount()
            indices = [i for i in (self.indices if self.indices is not None else range(count)) if i < count]
            handles = [(i, pynvml.nvmlDeviceGetHandleByIndex(i)) for i in indices]
            while not self._stop.is_set():
                snapshot = []
                for index, handle in handles:
                    memory = _try(lambda: pynvml.nvmlDeviceGetMemoryInfo(handle))
                    snapshot.append({
                        "index": index,
                        "utilization_percent": _try(lambda: pynvml.nvmlDeviceGetUtilizationRates(handle).gpu),
                        "memory_used_bytes": int(memory.used) if memory else None,
                        "memory_total_bytes": int(memory.total) if memory else None,
                        "power_watts": _try(lambda: pynvml.nvmlDeviceGetPowerUsage(handle) / 1000),
                        "temperature_c": _try(lambda: pynvml.nvmlDeviceGetTemperature(
                            handle, pynvml.NVML_TEMPERATURE_GPU)),
                        "sm_clock_mhz": _try(lambda: pynvml.nvmlDeviceGetClockInfo(handle, pynvml.NVML_CLOCK_SM)),
                    })
                self.latest = snapshot
                self.history.append({"at": time.time(), "gpus": snapshot})
                if self._record:
                    self.samples.append({"at": time.time(), "gpus": snapshot})
                self._stop.wait(self.interval)
        except Exception as exc:
            self.error = str(exc)
        finally:
            _try(pynvml.nvmlShutdown)

    def summary(self) -> dict:
        """What NVML measured during the benchmark window."""
        if not self.samples:
            return {"samples": 0, "note": self.error or "No NVML samples (no GPU visible or the run was too short)."}
        per_gpu = collections.defaultdict(lambda: {"util": [], "mem": [], "power": [], "temp": []})
        for sample in self.samples:
            for gpu in sample["gpus"]:
                bucket = per_gpu[gpu["index"]]
                for key, field in (("util", "utilization_percent"), ("mem", "memory_used_bytes"),
                                   ("power", "power_watts"), ("temp", "temperature_c")):
                    if gpu.get(field) is not None:
                        bucket[key].append(gpu[field])

        def mean(values):
            return sum(values) / len(values) if values else None

        gpus = []
        for index, bucket in sorted(per_gpu.items()):
            gpus.append({"index": index, "utilization_mean_percent": mean(bucket["util"]),
                         "utilization_max_percent": max(bucket["util"], default=None),
                         "memory_peak_bytes": max(bucket["mem"], default=None),
                         "power_mean_watts": mean(bucket["power"]),
                         "power_max_watts": max(bucket["power"], default=None),
                         "temperature_max_c": max(bucket["temp"], default=None)})
        utils = [g["utilization_mean_percent"] for g in gpus if g["utilization_mean_percent"] is not None]
        powers = [g["power_mean_watts"] for g in gpus if g["power_mean_watts"] is not None]
        memory = [g["memory_peak_bytes"] for g in gpus if g["memory_peak_bytes"] is not None]
        return {"samples": len(self.samples), "interval_seconds": self.interval, "gpus": gpus,
                "utilization_mean_percent": mean(utils),
                "power_mean_watts_total": sum(powers) if powers else None,
                "memory_peak_bytes_total": sum(memory) if memory else None}


# The 0.5.0 name.
NvmlSampler = GpuMonitor
