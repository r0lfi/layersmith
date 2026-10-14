"""GPU and host discovery using the host driver's NVML interface."""

from __future__ import annotations

import datetime as dt
import platform
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path


def _text(value) -> str:
    return value.decode(errors="replace") if isinstance(value, bytes) else str(value)


def run(args: list[str], timeout: int = 20) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(args, 127, "", str(exc))


def _nvml_rows() -> list[dict]:
    try:
        import pynvml

        pynvml.nvmlInit()
        rows = []
        for index in range(pynvml.nvmlDeviceGetCount()):
            handle = pynvml.nvmlDeviceGetHandleByIndex(index)
            memory = pynvml.nvmlDeviceGetMemoryInfo(handle)
            row = {
                "index": index,
                "name": _text(pynvml.nvmlDeviceGetName(handle)),
                "uuid": _text(pynvml.nvmlDeviceGetUUID(handle)),
                "memory_total_bytes": int(memory.total),
                "memory_free_bytes": int(memory.free),
                "memory_used_bytes": int(memory.used),
                "driver_version": _text(pynvml.nvmlSystemGetDriverVersion()),
            }
            try:
                pci = pynvml.nvmlDeviceGetPciInfo(handle)
                row["pci_bus_id"] = pci.busId.decode(errors="replace")
            except Exception:
                row["pci_bus_id"] = None
            for key, getter in (("temperature_c", lambda: pynvml.nvmlDeviceGetTemperature(
                    handle, pynvml.NVML_TEMPERATURE_GPU)),
                                ("power_watts", lambda: pynvml.nvmlDeviceGetPowerUsage(handle) / 1000)):
                try:
                    row[key] = getter()
                except Exception:
                    row[key] = None
            try:
                row["utilization_percent"] = pynvml.nvmlDeviceGetUtilizationRates(handle).gpu
            except Exception:
                row["utilization_percent"] = None
            try:
                row["pcie_link"] = {
                    "generation": pynvml.nvmlDeviceGetCurrPcieLinkGeneration(handle),
                    "width": pynvml.nvmlDeviceGetCurrPcieLinkWidth(handle),
                }
            except Exception:
                row["pcie_link"] = None
            try:
                row["compute_capability"] = ".".join(map(str, pynvml.nvmlDeviceGetCudaComputeCapability(handle)))
            except Exception:
                row["compute_capability"] = None
            links = []
            if hasattr(pynvml, "nvmlDeviceGetNvLinkState"):
                for link in range(12):
                    try:
                        if pynvml.nvmlDeviceGetNvLinkState(handle, link):
                            links.append(link)
                    except Exception:
                        break
            row["nvlink_active_links"] = links
            rows.append(row)
        pynvml.nvmlShutdown()
        return rows
    except Exception:
        return []


def collect_system_info() -> dict:
    """Collect useful facts without requiring a GPU to be present."""
    info = {
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "driver_version": None,
        "cuda_version": None,
        "gpu_count": 0,
        "gpus": [],
        "gpu_topology": None,
        "nvlink_status": None,
        "torch_cuda_available": False,
        "rdma_devices": [],
        "network_interfaces": [],
    }
    rows = _nvml_rows()
    info["gpus"] = rows
    info["gpu_count"] = len(rows)
    if rows:
        info["driver_version"] = rows[0].get("driver_version")
    smi = shutil.which("nvidia-smi")
    if smi:
        topology = run([smi, "topo", "-m"], timeout=5)
        if topology.returncode == 0:
            info["gpu_topology"] = topology.stdout.strip()
        nvlink = run([smi, "nvlink", "--status"], timeout=5)
        if nvlink.returncode == 0:
            info["nvlink_status"] = nvlink.stdout.strip()
        elif any(row.get("nvlink_active_links") for row in rows):
            info["nvlink_status"] = "Active links reported by NVML"
    try:
        import torch

        info["torch_version"] = torch.__version__
        info["torch_cuda_version"] = torch.version.cuda
        info["torch_cuda_available"] = bool(torch.cuda.is_available())
        info["cuda_version"] = torch.version.cuda
        for index, gpu in enumerate(info["gpus"]):
            try:
                props = torch.cuda.get_device_properties(index)
                gpu["compute_capability"] = f"{props.major}.{props.minor}"
                gpu["torch_device_name"] = props.name
                free, total = torch.cuda.mem_get_info(index)
                gpu["memory_free_bytes"] = int(free)
                gpu["memory_total_bytes"] = int(total)
            except Exception:
                pass
    except Exception as exc:
        info["torch_error"] = str(exc)
    nvcc = shutil.which("nvcc")
    if nvcc:
        result = run([nvcc, "--version"], timeout=5)
        for line in result.stdout.splitlines():
            if "release " in line:
                info["cuda_toolkit_version"] = line.split("release ", 1)[1].split(",", 1)[0]
                info["cuda_version"] = info.get("cuda_version") or info["cuda_toolkit_version"]
                break
    nccl = run(["rdma", "link", "show"])
    if nccl.returncode == 0:
        info["rdma_devices"] = [line.strip() for line in nccl.stdout.splitlines() if line.strip()]
    interfaces = run(["ip", "-brief", "link"])
    if interfaces.returncode == 0:
        info["network_interfaces"] = [line.strip() for line in interfaces.stdout.splitlines() if line.strip()]
    return info


