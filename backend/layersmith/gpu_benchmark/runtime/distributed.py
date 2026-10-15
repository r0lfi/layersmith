"""Multi-node NCCL collectives through torch.distributed (launched by torchrun).

torchrun's TCP rendezvous needs no MPI and no SSH between containers, so one
`podman run ... torchrun ...` per node (by hand or from Slurm) is enough. The
collectives are NCCL's own; timing and the algbw/busbw definitions follow
nccl-tests, so the numbers are comparable with `gpu-bench nccl`.

NCCL is asked to log its INIT/NET subsystem to a file per process; rank 0
reads its own file to report which transport NCCL actually used (IB/RoCE via
verbs, or plain sockets), which is the practical check that RDMA works.
"""

from __future__ import annotations

import os
import re
import socket
import tempfile
import time
from pathlib import Path

from .benchmarks import BenchmarkError, Unavailable
from .events import EMITTER

COLLECTIVES = ("all_reduce", "all_gather", "reduce_scatter", "broadcast", "alltoall")
# nccl-tests bus bandwidth factors (doc/PERFORMANCE.md).
BUS_FACTOR = {"all_reduce": lambda n: 2 * (n - 1) / n, "all_gather": lambda n: (n - 1) / n,
              "reduce_scatter": lambda n: (n - 1) / n, "broadcast": lambda n: 1.0,
              "alltoall": lambda n: (n - 1) / n}


def parse_size(value: str | int) -> int:
    if isinstance(value, int):
        return value
    match = re.fullmatch(r"\s*(\d+)\s*([KMGT]?)i?B?\s*", str(value), re.I)
    if not match:
        raise BenchmarkError(f"Size must be like 8, 64K, 128M or 1G, not {value!r}")
    return int(match.group(1)) * 1024 ** "_KMGT".index(match.group(2).upper() or "_")


def prepare_nccl_logging() -> Path:
    """Before NCCL starts: log INIT/NET per process unless the user set NCCL_DEBUG."""
    directory = Path(tempfile.gettempdir()) / "gpu-bench-nccl"
    directory.mkdir(parents=True, exist_ok=True)
    if not os.environ.get("NCCL_DEBUG"):
        os.environ["NCCL_DEBUG"] = "INFO"
        os.environ.setdefault("NCCL_DEBUG_SUBSYS", "INIT,NET")
        os.environ.setdefault("NCCL_DEBUG_FILE", str(directory / "nccl.%h.%p.log"))
    return directory


def nccl_transport(text: str) -> dict:
    """What NCCL said about its network in INFO logs."""
    ib = re.findall(r"NET/IB\s*:\s*Using\s*(.+)", text)
    sockets = re.findall(r"NET/Socket\s*:\s*Using\s*(.+)", text)
    gdr = re.search(r"GPU Direct RDMA (Enabled|Disabled)[^\n]*", text)
    plugin = re.search(r"NET/Plugin[^\n]*", text)
    if ib:
        transport = "RDMA (IB verbs: InfiniBand or RoCE)"
    elif sockets:
        transport = "TCP sockets (no RDMA)"
    else:
        transport = None
    return {"transport": transport, "ib_devices": ib[0].strip() if ib else None,
            "socket_interfaces": sockets[0].strip() if sockets else None,
            "gpudirect_rdma": gdr.group(0).strip() if gdr else None,
            "net_plugin": plugin.group(0).strip() if plugin else None}


def _sizes(min_bytes: int, max_bytes: int) -> list[int]:
    sizes, size = [], max(min_bytes, 8)
    while size <= max_bytes:
        sizes.append(size)
        size *= 2
    return sizes


