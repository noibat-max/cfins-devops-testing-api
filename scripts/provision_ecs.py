#!/usr/bin/env python3
"""
Provision the ECS Fargate infrastructure that runs the QA Workbench worker image
(the `run_now` remote-execution path).

Creates, idempotently:
  * task-execution role  cfins-qaworkbench-runner-exec   (ECR pull + CloudWatch logs)
  * task role            cfins-qaworkbench-runner-task    (scoped cfins-qaworkbench* DDB/S3/Secrets)
  * log group            /ecs/cfins-qaworkbench-runner
  * ECS cluster          cfins-qaworkbench
  * task definition      cfins-qaworkbench-runner         (pins the image DIGEST)

…and prints the default-VPC subnets + security group and a ready-to-run
`aws ecs run-task` command for the Phase-D proof.

This is the dev-provisioning + hand-over artifact: it mirrors what a DevOps CDK
stack would create, using the `cfins-local-ecs-provisioning` IAM grant. Creating
these costs nothing until a task actually runs.

Design notes:
  * The image is pinned by DIGEST (immutable) resolved from a tag, matching the
    "build once, promote the digest" model.
  * NO Nova Act key here — for a full green run, pass NOVA_ACT_SECRET_ARN (a
    Secrets Manager secret ARN); the script wires it into the task def's
    `secrets` and grants the exec role read on just that secret. Without it the
    task still proves launch + ECR pull + task-role AWS access + worker boot.

Run locally against real AWS via the cfins-local profile:

    AWS_PROFILE=cfins-local AWS_REGION=us-east-1 \\
        python scripts/provision_ecs.py
"""
from __future__ import annotations

import json
import os
import sys

import boto3
from botocore.exceptions import ClientError

REGION = os.environ.get("AWS_REGION", "us-east-1")
ACCOUNT = "103930328611"

REPO = "cfins-qaworkbench-runner"
IMAGE_TAG = os.environ.get("IMAGE_TAG", "0.1.0")
EXEC_ROLE = "cfins-qaworkbench-runner-exec"
TASK_ROLE = "cfins-qaworkbench-runner-task"
LOG_GROUP = "/ecs/cfins-qaworkbench-runner"
CLUSTER = "cfins-qaworkbench"
FAMILY = "cfins-qaworkbench-runner"

TABLE = os.environ.get("WORKBENCH_TABLE", "cfins-qaworkbench")
BUCKET = os.environ.get("ARTIFACTS_BUCKET", "cfins-qaworkbench-local")
SECRET_PREFIX = os.environ.get("SECRET_PREFIX", "cfins-qaworkbench")
CPU = os.environ.get("TASK_CPU", "2048")      # 2 vCPU
MEMORY = os.environ.get("TASK_MEMORY", "4096")  # 4 GB — headroom for headless Chromium
NOVA_ACT_SECRET_ARN = os.environ.get("NOVA_ACT_SECRET_ARN")  # optional

ECS_TASKS_TRUST = {
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "ecs-tasks.amazonaws.com"},
        "Action": "sts:AssumeRole",
    }],
}

# What the worker needs at runtime — scoped to cfins-qaworkbench* resources.
TASK_ROLE_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "Ddb",
            "Effect": "Allow",
            "Action": [
                "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem",
                "dynamodb:DeleteItem", "dynamodb:Query", "dynamodb:BatchGetItem",
                "dynamodb:BatchWriteItem",
            ],
            "Resource": [
                f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/{TABLE}",
                f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/{TABLE}/index/*",
            ],
        },
        {
            "Sid": "S3Objects",
            "Effect": "Allow",
            "Action": ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"],
            "Resource": "arn:aws:s3:::cfins-qaworkbench-*/*",
        },
        {
            "Sid": "S3List",
            "Effect": "Allow",
            "Action": ["s3:ListBucket"],
            "Resource": "arn:aws:s3:::cfins-qaworkbench-*",
        },
        {
            "Sid": "Secrets",
            "Effect": "Allow",
            "Action": ["secretsmanager:GetSecretValue"],
            "Resource": f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:{SECRET_PREFIX}*",
        },
    ],
}

