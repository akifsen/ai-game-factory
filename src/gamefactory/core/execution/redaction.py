"""Centralized secret redaction engine for logs, console, artifacts, and persistence.

Prevents leakage of API keys, bearer tokens, passwords, and sensitive
environment variable values.
"""

import os
import re
from typing import Any

# Regex patterns for common sensitive tokens
_SECRET_PATTERNS = [
    re.compile(r"(Bearer\s+)[^\s,;]+", re.IGNORECASE),
    re.compile(r"(Basic\s+)[A-Za-z0-9+/=]+", re.IGNORECASE),
    re.compile(
        r"((?:api[_-]?key|secret|token|password|auth|authorization)[\s:=]+)(?!Bearer\b|Basic\b)(?:'[^']*'|\"[^\"]*\"|[^\s,;]+)",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:sk|pk)[-_][a-zA-Z0-9_\-]{14,}\b"),
]

# Sensitive environment variable name substrings
_SENSITIVE_ENV_SUBSTRINGS = (
    "KEY",
    "SECRET",
    "TOKEN",
    "PASSWORD",
    "AUTH",
    "CREDENTIAL",
)

_COMMON_SECRET_LITERALS = frozenset(
    {"true", "false", "yes", "no", "on", "off", "null", "none", "enabled", "disabled"}
)


AMBIENT_SECRET_MIN_LENGTH = 8
EXPLICIT_SECRET_MIN_LENGTH = 4


def is_eligible_exact_secret(value: Any, *, explicit: bool = False) -> bool:
    """Determine whether a string is eligible for exact-value secret replacement.

    Rationale:
    Ambient values (any environment variable whose NAME merely looks sensitive) need
    at least 8 characters: that keeps the chance of a coincidental match inside a
    64-hex digest, UUID or task id negligible, while real API keys and tokens are far
    longer. Explicit, request-scoped secrets (credential env overrides and values of
    credential flags such as ``--api-key``) are known credentials for that command,
    so a lower floor of 4 still protects short passwords while single characters and
    two-digit flags can never corrupt output. Common boolean, toggle, and null
    literals are never eligible. Shorter values are still caught by the key-aware
    regex patterns (e.g. "API_KEY=abc", "password: x", "Bearer x").
    """
    if not isinstance(value, str):
        return False
    stripped = value.strip()
    minimum = EXPLICIT_SECRET_MIN_LENGTH if explicit else AMBIENT_SECRET_MIN_LENGTH
    if len(stripped) < minimum:
        return False
    if stripped.lower() in _COMMON_SECRET_LITERALS:
        return False
    return True


def _is_identity_key(key: Any) -> bool:
    """Keep database identities and hashes byte-for-byte stable."""
    normalized = str(key).strip().lower().replace("-", "_")
    return normalized in {"id", "hash", "fingerprint"} or normalized.endswith(
        ("_id", "_hash", "_fingerprint")
    )


is_identity_key = _is_identity_key


class SecretRedactor:
    """Detects and redacts sensitive credentials from strings, arguments, and structures."""

    def __init__(self, extra_secrets: list[str] | None = None) -> None:
        self.exact_secrets: set[str] = set()
        if extra_secrets:
            for s in extra_secrets:
                if is_eligible_exact_secret(s, explicit=True):
                    self.exact_secrets.add(s)

    def redact_text(self, text: str, secrets: list[str] | set[str] | None = None) -> str:
        """Redact known secrets and pattern matches from text."""
        if not text:
            return text

        result = text
        # Redact exact matches
        current_env_secrets = {
            value
            for key, value in os.environ.items()
            if any(sub in key.upper() for sub in _SENSITIVE_ENV_SUBSTRINGS)
            and is_eligible_exact_secret(value)
        }
        passed_secrets = {s for s in (secrets or ()) if is_eligible_exact_secret(s, explicit=True)}
        all_secrets = self.exact_secrets | current_env_secrets | passed_secrets
        for secret in sorted((s for s in all_secrets if s), key=len, reverse=True):
            if secret in result:
                result = result.replace(secret, "[REDACTED]")

        # Redact regex patterns
        for pattern in _SECRET_PATTERNS:
            # Handle patterns with capture groups
            def _replace_match(match: re.Match[str]) -> str:
                groups = match.groups()
                if len(groups) == 2:
                    return f"{groups[0]}[REDACTED]"
                elif len(groups) == 1:
                    return f"{groups[0]}[REDACTED]"
                return "[REDACTED]"

            result = pattern.sub(_replace_match, result)

        return result

    def redact_data(self, data: Any, secrets: list[str] | set[str] | None = None) -> Any:
        """Recursively redact dictionaries, lists, and primitives."""
        if isinstance(data, str):
            return self.redact_text(data, secrets)
        elif isinstance(data, dict):
            redacted_dict: dict[str, Any] = {}
            for k, v in data.items():
                k_upper = str(k).upper()
                if any(sub in k_upper for sub in _SENSITIVE_ENV_SUBSTRINGS):
                    # Presence flags (bool/None) are not credential values.
                    if v is None or isinstance(v, bool):
                        redacted_dict[k] = v
                    else:
                        redacted_dict[k] = "[REDACTED]"
                else:
                    redacted_dict[k] = self.redact_data(v, secrets)
            return redacted_dict
        elif isinstance(data, (list, tuple)):
            return [self.redact_data(item, secrets) for item in data]
        return data


# Default global redactor instance
redactor = SecretRedactor()
