"""Unit tests for SecretRedactor."""

from gamefactory.core.execution.redaction import SecretRedactor


class TestSecretRedactor:
    def test_redact_bearer_token(self) -> None:
        redactor = SecretRedactor()
        text = "Authorization: Bearer supersecrettoken123456789"
        redacted = redactor.redact_text(text)
        assert "supersecrettoken123456789" not in redacted
        assert "Authorization: Bearer [REDACTED]" in redacted

    def test_redact_api_key_syntax(self) -> None:
        redactor = SecretRedactor()
        text = "Connecting with api_key: 'abcdef1234567890' to provider"
        redacted = redactor.redact_text(text)
        assert "abcdef1234567890" not in redacted
        assert "[REDACTED]" in redacted

    def test_redact_sk_prefixed_keys(self) -> None:
        redactor = SecretRedactor()
        text = "Using OpenAI key sk_live_12345678901234567890"
        redacted = redactor.redact_text(text)
        assert "sk_live_12345678901234567890" not in redacted
        assert "[REDACTED]" in redacted

    def test_redact_custom_exact_secrets(self) -> None:
        redactor = SecretRedactor(extra_secrets=["my_super_secret_db_pass"])
        text = "Connecting to db using password=my_super_secret_db_pass on localhost"
        redacted = redactor.redact_text(text)
        assert "my_super_secret_db_pass" not in redacted
        assert "[REDACTED]" in redacted

    def test_redact_nested_dictionary(self) -> None:
        redactor = SecretRedactor()
        payload = {
            "name": "asset_gen",
            "api_key": "some_secret_value_123",
            "nested": {
                "user_token": "token_abc_123456",
                "safe_field": "hello world",
            },
            "items": ["safe", "Bearer sensitive987654321"],
        }
        redacted = redactor.redact_data(payload)
        assert redacted["api_key"] == "[REDACTED]"
        assert redacted["nested"]["user_token"] == "[REDACTED]"
        assert redacted["nested"]["safe_field"] == "hello world"
        assert "sensitive987654321" not in redacted["items"][1]

    def test_normal_text_untouched(self) -> None:
        redactor = SecretRedactor()
        text = "Godot engine 4.7.2 detected at C:/Users/lenovo/devel/godot"
        assert redactor.redact_text(text) == text

    def test_redact_short_punctuated_password_and_auth_headers(self) -> None:
        redactor = SecretRedactor()
        text = "password: 'u$p:9!q+2' Authorization: Basic dXNlcjpwYXNz Bearer x!"
        result = redactor.redact_text(text)
        for secret in ("u$p:9!q+2", "dXNlcjpwYXNz", "Bearer x!"):
            assert secret not in result
        assert "Authorization: Basic [REDACTED]" in result

    def test_redact_data_recurses_through_tuples(self) -> None:
        result = SecretRedactor().redact_data(
            ("password='short!'", {"items": ("Authorization: Basic abc123",)})
        )
        assert "short!" not in result[0]
        assert "abc123" not in result[1]["items"][0]
