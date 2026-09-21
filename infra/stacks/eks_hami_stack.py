"""EKS cluster with a GPU node group and HAMi (virtual-GPU management).

Two networking modes (config.air_gapped):

* **air_gapped=True (default) -- NO public resources.** The VPC has no Internet
  Gateway and no NAT gateway; only isolated subnets reached via PrivateLink
  (VPC) endpoints. Nodes pull container images from **private ECR** (mirror them
  first with ``scripts/mirror-images.sh``). HAMi is installed from the vendored
  chart in ``infra/charts/`` because the public Helm repo is unreachable in-VPC.
  A no-inbound SSM bastion is created so you can tunnel to the private API for
  verification.
* **air_gapped=False -- private endpoint + NAT egress.** Nodes sit in private
  subnets with outbound-only internet via a NAT gateway, so images pull from
  public registries and HAMi installs from its public chart repo.

In both modes the EKS API endpoint is **private-only**.

HAMi image repointing (air-gapped)
----------------------------------
The HAMi chart honors ``global.imageRegistry``. We set it to the account's ECR
registry host and mirror each upstream image into an ECR repo whose name matches
the chart's ``image.repository`` field (see ``infra/images.json``), so every
HAMi image resolves to ECR with no per-component edits.
"""

import json
import os
from typing import Dict, List

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    Tags,
    aws_ec2 as ec2,
    aws_ecr as ecr,
    aws_eks as eks,
    aws_iam as iam,
    aws_s3_assets as s3_assets,
)
from aws_cdk.lambda_layer_kubectl_v36 import KubectlV36Layer
from constructs import Construct

from config import Config

_HERE = os.path.dirname(os.path.abspath(__file__))
# CDK's HelmChart chartAsset expects a chart DIRECTORY (CDK zips it and the
# kubectl handler unzips + `helm install`s it). A .tgz fails with
# "File is not a zip file". The dir is the extracted hami-2.10.0 chart.
_CHART_DIR = os.path.join(_HERE, "..", "charts", "hami")
_KARPENTER_CRD_DIR = os.path.join(_HERE, "..", "charts", "karpenter-crd")
_KARPENTER_DIR = os.path.join(_HERE, "..", "charts", "karpenter")
_KEDA_DIR = os.path.join(_HERE, "..", "charts", "keda")
_CAS_DIR = os.path.join(_HERE, "..", "charts", "cluster-autoscaler")
_IMAGES_JSON = os.path.join(_HERE, "..", "images.json")


def _load_images() -> List[dict]:
    with open(_IMAGES_JSON) as fh:
        return json.load(fh)["images"]


