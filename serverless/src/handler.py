"""
Lambda handler — Serverless RDS Cluster Automation

Two entry points:
  lambda_handler   — SQS trigger: parse request → open provisioning PR
  cleanup_handler  — EventBridge schedule: find expired clusters → open cleanup PRs
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import boto3
from botocore.exceptions import ClientError
from github import Github, GithubException

# ---------------------------------------------------------------------------
# Logging — emit one JSON line per log call so CloudWatch Insights can query it
# ---------------------------------------------------------------------------
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Module-level constants — resolved once per Lambda container lifetime
# ---------------------------------------------------------------------------
_GITHUB_TOKEN_SECRET_ARN: str = os.environ["GITHUB_TOKEN_SECRET_ARN"]
_GITHUB_REPO: str = os.environ["GITHUB_REPO"]           # "org/repo-name"
_GITHUB_BASE_BRANCH: str = os.environ.get("GITHUB_BASE_BRANCH", "main")

# Simple in-process secret cache.  Lambda containers are reused across warm
# invocations, so we avoid a Secrets Manager call on every message.
# The cache is intentionally never invalidated within a container lifetime;
# if a token is rotated, a container restart (cold start) picks up the new value.
_secret_cache: dict[str, str] = {}

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, list[dict]]:
    """
    SQS trigger entry point.

    Returns a ``batchItemFailures`` list so the SQS event source mapping can
    requeue only failed records (``ReportBatchItemFailures`` mode set in SAM).
    Successful records are deleted by the runtime automatically.
    """
    batch_item_failures: list[dict[str, str]] = []

    for record in event.get("Records", []):
        message_id: str = record["messageId"]
        logger.info(json.dumps({"action": "received", "messageId": message_id}))

        try:
            _process_record(record)
            logger.info(json.dumps({"action": "success", "messageId": message_id}))

        except _UnrecoverableError as exc:
            # Validation / logic errors — retrying will never help.
            # Log and swallow so the message is NOT re-queued (avoids DLQ noise).
            logger.error(
                json.dumps({
                    "action": "dropped",
                    "messageId": message_id,
                    "reason": str(exc),
                })
            )

        except Exception as exc:  # noqa: BLE001
            # Transient errors (network, API rate-limit) — let SQS retry.
            logger.exception(
                json.dumps({
                    "action": "failed",
                    "messageId": message_id,
                    "error": str(exc),
                })
            )
            batch_item_failures.append({"itemIdentifier": message_id})

    return {"batchItemFailures": batch_item_failures}


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _process_record(record: dict[str, Any]) -> None:
    """End-to-end processing for one SQS record."""
    payload = _parse_payload(record["body"])
    logger.info(json.dumps({"action": "parsed", "payload": payload}))

    github_token = _get_secret(_GITHUB_TOKEN_SECRET_ARN)

    tf_content = _render_terraform_module(payload)

    pr_url = _create_github_pr(
        token=github_token,
        payload=payload,
        tf_content=tf_content,
    )
    logger.info(json.dumps({"action": "pr_created", "url": pr_url}))


# ---------------------------------------------------------------------------
# Step 1 — Parse & validate the SQS payload
# ---------------------------------------------------------------------------

_VALID_ENVIRONMENTS = frozenset({"dev", "prod"})
_VALID_ENGINES = frozenset({"mysql", "postgres"})
_SAFE_NAME_RE = re.compile(r"[^a-z0-9-]")


def _parse_payload(raw_body: str) -> dict[str, str]:
    """
    Deserialise and validate the JSON message body.

    Required keys: cluster_name, environment, engine
    Optional keys: database_name, master_username

    Raises ``_UnrecoverableError`` on schema / value failures so the message
    is dropped rather than endlessly retried.
    """
    try:
        data: dict = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise _UnrecoverableError(f"Body is not valid JSON: {exc}") from exc

    _require_fields(data, {"cluster_name", "environment", "engine"})

    environment: str = data["environment"]
    if environment not in _VALID_ENVIRONMENTS:
        raise _UnrecoverableError(
            f"environment must be one of {sorted(_VALID_ENVIRONMENTS)}, got '{environment}'"
        )

    engine: str = data["engine"]
    if engine not in _VALID_ENGINES:
        raise _UnrecoverableError(
            f"engine must be one of {sorted(_VALID_ENGINES)}, got '{engine}'"
        )

    # Sanitise cluster_name for use in branch names, file paths, and TF identifiers.
    # Lowercase, replace anything outside [a-z0-9-] with a dash, collapse runs.
    raw_name: str = data["cluster_name"].lower()
    cluster_name = _SAFE_NAME_RE.sub("-", raw_name).strip("-")
    cluster_name = re.sub(r"-{2,}", "-", cluster_name)   # collapse consecutive dashes
    if not cluster_name:
        raise _UnrecoverableError("cluster_name is empty after sanitisation")

    return {
        "cluster_name": cluster_name,
        "environment": environment,
        "engine": engine,
        "database_name": data.get("database_name", cluster_name),
        "master_username": data.get("master_username", "dbadmin"),
        "requested_at": datetime.now(timezone.utc).isoformat(),
    }


def _require_fields(data: dict, fields: set[str]) -> None:
    missing = fields - data.keys()
    if missing:
        raise _UnrecoverableError(f"Missing required fields: {sorted(missing)}")


# ---------------------------------------------------------------------------
# Step 2 — Fetch secrets from Secrets Manager
# ---------------------------------------------------------------------------

def _get_secret(secret_arn: str) -> str:
    """
    Return the secret string for *secret_arn*.

    Handles both plain-string secrets (GitHub PAT stored as raw text) and
    JSON-envelope secrets (e.g. ``{"token": "ghp_..."}``).
    Results are cached for the container lifetime.
    """
    if secret_arn in _secret_cache:
        return _secret_cache[secret_arn]

    client = boto3.client("secretsmanager")
    try:
        response = client.get_secret_value(SecretId=secret_arn)
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        # ResourceNotFoundException / AccessDeniedException are unrecoverable
        if code in ("ResourceNotFoundException", "AccessDeniedException",
                    "InvalidParameterException"):
            raise _UnrecoverableError(
                f"Cannot retrieve secret '{secret_arn}': {code}"
            ) from exc
        # Everything else (throttling, transient) is retryable
        raise RuntimeError(f"Secrets Manager error for '{secret_arn}': {code}") from exc

    raw: str = response.get("SecretString") or ""
    if not raw:
        raise _UnrecoverableError(f"Secret '{secret_arn}' has no SecretString value")

    # Support both plain PAT strings and JSON envelopes
    try:
        parsed = json.loads(raw)
        token = (
            parsed.get("token")
            or parsed.get("github_token")
            or parsed.get("value")
        )
        if not token:
            raise _UnrecoverableError(
                f"Secret '{secret_arn}' is JSON but contains no 'token' / 'github_token' key"
            )
        value = token
    except json.JSONDecodeError:
        value = raw  # plain string token

    _secret_cache[secret_arn] = value
    return value


# ---------------------------------------------------------------------------
# Step 3 — Render the Terraform module call
# ---------------------------------------------------------------------------

def _render_terraform_module(payload: dict[str, str]) -> str:
    """
    Return a complete .tf file that invokes terraform-modules/rds-cluster.

    The generated file lives at clusters/<cluster_name>.tf in the IaC repo.
    It references var.vpc_id / var.subnet_ids which callers must define in
    the clusters/ root module.
    """
    cluster_name = payload["cluster_name"]
    environment = payload["environment"]
    engine = payload["engine"]
    database_name = payload["database_name"]
    master_username = payload["master_username"]
    requested_at = payload["requested_at"]

    # Terraform identifiers cannot contain dashes
    tf_id = cluster_name.replace("-", "_")

    return f"""\
