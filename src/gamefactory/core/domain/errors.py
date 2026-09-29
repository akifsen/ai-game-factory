"""Domain error taxonomy for AI Game Factory.

Defines deliberate, structured exceptions with error codes and contextual detail,
strictly avoiding secret leakage.
"""

from typing import Any


class FactoryError(Exception):
    """Base class for all Game Factory domain and runtime errors."""

    def __init__(
        self, message: str, code: str = "FACTORY_ERROR", details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": self.code,
            "message": self.message,
            "details": self.details,
        }


class ConfigurationError(FactoryError):
    """Raised when configuration is invalid, missing, or malformed."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="CONFIGURATION_ERROR", details=details)


class PolicyViolation(FactoryError):
    """Raised when an operation violates active security, budget, or execution policy."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="POLICY_VIOLATION", details=details)


class ApprovalRequired(FactoryError):
    """Raised when an operation cannot proceed without human approval."""

    def __init__(
        self, message: str, approval_id: str | None = None, details: dict[str, Any] | None = None
    ) -> None:
        merged = details or {}
        if approval_id:
            merged["approval_id"] = approval_id
        super().__init__(message, code="APPROVAL_REQUIRED", details=merged)
        self.approval_id = approval_id


class ProviderUnavailable(FactoryError):
    """Raised when an external tool, provider, or engine is not available or configured."""

    def __init__(
        self, message: str, provider: str | None = None, details: dict[str, Any] | None = None
    ) -> None:
        merged = details or {}
        if provider:
            merged["provider"] = provider
        super().__init__(message, code="PROVIDER_UNAVAILABLE", details=merged)
        self.provider = provider


