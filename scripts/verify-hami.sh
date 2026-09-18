#!/usr/bin/env bash
# Verify that HAMi is installed and the GPU node is advertising vGPU resources.
#
# Prereqs: kubectl configured against the cluster (see docs/02-provision-cluster.md).
#
#   ./scripts/verify-hami.sh
set -euo pipefail

NS="${HAMI_NAMESPACE:-kube-system}"

echo "==> HAMi pods in namespace '${NS}'"
kubectl get pods -n "${NS}" -l 'app.kubernetes.io/component in (hami-scheduler,hami-device-plugin)' -o wide \
  || kubectl get pods -n "${NS}" | grep -i hami || true

echo
echo "==> GPU nodes (label gpu=on)"
kubectl get nodes -l gpu=on -o wide

echo
echo "==> Allocatable GPU resources per node"
kubectl get nodes -o json \
  | python3 -c '
import json, sys
data = json.load(sys.stdin)
for node in data["items"]:
    name = node["metadata"]["name"]
    alloc = node["status"].get("allocatable", {})
    gpu = {k: v for k, v in alloc.items() if k.startswith("nvidia.com/")}
    if gpu:
        print(f"  {name}: {gpu}")
'

echo
echo "If you see nvidia.com/gpu (a count > number of physical GPUs), plus"
echo "nvidia.com/gpumem and nvidia.com/gpucores, HAMi is working."