# =============================================================================
# Auto-generated by serverless-rds-automation
# Requested at : {requested_at}
#
# DO NOT EDIT MANUALLY — re-provisioning will overwrite this file.
# To change cluster settings open a manual PR and update this file directly.
# =============================================================================

module "rds_{tf_id}" {{
  source = "../terraform-modules/rds-cluster"

  cluster_identifier = "{cluster_name}"
  environment        = "{environment}"
  engine             = "{engine}"
  database_name      = "{database_name}"
  master_username    = "{master_username}"

  # These variables must be defined in clusters/variables.tf
  vpc_id     = var.vpc_id
  subnet_ids = var.subnet_ids

  # Allow the Lambda execution SG inbound access to this cluster.
  # The SG ID is exported by the SAM template (LambdaSecurityGroupId).
  allowed_security_group_ids = var.allowed_security_group_ids

  tags = {{
    AutoProvisioned = "true"
    RequestedAt     = "{requested_at}"
  }}
}}

output "{tf_id}_cluster_endpoint" {{
  description = "Writer endpoint for the {cluster_name} cluster"
  value       = module.rds_{tf_id}.cluster_endpoint
}}

output "{tf_id}_reader_endpoint" {{
  description = "Reader endpoint for the {cluster_name} cluster"
  value       = module.rds_{tf_id}.cluster_reader_endpoint
}}

