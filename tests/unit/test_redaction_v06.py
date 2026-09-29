"""Unit tests for V0.6 secret redactor eligibility, identity key preservation, and JSON boundaries."""

from __future__ import annotations

import json
from typing import Any

import pytest

from gamefactory.core.execution.redaction import (
    SecretRedactor,
    _is_identity_key,
    is_eligible_exact_secret,
    redactor,
)


def test_is_eligible_exact_secret() -> None:
    # Non-strings rejected
    assert not is_eligible_exact_secret(None)
    assert not is_eligible_exact_secret(12345678)
    assert not is_eligible_exact_secret(True)

    # Short values (< 8 chars stripped) rejected
    assert not is_eligible_exact_secret("")
    assert not is_eligible_exact_secret("1")
    assert not is_eligible_exact_secret("0")
    assert not is_eligible_exact_secret("abc")
    assert not is_eligible_exact_secret("1234567")
    assert not is_eligible_exact_secret(" 1234567 ")

    # Exactly 8 chars boundary
    assert is_eligible_exact_secret("12345678")
    assert is_eligible_exact_secret("abcdefgh")
    assert is_eligible_exact_secret("  abcdefgh  ")

    # Common literals (regardless of case) rejected
    literals = [
        "true",
        "TRUE",
        "True",
        "false",
        "FALSE",
        "yes",
        "no",
        "on",
        "off",
        "null",
        "none",
        "enabled",
        "ENABLED",
        "disabled",
        "DISABLED",
    ]
    for lit in literals:
        assert not is_eligible_exact_secret(lit)
        assert not is_eligible_exact_secret(f"  {lit}  ")

    # Realistic credentials accepted
    assert is_eligible_exact_secret("msy_live_abcdef1234567890abcdef123456")
    assert is_eligible_exact_secret("ghp_1234567890abcdefghijklmnopqrstuvwxyz")


def test_redactor_matrix_with_hostile_env_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    # Set hostile env flags that previously caused widespread corruption (finding P0-3)
    monkeypatch.setenv("SOME_AUTH_FLAG", "1")
    monkeypatch.setenv("TOKEN_FLAG", "0")
    monkeypatch.setenv("OAUTH_FLAG", "true")
    monkeypatch.setenv("SECRET_ENABLED", "false")
    monkeypatch.setenv("CLAUDE_CODE_SDK_HAS_HOST_AUTH_REFRESH", "1")

    uuid_val = "01a0e9c1-f324-75f4-ab7c-59ee3b6c541d"
    sha_val = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    json_str = json.dumps({"task_id": uuid_val, "sha256": sha_val})

    # Assert strings containing "1", "0", "true", "false" come out UNCHANGED
    assert redactor.redact_text("abc123") == "abc123"
    assert redactor.redact_text(uuid_val) == uuid_val
    assert redactor.redact_text(sha_val) == sha_val
    assert redactor.redact_text(json_str) == json_str

    # redact_data with structured data
    struct_data: dict[str, Any] = {
        "task_id": uuid_val,
        "sha256": sha_val,
        "count": 10,
        "auth_token_id": "token-id-101",
        "status": "active",
        "has_auth": True,
        "token_present": False,
        "api_key_status": None,
    }
    redacted_struct = redactor.redact_data(struct_data)
    assert redacted_struct["task_id"] == uuid_val
    assert redacted_struct["sha256"] == sha_val
    assert redacted_struct["count"] == 10
    # Sensitive-named keys never pass their value through, even with an _id suffix.
    assert redacted_struct["auth_token_id"] == "[REDACTED]"
    assert redacted_struct["status"] == "active"
    # Preserved presence booleans / None under sensitive-named keys
    assert redacted_struct["has_auth"] is True
    assert redacted_struct["token_present"] is False
    assert redacted_struct["api_key_status"] is None


