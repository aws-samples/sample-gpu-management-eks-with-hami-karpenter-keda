# Stop Wasting GPUs — HAMi + Karpenter on EKS (presenter runbook)

Live-demo runbook for the session **"Stop Wasting GPUs: Open-Source GPU Management
at Scale with HAMi & Karpenter."**

Two things get demonstrated:

1. **GPU virtualization** — one physical NVIDIA T4 serving **4 isolated workloads**,
   each with a hard 3800 MiB memory ceiling it cannot exceed.
2. **Autoscaling under load** — when the cards are full, **Karpenter adds another
   GPU node** and the stranded workloads schedule onto it.

Everything below was executed against the live air-gapped cluster on
2026-09-10. Measured results are marked **VERIFIED**. Where a thing does *not*
work, it says so — those are the session's most valuable slides.

---

## 1. The mental model (say this before touching a terminal)

Two schedulers with **two different views of a GPU**, and they do not share a
vocabulary:

| | HAMi | Karpenter |
|---|---|---|
| Unit it understands | vGPU slices: `gpu`, `gpumem`, `gpucores` | EC2 instance types |
| Where it acts | *inside* a card — packs pods onto one GPU | *at the fleet edge* — adds/removes nodes |
| Source of truth | its own device plugin + scheduler | the AWS instance-type catalogue |

HAMi makes one card serve many pods. Karpenter makes more cards appear. The
interesting engineering is the **seam between them** — which is where both of
this session's real lessons live.

---

## 2. Cluster config that makes sharing actually work

`deviceSplitCount` alone is **not enough**, and this is the first non-obvious
finding. Advertising 4 vGPUs does not mean 4 pods fit.

A pod that asks for `nvidia.com/gpu: 1` and nothing else gets, by HAMi default:

- **all 15360 MiB** of the card (`defaultMemory: 0` means *whole card*), and
- **all 100 SM cores** (`defaultCores: 0` means *whole card*).

So pod #2 fails with `CardInsufficientMemory`, and once memory is fixed, pod #2
fails again with `CardInsufficientCore`. **VERIFIED** — both failures observed in
sequence.

The working configuration (in `infra/config.py`, applied via the HAMi chart):

```python
hami_device_split_count: int = 4     # 4 schedulable vGPUs per card
hami_default_memory_mb: int = 3800   # card_mem / split_count (15360 / 4)
hami_device_core_scaling: int = 4    # advertise 4 x 100 cores so 4 pods fit
```

What each one buys you:

- `deviceSplitCount: 4` — the card reports `nvidia.com/gpu: 4`.
- `defaultMemory: 3800` — a plain `nvidia.com/gpu: 1` request gets a **hard-capped
  3800 MiB slice** instead of the whole card. This is the isolation guarantee.
- `deviceCoreScaling: 4` — the card advertises 400 cores, so four default
  (100-core) pods fit. **Compute is time-sliced (soft); memory is hard-partitioned.**
  Be precise about that distinction on stage — it's the honest trade-off of
  software GPU sharing versus MIG.

Confirm on a live node:

```bash
kubectl get node -l pool=karpenter -o jsonpath='{.items[0].status.allocatable.nvidia\.com/gpu}'
# -> "4"

kubectl get node <gpu-node> -o json \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["metadata"]["annotations"]["hami.io/node-nvidia-register"])'
# -> [{"count":4,"devmem":15360,"devcore":400,"type":"NVIDIA-Tesla T4",...}]
```

> **Note:** HAMi does **not** publish `nvidia.com/gpumem` as node-allocatable —
> node capacity only ever shows `nvidia.com/gpu`. That single fact is why no
> autoscaler can scale on GPU memory. It matters in §5.

---

## Demo 1 — one T4, four isolated workloads

**Claim:** one physical GPU, four real workloads, hard memory isolation.

```bash
export KUBECONFIG=/tmp/hami.kube      # tunnel: ./scripts/connect-private.sh
cd .

kubectl apply -f manifests/rendered/00-priorityclasses.yaml
kubectl apply -f manifests/rendered/01-headroom-pods.yaml     # warms one card
kubectl get nodeclaims -w                                # Ctrl-C when Ready (~2 min)
```

Submit four real training jobs (PyTorch CNN on synthetic data — no external data):

