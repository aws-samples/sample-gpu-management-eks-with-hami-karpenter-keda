# 06 · Cleanup

GPU instances and the EKS control plane bill by the hour. Tear everything down
when you're finished.

## 1. Remove workloads (optional)

`cdk destroy` deletes the whole cluster, so this is only needed if you want to
clean up jobs without destroying the cluster:

```bash
kubectl delete -f examples/ --ignore-not-found
```

## 2. Destroy the stack

```bash
cd infra
source .venv/bin/activate     # if not already active
cdk destroy
```

Confirm when prompted. This removes, in order: the HAMi Helm release, both node
groups, the EKS control plane, the VPC endpoints, and the VPC. It takes ~20–30
minutes (deleting the in-VPC kubectl-provider Lambdas waits on ENI cleanup,
which can add 20+ minutes — this is normal).

## 3. Verify nothing is left billing

```bash
# No cluster
aws eks list-clusters --region us-east-1

# No leftover GPU instances from this demo
aws ec2 describe-instances --region us-east-1 \
  --filters "Name=instance-state-name,Values=running,pending" \
            "Name=tag:eks:cluster-name,Values=<CLUSTER_NAME>" \
  --query 'Reservations[].Instances[].InstanceId'

# No leftover VPC interface endpoints (they bill hourly)
aws ec2 describe-vpc-endpoints --region us-east-1 \
  --filters "Name=tag:aws:cloudformation:stack-name,Values=HamiEksStack" \
  --query 'VpcEndpoints[].VpcEndpointId'
```

All three should return empty.

## If `cdk destroy` gets stuck

Occasionally a stack won't delete because Kubernetes-managed resources (e.g. a
`LoadBalancer` Service that created an ELB, or leftover ENIs) still exist.

1. Delete any `Service type=LoadBalancer` you created, then retry `cdk destroy`.
2. If a CloudFormation stack is stuck in `DELETE_FAILED`, open the
   [CloudFormation console](https://us-east-1.console.aws.amazon.com/cloudformation/),
   inspect the failed resource, remove it manually, and delete the stack again.
3. The CDK bootstrap stack (`CDKToolkit`) and its S3 bucket are shared
   infrastructure — leave them unless you're sure nothing else uses them.

## Cost hygiene

- The **GPU instance**, **VPC endpoints**, and **bastion** are the ongoing costs
  — make sure the whole stack is gone (step 3).
- Consider setting an [AWS Budget](https://console.aws.amazon.com/billing/home#/budgets)
  alert before your next deploy.
