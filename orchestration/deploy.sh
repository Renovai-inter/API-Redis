#!/usr/bin/env bash
# Usado pelo GitHub Actions e disponível também para execução manual.
set -euo pipefail
cd "$(dirname "$0")/.."

: "${AWS_REGION:?Informe AWS_REGION}"
: "${EKS_CLUSTER_NAME:?Informe EKS_CLUSTER_NAME}"
ECR_REPOSITORY="${ECR_REPOSITORY:-renovai-api-redis}"
IMAGE_TAG="${IMAGE_TAG:-$(date -u +%Y%m%d%H%M%S)}"
TASK_DIR="$(mktemp -d)"
trap 'rm -rf "$TASK_DIR"' EXIT
export KUBECONFIG="$TASK_DIR/kubeconfig"

aws eks update-kubeconfig --name "$EKS_CLUSTER_NAME" --region "$AWS_REGION" --kubeconfig "$KUBECONFIG"
kubectl get secret redis-api-secrets -n renovai-api >/dev/null
aws ecr describe-repositories --repository-names "$ECR_REPOSITORY" --region "$AWS_REGION" >/dev/null
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
REGISTRY="$ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com"
export ECR_IMAGE="$REGISTRY/$ECR_REPOSITORY:$IMAGE_TAG"

aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "$REGISTRY"
# Git aplica o .gitignore mesmo quando o projeto veio do ZIP, sem pasta .git.
git init --quiet --bare "$TASK_DIR/context.git"
git --git-dir="$TASK_DIR/context.git" --work-tree="$PWD" ls-files --others --exclude-standard -z \
    | tar --null --verbatim-files-from -T - -cf "$TASK_DIR/context.tar"
docker build --platform linux/amd64 -f orchestration/Dockerfile -t "$ECR_IMAGE" - < "$TASK_DIR/context.tar"
docker push "$ECR_IMAGE"

python3 - "$TASK_DIR/app.yaml" <<'PY'
import os
import sys
from pathlib import Path

manifest = Path("orchestration/k8s/app.yaml").read_text()
assert '${ECR_IMAGE}' in manifest, "Placeholder ECR_IMAGE ausente"
Path(sys.argv[1]).write_text(manifest.replace('${ECR_IMAGE}', os.environ["ECR_IMAGE"]))
PY
kubectl apply -f "$TASK_DIR/app.yaml"
kubectl rollout status deployment/redis-api -n renovai-api --timeout=300s
kubectl get pods,service -n renovai-api -l app=redis-api
