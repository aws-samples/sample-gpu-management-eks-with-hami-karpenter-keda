#!/usr/bin/env python3
"""CDK application entry point for the HAMi-on-EKS demo.

Synthesizes a single stack that provisions a ready-to-use EKS cluster with a
GPU node group and HAMi installed for virtual-GPU management.

Usage:
    cdk synth
    cdk deploy
    cdk deploy -c gpu_instance_type=g5.xlarge -c gpu_desired=2
"""

import aws_cdk as cdk

from config import Config
from stacks.eks_hami_stack import EksHamiStack

app = cdk.App()
config = Config.from_context(app)

EksHamiStack(
    app,
    "HamiEksStack",
    config=config,
    description="EKS cluster with HAMi for virtual-GPU management (demo/workshop)",
    env=cdk.Environment(account=config.account, region=config.region),
)

app.synth()
