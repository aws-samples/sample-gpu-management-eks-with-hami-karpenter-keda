# 02 · Provision the cluster

Because the cluster is **fully air-gapped** (no internet egress), setup runs in
**three stages**: stand up the infrastructure, mirror the container images into
your private ECR, then install HAMi. Total time ≈ **30 minutes**, mostly the EKS
control plane.

> Why staged? Air-gapped nodes can only pull images from your private ECR. So we
> create the cluster + empty ECR repos first, fill ECR, then install HAMi — which
> then pulls everything privately.

## 1. Install the CDK app

```bash
cd infra
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate.bat
pip install -r requirements.txt
```

## 2. Bootstrap CDK (once per account/region)

```bash
cdk bootstrap aws://$(aws sts get-caller-identity --query Account --output text)/us-east-1
```

## 3. Stage 1 — infrastructure, no HAMi yet  ·  ~20–25 min

Creates the private VPC, PrivateLink endpoints, EKS cluster, node groups, the
(empty) ECR repos, and the SSM bastion:

```bash
cdk deploy -c install_hami=false
```

## 4. Stage 2 — mirror images into ECR  ·  ~2–3 min

Copies the required public images (HAMi, kube-scheduler, webhook cert-gen, CUDA)
into the ECR repos created in Stage 1. Run from the **repo root**:

```bash
cd ..
./scripts/mirror-images.sh
cd infra
```

Uses the bundled `tools/crane` — no Docker daemon and no Docker Hub account
needed. The images it mirrors are listed in `infra/images.json`.

## 5. Stage 3 — install HAMi  ·  ~5–10 min

```bash
cdk deploy
```

Installs HAMi from the vendored chart, pulling every image from ECR over
PrivateLink. The deploy **waits until HAMi's pods are Ready**, so a successful
`cdk deploy` means HAMi is up and running.

## 6. Connect to the private cluster

The API endpoint is **private** — `kubectl` from your laptop over the internet
will time out. The helper opens an SSM tunnel through the stack's bastion and
writes a ready-to-use kubeconfig:

```bash
cd ..
export AWS_PROFILE=<your-profile>
./scripts/connect-private.sh        # leave this running

# in a second terminal:
export KUBECONFIG=/tmp/hami.kube
kubectl get nodes -L gpu,role
```

You should see the system and GPU nodes `Ready`; the GPU node carries the
`gpu=on` label. (Requires the `session-manager-plugin` — see
[01-prerequisites.md](01-prerequisites.md).)

### Common overrides

Anything in `infra/config.py`, via `-c key=value`:

```bash
cdk deploy -c gpu_instance_type=g5.xlarge   # A10G (24 GiB) instead of T4
cdk deploy -c gpu_desired=2                  # two GPU nodes
cdk deploy -c system_desired=1               # cheaper: one system node
```

> **Prefer a quick, non-air-gapped demo?** `cdk deploy -c air_gapped=false`
> uses a NAT gateway for egress (nodes pull from public registries, HAMi from
> its public chart) — then Stages 2 and this staging aren't needed. Note: that
> adds a NAT gateway (a public-facing resource).

## Next

→ [03-verify-hami.md](03-verify-hami.md) to confirm HAMi is slicing the GPU.