def health_checks(info: dict | None = None) -> dict:
    info = info or collect_system_info()
    checks = []
    cuda_ok = info.get("gpu_count", 0) > 0 and bool(info.get("torch_cuda_available"))
    checks.append({"name": "CUDA GPU access", "status": "PASS" if cuda_ok else "FAIL",
                   "detail": (f"{info['gpu_count']} GPU(s); PyTorch CUDA {info.get('torch_cuda_version')}"
                              if cuda_ok else "No NVIDIA GPU is visible to the container or PyTorch CUDA is unavailable.")})
    torch_ok = bool(info.get("gpus")) and bool(info.get("torch_cuda_available")) and not info.get("torch_error")
    checks.append({"name": "PyTorch CUDA", "status": "PASS" if torch_ok else "FAIL",
                   "detail": f"PyTorch {info.get('torch_version') or 'not importable'}"})
    checks.append({"name": "NVIDIA driver", "status": "PASS" if info.get("driver_version") else "FAIL",
                   "detail": info.get("driver_version") or "NVML could not read the host driver."})
    cuda_version = str(info.get("torch_cuda_version") or info.get("cuda_version") or "")
    driver_minimums = {"13.0": "580.65.06", "12.8": "570.26.00"}
    required_driver = next((version for prefix, version in driver_minimums.items()
                            if cuda_version.startswith(prefix)), None)
    driver = str(info.get("driver_version") or "")
    if required_driver and re.match(r"^\d+(?:\.\d+)+$", driver):
        actual_parts = tuple(int(part) for part in driver.split("."))
        needed_parts = tuple(int(part) for part in required_driver.split("."))
        compatible = actual_parts >= needed_parts
        detail = (f"Host driver {driver} meets CUDA {cuda_version} minimum {required_driver}."
                  if compatible else
                  f"Host driver {driver} is below CUDA {cuda_version} minimum {required_driver}. Upgrade the host driver.")
        checks.append({"name": "CUDA driver compatibility", "status": "PASS" if compatible else "FAIL",
                       "detail": detail})
    else:
        checks.append({"name": "CUDA driver compatibility", "status": "OPTIONAL",
                       "detail": "CUDA runtime or numeric NVIDIA driver version could not be checked."})
    free_bytes = sum(int(gpu.get("memory_free_bytes") or 0) for gpu in info.get("gpus", []))
    checks.append({"name": "GPU memory", "status": "PASS" if free_bytes > 0 else "FAIL",
                   "detail": f"{free_bytes} bytes available across visible GPUs"})
    try:
        import torch

        nccl_version = torch.cuda.nccl.version() if torch.cuda.is_available() else None
    except Exception:
        nccl_version = None
    checks.append({"name": "NCCL", "status": "PASS" if nccl_version else "OPTIONAL",
                   "detail": f"NCCL {nccl_version}" if nccl_version else "NCCL is unavailable or no GPU is visible."})
    rdma = bool(info.get("rdma_devices"))
    checks.append({"name": "RDMA devices", "status": "PASS" if rdma else "OPTIONAL",
                   "detail": "RDMA devices detected" if rdma else "No RDMA device is exposed to the container."})
    interfaces = info.get("network_interfaces") or []
    checks.append({"name": "Network interfaces", "status": "PASS" if interfaces else "OPTIONAL",
                   "detail": ", ".join(interfaces) if interfaces else "The ip command could not enumerate interfaces."})
    return {"status": "FAIL" if any(check["status"] == "FAIL" for check in checks) else "PASS",
            "checks": checks}


class NvmlSampler:
    """Lightweight background GPU-utilization and VRAM sampler."""

    def __init__(self, interval: float = 1.0):
        self.interval = interval
        self.samples: list[dict] = []
        self._stop = False
        self._thread = None

    def __enter__(self):
        import threading

        self._thread = threading.Thread(target=self._sample, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_):
        self._stop = True
        if self._thread:
            self._thread.join(timeout=3)

    def _sample(self):
        try:
            import pynvml

            pynvml.nvmlInit()
            count = pynvml.nvmlDeviceGetCount()
            while not self._stop:
                sample = {"at": time.time(), "gpus": []}
                for index in range(count):
                    handle = pynvml.nvmlDeviceGetHandleByIndex(index)
                    mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
                    try:
                        util = pynvml.nvmlDeviceGetUtilizationRates(handle).gpu
                    except Exception:
                        util = None
                    sample["gpus"].append({"memory_used_bytes": int(mem.used), "utilization_percent": util})
                self.samples.append(sample)
                time.sleep(self.interval)
            pynvml.nvmlShutdown()
        except Exception:
            return

    def summary(self) -> dict:
        if not self.samples:
            return {"samples": 0, "memory_peak_bytes": None, "gpu_utilization_mean_percent": None}
        memory = [gpu["memory_used_bytes"] for sample in self.samples for gpu in sample["gpus"]]
        util = [gpu["utilization_percent"] for sample in self.samples for gpu in sample["gpus"]
                if gpu["utilization_percent"] is not None]
        return {"samples": len(self.samples), "memory_peak_bytes": max(memory, default=None),
                "gpu_utilization_mean_percent": sum(util) / len(util) if util else None}
