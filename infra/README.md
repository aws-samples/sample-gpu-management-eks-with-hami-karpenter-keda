# infra/ — Python CDK for EKS + HAMi

Python AWS CDK app that provisions the demo cluster. See
[`../docs/02-provision-cluster.md`](../docs/02-provision-cluster.md) for the full
walkthrough; this is a quick reference.

## Layout

```
infra/
├── app.py                     # CDK app entry point
├── config.py                  # ALL tunables (region, versions, instance types)
├── cdk.json                   # CDK config (app command, feature flags)
├── requirements.txt           # aws-cdk-lib + kubectl layer (must match k8s ver)
└── stacks/
    └── eks_hami_stack.py       # VPC + EKS + node groups + HAMi Helm release
```

## Commands

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cdk bootstrap                 # once per account/region
cdk synth                     # render CloudFormation
cdk deploy                    # provision (~20–30 min)
cdk destroy                   # tear down
```

## What gets created

- VPC (2 AZs, **no IGW/NAT**; PrivateLink VPC endpoints for egress — air-gapped).
- EKS control plane (`config.k8s_version`, default 1.36) with a **private-only
  API endpoint** — reachable only from inside the VPC. CDK's kubectl/Helm
  handlers run as Lambdas in the VPC so `cdk deploy` still works.
- **System** node group (`t3.large` ×2, AL2023) for cluster services.
- **GPU** node group (`g4dn.xlarge` ×1, AL2023 NVIDIA AMI), labeled `gpu=on`,
  in private subnets (no public IPs).
- HAMi Helm release in `kube-system`, with the bundled kube-scheduler image tag
  pinned to the cluster version.

## Configuration

Edit [`config.py`](config.py) or override per-deploy with `-c key=value`:

| Key | Default | Notes |
|-----|---------|-------|
| `gpu_instance_type` | `g4dn.xlarge` | any GPU type in us-east-1 |
| `gpu_desired` / `gpu_max` | `1` / `2` | GPU node count |
| `system_desired` | `2` | lower to `1` to save cost |
| `k8s_version` | `1.36` | also update the kubectl layer dep |
| `private_endpoint_only` | `true` | `false` = public+private endpoint (laptop kubectl) |
| `hami_chart_version` | latest | pin for reproducibility |
| `cluster_admin_role_arn` | — | grant an IAM principal cluster-admin |

Example:

```bash
cdk deploy -c gpu_instance_type=g5.xlarge -c gpu_desired=2 -c system_desired=1
```

## Changing the Kubernetes version

Currently **1.36**. To change it, keep four things in sync:

1. `config.py`: set `k8s_version` (e.g. `"1.37"`).
2. `requirements.txt`: matching `aws-cdk.lambda-layer-kubectl-vNN` (e.g. `-v37`).
   These layer packages require **Python ≥ 3.10**.
3. `stacks/eks_hami_stack.py`: import the matching `KubectlVNNLayer`.
4. `images.json`: bump the `google_containers/kube-scheduler` entry to the
   matching tag (e.g. `v1.37.0`) and re-run `scripts/mirror-images.sh`. (HAMi's
   bundled kube-scheduler tag is derived automatically as `v<k8s_version>.0`.)

EKS only upgrades **one minor version at a time** — you cannot jump several
minors in a single `cdk deploy`. For a throwaway demo it's simpler to
`cdk destroy` and redeploy at the new version than to hop minor-by-minor.
