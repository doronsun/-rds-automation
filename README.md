# Serverless RDS Cluster Automation

> Provision AWS Aurora RDS clusters on demand via a single HTTP request — no console, no manual Terraform, no secrets in code.

[![CircleCI](https://dl.circleci.com/status-badge/img/gh/doronsun/-rds-automation/tree/main.svg?style=shield)](https://dl.circleci.com/status-badge/redirect/gh/doronsun/-rds-automation/tree/main)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/release/python-3120/)
[![Terraform ≥ 1.6](https://img.shields.io/badge/terraform-%E2%89%A51.6-7B42BC.svg)](https://developer.hashicorp.com/terraform/install)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## 🗺 Architecture

```
                             CALLER
                               │
                curl -X POST /provision
                  -H "x-api-key: <key>"
                  -d '{"cluster_name":"payments-db",
                        "environment":"prod",
                        "engine":"postgres"}'
                               │
                               ▼
              ┌────────────────────────────────┐
              │         API Gateway            │
              │  POST /provision               │
              │  x-api-key required            │
              │  Throttle: 2 req/s, 100/day    │
              │  → 202 Accepted immediately    │
              └─────────────┬──────────────────┘
                            │  AWS service proxy
                            │  (no Lambda in hot path)
                            ▼
              ┌────────────────────────────────┐
              │         SNS Topic              │
              │  rds-provisioning-{env}        │
              │  SSE with alias/aws/sns        │
              │  Fan-out hub for future        │
              │  subscribers (audit, Slack…)   │
              └─────────────┬──────────────────┘
                            │  subscription
                            │  RawMessageDelivery: true
                            ▼
              ┌────────────────────────────────┐
              │         SQS Queue              │
              │  rds-provisioning-{env}        │
              │  VisibilityTimeout: 360 s      │
              │  SSE with alias/aws/sqs        │
              │  maxReceiveCount: 3            │
              └─────────────┬──────────────────┘
                            │  BatchSize: 1
                            │  ReportBatchItemFailures
                            ▼
              ┌────────────────────────────────┐      ┌──────────────────────┐
              │      Lambda (Provisioning)     │─────▶│   Secrets Manager    │
              │  rds-provisioning-{env}        │      │  GitHub PAT          │
              │  Python 3.12 · 300 s timeout  │      │  RDS credentials     │
              │  X-Ray tracing                 │      └──────────────────────┘
              └─────────────┬──────────────────┘
                            │  PyGithub
                            ▼
              ┌────────────────────────────────┐
              │         GitHub API             │
              │  1. Create branch              │
              │     provision/<name>-<env>-ts  │
              │  2. Commit clusters/<name>.tf  │
              │  3. Open Pull Request          │
              └─────────────┬──────────────────┘
                            │  PR reviewed + merged
                            ▼
              ┌────────────────────────────────┐
              │      CircleCI Pipeline         │
              │                                │
              │  pr-checks branch:             │
              │    lint → validate → plan      │
              │                                │
              │  deploy (main):                │
              │    lint → plan ──────────────┐ │
              │               [hold-approval]│ │
              │                     ▼        │ │
              │              terraform apply ◀┘ │
              └─────────────┬──────────────────┘
                            │
                            ▼
              ┌────────────────────────────────┐
              │    AWS Aurora RDS Cluster      │
              │  storage_encrypted = true      │
              │  Master password →             │
              │    Secrets Manager (auto-gen)  │
              │  No public access              │
              └────────────────────────────────┘


  CLEANUP FLOW (daily, independent)
  ──────────────────────────────────

  EventBridge cron(0 2 * * ? *)
              │
              ▼
  ┌────────────────────────────────┐
  │   Lambda (Cleanup)             │
  │   rds-cleanup-{env}            │
  │   Paginates DescribeDBClusters │
  │   Tags: AutoProvisioned=true   │
  │   Age check vs CLUSTER_TTL_DAYS│
  └─────────────┬──────────────────┘
                │  for each expired cluster
                ▼
  ┌────────────────────────────────┐
  │  GitHub: open cleanup PR       │
  │  Deletes clusters/<name>.tf    │
  │  Branch: cleanup/<name>-ts     │
  └─────────────┬──────────────────┘
                │  PR merged by human
                ▼
  CircleCI → terraform apply → cluster destroyed
```

---

## 🤔 Why This Design

Every architectural decision has a reason. This table captures them.

| Decision | Rationale |
|---|---|
| **API GW → SNS direct integration** | The Lambda is completely out of the synchronous request path. API Gateway calls the SNS `Publish` action natively, returning 202 before any Python executes. This eliminates cold-start latency from the caller's perspective and removes a potential Lambda timeout failure from the ingestion path. |
| **SNS between API GW and SQS** | SNS is a fan-out bus. Today there is one SQS subscriber; tomorrow an audit-log Firehose or a Slack notifier can subscribe without touching existing infrastructure or adding coupling. |
| **SQS with DLQ** | Lambda invocations are retried automatically on failure. After 3 failures (`maxReceiveCount: 3`) the message is moved to the DLQ where it is retained for 14 days for manual inspection. No provisioning request is silently dropped. |
| **`BatchSize: 1`** | RDS cluster provisioning is a stateful, heavyweight, and non-idempotent operation that can take 10+ minutes. Processing multiple requests in one Lambda invocation would make partial-failure reporting ambiguous. `ReportBatchItemFailures` combined with BatchSize 1 gives clean per-message retry semantics. |
| **PR-based provisioning** | Every cluster has a code review audit trail, a `git blame` history, and a natural rollback path (delete the `.tf` file, open a PR, merge, apply). No database is ever created without at least one human approval. |
| **Secrets Manager for all credentials** | Zero secrets in environment variables, source code, SAM templates, or CI logs. The Lambda fetches secrets at runtime and caches them in the container process — a token rotation requires only a container restart (cold start), not a redeployment. |
| **SSE-SQS and SSE-SNS encryption** | `alias/aws/sqs` and `alias/aws/sns` provide server-side encryption at rest using AWS-managed keys. Messages containing cluster names and environment identifiers are encrypted before leaving the SQS/SNS service boundary. |
| **`_UnrecoverableError` sentinel** | Input validation failures (bad JSON, unknown engine) must not be retried — retrying would just burn the `maxReceiveCount` allowance and eventually pollute the DLQ with noise. The sentinel class causes the handler to drop the message cleanly instead. |
| **Partial backend config in `clusters/`** | `clusters/backend.tf` contains only `backend "s3" {}`. All backend values (bucket, key, region) are injected by CircleCI at `terraform init` time. No state bucket name or region lives in source code. |
| **`lifecycle { ignore_changes = [master_password] }`** | After initial cluster creation, the master password lives exclusively in Secrets Manager. Without this lifecycle rule, every `terraform plan` would show a diff for the password even though it has not changed, creating noise and risk. |

---

## 📁 Folder Structure

```
.
├── .circleci/
│   └── config.yml                  # Two-workflow pipeline (pr-checks + deploy).
│                                   # Defines executors, reusable commands,
│                                   # and the approval gate before terraform apply.
│
├── serverless/                     # AWS SAM application — single sam deploy.
│   ├── src/
│   │   ├── handler.py              # All Lambda logic:
│   │   │                           #   lambda_handler  — SQS → GitHub PR
│   │   │                           #   cleanup_handler — EventBridge → cleanup PR
│   │   │                           # Includes _UnrecoverableError for drop-not-retry.
│   │   └── requirements.txt        # PyGithub==2.3.0, boto3>=1.34.0
│   └── template.yaml               # SAM/CloudFormation template. Declares:
│                                   #   SQS queue + DLQ, SNS topic,
│                                   #   API GW (OpenAPI 3.0 inline, direct SNS integration),
│                                   #   API Key + Usage Plan (throttle + quota),
│                                   #   Lambda (Provisioning + Cleanup),
│                                   #   IAM roles (least-privilege, explicit ARN scoping),
│                                   #   Optional custom domain + ACM binding,
│                                   #   CloudWatch Log Groups with env-conditional retention.
│
├── terraform-modules/
│   └── rds-cluster/                # Reusable module. Called by every auto-generated file.
│       ├── main.tf                 # Aurora cluster, instances (count driven by env),
│       │                           # Secrets Manager secret + version, Security Group
│       │                           # with SG-based ingress rules, random_password.
│       ├── variables.tf            # All inputs with validation blocks.
│       │                           # engine → aurora-mysql / aurora-postgresql,
│       │                           # subnet_ids requires len ≥ 2.
│       └── outputs.tf              # cluster_endpoint, cluster_reader_endpoint,
│                                   # cluster_port, cluster_arn, secret_arn,
│                                   # security_group_id, db_subnet_group_name.
│
├── clusters/                       # Root Terraform module — DO NOT edit .tf files here
│   │                               # other than backend.tf and variables.tf.
│   │                               # Lambda auto-generates one file per cluster.
│   ├── backend.tf                  # Partial S3 backend (bucket/key/region injected by CI).
│   └── variables.tf                # vpc_id, subnet_ids, allowed_security_group_ids.
│                                   # All set via TF_ env vars in CircleCI Context.
│
├── .gitignore                      # Blocks: .env, *.tfstate, *.tfplan, .aws-sam/,
│                                   # samconfig.toml, __pycache__, .venv, secrets.*
└── README.md                       # This file.
```

---

## ✅ Prerequisites

| Tool | Minimum version | Install |
|---|---|---|
| AWS CLI | v2.x | `brew install awscli` / [official docs](https://docs.aws.amazon.com/cli/latest/userguide/install-cliv2.html) |
| AWS SAM CLI | 1.100 | `pip install aws-sam-cli` |
| Terraform | 1.6 | `brew install terraform` / [official docs](https://developer.hashicorp.com/terraform/install) |
| Python | 3.12 | `brew install python@3.12` / [python.org](https://www.python.org/downloads/) |
| Git | 2.x | Pre-installed on macOS / `apt install git` |
| GitHub PAT | — | [Settings → Developer settings → Fine-grained tokens](https://github.com/settings/tokens). Required permissions: `Contents: Read & Write`, `Pull requests: Read & Write`. |

AWS credentials must be configured before running any CLI commands:

```bash
aws configure           # or export AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY
aws sts get-caller-identity   # verify
```

---

## 🔐 Pre-Deployment: Store Secrets in Secrets Manager

All secrets must exist in Secrets Manager **before** `sam deploy`. No secret value ever touches a config file, environment variable, or CI log.

### Step 1 — GitHub Personal Access Token

```bash
aws secretsmanager create-secret \
  --name        "dev/rds-automation/github-token" \
  --description "GitHub PAT for serverless-rds-automation" \
  --secret-string "ghp_YOUR_TOKEN_HERE"
```

The command returns an ARN like:
```
arn:aws:secretsmanager:us-east-1:123456789012:secret:dev/rds-automation/github-token-AbCdEf
```

Save it. You will pass it as `GitHubTokenSecretArn` in the next step.

### Step 2 — RDS Master Credentials Placeholder

The `terraform-modules/rds-cluster` module auto-generates the RDS secret on `terraform apply`. For the **initial** SAM deployment (before any cluster exists), create a placeholder:

```bash
aws secretsmanager create-secret \
  --name        "dev/rds-automation/placeholder-credentials" \
  --description "Placeholder — replaced by terraform-modules/rds-cluster on first apply" \
  --secret-string '{"username":"dbadmin","password":"placeholder"}'
```

Save its ARN. Pass it as `RdsSecretArn` during the first `sam deploy`. Update the stack to the real ARN after the first Terraform apply.

> The Lambda function only reads these ARNs when it calls `secretsmanager:GetSecretValue` at runtime. The IAM policy is scoped to the exact two ARNs passed at deploy time.

---

## 🚀 Deployment

### Step 1 — Create the SAM artifact S3 bucket

```bash
aws s3 mb s3://your-sam-artifacts-bucket --region us-east-1
```

### Step 2 — Create the Terraform state S3 bucket

```bash
aws s3 mb s3://your-tf-state-bucket --region us-east-1

# Enable versioning — allows state recovery if the file is corrupted
aws s3api put-bucket-versioning \
  --bucket your-tf-state-bucket \
  --versioning-configuration Status=Enabled
```

### Step 3 — Build and deploy the SAM stack

```bash
cd serverless

sam build

sam deploy \
  --stack-name    rds-automation-dev \
  --s3-bucket     your-sam-artifacts-bucket \
  --region        us-east-1 \
  --capabilities  CAPABILITY_IAM CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    "Environment=dev" \
    "RdsSecretArn=arn:aws:secretsmanager:us-east-1:123456789012:secret:dev/rds-automation/placeholder-credentials-XxXx" \
    "GitHubTokenSecretArn=arn:aws:secretsmanager:us-east-1:123456789012:secret:dev/rds-automation/github-token-AbCdEf" \
    "GitHubRepo=your-org/your-iac-repo" \
    "GitHubBaseBranch=main" \
    "ClusterTtlDays=30"
```

All SAM parameters explained:

| Parameter | Required | Description |
|---|---|---|
| `Environment` | yes | `dev` or `prod`. Controls log retention (14 vs 90 days) and conditions. |
| `RdsSecretArn` | yes | ARN of the Secrets Manager secret Lambda is permitted to read. Scoped in the IAM policy — only this ARN. |
| `GitHubTokenSecretArn` | yes | ARN of the GitHub PAT secret. |
| `GitHubRepo` | yes | `owner/repo` — the IaC repository Lambda will open PRs against. |
| `GitHubBaseBranch` | no | Default: `main`. Branch Lambda creates PRs against. |
| `CustomDomainName` | no | e.g. `rds-api.example.com`. Leave blank to use the auto-generated API GW URL. |
| `AcmCertificateArn` | no | Required only when `CustomDomainName` is set. ACM cert in the same region. |
| `ClusterTtlDays` | no | Default: `30`. Cleanup Lambda opens a deletion PR for clusters older than this. |

### Step 4 — (Optional) Configure a custom domain

If you passed `CustomDomainName`, create the DNS record after deploy:

```bash
REGIONAL_DOMAIN=$(aws cloudformation describe-stacks \
  --stack-name rds-automation-dev \
  --query "Stacks[0].Outputs[?OutputKey=='RegionalDomainName'].OutputValue" \
  --output text)

echo "CNAME: rds-api.example.com → ${REGIONAL_DOMAIN}"
```

| DNS provider | Record type | Target |
|---|---|---|
| Route 53 | Alias (`A`) | `$REGIONAL_DOMAIN` |
| All other providers | CNAME | `$REGIONAL_DOMAIN` |

> The ACM certificate must be in `ISSUED` status before deploying. Request it with
> `aws acm request-certificate --domain-name rds-api.example.com --validation-method DNS`
> and complete DNS validation first.

### Step 5 — Retrieve the API key value

```bash
KEY_ID=$(aws cloudformation describe-stacks \
  --stack-name rds-automation-dev \
  --query "Stacks[0].Outputs[?OutputKey=='ApiKeyId'].OutputValue" \
  --output text)

aws apigateway get-api-key \
  --api-key  "$KEY_ID" \
  --include-value \
  --query    "value" \
  --output   text
```

Store this value in your password manager. Do not commit it or put it in a CI log. You will need it to call the API and as a CircleCI environment variable if you automate calls from CI.

### Step 6 — Configure CircleCI

In your CircleCI project settings (**Organization Settings → Contexts**), create two contexts:

**Context: `aws-readonly`** — used on PR branches (plan, no write access)

| Variable | Value |
|---|---|
| `AWS_ACCESS_KEY_ID` | IAM user/role with Terraform read-only permissions |
| `AWS_SECRET_ACCESS_KEY` | — |
| `AWS_DEFAULT_REGION` | `us-east-1` |
| `TF_STATE_BUCKET` | `your-tf-state-bucket` |
| `TF_VPC_ID` | `vpc-xxxxxxxxxxxxxxxxx` |
| `TF_SUBNET_IDS` | `["subnet-aaa","subnet-bbb"]` (JSON array) |
| `TF_ALLOWED_SGS` | `["sg-0123456789abcdef0"]` (JSON array) |

**Context: `aws-deploy`** — used on `main` only; includes all `aws-readonly` vars plus:

| Variable | Value |
|---|---|
| `SAM_STACK_NAME` | `rds-automation-dev` |
| `SAM_ARTIFACT_BUCKET` | `your-sam-artifacts-bucket` |
| `DEPLOY_ENVIRONMENT` | `dev` |
| `RDS_SECRET_ARN` | ARN from Step 2 |
| `GITHUB_TOKEN_SECRET_ARN` | ARN from Step 1 |
| `GITHUB_REPO` | `your-org/your-iac-repo` |
| `GITHUB_BASE_BRANCH` | `main` |

> `TF_SUBNET_IDS` and `TF_ALLOWED_SGS` must be JSON-encoded list strings — the Terraform
> CLI receives them via `-var` flags which accept HCL list syntax: `["a","b"]`.

---

## 📬 Usage

### Send a provisioning request

```bash
API_URL="https://<api-id>.execute-api.us-east-1.amazonaws.com/dev/provision"
API_KEY="your-api-key-from-step-5"

curl -s -X POST "$API_URL" \
  -H "Content-Type: application/json" \
  -H "x-api-key: $API_KEY" \
  -d '{
    "cluster_name": "payments-db",
    "environment": "dev",
    "engine":      "postgres"
  }'
```

**Expected response — 202 Accepted:**
```json
{
  "message": "Provisioning request accepted",
  "requestId": "abc12345-6789-def0-1234-abcdef012345"
}
```

The response arrives in milliseconds. All provisioning work is asynchronous from this point.

### Full request payload reference

| Field | Required | Type | Allowed values | Default |
|---|---|---|---|---|
| `cluster_name` | yes | string | Lowercase, alphanumeric + hyphens. Sanitised automatically: spaces and special chars become `-`, consecutive dashes collapsed. | — |
| `environment` | yes | string | `dev` \| `prod` | — |
| `engine` | yes | string | `mysql` \| `postgres` | — |
| `database_name` | no | string | Any valid identifier | Same as `cluster_name` |
| `master_username` | no | string | Any valid DB username | `dbadmin` |

### What happens after the 202

1. API Gateway publishes the request body to SNS (`rds-provisioning-{env}`).
2. SNS forwards the message to SQS (`rds-provisioning-{env}`), encrypted at rest.
3. Lambda (`rds-provisioning-{env}`) is triggered with `BatchSize: 1`.
4. Lambda validates the payload, fetches the GitHub PAT from Secrets Manager (cached per container), and renders a `.tf` file.
5. Lambda calls the GitHub API to: create branch `provision/<cluster>-<env>-<timestamp>`, commit `clusters/<cluster_name>.tf`, and open a PR titled `[RDS] Provision <cluster> (<env> / <engine>)`.
6. The PR body includes a pre-merge checklist (confirm VPC/subnets, review Terraform plan, team approval).
7. A reviewer approves and merges the PR.
8. CircleCI detects the change in `clusters/` and runs the `deploy` workflow.
9. `terraform-plan` runs against the real S3 state with `aws-deploy` credentials. The plan artifact is persisted to the workspace.
10. The `hold-for-approval` manual gate requires a human to review the plan output in the CircleCI UI and click **Approve**.
11. `terraform-apply` picks up the exact plan artifact from step 9 and runs `terraform apply -auto-approve tfplan`.
12. Aurora cluster is live. Master credentials (username, password, host, port, dbname) are written to `{env}/{cluster_identifier}/db-credentials` in Secrets Manager.

---

## 🔒 Security Model

### Secrets flow

```
GitHub PAT
  └─▶ aws secretsmanager create-secret (one-time, by operator)
        └─▶ Lambda reads GetSecretValue at runtime
              └─▶ Cached in _secret_cache (per container, never logged)
                    └─▶ Used to authenticate PyGithub client
                          └─▶ Never written to env vars, logs, or disk

RDS master password
  └─▶ random_password.master (Terraform — 32 chars, special allowed)
        └─▶ aws_secretsmanager_secret_version (written after cluster endpoint resolves)
              └─▶ Secret JSON: {username, password, engine, host, port, dbname}
                    └─▶ Applications call GetSecretValue — no password in env vars

API key
  └─▶ AWS::ApiGateway::ApiKey (managed by API Gateway — never in any file)
        └─▶ Caller must send x-api-key header
              └─▶ Checked by Usage Plan before request reaches SNS integration
```

### IAM — least-privilege summary

| Identity | Exact permissions |
|---|---|
| **`apigw-sns-publish-{env}`** | `sns:Publish` on `rds-provisioning-{env}` ARN only |
| **`rds-provisioning-fn-{env}`** | `sqs:ReceiveMessage`, `sqs:DeleteMessage`, `sqs:GetQueueAttributes`, `sqs:ChangeMessageVisibility` on the provisioning queue ARN; `secretsmanager:GetSecretValue` on the two explicit secret ARNs passed at deploy time; `logs:CreateLogGroup/Stream/PutLogEvents` on the function's log group ARN; `xray:PutTraceSegments/PutTelemetryRecords` |
| **`rds-cleanup-fn-{env}`** | `rds:DescribeDBClusters`, `rds:ListTagsForResource` on `*`; `secretsmanager:GetSecretValue` on the GitHub token ARN; CloudWatch Logs + X-Ray (same as above) |
| **CircleCI `aws-readonly`** | Terraform state read (`s3:GetObject`, `s3:ListBucket`), `rds:Describe*`, `ec2:Describe*` — no write operations |
| **CircleCI `aws-deploy`** | Terraform apply + SAM deploy — scoped to CloudFormation, RDS, EC2 SGs, Secrets Manager, Lambda, IAM roles in this stack |

### Network isolation

- Lambda **does not run inside a VPC**. It communicates only with AWS APIs (SQS, Secrets Manager, X-Ray) and the GitHub API over the public internet. All traffic goes through AWS-managed TLS endpoints.
- RDS clusters are placed in private subnets. The security group accepts inbound DB traffic **only from security group IDs** listed in `var.allowed_security_group_ids`. No CIDR-based `0.0.0.0/0` rules. No public accessibility (`publicly_accessible = false`).
- An explicit egress deny rule on the RDS security group blocks all outbound traffic from the cluster nodes.

### Encryption at rest

| Resource | Encryption |
|---|---|
| SQS main queue | SSE-SQS (`SqsManagedSseEnabled: true`) — `alias/aws/sqs` |
| SQS DLQ | SSE-SQS (`SqsManagedSseEnabled: true`) — `alias/aws/sqs` |
| SNS topic | `KmsMasterKeyId: alias/aws/sns` |
| RDS Aurora cluster | `storage_encrypted = true` (AWS-managed key) |
| All Secrets Manager secrets | AWS-managed KMS key (default) |
| Terraform state (S3) | Enable SSE-S3 or SSE-KMS on the state bucket (not managed by this project — operator responsibility) |

---

## 🌍 Environment Differences: `dev` vs `prod`

| Setting | `dev` | `prod` |
|---|---|---|
| RDS instance class | `db.t3.medium` | `db.r6g.large` (Graviton2, memory-optimised) |
| Instance count | 1 (writer only) | 2 (writer + reader) |
| Deletion protection | off | **on** — must be disabled before a destroy |
| `skip_final_snapshot` | `true` | `false` — snapshot named `<cluster>-<env>-final` |
| Backup retention | 1 day | 7 days (PITR) |
| Performance Insights | off | on |
| Secrets Manager recovery window | 0 days (immediate delete) | 30 days |
| CloudWatch log retention (Lambda) | 14 days | 90 days |
| Aurora MySQL log exports | audit, error, general, slowquery | same |
| Aurora PostgreSQL log exports | postgresql | same |

To deploy the prod stack, re-run `sam deploy` with `Environment=prod` as a separate CloudFormation stack (e.g. `--stack-name rds-automation-prod`).

---

## 🧹 Auto-Cleanup

### How it works

The `rds-cleanup-{env}` Lambda runs on an EventBridge schedule every day at **02:00 UTC**. It paginates through all RDS clusters in the account and finds those tagged `AutoProvisioned=true` whose `RequestedAt` tag is older than `CLUSTER_TTL_DAYS` days. For each expired cluster it opens a GitHub PR that **deletes** `clusters/<name>.tf`. Merging that PR triggers CircleCI → `terraform apply` → cluster destroyed.

The Lambda never destroys clusters directly — it only opens PRs. Human review is always required before destruction.

### Configuring the TTL

Set `ClusterTtlDays` at deploy time:

```bash
# 7 days for dev, 90 days for prod
--parameter-overrides "ClusterTtlDays=7"
```

To update a running stack without a full redeploy:

```bash
aws cloudformation update-stack \
  --stack-name rds-automation-dev \
  --use-previous-template \
  --parameters \
    ParameterKey=ClusterTtlDays,ParameterValue=14 \
    ParameterKey=Environment,UsePreviousValue=true \
    ParameterKey=RdsSecretArn,UsePreviousValue=true \
    ParameterKey=GitHubTokenSecretArn,UsePreviousValue=true \
    ParameterKey=GitHubRepo,UsePreviousValue=true \
    ParameterKey=GitHubBaseBranch,UsePreviousValue=true
```

### Prod cluster warning

Prod clusters have `deletion_protection = true`. Before merging a cleanup PR for a prod cluster, you must first:

1. Open a separate PR that sets `deletion_protection = false` on the target cluster.
2. Merge it and wait for `terraform apply` to complete.
3. Then merge the cleanup PR.

Attempting to destroy a cluster with deletion protection enabled will cause `terraform apply` to fail and the CircleCI job to error.

---

## ⚙️ CI/CD Pipeline

The CircleCI pipeline is defined entirely in `.circleci/config.yml`. It has two workflows.

### Workflow: `pr-checks`

Triggers on every push to any branch **except `main`**.

```
push to PR branch
       │
       ├──▶ lint-and-test        (python-ci executor — no AWS creds)
       │      flake8 · pytest · coverage XML
       │
       ├──▶ terraform-validate   (tf-runner executor — no AWS creds)
       │      terraform init -backend=false
       │      terraform validate (terraform-modules/rds-cluster)
       │
       └──▶ terraform-plan       (tf-runner executor — aws-readonly context)
              terraform init (S3 backend)
              terraform plan -var vpc_id=... -var subnet_ids=... -out=tfplan
              [halts gracefully if clusters/ unchanged]
```

All three jobs run in parallel. Reviewers can see the exact Terraform plan output before approving the PR.

### Workflow: `deploy`

Triggers on every push to `main` (i.e. PR merge). Two independent paths run after the shared gate.

```
merge to main
       │
       └──▶ lint-and-test (shared gate — must pass before anything deploys)
                  │
       ┌──────────┴──────────────────────────────────────┐
       │                                                  │
       ▼                                                  ▼
  deploy-sam                                       terraform-plan
  (aws-deploy context)                             (aws-deploy context)
  sam build + sam deploy                           terraform init + plan
  [halts if serverless/ unchanged]                 persists tfplan to workspace
                                                   [halts if clusters/ unchanged]
                                                          │
                                                          ▼
                                                   hold-for-approval
                                                   [MANUAL — human reviews plan
                                                    in CircleCI UI and clicks Approve]
                                                          │
                                                          ▼
                                                   terraform-apply
                                                   (aws-deploy context)
                                                   attaches workspace
                                                   terraform apply -auto-approve tfplan
```

The `hold-for-approval` job is a CircleCI `type: approval` job. It blocks `terraform-apply` until a team member reviews the plan output printed by `terraform-plan` and explicitly approves in the CircleCI UI. This is the last human gate before an Aurora cluster is created or destroyed in AWS.

The `deploy-sam` and `terraform-plan → hold → apply` paths are completely independent and can run in parallel on commits that change both `serverless/` and `clusters/` simultaneously.

### Path-triggered jobs

Both `deploy-sam` and `terraform-apply` check whether their respective source paths (`serverless/` and `clusters/`) contain changes compared to the parent commit. If no files changed in that path, `circleci-agent step halt` exits the job with code 0 — the job shows as "Halted" (green) in the UI without running any AWS operations.

---

## 🧪 Unit Tests

Tests live in `serverless/tests/` and use `pytest`.

### Run locally

```bash
cd serverless
pip install -r src/requirements.txt
pip install pytest pytest-cov flake8

# Lint
flake8 src/ --max-line-length=120

# Tests with coverage
pytest tests/ -v \
  --cov=src \
  --cov-report=term-missing \
  --cov-report=xml:test-results/coverage.xml
```

### What is covered

| Area | What the tests verify |
|---|---|
| `_parse_payload` | Valid payloads pass; missing required fields raise `_UnrecoverableError`; invalid `environment` / `engine` values raise `_UnrecoverableError`; `cluster_name` is sanitised (spaces → dashes, consecutive dashes collapsed, trailing dashes stripped). |
| `_get_secret` | Happy path (plain string); JSON envelope with `token` key; JSON envelope with `github_token` key; `ResourceNotFoundException` → `_UnrecoverableError`; throttling errors → retryable `RuntimeError`; empty `SecretString` → `_UnrecoverableError`; cache hit avoids second Secrets Manager call. |
| `_render_terraform_module` | Generated `.tf` content contains the correct module source path, cluster identifier, engine, and tag values. |
| `lambda_handler` | Successful record → returns empty `batchItemFailures`; `_UnrecoverableError` → message dropped (not in failures list); transient exception → message ID returned in `batchItemFailures`; multiple records processed independently. |
| `cleanup_handler` | Expired clusters (age > TTL) → PR opened; non-expired clusters → skipped; clusters without `AutoProvisioned=true` tag → ignored; pagination handled correctly. |

---

## 📊 Monitoring

| Signal | Where | Why it matters |
|---|---|---|
| `ApproximateNumberOfMessagesVisible` on **DLQ** | CloudWatch → SQS → `rds-provisioning-dlq-{env}` | Any value > 0 means a provisioning request failed all 3 retries. Messages are held for 14 days. Investigate before they expire. |
| Lambda `Errors` metric | CloudWatch → Lambda → `rds-provisioning-{env}` | Transient errors (GitHub API rate limit, Secrets Manager throttle) that SQS will retry. Alert on sustained non-zero. |
| Lambda `Duration` metric | CloudWatch → Lambda → `rds-provisioning-{env}` | Timeout is 300 s. A p99 Duration near 300 s indicates a GitHub API or network issue. |
| Lambda `Errors` metric | CloudWatch → Lambda → `rds-cleanup-{env}` | Silent cleanup failures mean expired clusters stay running. |
| CloudWatch Log Insights | `/aws/lambda/rds-provisioning-{env}` | All log entries are structured JSON. Query: `filter action="pr_created"` to see provisioning success rate. |
| API GW `4XXError` metric | CloudWatch → API Gateway → `rds-automation-{env}` | High 403 rate → invalid API key in use. High 400 rate → caller sending bad payloads. |
| S3 state bucket versioning | S3 console → your-tf-state-bucket | Always-on versioning is mandatory. If state is corrupted, you can roll back to a previous version. |
| CircleCI `hold-for-approval` TTL | CircleCI project settings | CircleCI approval jobs expire after a configurable timeout. Set a Slack notification so approvers don't miss them. |

---

## 💥 Teardown

To destroy everything safely, work in reverse deployment order.

### Step 1 — Delete all provisioned RDS clusters

For each `clusters/<name>.tf` file:

1. If environment is `prod`, first open a PR removing `deletion_protection = true` from the module call, merge it, and wait for `terraform apply` to complete.
2. Open a PR deleting `clusters/<name>.tf`.
3. Merge the PR and approve the `hold-for-approval` gate in CircleCI.
4. Confirm `terraform apply` completes with no errors.

### Step 2 — Destroy the SAM stack

```bash
aws cloudformation delete-stack \
  --stack-name rds-automation-dev \
  --region     us-east-1

# Wait for completion
aws cloudformation wait stack-delete-complete \
  --stack-name rds-automation-dev \
  --region     us-east-1
```

This deletes: API Gateway, SNS, SQS (main + DLQ), both Lambda functions, IAM roles, log groups, usage plan, and API key.

### Step 3 — Delete Secrets Manager secrets

```bash
# GitHub PAT secret
aws secretsmanager delete-secret \
  --secret-id   "dev/rds-automation/github-token" \
  --force-delete-without-recovery

# Placeholder credential secret (if still present)
aws secretsmanager delete-secret \
  --secret-id   "dev/rds-automation/placeholder-credentials" \
  --force-delete-without-recovery
```

RDS credential secrets created by the Terraform module use `recovery_window_in_days = 0` in dev — they are deleted immediately when `terraform destroy` runs.

### Step 4 — Delete S3 buckets

```bash
# Empty and delete SAM artifacts bucket
aws s3 rm s3://your-sam-artifacts-bucket --recursive
aws s3 rb s3://your-sam-artifacts-bucket

# Empty and delete Terraform state bucket
# (versioned — must delete all versions first)
aws s3api delete-objects \
  --bucket your-tf-state-bucket \
  --delete "$(aws s3api list-object-versions \
    --bucket your-tf-state-bucket \
    --query '{Objects: Versions[].{Key:Key,VersionId:VersionId}}')"
aws s3 rb s3://your-tf-state-bucket
```

---

## License

MIT
