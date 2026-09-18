#!/usr/bin/env bash
# Fetch the third-party Helm charts this stack installs.
#
# The charts are NOT vendored into this repository -- they belong to their upstream
# projects (all Apache-2.0) and are pulled at setup time instead. The CDK stack
# installs them from infra/charts/ via chartAsset, which is required for an
# air-gapped cluster: the public chart repos are unreachable from inside a VPC with
# no internet egress, so the chart must be uploaded as a CDK asset.
#
# Run this ONCE before `cdk deploy`.
#
#   ./scripts/fetch-charts.sh
#
# Requires: helm 3.x on PATH.
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
CHARTS="$HERE/infra/charts"
HAMI_VERSION="${HAMI_VERSION:-2.10.0}"
KARPENTER_VERSION="${KARPENTER_VERSION:-1.14.1}"
KEDA_VERSION="${KEDA_VERSION:-2.20.2}"

command -v helm >/dev/null 2>&1 || { echo "ERROR: helm not found on PATH." >&2; exit 1; }
mkdir -p "$CHARTS"

pull() {  # repo_name repo_url chart version dest
  local name="$1" url="$2" chart="$3" version="$4" dest="$5"
  echo "==> $chart $version -> infra/charts/$dest"
  helm repo add "$name" "$url" >/dev/null 2>&1 || true
  helm repo update "$name" >/dev/null 2>&1
  rm -rf "$CHARTS/$dest"
  # CDK's chartAsset needs an extracted DIRECTORY, not a .tgz
  helm pull "$name/$chart" --version "$version" --destination "$CHARTS" --untar
  [ "$chart" != "$dest" ] && mv "$CHARTS/$chart" "$CHARTS/$dest"
  return 0
}

pull hami       https://project-hami.github.io/HAMi/ hami          "$HAMI_VERSION"      hami
pull keda       https://kedacore.github.io/charts    keda          "$KEDA_VERSION"      keda

# Karpenter ships from a public ECR OCI registry (no repo add needed)
echo "==> karpenter $KARPENTER_VERSION -> infra/charts/karpenter"
rm -rf "$CHARTS/karpenter" "$CHARTS/karpenter-crd"
helm pull "oci://public.ecr.aws/karpenter/karpenter" --version "$KARPENTER_VERSION" \
  --destination "$CHARTS" --untar
helm pull "oci://public.ecr.aws/karpenter/karpenter-crd" --version "$KARPENTER_VERSION" \
  --destination "$CHARTS" --untar

echo
echo "==> Done. Charts in infra/charts:"
ls -1 "$CHARTS"
echo
echo "Next: ./scripts/mirror-images.sh   (mirror images to ECR)"
