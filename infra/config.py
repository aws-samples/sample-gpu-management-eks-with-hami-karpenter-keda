"""Central configuration for the HAMi-on-EKS demo cluster.

Everything you are likely to tune lives here so you don't have to hunt through
the stack code. Values can also be overridden at synth/deploy time with CDK
context, e.g.:

    cdk deploy -c gpu_instance_type=g5.xlarge -c gpu_desired=2

Cost note: GPU instances and the NAT gateway are the main cost drivers. See the
docs (docs/01-prerequisites.md) for an itemized estimate before you deploy.
"""

import os
from dataclasses import dataclass, field
from typing import List, Optional


def _ctx(app_context: dict, key: str, default):
    """Read an override from CDK context, falling back to the default."""
    val = app_context.get(key)
    return val if val is not None else default


@dataclass
class Config:
    # --- AWS environment -------------------------------------------------
    # Region is fixed to us-east-1 per the project brief. Account is taken
    # from the ambient AWS credentials/profile at deploy time.
    region: str = "us-east-1"
    account: Optional[str] = field(
        default_factory=lambda: os.environ.get("CDK_DEFAULT_ACCOUNT")
    )

    # --- Cluster ---------------------------------------------------------
    cluster_name: str = "hami-eks-demo"
    # Kubernetes minor version. IMPORTANT: HAMi's scheduler runs a bundled
    # kube-scheduler whose image tag must match this version (handled
    # automatically in the stack). If you change this, also bump the kubectl
    # layer dependency in requirements.txt (lambda-layer-kubectl-vNN).
    k8s_version: str = "1.36"

    # Private-only API endpoint (default). When True the Kubernetes API server
    # is reachable ONLY from inside the VPC -- never the public internet -- so
    # you must reach kubectl via VPN / Direct Connect / a bastion / SSM
    # port-forward. `cdk deploy` still works: CDK runs its kubectl/Helm handlers
    # as Lambdas inside the VPC. Set False (or -c private_endpoint_only=false)
    # to restore a public+private endpoint.
    private_endpoint_only: bool = True

    # --- System (non-GPU) node group ------------------------------------
    # Runs CoreDNS, the HAMi scheduler/webhook, metrics, etc. so the
    # (expensive) GPU nodes are reserved for GPU workloads.
    system_instance_type: str = "t3.large"
    system_min: int = 2
    system_desired: int = 2
    system_max: int = 3

    # --- GPU node group --------------------------------------------------
    # g4dn.xlarge = 1x NVIDIA T4 (16 GB), the cheapest single-GPU option in
    # us-east-1 and ideal for demonstrating vGPU slicing. Alternatives:
    #   g5.xlarge   -> 1x A10G (24 GB)
    #   g6.xlarge   -> 1x L4   (24 GB)
    gpu_instance_type: str = "g4dn.xlarge"
    gpu_min: int = 1
    gpu_desired: int = 1
    gpu_max: int = 2
    gpu_disk_size_gb: int = 100

    # --- HAMi ------------------------------------------------------------
    hami_namespace: str = "kube-system"
    # vGPUs each physical GPU is split into. Set so one vGPU ≈ one memory slice
    # (e.g. 4 on a 16 GiB T4 -> ~4 GiB per vGPU) so vGPU-count exhaustion tracks
    # memory exhaustion -- required for the buffer-pod autoscaling demo (docs/10).
    hami_device_split_count: int = 4
    # Default GPU memory (MiB) granted to a pod that requests nvidia.com/gpu
    # WITHOUT an explicit nvidia.com/gpumem. HAMi's own default is 0, which means
    # "the whole card" -- that lets ONE pod monopolise a 16 GiB T4 and is why
    # extra pods failed with CardInsufficientMemory. Set it to
    # card_memory / hami_device_split_count (T4: 15360 / 4 ≈ 3800) so a plain
    # `nvidia.com/gpu: 1` request gets a hard-capped slice. VERIFIED on-cluster:
    # 4 pods packed onto one T4, each seeing 3800 MiB via nvidia-smi.
    hami_default_memory_mb: int = 3800
    # Compute (SM core) oversubscription factor. HAMi assigns 100 cores -- the
    # whole card -- to any pod that doesn't request nvidia.com/gpucores, so
    # without oversubscription only ONE pod fits per card
    # (CardInsufficientCore / CardTimeSlicingExhausted). Set this to
    # hami_device_split_count so the card advertises N*100 cores and N pods fit.
    # Compute is then time-sliced (soft) while memory stays hard-isolated.
    hami_device_core_scaling: int = 4
    # Pin the HAMi Helm chart version for reproducibility, or leave empty to
    # take the latest published chart. See https://github.com/Project-HAMi/HAMi
    hami_chart_version: str = ""

    # --- Access ----------------------------------------------------------
    # Optional IAM principal (role/user ARN) to grant cluster-admin access via
    # an EKS access entry. Leave empty and grant access after deploy instead
    # (see docs/02-provision-cluster.md). Also settable with:
    #   cdk deploy -c cluster_admin_role_arn=arn:aws:iam::...:role/MyRole
    cluster_admin_role_arn: str = ""

    # --- Networking ------------------------------------------------------
    max_azs: int = 2
    nat_gateways: int = 1

    # --- Air-gapped mode -------------------------------------------------
    # When True (default): NO public resources. The VPC has no Internet
    # Gateway and no NAT gateway -- only isolated subnets reached via
    # PrivateLink (VPC) endpoints. Container images are pulled from private
    # ECR (mirror them first with scripts/mirror-images.sh), and HAMi is
    # installed from the vendored chart in infra/charts/ (the public chart
    # repo is unreachable in-VPC). Set False for the NAT-egress variant.
    air_gapped: bool = True

    # Install HAMi as part of the stack. Set False to stand up the cluster,
    # ECR and bastion FIRST (so images can be mirrored), then redeploy with
    # True. This staged approach avoids a blind Helm install on a freshly
    # created cluster. Override: -c install_hami=false
    install_hami: bool = True

    # SSM bastion (no inbound ports) used to reach the private API endpoint
    # for verification. Only created in air-gapped mode.
    deploy_bastion: bool = True
    bastion_instance_type: str = "t3.small"

    # --- Karpenter (intelligent node autoscaling) ------------------------
    # When True, install Karpenter (controller + CRDs from vendored charts,
    # images mirrored to ECR) plus a GPU NodePool/EC2NodeClass so GPU nodes are
    # provisioned just-in-time and consolidated when idle. See
    # docs/10-hami-karpenter-demo.md.
    enable_karpenter: bool = True
    karpenter_version: str = "1.14.1"

    # --- KEDA (in-cluster GPU headroom autoscaling, no custom code) --------
    # KEDA scales the balloon/headroom Deployment from a declarative
    # ScaledObject, replacing the demo-only scripts/gpu-headroom-bridge.sh loop.
    # It uses the built-in `kubernetes-workload` scaler:
    #   replicas = (pods matching podSelector) / value
    # with value = hami_device_split_count, so one warm card is held per N GPU
    # workloads. The scaler counts Pending AND Running pods (it excludes only
    # Succeeded/Failed), so headroom is provisioned proactively.
    enable_keda: bool = True
    keda_version: str = "2.20.2"

    # --- Cluster Autoscaler (scales a dedicated GPU node group on gpumem) --
    # Runs ALONGSIDE Karpenter. CAS manages its own GPU managed node group
    # (label pool=cas, min 1 so CAS can template the real HAMi nvidia.com/gpumem
    # allocatable for scale-up); Karpenter owns whole-GPU provisioning
    # (pool=karpenter). Demo pods pick a lane via a `pool` nodeSelector so the
    # two autoscalers never contend. See docs/10.
    # Cluster Autoscaler is an OPTIONAL alternative kept for comparison, but it
    # CANNOT scale on HAMi's gpumem (not node-allocatable) -- it only scales on
    # nvidia.com/gpu, same as Karpenter. Off by default; Karpenter is the sole
    # GPU autoscaler. Enable with -c enable_cluster_autoscaler=true.
    enable_cluster_autoscaler: bool = False
    cas_version: str = "v1.36.1"  # image tag; match the cluster's K8s minor
    cas_gpu_min: int = 1
    cas_gpu_desired: int = 1
    cas_gpu_max: int = 3

    # Static (non-autoscaled) GPU node group. Disable when Karpenter + CAS own
    # GPU capacity, so Cluster Autoscaler doesn't also try to manage it.
    enable_static_gpu_nodegroup: bool = True

    @classmethod
    def from_context(cls, app) -> "Config":
        """Build a Config, letting CDK `-c key=value` context override fields."""
        ctx = {}
        for f in cls.__dataclass_fields__:  # type: ignore[attr-defined]
            v = app.node.try_get_context(f)
            if v is not None:
                ctx[f] = v

        base = cls()
        for key, raw in ctx.items():
            current = getattr(base, key)
            # Coerce from string context values to the field's type.
            if isinstance(current, bool):
                raw = str(raw).strip().lower() in ("1", "true", "yes", "on")
            elif isinstance(current, int):
                raw = int(raw)
            setattr(base, key, raw)
        return base

    @property
    def gpu_instance_families(self) -> List[str]:
        return ["g4dn", "g5", "g6", "p3", "p4d", "p5"]