def test_realistic_credential_redaction(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_key = "msy_abcdefghijklmnopqrstuvwxyz1234567890"
    monkeypatch.setenv("MESHY_TEST_API_KEY", fake_key)

    plain = f"Connecting to provider using {fake_key} now"
    assert redactor.redact_text(plain) == "Connecting to provider using [REDACTED] now"

    data = {"endpoint": "https://api.example.com", "key_payload": fake_key}
    assert redactor.redact_data(data) == {
        "endpoint": "https://api.example.com",
        "key_payload": "[REDACTED]",
    }


def test_key_aware_regex_pattern_catches_short_secrets() -> None:
    # Short secret is not globally replaced in general text...
    text = "Status: short1. Result is 42."
    assert redactor.redact_text(text) == text

    # ...but key-aware pattern ("API_KEY=short1", "password: short1", "Bearer short1") catches it
    assert redactor.redact_text("API_KEY=short1") == "API_KEY=[REDACTED]"
    assert redactor.redact_text("api_key: short1") == "api_key: [REDACTED]"
    assert redactor.redact_text("password=short1") == "password=[REDACTED]"
    assert redactor.redact_text("Bearer short1") == "Bearer [REDACTED]"


def test_boundary_exact_secret_length(monkeypatch: pytest.MonkeyPatch) -> None:
    # 7-character secret is not globally replaced
    monkeypatch.setenv("SECRET_SEVEN", "1234567")
    # 8-character secret is globally replaced
    monkeypatch.setenv("SECRET_EIGHT", "12345678")

    text = "Check 1234567 and 12345678 values"
    redacted = redactor.redact_text(text)
    assert "1234567" in redacted
    assert "12345678" not in redacted
    assert redacted == "Check 1234567 and [REDACTED] values"


def test_identity_key_rule_and_conservative_structured_redaction() -> None:
    """The identity-key rule serves persistence; CLI/structured output stays conservative."""
    # Keys matching identity-key rule (_is_identity_key)
    identity_keys = [
        "id",
        "hash",
        "fingerprint",
        "asset_id",
        "task_id",
        "workflow_id",
        "concept_hash",
        "spec_hash",
        "request_fingerprint",
        "auth_token_id",
        "api_key_id",
        "secret_hash",
    ]
    for key in identity_keys:
        assert _is_identity_key(key)

    data = {
        "task_id": "01a0e9c1-f324-75f4-ab7c-59ee3b6c541d",
        "concept_hash": "0" * 64,
        "auth_token_id": "tok-12345",
        "api_key_id": "key-99999",
        "credential_configured": True,
        "actual_secret": "my-secret-value-longer-than-8",
    }
    redacted = redactor.redact_data(data)
    # Plain identities are untouched...
    assert redacted["task_id"] == data["task_id"]
    assert redacted["concept_hash"] == data["concept_hash"]
    # ...but a sensitive-named key never passes its value through, even with an id suffix.
    assert redacted["auth_token_id"] == "[REDACTED]"
    assert redacted["api_key_id"] == "[REDACTED]"
    assert redacted["credential_configured"] is True
    assert redacted["actual_secret"] == "[REDACTED]"


def test_secret_redactor_extra_secrets_and_call_secrets() -> None:
    # Explicit secrets use the 4-character floor; literals and 1-3 characters never qualify.
    custom_redactor = SecretRedactor(extra_secrets=["pw9", "true", "hunt3r", "eligible_extra"])
    text = "Values: pw9, true, hunt3r and eligible_extra"
    res = custom_redactor.redact_text(text)
    assert res == "Values: pw9, true, [REDACTED] and [REDACTED]"

    # Call-level (request-scoped) secrets are explicit as well.
    res2 = custom_redactor.redact_text(
        "Checking 123 and s3cr and another_long_secret",
        secrets=["123", "s3cr", "another_long_secret"],
    )
    assert res2 == "Checking 123 and [REDACTED] and [REDACTED]"


def test_ambient_and_explicit_floors_are_distinct(monkeypatch: pytest.MonkeyPatch) -> None:
    from gamefactory.core.execution.redaction import (
        AMBIENT_SECRET_MIN_LENGTH,
        EXPLICIT_SECRET_MIN_LENGTH,
    )

    assert (EXPLICIT_SECRET_MIN_LENGTH, AMBIENT_SECRET_MIN_LENGTH) == (4, 8)
    assert is_eligible_exact_secret("hunt3r", explicit=True)
    assert not is_eligible_exact_secret("hunt3r")
    assert not is_eligible_exact_secret("123", explicit=True)
    assert not is_eligible_exact_secret("true", explicit=True)
    # An ambient short value (env name merely looks sensitive) stays harmless.
    monkeypatch.setenv("SOME_AUTH_PIN", "4821")
    assert redactor.redact_text("task 01a04821-f324") == "task 01a04821-f324"
