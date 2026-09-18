# 07 · Troubleshooting

## `kubectl` hangs / times out (`Unable to connect to the server`)

**Symptom:** `kubectl get nodes` from your workstation hangs and eventually
times out, even though `cdk deploy` succeeded.

**Cause:** the cluster has a **private-only API endpoint** — it is not reachable
over the public internet. This is by design.

**Fix:** reach the API from inside the VPC — SSM port-forward through a bastion,
run kubectl on an in-VPC instance/Cloud9, or connect via VPN/Direct Connect. See
[docs/02](02-provision-cluster.md#6-connect-to-the-private-cluster). For a
throwaway demo you can instead deploy with `-c private_endpoint_only=false` to
get a public+private endpoint.

## `hami-scheduler` pod crash-loops

**Symptom:** `hami-scheduler` in `CrashLoopBackOff`; logs show a kube-scheduler
version/API error.

**Cause:** HAMi's scheduler runs a bundled `kube-scheduler` whose image tag must
match the cluster's Kubernetes minor version.

**Fix:** This stack sets `scheduler.kubeScheduler.imageTag` from
`config.k8s_version` automatically. If you changed the cluster version, make
sure the tag matches, e.g. redeploy with:

```bash
cd infra
cdk deploy -c k8s_version=1.36   # tag becomes v1.36.0
```

Also bump the kubectl layer in `requirements.txt`
(`aws-cdk.lambda-layer-kubectl-vNN`) and the import in the stack to match.

## Node shows `nvidia.com/gpu: 1` (not virtualized)

**Symptom:** allocatable GPU count equals the physical GPU count; slicing
doesn't work.

**Causes & fixes:**
- The **stock NVIDIA device plugin** is running instead of HAMi's. Check:
  `kubectl get ds -n kube-system | grep -i nvidia`. This stack does **not**
  install the stock plugin, but the NVIDIA GPU Operator or a manual install
  would. Remove the stock `nvidia-device-plugin` DaemonSet so HAMi's plugin owns
  the device.
- The GPU node is **missing the `gpu=on` label**. The CDK stack sets it on the
  GPU node group; verify with `kubectl get nodes -L gpu`. Label manually if
  needed: `kubectl label node <node> gpu=on`.

## GPU pod stuck `Pending`

```bash
kubectl describe pod <pod> | sed -n '/Events/,$p'
```

- **`Insufficient nvidia.com/gpumem` / no slice fits:** the remaining slice is
  smaller than the request. Reduce `gpumem`/`gpucores`, delete other GPU pods,
  or add a GPU node (`cdk deploy -c gpu_desired=2`).
- **`0/N nodes are available: … node(s) didn't match Pod's node affinity`:** no
  `gpu=on` node is Ready. Check the GPU node group came up:
  `kubectl get nodes -l gpu=on`.
- **Scheduler not used:** HAMi schedules via a webhook/extender. If pods aren't
  getting HAMi allocations, confirm the HAMi mutating webhook and scheduler pods
  are healthy in `kube-system`.

## `nvidia-smi` in a pod shows the full GPU, not the slice

- Confirm the pod actually requested `nvidia.com/gpumem` (a plain
  `nvidia.com/gpu: 1` with no mem cap gets the whole device).
- The container image must be CUDA-based so HAMi's in-container library loads.
- Check the **default container runtime is nvidia** on the node. The AL2023
  NVIDIA AMI configures this; if you switched AMIs, HAMi's memory isolation
  won't inject. Verify on the node (via SSM):
  `sudo grep -A2 default_runtime_name /etc/containerd/config.toml`.

## GPU nodes never become `Ready`

- **Insufficient quota / capacity:** check the node group in the EKS console or
  `aws eks describe-nodegroup`. G-instance vCPU quota of 0 or no capacity in the
  AZ are the usual culprits — see
  [01-prerequisites.md](01-prerequisites.md#gpu-capacity--quota).
- Try a different instance type/AZ: `cdk deploy -c gpu_instance_type=g5.xlarge`.

## Helm release times out during `cdk deploy`

The HAMi chart is installed with `wait=true` (15-min timeout) so it waits for
the device plugin. If GPU nodes are slow/failing to join, the device plugin
never becomes Ready and the deploy fails. Fix the node group first (quota/
capacity above), then re-run `cdk deploy`.

## `cdk destroy` won't complete

See [06-cleanup.md](06-cleanup.md#if-cdk-destroy-gets-stuck).

## Getting help

- HAMi docs: https://project-hami.io/docs/
- HAMi GitHub / issues: https://github.com/Project-HAMi/HAMi
- EKS GPU AMIs: https://docs.aws.amazon.com/eks/latest/userguide/ml-eks-optimized-ami.html