output "{tf_id}_secret_arn" {{
  description = "Secrets Manager ARN holding {cluster_name} DB credentials"
  value       = module.rds_{tf_id}.secret_arn
  sensitive   = true
}}
"""


# ---------------------------------------------------------------------------
# Step 4 — GitHub operations: branch → commit → PR
# ---------------------------------------------------------------------------

def _create_github_pr(
    token: str,
    payload: dict[str, str],
    tf_content: str,
) -> str:
    """
    1. Resolve the tip SHA of the base branch.
    2. Create a new branch  provision/<cluster>-<env>-<timestamp>.
    3. Commit clusters/<cluster_name>.tf on that branch.
    4. Open a pull request against the base branch.

    Returns the HTML URL of the newly created PR.
    """
    gh = Github(token)

    try:
        repo = gh.get_repo(_GITHUB_REPO)
    except GithubException as exc:
        # 401 / 403 / 404 are configuration errors — unrecoverable
        if exc.status in (401, 403, 404):
            raise _UnrecoverableError(
                f"Cannot access repo '{_GITHUB_REPO}' (HTTP {exc.status}): {exc.data}"
            ) from exc
        raise RuntimeError(f"GitHub error accessing '{_GITHUB_REPO}': {exc.data}") from exc

    cluster_name = payload["cluster_name"]
    environment = payload["environment"]
    engine = payload["engine"]
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    branch_name = f"provision/{cluster_name}-{environment}-{timestamp}"
    file_path = f"clusters/{cluster_name}.tf"

    # ── 1. Resolve base branch tip ──────────────────────────────────────────
    try:
        base_ref = repo.get_branch(_GITHUB_BASE_BRANCH)
    except GithubException as exc:
        raise _UnrecoverableError(
            f"Base branch '{_GITHUB_BASE_BRANCH}' not found in '{_GITHUB_REPO}'"
        ) from exc

    base_sha: str = base_ref.commit.sha
    logger.info(json.dumps({
        "action": "base_resolved",
        "branch": _GITHUB_BASE_BRANCH,
        "sha": base_sha[:8],
    }))

    # ── 2. Create feature branch ─────────────────────────────────────────────
    try:
        repo.create_git_ref(ref=f"refs/heads/{branch_name}", sha=base_sha)
        logger.info(json.dumps({"action": "branch_created", "branch": branch_name}))
    except GithubException as exc:
        # 422 means the ref already exists (clock skew / duplicate request)
        if exc.status == 422:
            raise _UnrecoverableError(
                f"Branch '{branch_name}' already exists — duplicate request?"
            ) from exc
        raise RuntimeError(f"Failed to create branch '{branch_name}': {exc.data}") from exc

    # ── 3. Commit the generated Terraform file ───────────────────────────────
    commit_message = (
        f"feat(rds): provision {cluster_name} cluster\n\n"
        f"Environment : {environment}\n"
        f"Engine      : {engine}\n"
        f"Requested at: {payload['requested_at']}\n"
        f"Auto-generated by serverless-rds-automation"
    )
    try:
        repo.create_file(
            path=file_path,
            message=commit_message,
            content=tf_content,
            branch=branch_name,
        )
        logger.info(json.dumps({"action": "file_committed", "path": file_path}))
    except GithubException as exc:
        raise RuntimeError(
            f"Failed to commit '{file_path}' on '{branch_name}': {exc.data}"
        ) from exc

    # ── 4. Open the pull request ─────────────────────────────────────────────
    try:
        pr = repo.create_pull(
            title=f"[RDS] Provision `{cluster_name}` ({environment} / {engine})",
            body=_render_pr_body(payload, file_path),
            head=branch_name,
            base=_GITHUB_BASE_BRANCH,
        )
        logger.info(json.dumps({
            "action": "pr_opened",
            "number": pr.number,
            "url": pr.html_url,
        }))
        return pr.html_url

    except GithubException as exc:
        raise RuntimeError(f"Failed to create PR from '{branch_name}': {exc.data}") from exc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _render_pr_body(payload: dict[str, str], file_path: str) -> str:
    """Return the Markdown body for the pull request."""
    return f"""\
