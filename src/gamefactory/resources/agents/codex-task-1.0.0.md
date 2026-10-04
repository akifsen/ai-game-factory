Task template version 1.0.0.

Task id: {task_id}
Agent id: {agent_id}

Objective:
{objective}

Acceptance criteria:
{acceptance_criteria}

Allowed output paths:
{allowed_output_paths}

Do not invent missing requirements. If the task cannot be completed from the supplied source references, return NEEDS_INPUT or BLOCKED with a clear blocking issue. For each changed file, include the complete proposed content encoded as base64 with CREATE, UPDATE, or DELETE and the required before/output SHA-256 values. Do not claim execution, approval, verification, or completion.