def run(collectives=COLLECTIVES, min_bytes="1M", max_bytes="1G", iterations: int = 20, warmup: int = 5) -> list[dict]:
    """Every rank calls this; only rank 0 returns entries (others return [])."""
    if "RANK" not in os.environ or "WORLD_SIZE" not in os.environ:
        raise Unavailable("gpu-bench dist runs under torchrun (it sets RANK/WORLD_SIZE). "
                          "Use /opt/gpu-bench/scripts/multinode-torchrun.sh.")
    log_dir = prepare_nccl_logging()
    import torch
    import torch.distributed as dist

    if not torch.cuda.is_available():
        raise Unavailable("No CUDA device visible on this node.")
    local_rank = int(os.environ.get("LOCAL_RANK", 0))
    torch.cuda.set_device(local_rank)
    dist.init_process_group("nccl", device_id=torch.device("cuda", local_rank))
    rank, world = dist.get_rank(), dist.get_world_size()
    if isinstance(collectives, str):
        collectives = list(COLLECTIVES) if collectives == "all" else [c.strip() for c in collectives.split(",") if c.strip()]
    unknown = [c for c in collectives if c not in COLLECTIVES]
    if unknown:
        raise BenchmarkError(f"Unknown collective {', '.join(unknown)}; choose from {', '.join(COLLECTIVES)}")
    sizes = _sizes(parse_size(min_bytes), parse_size(max_bytes))
    hosts = [None] * world
    dist.all_gather_object(hosts, socket.gethostname())
    nodes = sorted(set(hosts))
    entries = []
    try:
        for name in collectives:
            EMITTER.check_stop()
            rows = []
            for size in sizes:
                elements = max(world, size // 4 // world * world)  # float32, divisible by world
                tensor = torch.ones(elements, dtype=torch.float32, device="cuda")
                if name == "all_gather":
                    shard = torch.ones(elements // world, dtype=torch.float32, device="cuda")

                    def op():
                        dist.all_gather_into_tensor(tensor, shard)
                elif name == "reduce_scatter":
                    out = torch.empty(elements // world, dtype=torch.float32, device="cuda")

                    def op():
                        dist.reduce_scatter_tensor(out, tensor)
                elif name == "broadcast":
                    def op():
                        dist.broadcast(tensor, src=0)
                elif name == "alltoall":
                    out = torch.empty_like(tensor)

                    def op():
                        dist.all_to_all_single(out, tensor)
                else:
                    def op():
                        dist.all_reduce(tensor)
                for _ in range(warmup):
                    op()
                torch.cuda.synchronize()
                dist.barrier()
                start, end = torch.cuda.Event(enable_timing=True), torch.cuda.Event(enable_timing=True)
                start.record()
                for _ in range(iterations):
                    op()
                end.record()
                end.synchronize()
                elapsed = torch.tensor([start.elapsed_time(end) / 1000 / iterations], device="cuda")
                dist.all_reduce(elapsed, op=dist.ReduceOp.MAX)  # the slowest rank defines the time
                seconds = float(elapsed.item())
                nbytes = elements * 4
                algbw = nbytes / seconds / 1e9
                rows.append({"bytes": nbytes, "time_us": seconds * 1e6, "algbw_gb_per_second": algbw,
                             "busbw_gb_per_second": algbw * BUS_FACTOR[name](world)})
                if rank == 0:
                    EMITTER.progress("nccl", test=name, size=nbytes, summary=f"{name} {nbytes} B: "
                                                                            f"{rows[-1]['busbw_gb_per_second']:.1f} GB/s busbw")
                del tensor
            if rank == 0:
                peak = max(rows, key=lambda r: r["busbw_gb_per_second"])
                large = [r["busbw_gb_per_second"] for r in rows if r["bytes"] >= 1 << 20]
                entry = {"test": "nccl", "name": f"NCCL {name} (torch.distributed)", "status": "PASSED",
                         "parameters": {"collective": name, "min_bytes": str(min_bytes), "max_bytes": str(max_bytes),
                                        "iterations": iterations, "ranks": world, "nodes": len(nodes),
                                        "gpus_per_rank": 1, "launcher": "torchrun", "multi_node": len(nodes) > 1},
                         "results": {"collective": name, "rows": rows, "nodes": nodes,
                                     "peak_busbw_gb_per_second": peak["busbw_gb_per_second"],
                                     "peak_algbw_gb_per_second": max(r["algbw_gb_per_second"] for r in rows),
                                     "peak_at_bytes": peak["bytes"],
                                     "avg_busbw_gb_per_second": sum(large) / len(large) if large else None,
                                     "measurement_note": "torch.distributed NCCL collectives, CUDA-event timed, "
                                                         "slowest rank; busbw factors as nccl-tests."},
                         "error": None}
                EMITTER.result(entry)
                entries.append(entry)
        if rank == 0 and entries:
            time.sleep(0.2)
            text = ""
            for path in log_dir.glob(f"nccl.{socket.gethostname()}.{os.getpid()}.log"):
                text += path.read_text(errors="replace")
            transport = nccl_transport(text)
            for entry in entries:
                entry["results"]["nccl_network"] = transport
            EMITTER.log(f"NCCL network: {transport.get('transport') or 'not reported'} "
                        f"{transport.get('ib_devices') or transport.get('socket_interfaces') or ''}")
    finally:
        dist.destroy_process_group()
    return entries if rank == 0 else []
