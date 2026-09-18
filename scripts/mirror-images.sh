#!/usr/bin/env bash
# Mirror the public images listed in infra/images.json into the account's
# private ECR, so the air-gapped cluster can pull them over PrivateLink.
#
# Uses `crane` for registry-to-registry copy (no Docker daemon, streams layers).
# Run AFTER the stack has created the ECR repos (they are created even when
# HAMi install is deferred). The ECR repos are named to match each image's
# `repo` field.
#
# Usage:
#   AWS_PROFILE=<your-aws-profile> ./scripts/mirror-images.sh
#   ./scripts/mirror-images.sh --profile <your-aws-profile> --region us-east-1
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
PROFILE="${AWS_PROFILE:-}"
while [ $# -gt 0 ]; do
  case "$1" in
    --profile) PROFILE="$2"; shift 2 ;;
    --region)  REGION="$2";  shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
PROFILE_ARG=(); [ -n "$PROFILE" ] && PROFILE_ARG=(--profile "$PROFILE")

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
IMAGES_JSON="$ROOT/infra/images.json"

# Locate crane (repo-local tools/ dir or PATH).
CRANE="$ROOT/tools/crane"
[ -x "$CRANE" ] || CRANE="$(command -v crane || true)"
if [ -z "$CRANE" ] || [ ! -x "$CRANE" ]; then
  echo "ERROR: crane not found. Install it or place the binary at tools/crane." >&2
  echo "  https://github.com/google/go-containerregistry/releases" >&2
  exit 1
fi

ACCOUNT="$(aws sts get-caller-identity "${PROFILE_ARG[@]}" --query Account --output text)"
ECR_HOST="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com"
echo "==> Target ECR: ${ECR_HOST} (region ${REGION})"

echo "==> Logging crane in to ECR"
aws ecr get-login-password "${PROFILE_ARG[@]}" --region "$REGION" \
  | "$CRANE" auth login --username AWS --password-stdin "$ECR_HOST"

# Iterate images.json with python3 (present on macOS/Linux).
python3 - "$IMAGES_JSON" <<'PY' | while IFS=$'\t' read -r SRC REPO TAG; do
import json, sys
for i in json.load(open(sys.argv[1]))["images"]:
    print(f'{i["source"]}\t{i["repo"]}\t{i["tag"]}')
PY
  DEST="${ECR_HOST}/${REPO}:${TAG}"
  echo "==> ${SRC}  ->  ${DEST}"
  "$CRANE" copy "$SRC" "$DEST"
done

echo "==> Done. Mirrored images:"
python3 - "$IMAGES_JSON" <<'PY'
import json, sys
for i in json.load(open(sys.argv[1]))["images"]:
    print(f'   {i["repo"]}:{i["tag"]}')
PY
