# DEMO RUNBOOK — Stop Wasting GPUs (HAMi + Karpenter + KEDA)

**The single guide to run the session.** Every command, what it does, and the
mechanism behind it. Companion: `docs/10-hami-karpenter-demo.md` (deeper *why*),
`docs/12-slides.md` (the deck).

Timings marked **MEASURED** were observed on this cluster. State verified
2026-09-16: 1 warm card, HAMi/Karpenter/KEDA all healthy, `defaultMemory: 3800`,
`deviceSplitCount: 4`, `deviceCoreScaling: 4`.

---

## Read this first — three things that will bite you

1. **Every `kubectl` costs ~3.5 s** through the SSM tunnel (MEASURED: 3.69/3.45/3.26).
   Nothing is instant. Keep talking while commands land.
2. **The tunnel drops silently** when idle — it did so repeatedly in testing.
   Symptom: `connection refused` on everything. Fix: re-run
   `./scripts/connect-private.sh`. Keep that terminal visible.
3. **Never `kubectl delete` a `nodepool` or `ec2nodeclass`.** Their finalizers call
   the IAM API, which is unreachable in this air-gapped VPC, so they hang in
   `Terminating` forever and take the NodePool down with them. Recovery in §6.

macOS has **no `watch`** and **no `timeout`** — use the provided scripts.

---

## 0. Three terminals

```bash
cd .
export KUBECONFIG=/tmp/hami.kube
```

| Terminal | Runs | Why |
|---|---|---|
| **A** | your demo commands | |
| **B** | `bash scripts/demo-watch.sh` | status pane; leave up |
| **C** | `./scripts/connect-private.sh` | SSM tunnel; leave up |

> `KUBECONFIG` points at the tunnel-aware config: `server` is
> `https://localhost:6443` but `tls-server-name` stays the real EKS hostname, so the
> API server's cert still validates through the tunnel.
>
> **B** makes exactly ONE `kubectl get nodes,pods,deploy,hpa -o json` per frame and
> formats it in Python. One call per section took ~21 s/frame and never refreshed.

---

## 1. Setup — T-20 min, before the audience arrives

```bash
# terminal C
export AWS_PROFILE=<your-aws-profile>
./scripts/connect-private.sh
```
> Finds the bastion by tag, rewrites `/tmp/hami.kube`, then runs `aws ssm
> start-session` with `AWS-StartPortForwardingSessionToRemoteHost`. Path:
> `kubectl → localhost:6443 → SSM → bastion → EKS private ENI`. No IGW/NAT exists,
> so this is the only route to the API server.

```bash
# terminal A — these are idempotent; safe to re-run
kubectl apply -f manifests/rendered/00-priorityclasses.yaml
kubectl apply -f manifests/rendered/01-headroom-pods.yaml
kubectl apply -f manifests/rendered/04-keda-scaledobject.yaml
```
> `00` creates `gpu-buffer` (-10) and `gpu-workload` (1000). Priority decides who
> wins when a *node* runs short on CPU — it is not what drives scaling.
> `01` is one balloon pod: `cpu: 100m`, **no `nvidia.com/*`**, with
> `podAntiAffinity` on hostname so only one lands per node. That absence of a GPU
> resource is what makes it visible to Karpenter (see §3).
> `04` makes KEDA's operator create a managed HPA and register an external metric.

**Pre-pull the PyTorch image** — required, and it is **not** cached on the current
card (only the small CUDA base is):

```bash
kubectl create -f manifests/rendered/02-train-job.yaml
kubectl get pods -l app=train -w        # Ctrl-C once Running
kubectl logs -l app=train | grep visible_mem     # sanity: 3800MiB
kubectl delete job -l app=train
```
> containerd's cache is **per node**, so only the card you pre-pull on is warm.
> MEASURED: 3.7 GB pulls in ~1 m 54 s, Running by t≈100 s. `create` not `apply` —
> the Job uses `generateName`. If Karpenter later replaces the node, pre-pull again.

### Pre-flight — all must pass

```bash
kubectl get ec2nodeclass,nodepool
kubectl -n kube-system get pods | grep -E "hami|karpenter|keda"
kubectl get scaledobject gpu-headroom
kubectl get nodes -l pool=karpenter \
  -o jsonpath='{.items[0].status.capacity.ephemeral-storage}'; echo
```
> Expect: both CRs `READY=True`; six control pods Running (hami-scheduler,
> hami-device-plugin, karpenter, 3× keda); ScaledObject `READY=True`; disk
> ~`104779756Ki` (100 GB). **If disk reads `20893676Ki`** the AMI default applied and
> pods get `Evicted` mid-pull — `blockDeviceMappings` didn't take.

---

## 2. ACT 1 — One T4, four isolated workloads  (~4 min)

*"One physical GPU. Watch how many real training jobs fit."*

