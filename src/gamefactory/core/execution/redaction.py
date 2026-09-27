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


class SecretRedactor:
    """Detects and redacts sensitive credentials from strings, arguments, and structures."""

    def __init__(self, extra_secrets: list[str] | None = None) -> None:
        self.exact_secrets: set[str] = set()
        if extra_secrets:
            for s in extra_secrets:
                if s:
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
            if value and any(sub in key.upper() for sub in _SENSITIVE_ENV_SUBSTRINGS)
        }
        all_secrets = self.exact_secrets | current_env_secrets | set(secrets or ())
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
                    redacted_dict[k] = "[REDACTED]"
                else:
                    redacted_dict[k] = self.redact_data(v, secrets)
            return redacted_dict
        elif isinstance(data, (list, tuple)):
            return [self.redact_data(item, secrets) for item in data]
        return data


# Default global redactor instance
redactor = SecretRedactor()
