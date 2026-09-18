#!/usr/bin/env bash
# Demo status pane -- macOS-safe. There is NO `watch` on macOS (GNU procps), and
# no `timeout` either, so this is a plain shell redraw loop.
#
#   export KUBECONFIG=/tmp/hami.kube
#   bash scripts/demo-watch.sh
#
# PERFORMANCE NOTE (measured): a kubectl round-trip through the SSM tunnel costs
# ~3.5 s. A naive pane that ran one kubectl per section took ~21 s per frame and
# never finished a refresh. So this makes exactly ONE `kubectl get` call per frame
# and formats it in Python -> ~4 s per frame.
#
# Env: INTERVAL (default 3s pause between frames), NS (default default)
set -uo pipefail

INTERVAL="${INTERVAL:-3}"
NS="${NS:-default}"

cleanup() { printf '\033[?25h'; echo; exit 0; }
trap cleanup INT TERM
printf '\033[?25l'    # hide cursor to reduce flicker

while true; do
  frame=$(kubectl get nodes,pods,deploy,hpa -n "$NS" -o json 2>/dev/null | python3 -c '
import json, sys
try:
    items = json.load(sys.stdin).get("items", [])
except Exception:
    print("  (cluster unreachable -- is the SSM tunnel up? re-run scripts/connect-private.sh)")
    sys.exit()

nodes = [i for i in items if i["kind"] == "Node"]
pods  = [i for i in items if i["kind"] == "Pod"]
deps  = [i for i in items if i["kind"] == "Deployment"]
hpas  = [i for i in items if i["kind"] == "HorizontalPodAutoscaler"]

gpu_nodes = [n for n in nodes
             if (n["metadata"].get("labels") or {}).get("pool") == "karpenter"]

out = []
out.append("=== GPU CARDS: %d ===" % len(gpu_nodes))
for n in gpu_nodes:
    ready = next((c["status"] for c in n["status"].get("conditions", [])
                  if c["type"] == "Ready"), "?")
    vgpu = (n["status"].get("allocatable") or {}).get("nvidia.com/gpu", "-")
    short = n["metadata"]["name"].replace(".ec2.internal", "")
    out.append("  %-22s ready=%-6s vGPU=%s" % (short, ready, vgpu))
if not gpu_nodes:
    out.append("  (none)")

out.append("")
out.append("=== HEADROOM ===")
for d in deps:
    if d["metadata"]["name"] == "gpu-buffer":
        spec = d["spec"].get("replicas", 0)
        rdy  = (d.get("status") or {}).get("readyReplicas", 0)
        out.append("  gpu-buffer  replicas=%s ready=%s" % (spec, rdy))
for h in hpas:
    st = h.get("status") or {}
    cur = st.get("currentMetrics") or []
    val = "-"
    if cur and cur[0].get("external"):
        val = (cur[0]["external"].get("current") or {}).get("averageValue", "-")
    out.append("  keda hpa    metric=%-8s desired=%s" % (val, st.get("desiredReplicas", "-")))

out.append("")
out.append("=== PODS ===")
if not pods:
    out.append("  (none)")
for p in sorted(pods, key=lambda x: x["metadata"]["name"]):
    node = (p["spec"].get("nodeName") or "-").replace(".ec2.internal", "")
    out.append("  %-30s %-12s %s" % (p["metadata"]["name"][:30],
                                     p["status"].get("phase", "?"), node))
print("\n".join(out))
')
  clear 2>/dev/null || printf '\033[2J\033[H'
  printf '%s\n' "$frame"
  printf '\n(one kubectl call/frame ~4s, then %ss pause -- Ctrl-C to stop)\n' "$INTERVAL"
  sleep "$INTERVAL"
done