class EksHamiStack(Stack):
    def __init__(
        self, scope: Construct, construct_id: str, *, config: Config, **kwargs
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.config = config
        self.images = _load_images()

        vpc = self._create_vpc()
        if config.air_gapped:
            self._add_vpc_endpoints(vpc)
        self.ecr_repos = self._create_ecr_repos() if config.air_gapped else {}

        cluster = self._create_cluster(vpc)
        self._add_system_nodegroup(cluster)
        if config.enable_static_gpu_nodegroup:
            self._add_gpu_nodegroup(cluster)
        if config.enable_cluster_autoscaler:
            self._add_cas_gpu_nodegroup(cluster)
        if config.install_hami:
            self._install_hami(cluster)
        if config.air_gapped and config.deploy_bastion:
            self._add_bastion(vpc, cluster)
        if config.enable_karpenter:
            self._add_karpenter(vpc, cluster)
        if config.enable_keda:
            self._install_keda(cluster)
        if config.enable_cluster_autoscaler:
            self._add_cluster_autoscaler(cluster)
        self._grant_admin_access(cluster)
        self._outputs(cluster)

    # ------------------------------------------------------------------ #
    @property
    def _node_subnet_type(self) -> ec2.SubnetType:
        return (
            ec2.SubnetType.PRIVATE_ISOLATED
            if self.config.air_gapped
            else ec2.SubnetType.PRIVATE_WITH_EGRESS
        )

    @property
    def ecr_registry_host(self) -> str:
        return f"{self.account}.dkr.ecr.{self.region}.amazonaws.com"

    # ------------------------------------------------------------------ #
    def _create_vpc(self) -> ec2.Vpc:
        if self.config.air_gapped:
            # No IGW, no NAT -- isolated subnets only.
            return ec2.Vpc(
                self,
                "HamiVpc",
                max_azs=self.config.max_azs,
                nat_gateways=0,
                subnet_configuration=[
                    ec2.SubnetConfiguration(
                        name="isolated",
                        subnet_type=ec2.SubnetType.PRIVATE_ISOLATED,
                        cidr_mask=20,
                    ),
                ],
            )
        return ec2.Vpc(
            self,
            "HamiVpc",
            max_azs=self.config.max_azs,
            nat_gateways=self.config.nat_gateways,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24
                ),
                ec2.SubnetConfiguration(
                    name="private",
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidr_mask=20,
                ),
            ],
        )

    # ------------------------------------------------------------------ #
    def _add_vpc_endpoints(self, vpc: ec2.Vpc) -> None:
        # S3 gateway endpoint (ECR layer blobs live in S3; also SSM).
        vpc.add_gateway_endpoint(
            "S3Endpoint", service=ec2.GatewayVpcEndpointAwsService.S3
        )

        S = ec2.InterfaceVpcEndpointAwsService
        # NOTE: LAMBDA is required because, for a private cluster, CDK runs the
        # kubectl/Helm provider framework INSIDE this isolated VPC; that
        # framework calls the Lambda service API to invoke the kubectl handler,
        # which fails without a lambda endpoint (aws-auth manifest times out).
        interface_endpoints = {
            "EcrApi": S.ECR,
            "EcrDkr": S.ECR_DOCKER,
            "Sts": S.STS,
            "Ec2": S.EC2,
            "Elb": S.ELASTIC_LOAD_BALANCING,
            "Autoscaling": S.AUTOSCALING,
            "Logs": S.CLOUDWATCH_LOGS,
            "Eks": S.EKS,
            "Lambda": S.LAMBDA_,
            "Ssm": S.SSM,
            "SsmMessages": S.SSM_MESSAGES,
            "Ec2Messages": S.EC2_MESSAGES,
        }
        for name, svc in interface_endpoints.items():
            vpc.add_interface_endpoint(
                f"{name}Endpoint",
                service=svc,
                private_dns_enabled=True,
                subnets=ec2.SubnetSelection(
                    subnet_type=ec2.SubnetType.PRIVATE_ISOLATED
                ),
            )

    # ------------------------------------------------------------------ #
    def _create_ecr_repos(self) -> Dict[str, ecr.Repository]:
        repos: Dict[str, ecr.Repository] = {}
        for img in self.images:
            repo_name = img["repo"]
            if repo_name in repos:
                continue
            cid = "Ecr" + "".join(
                p.capitalize() for p in repo_name.replace("/", "-").split("-")
            )
            repos[repo_name] = ecr.Repository(
                self,
                cid,
                repository_name=repo_name,
                image_scan_on_push=True,
                empty_on_delete=True,  # allow `cdk destroy` to remove non-empty repos
                removal_policy=RemovalPolicy.DESTROY,
            )
        return repos

    # ------------------------------------------------------------------ #
    def _create_cluster(self, vpc: ec2.Vpc) -> eks.Cluster:
        return eks.Cluster(
            self,
            "HamiCluster",
            cluster_name=self.config.cluster_name,
            version=eks.KubernetesVersion.of(self.config.k8s_version),
            kubectl_layer=KubectlV36Layer(self, "KubectlLayer"),
            vpc=vpc,
            vpc_subnets=[ec2.SubnetSelection(subnet_type=self._node_subnet_type)],
            default_capacity=0,
            authentication_mode=eks.AuthenticationMode.API_AND_CONFIG_MAP,
            # Private-only API endpoint in BOTH modes. CDK automatically places
            # the *kubectl* handler in the VPC (it talks to the private K8s API,
            # reached via the VPC endpoints provisioned above). The cluster
            # *creation* handler stays outside the VPC on purpose: it calls the
            # EKS management API (eks.<region>.amazonaws.com), which is always
            # reachable and unrelated to the cluster's private K8s endpoint.
            # Forcing it in-VPC makes that management call fail. This is a
            # deploy-time Lambda on AWS's network -- NOT a resource in your VPC,
            # which stays fully private (no IGW/NAT).
            endpoint_access=eks.EndpointAccess.PRIVATE,
        )

    # ------------------------------------------------------------------ #
    def _add_system_nodegroup(self, cluster: eks.Cluster) -> None:
        cluster.add_nodegroup_capacity(
            "SystemNodeGroup",
            instance_types=[ec2.InstanceType(self.config.system_instance_type)],
            min_size=self.config.system_min,
            desired_size=self.config.system_desired,
            max_size=self.config.system_max,
            ami_type=eks.NodegroupAmiType.AL2023_X86_64_STANDARD,
            subnets=ec2.SubnetSelection(subnet_type=self._node_subnet_type),
            labels={"role": "system"},
        )

    # ------------------------------------------------------------------ #
    def _add_gpu_nodegroup(self, cluster: eks.Cluster) -> None:
        cluster.add_nodegroup_capacity(
            "GpuNodeGroup",
            instance_types=[ec2.InstanceType(self.config.gpu_instance_type)],
            min_size=self.config.gpu_min,
            desired_size=self.config.gpu_desired,
            max_size=self.config.gpu_max,
            ami_type=eks.NodegroupAmiType.AL2023_X86_64_NVIDIA,
            disk_size=self.config.gpu_disk_size_gb,
            subnets=ec2.SubnetSelection(subnet_type=self._node_subnet_type),
            labels={"gpu": "on", "role": "gpu"},
        )

    # ------------------------------------------------------------------ #
    def _install_hami(self, cluster: eks.Cluster) -> None:
        values: dict = {
            "resourceName": "nvidia.com/gpu",
            "resourceMem": "nvidia.com/gpumem",
            "resourceCores": "nvidia.com/gpucores",
            # split each physical GPU into N vGPUs (see config note)
            # deviceCoreScaling + defaultMemory are what let N pods actually
            # PACK onto one card: HAMi otherwise gives the first pod the whole
            # card's memory AND all 100 SM cores. Verified on-cluster.
            "devicePlugin": {
                "deviceSplitCount": self.config.hami_device_split_count,
                "deviceCoreScaling": self.config.hami_device_core_scaling,
                "defaultMemory": self.config.hami_default_memory_mb,
            },
        }

        if self.config.air_gapped:
            # Repoint every chart image at ECR via the global registry, and pin
            # the bundled kube-scheduler tag to the cluster version.
            values["global"] = {
                "imageRegistry": self.ecr_registry_host,
                "imageTag": "v2.10.0",
            }
            values["scheduler"] = {
                "kubeScheduler": {"image": {"tag": f"v{self.config.k8s_version}.0"}}
            }
            chart_asset = s3_assets.Asset(
                self, "HamiChartAsset", path=os.path.normpath(_CHART_DIR)
            )
            chart = cluster.add_helm_chart(
                "Hami",
                chart_asset=chart_asset,
                release="hami",
                namespace=self.config.hami_namespace,
                values=values,
                wait=True,
                timeout=Duration.minutes(15),
            )
            # Ensure images exist in ECR before Helm pulls them.
            for repo in self.ecr_repos.values():
                chart.node.add_dependency(repo)
        else:
            values["scheduler"] = {
                "kubeScheduler": {"image": {"tag": f"v{self.config.k8s_version}.0"}}
            }
            chart_kwargs = dict(
                chart="hami",
                repository="https://project-hami.github.io/HAMi/",
                release="hami",
                namespace=self.config.hami_namespace,
                values=values,
                wait=True,
                timeout=Duration.minutes(15),
            )
            if self.config.hami_chart_version:
                chart_kwargs["version"] = self.config.hami_chart_version
            cluster.add_helm_chart("Hami", **chart_kwargs)

    # ------------------------------------------------------------------ #
    def _add_bastion(self, vpc: ec2.Vpc, cluster: eks.Cluster) -> None:
        """A no-inbound SSM instance used purely as a tunnel to the private API.

        Reach the cluster from your workstation with an SSM port-forward
        (see docs/02-provision-cluster.md). The bastion needs no kubectl/aws
        tooling -- it only relays the TLS connection to the EKS endpoint.
        """
        role = iam.Role(
            self,
            "BastionRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "AmazonSSMManagedInstanceCore"
                )
            ],
        )

        sg = ec2.SecurityGroup(
            self,
            "BastionSg",
            vpc=vpc,
            description="HAMi SSM bastion - no inbound",
            allow_all_outbound=True,
        )

        bastion = ec2.Instance(
            self,
            "Bastion",
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_ISOLATED
            ),
            instance_type=ec2.InstanceType(self.config.bastion_instance_type),
            machine_image=ec2.MachineImage.latest_amazon_linux2023(),
            security_group=sg,
            role=role,
            require_imdsv2=True,
        )
        Tags.of(bastion).add("Name", f"{self.config.cluster_name}-bastion")

        # Allow the bastion to reach the private EKS API endpoint on 443.
        cluster.cluster_security_group.add_ingress_rule(
            sg,
            ec2.Port.tcp(443),
            "SSM bastion to EKS private API",
        )

        CfnOutput(self, "BastionInstanceId", value=bastion.instance_id)

    # ------------------------------------------------------------------ #
    def _add_karpenter(self, vpc: ec2.Vpc, cluster: eks.Cluster) -> None:
        """Install Karpenter (CRDs + controller) and a GPU NodePool.

        Air-gapped specifics:
        * controller image is mirrored to ECR (see images.json / mirror script);
        * CRD + controller charts are vendored under infra/charts/ and installed
          via chartAsset (the public OCI chart repo is unreachable in-VPC);
        * the controller reaches EC2/SSM/STS/EKS through the VPC endpoints, and
          Spot interruption (SQS) is disabled -> on-demand only.
        """
        discovery = self.config.cluster_name  # literal string, for tag + selectors
        subnets = (
            vpc.isolated_subnets if self.config.air_gapped else vpc.private_subnets
        )

        # Tag the isolated subnets so Karpenter can discover them by tag.
        for sn in subnets:
            Tags.of(sn).add("karpenter.sh/discovery", discovery)
        # NOTE: the EKS-managed cluster security group is NOT tagged here -- CDK only
        # references it, so Tags.of() would be a silent no-op. The EC2NodeClass below
        # selects it by ID (cluster.cluster_security_group_id) instead.

        # --- IAM: role for Karpenter-launched nodes ----------------------
        node_role = iam.Role(
            self,
            "KarpenterNodeRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(p)
                for p in (
                    "AmazonEKSWorkerNodePolicy",
                    "AmazonEKS_CNI_Policy",
                    "AmazonEC2ContainerRegistryReadOnly",
                    "AmazonSSMManagedInstanceCore",
                )
            ],
        )
        # Let Karpenter nodes join the cluster (EC2_LINUX access entry -> system:nodes).
        eks.CfnAccessEntry(
            self,
            "KarpenterNodeAccessEntry",
            cluster_name=cluster.cluster_name,
            principal_arn=node_role.role_arn,
            type="EC2_LINUX",
        )

        # Pre-create the node instance profile OUT-OF-BAND (at deploy time, from the
        # deployer which can reach IAM). In a fully air-gapped VPC there is NO IAM
        # VPC endpoint (AWS offers none in commercial regions), so Karpenter CANNOT
        # create the instance profile at runtime -- InstanceProfileReady hangs at
        # Unknown and the NodePool is "not ready" forever. Referencing this profile
        # by name (instanceProfile, not role) in the EC2NodeClass makes Karpenter
        # skip the IAM call entirely.
        # No explicit instance_profile_name: a fixed name collides with any profile
        # created out-of-band (e.g. the manual repair on the live cluster), and CFN
        # refuses with "already exists". CDK generates a unique name; .ref resolves
        # to it for the EC2NodeClass below.
        node_instance_profile = iam.CfnInstanceProfile(
            self,
            "KarpenterNodeInstanceProfile",
            roles=[node_role.role_name],
        )

        # --- IAM: controller role (IRSA) ---------------------------------
        sa = cluster.add_service_account(
            "KarpenterSa", name="karpenter", namespace="kube-system"
        )
        # --- Least-privilege controller policy --------------------------------
        # Modelled on the official Karpenter controller policy
        # (karpenter-provider-aws getting-started cloudformation.yaml), reduced
        # further for this stack:
        #   * no instance-profile write actions: the profile is pre-created above,
        #     so Karpenter only reads it (spec.instanceProfile, not spec.role);
        #   * no SQS actions: the interruption queue is disabled (on-demand only).
        # Mutating actions are restricted to resources carrying this cluster's
        # ownership tag, so the controller cannot touch unrelated EC2 resources.
        part, region, account = self.partition, self.region, self.account
        cluster_tag = f"aws:ResourceTag/kubernetes.io/cluster/{self.config.cluster_name}"
        req_cluster_tag = f"aws:RequestTag/kubernetes.io/cluster/{self.config.cluster_name}"
        owned_by_this_cluster = {
            "StringEquals": {cluster_tag: "owned"},
            "StringLike": {"aws:ResourceTag/karpenter.sh/nodepool": "*"},
        }
        tagged_for_this_cluster = {
            "StringEquals": {req_cluster_tag: "owned"},
            "StringLike": {"aws:RequestTag/karpenter.sh/nodepool": "*"},
        }
        launch_targets = [
            f"arn:{part}:ec2:{region}::image/*",
            f"arn:{part}:ec2:{region}::snapshot/*",
            f"arn:{part}:ec2:{region}:*:security-group/*",
            f"arn:{part}:ec2:{region}:*:subnet/*",
            f"arn:{part}:ec2:{region}:*:capacity-reservation/*",
            f"arn:{part}:ec2:{region}:*:placement-group/*",
        ]
        created_resources = [
            f"arn:{part}:ec2:{region}:*:fleet/*",
            f"arn:{part}:ec2:{region}:*:instance/*",
            f"arn:{part}:ec2:{region}:*:volume/*",
            f"arn:{part}:ec2:{region}:*:network-interface/*",
            f"arn:{part}:ec2:{region}:*:launch-template/*",
            f"arn:{part}:ec2:{region}:*:spot-instances-request/*",
        ]

        # Read-only discovery, confined to this region.
        sa.add_to_principal_policy(
            iam.PolicyStatement(
                sid="AllowRegionalReadActions",
                actions=[
                    "ec2:DescribeAvailabilityZones",
                    "ec2:DescribeCapacityReservations",
                    "ec2:DescribeImages",
                    "ec2:DescribeInstances",
                    "ec2:DescribeInstanceStatus",
                    "ec2:DescribeInstanceTypeOfferings",
                    "ec2:DescribeInstanceTypes",
                    "ec2:DescribeLaunchTemplates",
                    "ec2:DescribePlacementGroups",
                    "ec2:DescribeSecurityGroups",
                    "ec2:DescribeSpotPriceHistory",
                    "ec2:DescribeSubnets",
                ],
                resources=["*"],  # Describe* actions do not support resource ARNs
                conditions={"StringEquals": {"aws:RequestedRegion": region}},
            )
        )
        # Launch: may only reference AMIs, subnets and security groups in region.
        sa.add_to_principal_policy(
            iam.PolicyStatement(
                sid="AllowScopedEC2InstanceAccessActions",
                actions=["ec2:RunInstances", "ec2:CreateFleet"],
                resources=launch_targets,
            )
        )
        # Launch from a launch template only if this cluster owns it.
        sa.add_to_principal_policy(
            iam.PolicyStatement(
                sid="AllowScopedEC2LaunchTemplateAccessActions",
                actions=["ec2:RunInstances", "ec2:CreateFleet"],
                resources=[f"arn:{part}:ec2:{region}:*:launch-template/*"],
                conditions=owned_by_this_cluster,
            )
        )
        # Create instances/volumes/ENIs/templates only when tagged to this cluster.
        sa.add_to_principal_policy(
            iam.PolicyStatement(
                sid="AllowScopedEC2InstanceActionsWithTags",
                actions=["ec2:RunInstances", "ec2:CreateFleet", "ec2:CreateLaunchTemplate"],
                resources=created_resources,
                conditions=tagged_for_this_cluster,
            )
        )
        # Tag only at creation time, and only with this cluster's tags.
        sa.add_to_principal_policy(
            iam.PolicyStatement(
                sid="AllowScopedResourceCreationTagging",
                actions=["ec2:CreateTags"],
                resources=created_resources,
                conditions={
                    "StringEquals": {
                        req_cluster_tag: "owned",
                        "ec2:CreateAction": [
                            "RunInstances",
                            "CreateFleet",
                            "CreateLaunchTemplate",
                        ],
                    },
                    "StringLike": {"aws:RequestTag/karpenter.sh/nodepool": "*"},
                },
            )
        )
        # Re-tag existing instances (nodeclaim linking) for owned instances only.
        sa.add_to_principal_policy(
            iam.PolicyStatement(
                sid="AllowScopedResourceTagging",
                actions=["ec2:CreateTags"],
                resources=[f"arn:{part}:ec2:{region}:*:instance/*"],
                conditions={
                    "StringEquals": {cluster_tag: "owned"},
                    "StringLike": {"aws:ResourceTag/karpenter.sh/nodepool": "*"},
                    "ForAllValues:StringEquals": {
                        "aws:TagKeys": ["karpenter.sh/nodeclaim", "Name"]
                    },
                },
            )
        )
        # Terminate / delete only what this cluster owns.
        sa.add_to_principal_policy(
            iam.PolicyStatement(
                sid="AllowScopedDeletion",
                actions=["ec2:TerminateInstances", "ec2:DeleteLaunchTemplate"],
                resources=[
                    f"arn:{part}:ec2:{region}:*:instance/*",
                    f"arn:{part}:ec2:{region}:*:launch-template/*",
                ],
                conditions=owned_by_this_cluster,
            )
        )
        # AMI ID lookup for the AL2023 GPU alias, restricted to the AWS namespace.
        sa.add_to_principal_policy(
            iam.PolicyStatement(
                sid="AllowSSMReadActions",
                actions=["ssm:GetParameter"],
                resources=[f"arn:{part}:ssm:{region}::parameter/aws/service/*"],
            )
        )
        # Instance-type pricing for consolidation decisions.
        sa.add_to_principal_policy(
            iam.PolicyStatement(
                sid="AllowPricingReadActions",
                actions=["pricing:GetProducts"],
                resources=["*"],  # pricing:GetProducts does not support ARNs
            )
        )
        # Cluster endpoint / CA discovery, this cluster only.
        sa.add_to_principal_policy(
            iam.PolicyStatement(
                sid="AllowAPIServerEndpointDiscovery",
                actions=["eks:DescribeCluster"],
                resources=[
                    f"arn:{part}:eks:{region}:{account}:cluster/{self.config.cluster_name}"
                ],
            )
        )
        # Pass ONLY the node role, and only to EC2.
        sa.add_to_principal_policy(
            iam.PolicyStatement(
                sid="AllowPassingInstanceRole",
                actions=["iam:PassRole"],
                resources=[node_role.role_arn],
                conditions={
                    "StringEquals": {"iam:PassedToService": "ec2.amazonaws.com"}
                },
            )
        )
        # Read the pre-created instance profile. No write actions: this stack
        # creates the profile itself, so Karpenter never manages one.
        sa.add_to_principal_policy(
            iam.PolicyStatement(
                sid="AllowInstanceProfileReadActions",
                actions=["iam:GetInstanceProfile"],
                resources=[f"arn:{part}:iam::{account}:instance-profile/*"],
            )
        )
        sa.add_to_principal_policy(
            iam.PolicyStatement(
                sid="AllowUnscopedInstanceProfileListAction",
                actions=["iam:ListInstanceProfiles"],
                resources=["*"],  # iam:ListInstanceProfiles does not support ARNs
            )
        )

        ecr_host = self.ecr_registry_host

        # --- CRDs (must exist before the controller / any NodePool) ------
        crd_chart = cluster.add_helm_chart(
            "KarpenterCrd",
            chart_asset=s3_assets.Asset(
                self, "KarpenterCrdAsset", path=os.path.normpath(_KARPENTER_CRD_DIR)
            ),
            release="karpenter-crd",
            namespace="kube-system",
            wait=True,
            timeout=Duration.minutes(10),
        )

        # --- Karpenter controller ----------------------------------------
        karpenter_chart = cluster.add_helm_chart(
            "Karpenter",
            chart_asset=s3_assets.Asset(
                self, "KarpenterAsset", path=os.path.normpath(_KARPENTER_DIR)
            ),
            release="karpenter",
            namespace="kube-system",
            values={
                "settings": {
                    "clusterName": cluster.cluster_name,
                    "clusterEndpoint": cluster.cluster_endpoint,
                    "interruptionQueue": "",  # Spot/SQS disabled -> on-demand only
                },
                "serviceAccount": {"create": False, "name": "karpenter"},
                "controller": {
                    "image": {
                        "repository": f"{ecr_host}/karpenter/controller",
                        "tag": self.config.karpenter_version,
                        "digest": "",  # use the tag on ECR, not the upstream digest
                    }
                },
                "replicas": 1,
                "nodeSelector": {"role": "system"},
            },
            wait=True,
            timeout=Duration.minutes(15),
        )
        karpenter_chart.node.add_dependency(crd_chart)
        karpenter_chart.node.add_dependency(sa)
        if "karpenter/controller" in self.ecr_repos:
            karpenter_chart.node.add_dependency(self.ecr_repos["karpenter/controller"])

        # --- GPU NodePool + EC2NodeClass ---------------------------------
        ec2nodeclass = {
            "apiVersion": "karpenter.k8s.aws/v1",
            "kind": "EC2NodeClass",
            "metadata": {"name": "gpu"},
            "spec": {
                "amiFamily": "AL2023",
                "amiSelectorTerms": [{"alias": "al2023@latest"}],
                # Use the pre-created instance profile (not `role`): Karpenter cannot
                # create one in-VPC because IAM has no VPC endpoint. See above.
                "instanceProfile": node_instance_profile.ref,
                # REQUIRED for GPU container images. Without this Karpenter uses the
                # AMI's default root volume (20 GB on AL2023), and a single 3.7 GB
                # PyTorch image fills it -> kubelet evicts pods with "node was low on
                # resource: ephemeral-storage". Mirrors gpu_disk_size_gb, which only
                # ever applied to the static/managed node groups.
                "blockDeviceMappings": [
                    {
                        "deviceName": "/dev/xvda",
                        "ebs": {
                            "volumeSize": f"{self.config.gpu_disk_size_gb}Gi",
                            "volumeType": "gp3",
                            "deleteOnTermination": True,
                        },
                    }
                ],
                "subnetSelectorTerms": [{"tags": {"karpenter.sh/discovery": discovery}}],
                # Select the EKS-managed cluster security group by ID. We cannot
                # discover it by tag: Tags.of() on cluster.cluster_security_group is
                # a no-op (CDK only references the EKS-owned SG, it doesn't own it),
                # so a tag selector matches nothing -> SecurityGroupsReady=False.
                "securityGroupSelectorTerms": [
                    {"id": cluster.cluster_security_group_id}
                ],
            },
        }
        nodepool = {
            "apiVersion": "karpenter.sh/v1",
            "kind": "NodePool",
            "metadata": {"name": "gpu"},
            "spec": {
                "template": {
                    "metadata": {"labels": {"gpu": "on", "role": "gpu", "pool": "karpenter"}},
                    "spec": {
                        "nodeClassRef": {
                            "group": "karpenter.k8s.aws",
                            "kind": "EC2NodeClass",
                            "name": "gpu",
                        },
                        "requirements": [
                            {
                                "key": "node.kubernetes.io/instance-type",
                                "operator": "In",
                                "values": [self.config.gpu_instance_type],
                            },
                            {
                                "key": "karpenter.sh/capacity-type",
                                "operator": "In",
                                "values": ["on-demand"],
                            },
                        ],
                        "expireAfter": "168h",
                    },
                },
                # NOTE: this counts HAMi *vGPUs*, not physical cards -- with
                # deviceSplitCount=4 a single card reports 4. A limit of 4 caps the
                # fleet at ONE card and silently blocks scale-out.
                "limits": {"nvidia.com/gpu": 16},
                "disruption": {
                    "consolidationPolicy": "WhenEmptyOrUnderutilized",
                    "consolidateAfter": "1m",
                },
            },
        }
        nodepool_manifest = cluster.add_manifest(
            "KarpenterGpuNodePool", ec2nodeclass, nodepool
        )
        nodepool_manifest.node.add_dependency(karpenter_chart)
        nodepool_manifest.node.add_dependency(node_instance_profile)

    # ------------------------------------------------------------------ #
    def _add_cas_gpu_nodegroup(self, cluster: eks.Cluster) -> None:
        """A dedicated GPU managed node group that Cluster Autoscaler scales.

        min=1 so CAS can build its scale-up node template from a real node's
        allocatable (which includes HAMi's nvidia.com/gpumem) -- this is what
        lets CAS scale on gpumem without needing custom ASG resource tags.
        EKS auto-tags this node group's ASG for CAS auto-discovery.
        """
        cluster.add_nodegroup_capacity(
            "CasGpuNodeGroup",
            instance_types=[ec2.InstanceType(self.config.gpu_instance_type)],
            min_size=self.config.cas_gpu_min,
            desired_size=self.config.cas_gpu_desired,
            max_size=self.config.cas_gpu_max,
            ami_type=eks.NodegroupAmiType.AL2023_X86_64_NVIDIA,
            disk_size=self.config.gpu_disk_size_gb,
            subnets=ec2.SubnetSelection(subnet_type=self._node_subnet_type),
            labels={"gpu": "on", "role": "gpu", "pool": "cas"},
        )

    # ------------------------------------------------------------------ #
    def _add_cluster_autoscaler(self, cluster: eks.Cluster) -> None:
        sa = cluster.add_service_account(
            "ClusterAutoscalerSa", name="cluster-autoscaler", namespace="kube-system"
        )
        sa.add_to_principal_policy(
            iam.PolicyStatement(
                actions=[
                    "autoscaling:DescribeAutoScalingGroups",
                    "autoscaling:DescribeAutoScalingInstances",
                    "autoscaling:DescribeLaunchConfigurations",
                    "autoscaling:DescribeScalingActivities",
                    "autoscaling:DescribeTags",
                    "autoscaling:SetDesiredCapacity",
                    "autoscaling:TerminateInstanceInAutoScalingGroup",
                    "ec2:DescribeInstanceTypes",
                    "ec2:DescribeImages",
                    "ec2:DescribeLaunchTemplateVersions",
                    "ec2:GetInstanceTypesFromInstanceRequirements",
                    "eks:DescribeNodegroup",
                ],
                resources=["*"],
            )
        )

        ecr_host = self.ecr_registry_host
        chart = cluster.add_helm_chart(
            "ClusterAutoscaler",
            chart_asset=s3_assets.Asset(
                self, "CasAsset", path=os.path.normpath(_CAS_DIR)
            ),
            release="cluster-autoscaler",
            namespace="kube-system",
            values={
                "cloudProvider": "aws",
                "awsRegion": self.region,
                "autoDiscovery": {"clusterName": self.config.cluster_name},
                "image": {
                    "repository": f"{ecr_host}/autoscaling/cluster-autoscaler",
                    "tag": self.config.cas_version,
                },
                "rbac": {
                    "serviceAccount": {
                        "create": False,
                        "name": "cluster-autoscaler",
                    }
                },
                "nodeSelector": {"role": "system"},
                "extraArgs": {
                    "expander": "least-waste",
                    "balance-similar-node-groups": True,
                    "scale-down-unneeded-time": "1m",
                    "scale-down-delay-after-add": "1m",
                    # allow scaling down GPU nodes whose only pods are daemonsets
                    "skip-nodes-with-system-pods": False,
                },
            },
            wait=True,
            timeout=Duration.minutes(15),
        )
        chart.node.add_dependency(sa)
        if "autoscaling/cluster-autoscaler" in self.ecr_repos:
            chart.node.add_dependency(self.ecr_repos["autoscaling/cluster-autoscaler"])

    # ------------------------------------------------------------------ #
    # ------------------------------------------------------------------ #
    def _install_keda(self, cluster: eks.Cluster) -> None:
        """KEDA -- the in-cluster, no-custom-code GPU headroom autoscaler.

        Replaces the demo-only bash loop (scripts/gpu-headroom-bridge.sh) with a
        declarative ScaledObject (examples/buffer/04-keda-scaledobject.yaml).

        Why this exists at all: HAMi's mutating webhook injects
        nvidia.com/gpucores into every GPU pod, and Karpenter sizes nodes from the
        EC2 instance-type catalogue, which has no such resource -- so Karpenter
        never provisions for GPU pods. KEDA's built-in `kubernetes-workload`
        scaler counts GPU pods and scales the cpu-only balloon Deployment, which
        Karpenter CAN see. That is the translation layer, with no code to run.

        Air-gapped notes: the chart is vendored under infra/charts/keda and all
        three images are mirrored to ECR; global.image.registry repoints them.
        """
        ecr_host = self.ecr_registry_host
        values: dict = {
            # KEDA needs to run somewhere that always exists -> system nodes.
            "nodeSelector": {"role": "system"},
            "crds": {"install": True},
            # Always-pull is pointless in an air-gapped cluster and slows starts.
            "image": {"pullPolicy": "IfNotPresent"},
        }
        if self.config.air_gapped:
            # One switch repoints all three component images at ECR. Tags must be
            # pinned because the chart otherwise derives them from appVersion.
            values["global"] = {"image": {"registry": ecr_host}}
            values["image"].update(
                {
                    "keda": {"tag": self.config.keda_version},
                    "metricsApiServer": {"tag": self.config.keda_version},
                    "webhooks": {"tag": self.config.keda_version},
                }
            )

        chart = cluster.add_helm_chart(
            "Keda",
            chart_asset=s3_assets.Asset(
                self, "KedaChartAsset", path=os.path.normpath(_KEDA_DIR)
            ),
            release="keda",
            namespace="kube-system",
            values=values,
            wait=True,
            timeout=Duration.minutes(15),
        )
        for repo_name, repo in self.ecr_repos.items():
            if repo_name.startswith("kedacore/"):
                chart.node.add_dependency(repo)

    def _grant_admin_access(self, cluster: eks.Cluster) -> None:
        arn = self.config.cluster_admin_role_arn
        if not arn:
            return
        cluster.grant_access(
            "ClusterAdminAccess",
            arn,
            [
                eks.AccessPolicy.from_access_policy_name(
                    "AmazonEKSClusterAdminPolicy",
                    access_scope_type=eks.AccessScopeType.CLUSTER,
                )
            ],
        )

    # ------------------------------------------------------------------ #
    def _outputs(self, cluster: eks.Cluster) -> None:
        CfnOutput(self, "ClusterNameOutput", value=cluster.cluster_name)
        CfnOutput(self, "RegionOutput", value=self.region)
        CfnOutput(self, "AirGapped", value=str(self.config.air_gapped))
        CfnOutput(self, "HamiInstalled", value=str(self.config.install_hami))
        CfnOutput(self, "KarpenterEnabled", value=str(self.config.enable_karpenter))
        CfnOutput(self, "KedaEnabled", value=str(self.config.enable_keda))
        CfnOutput(
            self,
            "UpdateKubeconfigCommand",
            value=(
                f"aws eks update-kubeconfig --region {self.region} "
                f"--name {cluster.cluster_name}"
            ),
        )
        if self.config.air_gapped:
            CfnOutput(self, "EcrRegistryHost", value=self.ecr_registry_host)