```bash
for i in $(seq 4); do kubectl create -f manifests/rendered/02-train-job.yaml; done
kubectl get pods -l app=train -o wide        # all 4 Running, all on ONE node
```

**The money shot** — each container sees only its slice:

```bash
kubectl logs -l app=train --tail=3 | grep visible_mem
# GPU=Tesla T4  visible_mem=3800MiB      <- not 15360. The cap is real.
```

**VERIFIED:** 4 pods Running on a single T4, each reporting `3800 MiB` via
`nvidia-smi` / `torch.cuda.get_device_properties`, none requesting `gpumem`
explicitly.

Show the packing from the node's side:

```bash
kubectl describe node <gpu-node> | grep -A3 "Allocated resources" | grep nvidia
# nvidia.com/gpu  4  4        <- 4 of 4 vGPUs in use, one physical card
```

**Talking point:** the workload asked for "a GPU" and got a *governed slice*. No
application change, no MIG-capable hardware, no CUDA_VISIBLE_DEVICES juggling.

### Optional: prove isolation is enforced, not advisory

Per-pod overrides let different workloads take different shapes:

```yaml
resources:
  limits:
    nvidia.com/gpu: 1
    nvidia.com/gpumem: 7000     # a bigger 7 GiB slice -> only 2 fit per card
    nvidia.com/gpucores: 50     # cap compute at 50% of the card
```

A pod that tries to allocate past its ceiling gets a CUDA OOM **inside the pod**
while its neighbours keep running — that is the containment story. See
`manifests/rendered/05-memory-limit-test.yaml`.

---

## Demo 2 — Karpenter adds a card when the fleet is full

### The lesson that makes this session worth attending

**A pod that requests a GPU can never trigger Karpenter.** This is not a
misconfiguration; it is structural.

Apply a pod requesting only `nvidia.com/gpu: 1`, then read back what actually
landed in the API server:

```bash
kubectl get pod <gpu-pod> -o jsonpath='{.spec.containers[0].resources.limits}'
# {"nvidia.com/gpu":"1","nvidia.com/gpucores":"100"}
#                        ^^^^^^^^^^^^^^^^^^^^^^^^^ nobody wrote this
```

HAMi's **mutating webhook** injected `nvidia.com/gpucores`. Karpenter then
simulates candidate nodes from the EC2 instance-type catalogue, which has no such
resource, and concludes:

```
could not schedule pod ... "no instance type has enough resources",
resources={"cpu":"150m","nvidia.com/gpu":"1","nvidia.com/gpucores":"100","pods":"4"}
```

**VERIFIED:** two GPU pods left Pending on a full card produced **zero
NodeClaims**, indefinitely.

> Any pod carrying a HAMi resource — `gpucores` or `gpumem`, whether you wrote it
> or the webhook did — is **invisible to Karpenter**.

### The bridge

Karpenter *does* act on plain `cpu`/`memory` pods. So the scaling signal has to
be expressed in the vocabulary Karpenter speaks:

```
pending GPU pods  →  +1 balloon replica  →  Karpenter adds a card  →  GPU pods land
```

Balloon (headroom) pods — `manifests/rendered/01-headroom-pods.yaml` — request **only
cpu/memory**, sized so exactly one fits per `g4dn.xlarge`, at low priority:

```yaml
priorityClassName: gpu-buffer          # value -10, always preemptible
nodeSelector: { pool: karpenter }      # the GPU lane
affinity:
  podAntiAffinity:                     # ONE balloon per node -> replicas == warm cards
    requiredDuringSchedulingIgnoredDuringExecution:
      - labelSelector: { matchLabels: { app: gpu-buffer } }
        topologyKey: kubernetes.io/hostname
resources:
  requests:
    cpu: "100m"                        # NO nvidia.com/* -> Karpenter can see it
    memory: "128Mi"
```

**Why anti-affinity rather than a big CPU request.** An earlier version asked for
`cpu: 3` so two balloons could not fit on one 4-vCPU `g4dn.xlarge`. It worked, but
it reserved **3150m of 3920m allocatable (80%)**, leaving only **770m** for real GPU
workloads — fine for synthetic-tensor demo jobs (~150m), starvation for real
training where DataLoader workers and image decode want CPU. Anti-affinity gives the
same one-per-node guarantee explicitly, so the request drops to 100m and
**~3670m stays free**. VERIFIED after the switch: node reservation fell from
`3150m (80%)` to `250m (6%)`, exactly one balloon per node, and replica 2 still went
Pending and still pulled in a new card — Karpenter's simulation honours
`podAntiAffinity`.