iam = boto3.client("iam", region_name=REGION)
ecs = boto3.client("ecs", region_name=REGION)
logs = boto3.client("logs", region_name=REGION)
ec2 = boto3.client("ec2", region_name=REGION)
ecr = boto3.client("ecr", region_name=REGION)


def _ok(msg):  print(f"  ✓ {msg}")
def _info(msg): print(f"  • {msg}")
def _warn(msg): print(f"  ! {msg}")


def ensure_role(name: str, trust: dict) -> str:
    try:
        iam.create_role(
            RoleName=name,
            AssumeRolePolicyDocument=json.dumps(trust),
            Description="QA Workbench runner (ECS Fargate)",
            Tags=[{"Key": "app", "Value": "cfins-qaworkbench"}],
        )
        _ok(f"role {name} created")
    except ClientError as e:
        if e.response["Error"]["Code"] == "EntityAlreadyExists":
            _info(f"role {name} already exists")
        else:
            raise
    return f"arn:aws:iam::{ACCOUNT}:role/{name}"


def attach_managed(role: str, policy_arn: str):
    iam.attach_role_policy(RoleName=role, PolicyArn=policy_arn)  # idempotent
    _ok(f"{role}: attached {policy_arn.split('/')[-1]}")


def put_inline(role: str, policy_name: str, doc: dict):
    iam.put_role_policy(RoleName=role, PolicyName=policy_name, PolicyDocument=json.dumps(doc))
    _ok(f"{role}: inline policy {policy_name} set")


def ensure_log_group(name: str):
    try:
        logs.create_log_group(logGroupName=name)
        _ok(f"log group {name} created")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceAlreadyExistsException":
            _info(f"log group {name} already exists")
        else:
            raise
    try:
        logs.put_retention_policy(logGroupName=name, retentionInDays=30)
        _ok(f"log group retention = 30d")
    except ClientError as e:
        _warn(f"set retention: {e.response['Error']['Code']}")


def ensure_cluster(name: str):
    ecs.create_cluster(clusterName=name, tags=[{"key": "app", "value": "cfins-qaworkbench"}])
    _ok(f"cluster {name} ready")


def resolve_digest(tag: str) -> str:
    resp = ecr.describe_images(repositoryName=REPO, imageIds=[{"imageTag": tag}])
    digest = resp["imageDetails"][0]["imageDigest"]
    _ok(f"image {REPO}:{tag} -> {digest}")
    return digest


def register_task_def(exec_arn: str, task_arn: str, image: str) -> str:
    container = {
        "name": "runner",
        "image": image,
        "essential": True,
        "environment": [
            {"name": "WORKBENCH_TABLE", "value": TABLE},
            {"name": "ARTIFACTS_BUCKET", "value": BUCKET},
            {"name": "SECRET_PREFIX", "value": SECRET_PREFIX},
            {"name": "AWS_REGION", "value": REGION},
            # Browser is baked into the image → Nova Act must not re-install it at
            # runtime (fails as non-root). Also set in the Dockerfile ENV; kept
            # here so an older image works via task-def injection too.
            {"name": "NOVA_ACT_SKIP_PLAYWRIGHT_INSTALL", "value": "1"},
        ],
        "logConfiguration": {
            "logDriver": "awslogs",
            "options": {
                "awslogs-group": LOG_GROUP,
                "awslogs-region": REGION,
                "awslogs-stream-prefix": "runner",
            },
        },
    }
    if NOVA_ACT_SECRET_ARN:
        container["secrets"] = [{"name": "NOVA_ACT_API_KEY", "valueFrom": NOVA_ACT_SECRET_ARN}]
        _info("wired NOVA_ACT_API_KEY from Secrets Manager into the task def")

    resp = ecs.register_task_definition(
        family=FAMILY,
        networkMode="awsvpc",
        requiresCompatibilities=["FARGATE"],
        cpu=CPU,
        memory=MEMORY,
        executionRoleArn=exec_arn,
        taskRoleArn=task_arn,
        containerDefinitions=[container],
        runtimePlatform={"cpuArchitecture": "X86_64", "operatingSystemFamily": "LINUX"},
        tags=[{"key": "app", "value": "cfins-qaworkbench"}],
    )
    arn = resp["taskDefinition"]["taskDefinitionArn"]
    _ok(f"task definition registered: {arn}")
    return arn


