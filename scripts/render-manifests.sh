#!/usr/bin/env bash
# Render the manifests in manifests/ with your account-specific values.
#
# The committed manifests use placeholders so no AWS account ID is ever published:
#   <AWS_ACCOUNT_ID> <AWS_REGION> <CLUSTER_NAME>
#   <CLUSTER_SECURITY_GROUP_ID> <KARPENTER_NODE_INSTANCE_PROFILE>
#
# This script substitutes real values and writes concrete files to
# manifests/rendered/ (gitignored). Apply THOSE -- never pipe placeholders through
# a shell variable at apply time, or you get InvalidImageName from a partially
# expanded path.
#
#   ./scripts/render-manifests.sh
#   kubectl apply -f manifests/rendered/00-priorityclasses.yaml
#
# Values are discovered from the deployed stack where possible; override with env.
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$HERE/manifests/rendered"
REGION="${AWS_REGION:-us-east-1}"
CLUSTER="${CLUSTER_NAME:-hami-eks-demo}"
PROFILE_ARG=(); [ -n "${AWS_PROFILE:-}" ] && PROFILE_ARG=(--profile "$AWS_PROFILE")

ACCOUNT="${AWS_ACCOUNT_ID:-$(aws sts get-caller-identity "${PROFILE_ARG[@]}" \
          --query Account --output text)}"

# Cluster security group: Karpenter's EC2NodeClass selects it by ID because CDK
# cannot tag the EKS-managed SG (Tags.of() on it is a silent no-op).
SG="${CLUSTER_SECURITY_GROUP_ID:-$(aws eks describe-cluster "${PROFILE_ARG[@]}" \
      --region "$REGION" --name "$CLUSTER" \
      --query 'cluster.resourcesVpcConfig.clusterSecurityGroupId' --output text)}"

# Node instance profile is pre-created by the stack (IAM is unreachable in-VPC).
PROFILE_NAME="${KARPENTER_NODE_INSTANCE_PROFILE:-$(aws iam list-instance-profiles \
      "${PROFILE_ARG[@]}" \
      --query "InstanceProfiles[?contains(InstanceProfileName,'KarpenterNode')].InstanceProfileName | [0]" \
      --output text)}"

for v in ACCOUNT SG PROFILE_NAME; do
  [ -z "${!v}" ] || [ "${!v}" = "None" ] && { echo "ERROR: could not resolve $v. Set it via env." >&2; exit 1; }
done

mkdir -p "$OUT"
for f in "$HERE"/manifests/*.yaml; do
  base="$(basename "$f")"
  sed -e "s|<AWS_ACCOUNT_ID>|$ACCOUNT|g" \
      -e "s|<AWS_REGION>|$REGION|g" \
      -e "s|<CLUSTER_NAME>|$CLUSTER|g" \
      -e "s|<CLUSTER_SECURITY_GROUP_ID>|$SG|g" \
      -e "s|<KARPENTER_NODE_INSTANCE_PROFILE>|$PROFILE_NAME|g" \
      "$f" > "$OUT/$base"
done

echo "==> Rendered into manifests/rendered/ using:"
echo "    account=$ACCOUNT  region=$REGION  cluster=$CLUSTER"
echo "    sg=$SG  instanceProfile=$PROFILE_NAME"
ls -1 "$OUT" | sed 's/^/    /'