`replicas` = **cards kept warm**. The webhook ignores these pods, so Karpenter
sees a request it can satisfy and launches from a NodePool that only offers
`g4dn.xlarge` — a GPU card.

**VERIFIED:** 2 balloon pods → a second `g4dn.xlarge` NodeClaim in **~75 s**;
previously-stranded GPU pods scheduled onto it immediately after it joined.

### Implementing the bridge without writing code

The translation is driven by **KEDA**'s built-in `kubernetes-workload` scaler, so
the whole thing is one declarative object — no controller to write, nothing running
on a laptop (`manifests/rendered/04-keda-scaledobject.yaml`):

```yaml
spec:
  scaleTargetRef: { name: gpu-buffer }
  minReplicaCount: 1
  maxReplicaCount: 4
  triggers:
    - type: kubernetes-workload
      metadata:
        podSelector: 'workload=gpu'   # every GPU workload
        value: '4'                    # = deviceSplitCount -> 1 warm card per 4 pods
```

`replicas = ceil(pods matching selector / value)`. The scaler counts **Pending and
Running** pods (it excludes only `Succeeded`/`Failed`), so headroom is added
*before* work piles up rather than after.

**VERIFIED end to end with no script running:** scaling `gpu-load` to 7 →
HPA metric `3500m/4` → balloon `1 → 2` → cards `1 → 2`, all within **40 s**;
7 pods placed 4+3 across two T4s. Deleting the workload took headroom back to 1
at ~135 s (the 120 s `scaleDown` window), then Karpenter consolidated the card.

Why KEDA over the obvious alternatives: an HPA alone can't count "pods matching a
selector", and a bash loop (`scripts/gpu-headroom-bridge.sh`, kept only as a
teaching aid) has no leader election, no restarts, and races anyone running
`kubectl scale`. Do not run both — they fight over the same replica field.

### Running the demo

Terminal A — the bridge (turns GPU pressure into Karpenter-visible demand):

```bash
export KUBECONFIG=/tmp/hami.kube
./scripts/gpu-headroom-bridge.sh
```

Terminal B — overload the fleet:

```bash
for i in $(seq 6); do kubectl create -f manifests/rendered/02-train-job.yaml; done

kubectl get pods -l app=train -o wide     # 4 Running, 2 Pending (card is full)
kubectl get nodeclaims -w                 # bridge scales headroom -> a card appears
kubectl get pods -l app=train -o wide     # the 2 stragglers now Running on card #2
```

**VERIFIED end state:** **6 GPU workloads across 2 physical T4s** (4 + 2), each
capped at 3800 MiB.

Scale-down — Karpenter consolidates idle cards on its own:

```bash
kubectl delete job -l app=train
kubectl scale deploy gpu-buffer --replicas=1
kubectl get nodes -l pool=karpenter -w    # extra card removed after ~1 min idle
```

### Why not just autoscale on GPU memory?

The obvious design — "add a node when `nvidia.com/gpumem` runs out" — cannot work,
and it's worth stating plainly:

- **HAMi never publishes `gpumem` as node-allocatable.** Node capacity shows
  `nvidia.com/gpu` only. There is no memory number for an autoscaler to compare
  against.
- **Karpenter** reasons purely from instance types; `gpumem`/`gpucores` don't
  exist there. **VERIFIED broken.**
- **Cluster Autoscaler** is the one with a real escape hatch: ASG tags like
  `k8s.io/cluster-autoscaler/node-template/resources/nvidia.com/gpumem` teach it
  about custom resources for scale-from-zero. We deployed CAS, tried this, did not
  get it working, and removed it — call it **unproven here, not impossible.**
- **Metrics-driven (KEDA + DCGM)** scales on *actual* utilization rather than
  requests. Most faithful to the "GPUs sit at 30%" premise, most moving parts.