def default_network() -> tuple[list[str], str]:
    vpcs = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"]
    if not vpcs:
        _warn("no default VPC found — pass subnets/SG manually for run-task")
        return [], ""
    vpc_id = vpcs[0]["VpcId"]
    subnets = [s["SubnetId"] for s in ec2.describe_subnets(
        Filters=[{"Name": "vpc-id", "Values": [vpc_id]},
                 {"Name": "map-public-ip-on-launch", "Values": ["true"]}])["Subnets"]]
    if not subnets:  # fall back to all subnets in the default VPC
        subnets = [s["SubnetId"] for s in ec2.describe_subnets(
            Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["Subnets"]]
    sgs = ec2.describe_security_groups(
        Filters=[{"Name": "vpc-id", "Values": [vpc_id]},
                 {"Name": "group-name", "Values": ["default"]}])["SecurityGroups"]
    sg = sgs[0]["GroupId"] if sgs else ""
    _ok(f"default VPC {vpc_id}: {len(subnets)} subnet(s), default SG {sg}")
    return subnets, sg


def main():
    print(f"Provisioning ECS runner infra in {REGION} (account {ACCOUNT})\n")

    print("[1/6] IAM roles")
    exec_arn = ensure_role(EXEC_ROLE, ECS_TASKS_TRUST)
    attach_managed(EXEC_ROLE, "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy")
    if NOVA_ACT_SECRET_ARN:  # exec role reads the Nova Act secret to inject it
        put_inline(EXEC_ROLE, "read-nova-secret", {
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Allow", "Action": "secretsmanager:GetSecretValue",
                           "Resource": NOVA_ACT_SECRET_ARN}],
        })
    task_arn = ensure_role(TASK_ROLE, ECS_TASKS_TRUST)
    put_inline(TASK_ROLE, "workbench-access", TASK_ROLE_POLICY)

    print("\n[2/6] CloudWatch log group")
    ensure_log_group(LOG_GROUP)

    print("\n[3/6] ECS cluster")
    ensure_cluster(CLUSTER)

    print("\n[4/6] Resolve image digest")
    digest = resolve_digest(IMAGE_TAG)
    image = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/{REPO}@{digest}"

    print("\n[5/6] Register task definition")
    td_arn = register_task_def(exec_arn, task_arn, image)

    print("\n[6/6] Default-VPC networking")
    subnets, sg = default_network()

    print("\nDONE. To launch a run (Phase D), pre-create an execution, then:\n")
    subnet_csv = ",".join(subnets[:2]) if subnets else "<subnet-a>,<subnet-b>"
    print(
        f"AWS_PROFILE=cfins-local aws ecs run-task --region {REGION} \\\n"
        f"  --cluster {CLUSTER} --launch-type FARGATE \\\n"
        f"  --task-definition {FAMILY} \\\n"
        f"  --network-configuration 'awsvpcConfiguration={{subnets=[{subnet_csv}],securityGroups=[{sg or '<sg>'}],assignPublicIp=ENABLED}}' \\\n"
        f"  --overrides '{{\"containerOverrides\":[{{\"name\":\"runner\",\"environment\":[{{\"name\":\"USECASE_ID\",\"value\":\"<uc>\"}},{{\"name\":\"EXECUTION_ID\",\"value\":\"<eid>\"}}]}}]}}'\n"
    )
    if not NOVA_ACT_SECRET_ARN:
        print("NOTE: no NOVA_ACT_SECRET_ARN set → the task will boot + reach AWS but fail at the")
        print("      browser step (no Nova Act key). Set it + re-run this script for a full green run.")


if __name__ == "__main__":
    try:
        main()
    except ClientError as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        sys.exit(1)
