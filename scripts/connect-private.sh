#!/usr/bin/env bash
# Open an SSM port-forward to the PRIVATE EKS API through the bastion, and write
# a kubeconfig that talks to the cluster through the tunnel. Run this in YOUR
# terminal (the KiroCrew agent environment blocks `aws ssm start-session`).
#
# Prereqs: aws CLI v2, the session-manager-plugin, and kubectl on PATH.
#
#   AWS_PROFILE=<your-aws-profile> ./scripts/connect-private.sh
#   # then, in the same shell:
#   export KUBECONFIG=/tmp/hami.kube
#   ./scripts/verify-hami.sh
#   kubectl apply -f examples/02-gpu-share-two-pods.yaml
#
# Ctrl-C stops the tunnel when you're done.
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
PROFILE_ARG=(); [ -n "${AWS_PROFILE:-}" ] && PROFILE_ARG=(--profile "$AWS_PROFILE")
CLUSTER="${CLUSTER_NAME:-hami-eks-demo}"
KUBECONFIG_OUT="${KUBECONFIG_OUT:-/tmp/hami.kube}"
LOCAL_PORT="${LOCAL_PORT:-6443}"

# Make the repo's vendored tools (session-manager-plugin, kubectl) available on
# PATH -- `aws ssm start-session` shells out to `session-manager-plugin` and only
# searches PATH for it.
TOOLS_DIR="$(cd "$(dirname "$0")/../tools" 2>/dev/null && pwd || true)"
[ -n "$TOOLS_DIR" ] && export PATH="$TOOLS_DIR:$PATH"
if ! command -v session-manager-plugin >/dev/null 2>&1; then
  echo "ERROR: session-manager-plugin not found (needed by 'aws ssm start-session')." >&2
  echo "  Install it:  brew install --cask session-manager-plugin" >&2
  echo "  or: https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html" >&2
  exit 1
fi

echo "==> Finding bastion instance"
BASTION=$(aws ec2 describe-instances "${PROFILE_ARG[@]}" --region "$REGION" \
  --filters "Name=tag:Name,Values=${CLUSTER}-bastion" "Name=instance-state-name,Values=running" \
  --query 'Reservations[0].Instances[0].InstanceId' --output text)
[ "$BASTION" = "None" ] && { echo "No running bastion found for ${CLUSTER}"; exit 1; }
echo "    bastion: $BASTION"

echo "==> Writing kubeconfig to $KUBECONFIG_OUT (server via localhost tunnel)"
aws eks update-kubeconfig "${PROFILE_ARG[@]}" --region "$REGION" \
  --name "$CLUSTER" --kubeconfig "$KUBECONFIG_OUT" >/dev/null
API_HOST=$(aws eks describe-cluster "${PROFILE_ARG[@]}" --region "$REGION" \
  --name "$CLUSTER" --query 'cluster.endpoint' --output text | sed 's#https://##')
CL=$(KUBECONFIG="$KUBECONFIG_OUT" kubectl config view -o jsonpath='{.clusters[0].name}')
KUBECONFIG="$KUBECONFIG_OUT" kubectl config set "clusters.${CL}.server" "https://localhost:${LOCAL_PORT}" >/dev/null
# Keep TLS SNI as the real API hostname so the cert validates through the tunnel.
python3 - "$KUBECONFIG_OUT" "$API_HOST" "$CL" <<'PY'
import sys, yaml
kc, host, cl = sys.argv[1:4]
d = yaml.safe_load(open(kc))
for c in d["clusters"]:
    if c["name"] == cl:
        c["cluster"]["tls-server-name"] = host
yaml.safe_dump(d, open(kc, "w"))
PY

echo "==> Starting SSM port-forward  localhost:${LOCAL_PORT} -> ${API_HOST}:443"
echo "    (leave this running; open another shell to run kubectl)"
echo
echo "    export KUBECONFIG=${KUBECONFIG_OUT}"
echo "    kubectl get nodes -L gpu"
echo
exec aws ssm start-session "${PROFILE_ARG[@]}" --region "$REGION" --target "$BASTION" \
  --document-name AWS-StartPortForwardingSessionToRemoteHost \
  --parameters "{\"host\":[\"${API_HOST}\"],\"portNumber\":[\"443\"],\"localPortNumber\":[\"${LOCAL_PORT}\"]}"