```bash
kubectl get nodes -l pool=karpenter \
  -o custom-columns=NODE:.metadata.name,TYPE:.metadata.labels.node\\.kubernetes\\.io/instance-type,VGPU:.status.allocatable.nvidia\\.com/gpu
```
→ `g4dn.xlarge ... VGPU 4`
> `allocatable` is what kubelet advertises to the scheduler. HAMi's device plugin
> registered **4** vGPUs over the DevicePlugin gRPC API for one physical GPU. This is
> the number the scheduler budgets against.

```bash
kubectl get node -l pool=karpenter -o json \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["items"][0]["metadata"]["annotations"]["hami.io/node-nvidia-register"])'
```
→ `[{"count":4,"devmem":15360,"devcore":400,"type":"NVIDIA-Tesla T4"}]`
> Written by the device plugin after inspecting the hardware. `devmem: 15360` is the
> **real** 15 GB — proof it is one card. `devcore: 400` is `deviceCoreScaling: 4` ×
> 100, the oversubscribed compute budget that lets four pods fit.

*"One Tesla T4. 15 GB. HAMi presents it as four."*

```bash
for i in $(seq 4); do kubectl create -f manifests/rendered/02-train-job.yaml; done
kubectl get pods -l app=train -o wide
```
→ MEASURED: **4/4 Running in <60 s, all on the same node**
> Four separate Jobs. HAMi's webhook rewrites each pod to
> `schedulerName: hami-scheduler`, so HAMi places them — it tracks per-card memory
> and cores itself and packs all four onto one card.

**The money shot:**
```bash
kubectl logs -l app=train --tail=5 | grep -E "visible_mem|step"
```
→ `GPU=Tesla T4  visible_mem=3800MiB` · `step 300  loss 0.001`
> `torch.cuda.get_device_properties(0).total_memory` reports **3800 MiB, not 15360**,
> because HAMi-core is `LD_PRELOAD`ed into the container and intercepts the CUDA
> driver calls. The app never asked for a limit and cannot escape it. The `step`/`loss`
> lines prove it is real training, not a sleep.

*"Four real training loops, one card. Each sees 3800 MB and physically cannot exceed it."*

```bash
N=$(kubectl get node -l pool=karpenter -o name | head -1 | cut -d/ -f2)
kubectl describe node $N | grep -A6 "Allocated resources" | grep nvidia
```
→ `nvidia.com/gpu  4  4`
> The scheduler's ledger: 4 of 4 vGPUs committed. Because
> `defaultMemory (3800) × 4 ≈ 15360`, the card is full by *count* and by *memory* at
> the same moment. That alignment is the core design trick.

*"That was 4 GPUs before. It's one."*

> **Move promptly** — the jobs run 3000 steps and **Complete in ~2–3 min**, freeing
> the card. Don't leave it idle then claim it's full.

---

## 3. ACT 2 — Run the load, verify the bridge  (~5 min)

*"Now let's overload it. There's a subtlety in how the fleet grows."*

```bash
kubectl apply -f manifests/rendered/03-gpu-load.yaml
kubectl scale deploy gpu-load --replicas=7
```
> Lightweight CUDA-base pods (~250 MB, so a *new* card runs them in seconds instead
> of pulling 3.7 GB). 7 pods against a 4-slice card = 3 that cannot fit. Each carries
> `workload: gpu`, which is what KEDA counts.

```bash
kubectl get hpa keda-hpa-gpu-headroom
```
→ `TARGETS 3500m/4`
> KEDA's metrics-apiserver serves the external metric `7 pods ÷ 2 replicas = 3.5`
> (`3500m`) against target 4. Above target → the HPA scales `gpu-buffer` up.
> **The entire bridge, visible as one number.**

```bash
kubectl get deploy gpu-buffer
kubectl get nodeclaims
```
> MEASURED chain, **~40 s**: `gpu_pods=7` → HPA `3500m/4` → `gpu-buffer 1→2` → the new
> balloon is Pending (anti-affinity: no eligible node) → Karpenter issues a NodeClaim
> → `cards 1→2`. All 7 pods Running by ~80 s.

**Now show *why* a bridge is needed** — the pods explain it themselves:

```bash
kubectl get pod -l app=gpu-load -o jsonpath='{.items[0].spec.containers[0].resources.limits}'; echo
```
→ `{"nvidia.com/gpu":"1","nvidia.com/gpucores":"100"}`
> The manifest requests **only** `nvidia.com/gpu: 1`. HAMi's **mutating admission
> webhook** added `gpucores` during admission — it is in the stored object, not your
> YAML.

```bash
kubectl -n kube-system logs deploy/karpenter --tail=200 | grep "could not schedule" | tail -1
```
→ `no instance type has enough resources ... "nvidia.com/gpucores":"100"`
> Karpenter decides what to launch by simulating candidate nodes from the **EC2
> instance-type catalogue**, which has no `gpucores` entry. An unsatisfiable resource
> means it declines — permanently. GPU pods can therefore *never* trigger it, and the
> cpu-only balloon is the translation layer.

