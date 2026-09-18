# 01 · Prerequisites

## Tools

Install these on your workstation:

| Tool | Version | Purpose |
|------|---------|---------|
| [AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) | v2 | AWS auth, `update-kubeconfig` |
| [Node.js](https://nodejs.org/) | ≥ 18 | Runtime for the CDK CLI |
| [AWS CDK](https://docs.aws.amazon.com/cdk/v2/guide/getting_started.html) | v2 (`npm i -g aws-cdk`) | Provision infra |
| [Python](https://www.python.org/) | ≥ 3.10 | CDK app language (the kubectl-v36 layer needs 3.10+) |
| [kubectl](https://kubernetes.io/docs/tasks/tools/) | matches cluster (1.36) | Talk to the cluster |
| [session-manager-plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html) | latest | Tunnel to the **private** API (Step 6) |

> `crane` (image mirroring) is already bundled at `tools/crane` — no install
> needed. It's used by `scripts/mirror-images.sh` in Stage 2.

Quick check:

```bash
aws --version && node --version && cdk --version && python3 --version && \
  kubectl version --client && session-manager-plugin --version
```

## AWS account & credentials

You need an AWS account and credentials with permission to create EKS clusters,
VPCs, EC2 instances, IAM roles, and CloudFormation stacks.

```bash
# Configure credentials (SSO shown; `aws configure` also works)
aws sso login --profile my-profile
export AWS_PROFILE=my-profile

# Confirm identity and that you're pointed at us-east-1
aws sts get-caller-identity
aws configure get region   # should be us-east-1, or pass --region us-east-1
```

This project deploys to **us-east-1**.

### GPU capacity / quota

GPU instances require sufficient **vCPU service quota** for the relevant
instance family, and capacity must be available in the region.

- The default GPU instance is **`g4dn.xlarge`** (1× NVIDIA T4, 16 GiB, 4 vCPU),
  which falls under the *Running On-Demand G and VT instances* quota.
- Check and, if needed, request an increase:

```bash
# Current limit for On-Demand G/VT instances
aws service-quotas get-service-quota \
  --service-code ec2 --quota-code L-DB2E81BA --region us-east-1 \
  --query 'Quota.Value'
```

If the value is `0` (common on new accounts), request an increase in the
[Service Quotas console](https://us-east-1.console.aws.amazon.com/servicequotas/home/services/ec2/quotas/L-DB2E81BA)
before deploying. You need at least **4 vCPUs** for one `g4dn.xlarge`.

## Estimated cost

Rough **on-demand** us-east-1 pricing (as of 2024; check current rates). Idle or
running, these bill by the hour while the stack exists:

| Resource | Qty | ~$/hour | ~$/day |
|----------|-----|---------|--------|
| EKS control plane | 1 | $0.10 | $2.40 |
| `g4dn.xlarge` (GPU) | 1 | ~$0.526 | ~$12.62 |
| `t3.large` (system) | 2 | ~$0.083 ea | ~$3.98 |
| `t3.small` (SSM bastion) | 1 | ~$0.021 | ~$0.50 |
| VPC interface endpoints | 11 | ~$0.01 ea | ~$2.64 |
| **Total** | | **~$0.90/hr** | **~$22/day** |

> Air-gapped mode uses PrivateLink **VPC endpoints** instead of a NAT gateway —
> no public egress. Treat this as an estimate, not a bill. Cut cost by lowering
> `system_desired`, using a smaller GPU, or — most importantly — **tearing down
> when done** ([06-cleanup.md](06-cleanup.md)).

## Next

→ [02-provision-cluster.md](02-provision-cluster.md)
