#!/usr/bin/env bash
# One-shot GPU smoke test: runs nvidia-smi inside a HAMi-managed vGPU slice and
# tears the pod down afterwards. Confirms end-to-end that a workload can be
# scheduled onto a sliced GPU.
#
#   ./scripts/gpu-smoke-test.sh
set -euo pipefail

POD="hami-smoke-test"

cleanup() { kubectl delete pod "${POD}" --ignore-not-found >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "==> Launching smoke-test pod (1 GPU, 2 GiB slice, 20% cores)"
kubectl run "${POD}" --restart=Never \
  --image=nvidia/cuda:12.2.0-base-ubuntu22.04 \
  --overrides='{
    "spec": {
      "containers": [{
        "name": "cuda",
        "image": "nvidia/cuda:12.2.0-base-ubuntu22.04",
        "command": ["nvidia-smi"],
        "resources": { "limits": {
          "nvidia.com/gpu": 1,
          "nvidia.com/gpumem": 2000,
          "nvidia.com/gpucores": 20
        }}
      }]
    }
  }' \
  --command -- nvidia-smi

echo "==> Waiting for completion..."
kubectl wait --for=jsonpath='{.status.phase}'=Succeeded pod/"${POD}" --timeout=180s \
  || kubectl wait --for=jsonpath='{.status.phase}'=Running pod/"${POD}" --timeout=180s || true

echo "==> nvidia-smi output from inside the slice:"
kubectl logs "${POD}"

echo
echo "The 'Memory' column should show ~2000 MiB (the slice), not the full GPU."