Design consequence, and a good closing line: **make the count dimension carry the
memory meaning.** With `defaultMemory = card_mem / deviceSplitCount`, running out
of vGPU *count* and running out of *memory* become the same event — and count is
the one thing every autoscaler can see.

---

## 5. Air-gapped notes (this cluster has no IGW and no NAT)

Two failures that cost real debugging time and are worth a slide if the audience
runs private clusters:

**Karpenter needs the IAM API, and IAM has no VPC endpoint.** AWS publishes no
`iam` interface endpoint in commercial regions, so a fully isolated VPC cannot
reach it. Karpenter creates the node instance profile via IAM at runtime, so
`EC2NodeClass` hangs at `InstanceProfileReady=Unknown` forever. Fix: pre-create
the profile at deploy time and reference it **by name**:

```python
node_instance_profile = iam.CfnInstanceProfile(...)      # created by CDK
# EC2NodeClass:
"instanceProfile": node_instance_profile.instance_profile_name,   # NOT "role"
```

The nastier corollary: **deleting an EC2NodeClass deadlocks.** Its finalizer also
needs IAM, so the object sits in `Terminating` forever, and a terminating NodeClass
makes the NodePool permanently `NodeClassReady=False`. Recovery:

```bash
kubectl patch ec2nodeclass gpu --type=merge -p '{"metadata":{"finalizers":null}}'
kubectl apply -f manifests/rendered/10-karpenter-gpu-pool.yaml
```

**Do not `kubectl delete` anything Karpenter-owned during the talk.**

**Security groups can't be discovered by tag.** CDK only *references* the
EKS-managed cluster SG, so `Tags.of(cluster.cluster_security_group)` is a silent
no-op — the tag never lands and `securityGroupSelectorTerms` matches nothing
(`SecurityGroupsReady=False`). Select by ID instead:

```python
"securityGroupSelectorTerms": [{"id": cluster.cluster_security_group_id}],
```

---

## 6. Gotchas checklist (pre-flight before you present)

| Symptom | Cause | Fix |
|---|---|---|
| GPU pods Pending, **0 NodeClaims** | HAMi injected `gpucores`; Karpenter blind | balloon pods (cpu-only) + bridge |
| 2nd pod on a card: `CardInsufficientMemory` | `defaultMemory: 0` = whole card | `defaultMemory: 3800` |
| 2nd pod on a card: `CardInsufficientCore` | `defaultCores: 0` = all 100 cores | `deviceCoreScaling: 4` |
| Scale-out stops after one card | `limits.nvidia.com/gpu` counts **vGPUs, not cards** — 4 caps you at one card | set it to `cards × splitCount` (16) |
| `ec2nodeclass` `READY=False`, SG selector matched nothing | CDK can't tag the EKS-managed SG | select SG by `id` |
| `InstanceProfileReady=Unknown` forever | no IAM VPC endpoint (air-gapped) | pre-create profile, use `instanceProfile` |
| NodeClass stuck `Terminating`, NodePool never ready | finalizer needs IAM | patch finalizers to `null`, re-apply |
| Pods `Evicted`, "low on resource: ephemeral-storage" | EC2NodeClass had no `blockDeviceMappings` → AMI default 20 GB disk; a 3.7 GB image fills it | set `blockDeviceMappings` to 100 GB gp3 |
| Pending pod with **no Events at all** | pod bound to `hami-scheduler`, not yet evaluated | check `hami-scheduler` is Running |

Pre-flight, in order:

```bash
kubectl get ec2nodeclass,nodepool                    # both READY=True
kubectl -n kube-system get pods | grep -E "hami|karpenter"   # all Running
kubectl get nodes -l pool=karpenter -o jsonpath='{.items[*].status.allocatable.nvidia\.com/gpu}'   # "4"
```

---

## 7. Closing numbers

| | Before | After |
|---|---|---|
| Workloads per T4 | 1 | **4** (VERIFIED) |
| Memory isolation | none (whole card) | **hard 3800 MiB cap** (VERIFIED) |
| Cards for 6 workloads | 6 | **2** (VERIFIED) |
| Idle GPU nodes | manual cleanup | Karpenter consolidates (~1 min) |

Cost note: the cluster bills ~$0.9/hr plus GPU nodes. `cdk destroy` when done —
see `docs/06-cleanup.md`.
