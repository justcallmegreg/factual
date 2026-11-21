"""The verdict schema Claude must match, and the citation check that guards it."""

from __future__ import annotations

from pydantic import BaseModel, Field, ValidationError, field_validator

VERDICTS = ["supported", "contradicted", "changed_later", "not_found", "unclear"]
ROLES = ["supports", "contradicts", "context", "changed"]
MAX_SUMMARY = 300

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": VERDICTS},
        "summary": {
            "type": "string",
            "description": "One line, at most 300 characters, that answers the question.",
        },
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "description": "the entry number, without the E"},
                    "role": {"type": "string", "enum": ROLES},
                },
                "required": ["id", "role"],
                "additionalProperties": False,
            },
        },
        "caveats": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["verdict", "summary", "evidence", "caveats"],
    "additionalProperties": False,
}


class Evidence(BaseModel):
    id: int
    role: str

    @field_validator("role")
    @classmethod
    def _known_role(cls, value: str) -> str:
        if value not in ROLES:
            raise ValueError("unknown role")
        return value


class Verdict(BaseModel):
    verdict: str
    summary: str = Field(max_length=MAX_SUMMARY)
    evidence: list[Evidence]
    caveats: list[str] = []

    @field_validator("verdict")
    @classmethod
    def _known_verdict(cls, value: str) -> str:
        if value not in VERDICTS:
            raise ValueError("unknown verdict")
        return value


def check_verdict(data: object, *, allowed_ids: set[int]) -> tuple[Verdict | None, list[int]]:
    """Validate Claude's output; keep only citations of entries Claude was shown.

    Returns (verdict, rejected ids). None when the output does not match the schema.
    """
    if not isinstance(data, dict):
        return None, []
    summary = data.get("summary")
    if isinstance(summary, str) and len(summary) > MAX_SUMMARY:
        data = {**data, "summary": summary[: MAX_SUMMARY - 1].rstrip() + "…"}
    try:
        verdict = Verdict.model_validate(data)
    except ValidationError:
        return None, []
    kept, rejected = [], []
    for item in verdict.evidence:
        (kept if item.id in allowed_ids else rejected).append(item)
    return verdict.model_copy(update={"evidence": kept}), [item.id for item in rejected]
