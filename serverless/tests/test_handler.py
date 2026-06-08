"""
Unit tests for serverless/src/handler.py

Covers:
  - _parse_payload  : validation, sanitisation, defaults
  - lambda_handler  : batchItemFailures routing
  - _get_secret     : plain string, JSON envelope, error handling, caching
"""
from __future__ import annotations

import importlib
import json
import sys
import types
from unittest.mock import MagicMock, patch, call

import pytest


# ---------------------------------------------------------------------------
# Helpers to import the handler module with stubbed-out environment variables
# and the github/boto3 top-level imports so we don't need real AWS credentials.
# ---------------------------------------------------------------------------

def _load_handler():
    """
    Import (or re-import) handler with the minimum env vars the module-level
    code requires.  We reload each time so that module-global state (_secret_cache
    in particular) is reset between test functions that need a clean slate.
    """
    env_patch = {
        "GITHUB_TOKEN_SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123456789012:secret:gh-token",
        "GITHUB_REPO": "myorg/my-iac-repo",
        "GITHUB_BASE_BRANCH": "main",
    }

    # Provide a lightweight stub for the `github` package so the import
    # succeeds even without PyGithub installed in the test runner.
    if "github" not in sys.modules:
        github_stub = types.ModuleType("github")
        github_stub.Github = MagicMock()
        github_stub.GithubException = Exception
        sys.modules["github"] = github_stub

    with patch.dict("os.environ", env_patch, clear=False):
        # Force a fresh import so module-level env lookups use our values
        if "handler" in sys.modules:
            del sys.modules["handler"]
        spec = importlib.util.spec_from_file_location(
            "handler",
            "/Users/doronsun/home_assignmet/serverless/src/handler.py",
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules["handler"] = mod
        spec.loader.exec_module(mod)
        return mod


# Load once for the whole module; individual test classes that care about
# _secret_cache isolation reload on their own.
handler = _load_handler()


# ===========================================================================
# _parse_payload tests
# ===========================================================================

class TestParsePayload:
    """Tests for _parse_payload()."""

    # ------------------------------------------------------------------
    # Happy-path
    # ------------------------------------------------------------------

    def test_valid_payload_returns_correct_dict(self):
        body = json.dumps({
            "cluster_name": "my-cluster",
            "environment": "dev",
            "engine": "postgres",
            "database_name": "mydb",
            "master_username": "admin",
        })
        result = handler._parse_payload(body)

        assert result["cluster_name"] == "my-cluster"
        assert result["environment"] == "dev"
        assert result["engine"] == "postgres"
        assert result["database_name"] == "mydb"
        assert result["master_username"] == "admin"
        # requested_at should be a non-empty ISO-8601 string
        assert result["requested_at"]

    def test_cluster_name_special_chars_sanitised(self):
        """'my DB!' → 'my-db'"""
        body = json.dumps({
            "cluster_name": "my DB!",
            "environment": "dev",
            "engine": "mysql",
        })
        result = handler._parse_payload(body)
        assert result["cluster_name"] == "my-db"

    def test_cluster_name_underscores_and_uppercase_sanitised(self):
        """'MY_APP_DB' → 'my-app-db'"""
        body = json.dumps({
            "cluster_name": "MY_APP_DB",
            "environment": "prod",
            "engine": "mysql",
        })
        result = handler._parse_payload(body)
        assert result["cluster_name"] == "my-app-db"

    # ------------------------------------------------------------------
    # Defaults
    # ------------------------------------------------------------------

    def test_database_name_defaults_to_cluster_name(self):
        body = json.dumps({
            "cluster_name": "my-cluster",
            "environment": "dev",
            "engine": "postgres",
        })
        result = handler._parse_payload(body)
        # cluster_name after sanitisation is still "my-cluster"
        assert result["database_name"] == "my-cluster"

    def test_master_username_defaults_to_dbadmin(self):
        body = json.dumps({
            "cluster_name": "testcluster",
            "environment": "dev",
            "engine": "mysql",
        })
        result = handler._parse_payload(body)
        assert result["master_username"] == "dbadmin"

    # ------------------------------------------------------------------
    # Validation failures → _UnrecoverableError
    # ------------------------------------------------------------------

    def test_missing_required_field_raises(self):
        # "engine" is absent
        body = json.dumps({"cluster_name": "foo", "environment": "dev"})
        with pytest.raises(handler._UnrecoverableError, match="engine"):
            handler._parse_payload(body)

    def test_invalid_environment_raises(self):
        body = json.dumps({
            "cluster_name": "foo",
            "environment": "staging",   # not in {"dev", "prod"}
            "engine": "mysql",
        })
        with pytest.raises(handler._UnrecoverableError, match="environment"):
            handler._parse_payload(body)

    def test_invalid_engine_raises(self):
        body = json.dumps({
            "cluster_name": "foo",
            "environment": "dev",
            "engine": "oracle",         # not in {"mysql", "postgres"}
        })
        with pytest.raises(handler._UnrecoverableError, match="engine"):
            handler._parse_payload(body)

    def test_invalid_json_raises(self):
        with pytest.raises(handler._UnrecoverableError, match="not valid JSON"):
            handler._parse_payload("{not-valid-json")


# ===========================================================================
# lambda_handler tests
# ===========================================================================

class TestLambdaHandler:
    """Tests for lambda_handler()."""

    def _make_event(self, message_id: str, body: dict) -> dict:
        return {
            "Records": [
                {"messageId": message_id, "body": json.dumps(body)},
            ]
        }

    def test_successful_processing_returns_empty_failures(self):
        event = self._make_event(
            "msg-001",
            {"cluster_name": "good-cluster", "environment": "dev", "engine": "mysql"},
        )
        with patch.object(handler, "_process_record") as mock_proc:
            mock_proc.return_value = None
            result = handler.lambda_handler(event, None)

        assert result == {"batchItemFailures": []}

    def test_unrecoverable_error_drops_message(self):
        """_UnrecoverableError must NOT appear in batchItemFailures."""
        event = self._make_event(
            "msg-bad",
            {"cluster_name": "x", "environment": "staging", "engine": "mysql"},
        )
        with patch.object(handler, "_process_record",
                          side_effect=handler._UnrecoverableError("bad input")):
            result = handler.lambda_handler(event, None)

        assert result == {"batchItemFailures": []}

    def test_transient_error_puts_message_id_in_failures(self):
        """RuntimeError (transient) must be retried via batchItemFailures."""
        event = self._make_event(
            "msg-transient",
            {"cluster_name": "cluster", "environment": "dev", "engine": "mysql"},
        )
        with patch.object(handler, "_process_record",
                          side_effect=RuntimeError("network timeout")):
            result = handler.lambda_handler(event, None)

        assert result == {"batchItemFailures": [{"itemIdentifier": "msg-transient"}]}


# ===========================================================================
# _get_secret tests
# ===========================================================================

class TestGetSecret:
    """Tests for _get_secret()."""

    # Each test reloads the module so the in-process cache starts empty.

    def _fresh_handler(self):
        return _load_handler()

    def _make_client_error(self, code: str):
        """Build a botocore ClientError with the given error code."""
        from botocore.exceptions import ClientError
        return ClientError(
            {"Error": {"Code": code, "Message": "some message"}},
            "GetSecretValue",
        )

    # ------------------------------------------------------------------
    # Correct value extraction
    # ------------------------------------------------------------------

    def test_plain_string_secret_returned_directly(self):
        h = self._fresh_handler()
        secret_value = "ghp_plain_token_string"

        mock_client = MagicMock()
        mock_client.get_secret_value.return_value = {"SecretString": secret_value}

        with patch("boto3.client", return_value=mock_client):
            result = h._get_secret("arn:fake:secret")

        assert result == secret_value

    def test_json_secret_with_token_key(self):
        h = self._fresh_handler()
        token = "ghp_token_from_json"
        mock_client = MagicMock()
        mock_client.get_secret_value.return_value = {
            "SecretString": json.dumps({"token": token})
        }

        with patch("boto3.client", return_value=mock_client):
            result = h._get_secret("arn:fake:secret")

        assert result == token

    def test_json_secret_with_github_token_key(self):
        h = self._fresh_handler()
        token = "ghp_github_token_value"
        mock_client = MagicMock()
        mock_client.get_secret_value.return_value = {
            "SecretString": json.dumps({"github_token": token})
        }

        with patch("boto3.client", return_value=mock_client):
            result = h._get_secret("arn:fake:secret2")

        assert result == token

    # ------------------------------------------------------------------
    # Error handling
    # ------------------------------------------------------------------

    def test_resource_not_found_raises_unrecoverable(self):
        h = self._fresh_handler()
        mock_client = MagicMock()
        mock_client.get_secret_value.side_effect = self._make_client_error(
            "ResourceNotFoundException"
        )

        with patch("boto3.client", return_value=mock_client):
            with pytest.raises(h._UnrecoverableError, match="ResourceNotFoundException"):
                h._get_secret("arn:fake:missing-secret")

    # ------------------------------------------------------------------
    # Caching
    # ------------------------------------------------------------------

    def test_second_call_uses_cache_not_boto3(self):
        """boto3.client should only be called once even after two _get_secret calls."""
        h = self._fresh_handler()
        token = "ghp_cached_token"
        mock_client = MagicMock()
        mock_client.get_secret_value.return_value = {"SecretString": token}

        with patch("boto3.client", return_value=mock_client) as mock_boto3_client:
            first = h._get_secret("arn:fake:cached-secret")
            second = h._get_secret("arn:fake:cached-secret")

        assert first == token
        assert second == token
        # boto3.client("secretsmanager") should have been called exactly once
        mock_boto3_client.assert_called_once_with("secretsmanager")
        # And the underlying API call likewise only once
        mock_client.get_secret_value.assert_called_once()