## RDS Cluster Provisioning Request

| Field | Value |
|---|---|
| Cluster name | `{payload['cluster_name']}` |
| Environment | `{payload['environment']}` |
| Engine | `{payload['engine']}` |
| Database name | `{payload['database_name']}` |
| Master username | `{payload['master_username']}` |
| Requested at | `{payload['requested_at']}` |

## What this PR does

Creates `{file_path}` which calls the `terraform-modules/rds-cluster` module
with the parameters above.

The DB password is **never** stored in this file — it is generated by
`random_password` and stored in AWS Secrets Manager at apply time.

## Pre-merge checklist

- [ ] Confirm `var.vpc_id` and `var.subnet_ids` are correct for `{payload['environment']}`
- [ ] Verify the Terraform plan in CI produces no unexpected changes
- [ ] Confirm instance class and Multi-AZ settings match expectations
- [ ] At least one infrastructure team approval

---
*Auto-generated by [serverless-rds-automation](../serverless/src/handler.py)*
"""


class _UnrecoverableError(Exception):
    """
    Raised when a failure cannot be resolved by retrying the same message
    (e.g. invalid input, missing configuration, permanent auth failure).

    The handler catches this and drops the message instead of returning it
    to the SQS queue, preventing infinite retry loops and DLQ pollution.
    """


# =============================================================================
# CLEANUP HANDLER — EventBridge scheduled trigger (daily at 02:00 UTC)
# =============================================================================

def cleanup_handler(event: dict[str, Any], context: Any) -> None:
    """
    Scan all RDS clusters tagged AutoProvisioned=true.
    Any cluster whose RequestedAt tag is older than CLUSTER_TTL_DAYS opens
    a GitHub PR that deletes its clusters/<name>.tf file.

    Merging the PR triggers CircleCI → terraform apply → cluster destroyed.
    Humans retain final approval; this Lambda only opens the PR.
    """
    ttl_days = int(os.environ.get("CLUSTER_TTL_DAYS", "30"))
    logger.info(json.dumps({"action": "cleanup_start", "ttl_days": ttl_days}))

    github_token = _get_secret(_GITHUB_TOKEN_SECRET_ARN)
    expired = _find_expired_clusters(ttl_days)

    logger.info(json.dumps({"action": "cleanup_scan", "expired_count": len(expired)}))

    for cluster in expired:
        try:
            pr_url = _open_cleanup_pr(github_token, cluster)
            logger.info(json.dumps({
                "action": "cleanup_pr_opened",
                "cluster": cluster["identifier"],
                "age_days": cluster["age_days"],
                "pr_url": pr_url,
            }))
        except _UnrecoverableError as exc:
            logger.warning(json.dumps({
                "action": "cleanup_skipped",
                "cluster": cluster["identifier"],
                "reason": str(exc),
            }))
        except Exception as exc:  # noqa: BLE001
            logger.exception(json.dumps({
                "action": "cleanup_pr_failed",
                "cluster": cluster["identifier"],
                "error": str(exc),
            }))


def _find_expired_clusters(ttl_days: int) -> list[dict[str, Any]]:
    """
    Return RDS clusters tagged AutoProvisioned=true whose RequestedAt tag
    is older than ttl_days. Uses pagination to handle large accounts.
    """
    rds = boto3.client("rds")
    cutoff = datetime.now(timezone.utc) - timedelta(days=ttl_days)
    expired: list[dict[str, Any]] = []

    paginator = rds.get_paginator("describe_db_clusters")
    for page in paginator.paginate():
        for cluster in page["DBClusters"]:
            try:
                tags_resp = rds.list_tags_for_resource(
                    ResourceName=cluster["DBClusterArn"]
                )
            except Exception:  # noqa: BLE001
                continue

            tags = {t["Key"]: t["Value"] for t in tags_resp.get("TagList", [])}

            if tags.get("AutoProvisioned") != "true":
                continue

            requested_at_str = tags.get("RequestedAt", "")
            if not requested_at_str:
                continue

            try:
                requested_at = datetime.fromisoformat(requested_at_str)
            except ValueError:
                continue

            if requested_at >= cutoff:
                continue

            env = tags.get("Environment", "")
            identifier = cluster["DBClusterIdentifier"]
            # Cluster identifier format: "<cluster_name>-<environment>"
            # Strip the suffix to derive the .tf file name.
            base_name = identifier.removesuffix(f"-{env}") if env else identifier

            expired.append({
                "identifier": identifier,
                "base_name": base_name,
                "environment": env,
                "requested_at": requested_at_str,
                "age_days": (datetime.now(timezone.utc) - requested_at).days,
            })

    return expired


def _open_cleanup_pr(token: str, cluster: dict[str, Any]) -> str:
    """
    Create a branch and open a PR that deletes clusters/<base_name>.tf.
    Returns the PR HTML URL.
    """
    gh = Github(token)

    try:
        repo = gh.get_repo(_GITHUB_REPO)
    except GithubException as exc:
        if exc.status in (401, 403, 404):
            raise _UnrecoverableError(
                f"Cannot access repo '{_GITHUB_REPO}' (HTTP {exc.status})"
            ) from exc
        raise RuntimeError(f"GitHub error: {exc.data}") from exc

    base_name = cluster["base_name"]
    file_path = f"clusters/{base_name}.tf"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    branch_name = f"cleanup/{base_name}-{timestamp}"

    # Verify the file exists before attempting deletion
    try:
        file_obj = repo.get_contents(file_path, ref=_GITHUB_BASE_BRANCH)
    except GithubException as exc:
        if exc.status == 404:
            raise _UnrecoverableError(
                f"{file_path} not found — cluster may already have been cleaned up"
            ) from exc
        raise RuntimeError(f"Failed to read '{file_path}': {exc.data}") from exc

    # Create the cleanup branch
    base_sha = repo.get_branch(_GITHUB_BASE_BRANCH).commit.sha
    try:
        repo.create_git_ref(ref=f"refs/heads/{branch_name}", sha=base_sha)
    except GithubException as exc:
        if exc.status == 422:
            raise _UnrecoverableError(f"Branch '{branch_name}' already exists") from exc
        raise RuntimeError(f"Failed to create branch: {exc.data}") from exc

    # Delete the Terraform file on the cleanup branch
    repo.delete_file(
        path=file_path,
        message=(
            f"chore(cleanup): remove expired cluster {base_name}\n\n"
            f"Cluster age  : {cluster['age_days']} days\n"
            f"Requested at : {cluster['requested_at']}\n"
            f"Auto-generated by cleanup Lambda (TTL={os.environ.get('CLUSTER_TTL_DAYS', 30)} days)"
        ),
        sha=file_obj.sha,
        branch=branch_name,
    )

    # Open the cleanup PR
    try:
        pr = repo.create_pull(
            title=f"[Cleanup] Remove expired cluster `{base_name}` ({cluster['environment']})",
            body=_render_cleanup_pr_body(cluster, file_path),
            head=branch_name,
            base=_GITHUB_BASE_BRANCH,
        )
    except GithubException as exc:
        raise RuntimeError(f"Failed to open cleanup PR: {exc.data}") from exc

    return pr.html_url


def _render_cleanup_pr_body(cluster: dict[str, Any], file_path: str) -> str:
    ttl = os.environ.get("CLUSTER_TTL_DAYS", "30")
    return f"""\
## Automated RDS Cluster Cleanup

This PR was opened automatically by the cleanup Lambda.
**Merging it will destroy the RDS cluster via `terraform apply`.**

| Field | Value |
|---|---|
| Cluster identifier | `{cluster['identifier']}` |
| Environment | `{cluster['environment']}` |
| Originally requested | `{cluster['requested_at']}` |
| Age | **{cluster['age_days']} days** (TTL: {ttl} days) |

## What this PR does

Deletes `{file_path}`. When merged to `main`, CircleCI runs `terraform plan`
then `terraform apply` (after human approval), which destroys the cluster and
removes its Secrets Manager secret.

## Before merging

- [ ] Confirm no active applications are using this cluster
- [ ] Verify data has been backed up or is no longer needed
- [ ] Check that the final snapshot will be created (prod clusters have `deletion_protection = true` — disable it first)
- [ ] Notify affected teams

---
*Auto-generated by the [cleanup Lambda](../serverless/src/handler.py) — TTL {ttl} days*
"""