class ToolExecutionError(FactoryError):
    """Raised when a subprocess, tool, or adapter execution fails."""

    def __init__(
        self,
        message: str,
        exit_code: int | None = None,
        stderr: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        merged = details or {}
        if exit_code is not None:
            merged["exit_code"] = exit_code
        if stderr:
            merged["stderr"] = stderr
        super().__init__(message, code="TOOL_EXECUTION_ERROR", details=merged)
        self.exit_code = exit_code
        self.stderr = stderr


class ValidationError(FactoryError):
    """Raised when artifact, schema, or gate validation fails."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="VALIDATION_ERROR", details=details)


class WorkflowError(FactoryError):
    """Raised when workflow dependency, DAG, or lifecycle rules are violated."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="WORKFLOW_ERROR", details=details)


class ArtifactError(FactoryError):
    """Raised when artifact creation, resolution, or verification fails."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="ARTIFACT_ERROR", details=details)


class BudgetExceeded(FactoryError):
    """Raised when an operation would exceed the project or workflow budget."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="BUDGET_EXCEEDED", details=details)


class TimeoutError(FactoryError):
    """Raised when a command or task exceeds its allocated duration."""

    def __init__(
        self,
        message: str,
        timeout_seconds: float | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        merged = details or {}
        if timeout_seconds is not None:
            merged["timeout_seconds"] = timeout_seconds
        super().__init__(message, code="TIMEOUT", details=merged)


class LockError(FactoryError):
    """Raised when an execution lock or lease cannot be acquired."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="LOCK_ERROR", details=details)


class ReconciliationRequired(FactoryError):
    """Raised when a crash or interruption leaves an external operation in an uncertain state."""

    def __init__(
        self, message: str, task_id: str, execution_id: str, details: dict[str, Any] | None = None
    ) -> None:
        merged = details or {}
        merged["task_id"] = task_id
        merged["execution_id"] = execution_id
        super().__init__(message, code="RECONCILIATION_REQUIRED", details=merged)
        self.task_id = task_id
        self.execution_id = execution_id


# V0.4 Asset Pipeline Structured Failure Taxonomy (Section 34)


class SpecInvalidError(ValidationError):
    """Raised when an asset specification violates schema or domain constraints."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, details=details)
        self.code = "SPEC_INVALID"


class BudgetBlockedError(BudgetExceeded):
    """Raised when asset generation or processing is blocked due to budget policy."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, details=details)
        self.code = "BUDGET_BLOCKED"


class ProviderFailedError(FactoryError):
    """Raised when an asset generation provider returns a failure or fails execution."""

    def __init__(
        self, message: str, provider: str | None = None, details: dict[str, Any] | None = None
    ) -> None:
        merged = details or {}
        if provider:
            merged["provider"] = provider
        super().__init__(message, code="PROVIDER_FAILED", details=merged)
        self.provider = provider


class ProviderUncertainError(ReconciliationRequired):
    """Raised when provider acceptance is uncertain after a crash or timeout."""

    def __init__(
        self,
        message: str,
        task_id: str,
        execution_id: str,
        provider: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        merged = details or {}
        if provider:
            merged["provider"] = provider
        super().__init__(message, task_id=task_id, execution_id=execution_id, details=merged)
        self.code = "PROVIDER_UNCERTAIN"
        self.provider = provider


class RawArtifactInvalidError(ArtifactError):
    """Raised when raw model output from a provider is missing, corrupt, or invalid."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, details=details)
        self.code = "RAW_ARTIFACT_INVALID"


class DccFailedError(ToolExecutionError):
    """Raised when Blender or DCC processing script execution fails or times out."""

    def __init__(
        self,
        message: str,
        exit_code: int | None = None,
        stderr: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, exit_code=exit_code, stderr=stderr, details=details)
        self.code = "DCC_FAILED"


class ToolUnavailableError(ToolExecutionError):
    """Raised before launch when a required local tool executable is missing or unusable."""

    def __init__(
        self,
        message: str,
        tool: str,
        reason: str,
        configured_path: str | None = None,
        task_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        merged = dict(details or {})
        merged["tool"] = tool
        merged["reason"] = reason
        if configured_path is not None:
            merged["configured_path"] = configured_path
        if task_id is not None:
            merged["task_id"] = task_id
        super().__init__(message, details=merged)
        self.code = "TOOL_UNAVAILABLE"
        self.tool = tool
        self.reason = reason
        self.configured_path = configured_path
        self.task_id = task_id


class AssetValidationFailedError(ValidationError):
    """Raised when deterministic asset validation checks fail on the processed asset."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, details=details)
        self.code = "ASSET_VALIDATION_FAILED"


class EngineImportFailedError(FactoryError):
    """Raised when the game engine fails to import or stage the processed asset."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="ENGINE_IMPORT_FAILED", details=details)


class RuntimeValidationFailedError(FactoryError):
    """Raised when in-engine runtime verification or observation checks fail."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="RUNTIME_VALIDATION_FAILED", details=details)


class VisualReviewRejectedError(FactoryError):
    """Raised when human reviewer rejects an asset during visual review."""

    def __init__(
        self, message: str, actor: str | None = None, details: dict[str, Any] | None = None
    ) -> None:
        merged = details or {}
        if actor:
            merged["actor"] = actor
        super().__init__(message, code="VISUAL_REVIEW_REJECTED", details=merged)
        self.actor = actor


class PaidRequestInvalidError(ValidationError):
    """Raised when a paid request snapshot violates schema or canonical requirements."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, details=details)
        self.code = "PAID_REQUEST_INVALID"


class PaidRequestIncompatibleError(ProviderFailedError):
    """Raised when current adapter cannot faithfully execute the approved snapshot; no paid call was made."""

    def __init__(
        self, message: str, provider: str | None = None, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message, provider=provider, details=details)
        self.code = "PAID_REQUEST_INCOMPATIBLE"


class PaidRequestRequiredError(FactoryError):
    """Raised when a new paid submission is attempted without an approved snapshot."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="PAID_REQUEST_REQUIRED", details=details)


class ProductionReadinessFailedError(ToolExecutionError):
    """Raised when pre-spend production readiness evaluation or checks fail."""

    def __init__(
        self,
        message: str,
        exit_code: int | None = None,
        stderr: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, exit_code=exit_code, stderr=stderr, details=details)
        self.code = "PRODUCTION_READINESS_FAILED"
