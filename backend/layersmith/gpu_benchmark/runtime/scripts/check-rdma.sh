#!/bin/sh
# Is RDMA (InfiniBand or RoCE) usable here? Run on the host or in the
# container started with --device /dev/infiniband --network=host.
echo "== RDMA devices";      ls /sys/class/infiniband 2>/dev/null || echo "none"
echo "== uverbs";            ls /dev/infiniband 2>/dev/null || echo "/dev/infiniband not present"
echo "== rdma link";         rdma link show 2>/dev/null || echo "rdma tool unavailable"
echo "== ibv_devinfo";       ibv_devinfo 2>/dev/null | grep -E "hca_id|state|link_layer|active_mtu|active_speed" || echo "ibv_devinfo failed"
echo "== RoCE GIDs (use the RoCE v2 index for NCCL_IB_GID_INDEX)"
for dev in /sys/class/infiniband/*; do
  [ -d "$dev" ] || continue
  for port in "$dev"/ports/*; do
    for gid in "$port"/gids/*; do
      value=$(cat "$gid" 2>/dev/null) || continue
      [ "$value" = "0000:0000:0000:0000:0000:0000:0000:0000" ] && continue
      type=$(cat "$port/gid_attrs/types/$(basename "$gid")" 2>/dev/null || echo "?")
      echo "$(basename "$dev") port $(basename "$port") index $(basename "$gid"): $value ($type)"
    done
  done
done
echo "== memlock limit";     ulimit -l
echo
echo "Bandwidth between two nodes (perftest), e.g. 400G RoCE:"
echo "  node2:  ib_write_bw -d mlx5_0 -x 3 --report_gbits"
echo "  node1:  ib_write_bw -d mlx5_0 -x 3 --report_gbits node2"
echo "Then check NCCL really uses it: gpu-bench dist/nccl report 'NCCL network: RDMA (IB verbs ...)'."