*"Three components, none of which understand each other's resources. KEDA counts GPU
pods, scales a CPU pod, Karpenter reacts to the CPU pod."*

### Confirm

```bash
kubectl get pods -l workload=gpu -o wide --no-headers | awk '{print $3, $7}' | sort | uniq -c
```
→ MEASURED: `4 Running <node-1>` / `3 Running <node-2>`
> HAMi filled card 1 to its 4-slice limit, then placed the rest on the card Karpenter
> added. **Seven GPU workloads, two physical cards.**

### Scale down — hands off

```bash
kubectl delete deploy gpu-load
kubectl delete job -l app=train
```
> MEASURED: `t=45s buffer=2` → `t=135s buffer=1` (KEDA's 120 s `scaleDown`
> stabilization window) → the emptied node holds only a balloon, so Karpenter's
> `WhenEmptyOrUnderutilized` policy drains it at **t≈180 s**.
> Do **not** `kubectl scale gpu-buffer` — the HPA owns that field and reverts you in ~15 s.

---

## 4. If asked "why the placeholder pods?"

```bash
kubectl get pod -l app=gpu-buffer -o jsonpath='{.items[0].spec.containers[0].resources}'; echo
```
→ `{"requests":{"cpu":"100m","memory":"128Mi"}}` — no `nvidia.com/*`
> That absence *is* the mechanism: the webhook only rewrites pods that ask for a GPU,
> so this one stays visible to Karpenter.

```bash
kubectl get pod -l app=gpu-buffer -o jsonpath='{.items[0].spec.affinity.podAntiAffinity}'; echo
kubectl get pods -l app=gpu-buffer -o wide --no-headers | awk '{print $7}' | sort | uniq -c
```
> `requiredDuringScheduling` anti-affinity on `kubernetes.io/hostname` enforces
> one-per-node explicitly, so `replicas` == warm cards and replica N+1 goes Pending
> (the signal Karpenter acts on). Karpenter honours anti-affinity in its simulation —
> verified. MEASURED: reserves **250m (6%)** per card; an earlier `cpu: 3` version
> took **3150m (80%)**, leaving only 770m for real GPU work.

```bash
kubectl get pods -o wide --no-headers | awk '{print $1, $7}' | sort -k2
```
> Balloon and GPU pods share a node — it holds **CPU**, not a vGPU slice, so it never
> blocks GPU work.

---

## 5. macOS notes

| Not available | Use instead |
|---|---|
| `watch` | `bash scripts/demo-watch.sh` (or `kubectl get X -w` for one resource) |
| `timeout` | plain `sleep`, or a backgrounded subshell + `kill` |
| GNU `sed -i` | BSD needs `sed -i ''` |

When checking node health, filter for `Ready` — **terminating nodes linger for
minutes and report zeros**, which looks alarming and isn't.

---

## 6. Fallbacks if something dies live

| Symptom | Do this |
|---|---|
| `connection refused` on everything | tunnel died — re-run `./scripts/connect-private.sh` |
| No card within 3 min | `kubectl get ec2nodeclass,nodepool` — if not `True`, go to slides; **delete nothing** |
| Pods stuck `ContainerCreating` | image pull; 3.7 GB on a cold node |
| Pods `Evicted` after pull | node disk too small — `ephemeral-storage` must be ~100 GB |
| Pod `Pending` with **no Events** | `kubectl -n kube-system get pods \| grep hami` — scheduler down |
| KEDA not scaling | GPU pods missing `workload=gpu` (only pods from the *current* template have it) |
| NodeClass stuck `Terminating` | `kubectl patch ec2nodeclass gpu --type=merge -p '{"metadata":{"finalizers":null}}'` then re-apply `manifests/rendered/10-karpenter-gpu-pool.yaml` |
| Everything wedged | `kubectl delete deploy gpu-load; kubectl delete job -l app=train` — never touch nodepool/ec2nodeclass |

---

## 7. Closing numbers

| | Before | After |
|---|---|---|
| Workloads per T4 | 1 | **4** |
| Memory isolation | none | **hard 3800 MiB cap** |
| Cards for 7 workloads | 7 | **2** |
| Idle cards | manual cleanup | auto-consolidated (~3 min) |
| Headroom scaling | a script on a laptop | **KEDA ScaledObject, in-cluster** |
| Balloon CPU overhead | 3150m (80%) | **250m (6%)** |

---

## 8. Teardown

```bash
kubectl delete deploy gpu-load --ignore-not-found
kubectl delete job -l app=train --ignore-not-found
# leave gpu-buffer + the ScaledObject in place if you'll demo again soon
```

The cluster bills **~$0.9/hr plus GPU nodes**. When finished for good:

```bash
cd infra && cdk destroy HamiEksStack        # see docs/06-cleanup.md
```
