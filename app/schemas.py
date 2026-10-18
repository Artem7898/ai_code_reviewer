"""Validated, portable review and proposal contracts (no executable model output)."""

from pathlib import PurePosixPath
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

type Severity = Literal["error", "warning", "note"]
type ProposalKind = Literal["migration", "tests", "scaffold", "rules"]


def validate_relative_path(value: str) -> str:
    """Reject ambiguous paths before paths enter reports, prompts or SARIF."""
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or "\x00" in value
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or ":" in value
    ):
        raise ValueError("A normalized project-relative POSIX path is required")
    return value


class SourceFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)

    path: str = Field(max_length=1024)
    content: str
    sha256: str = ""
    changed_lines: tuple[int, ...] = ()

    _path = field_validator("path")(validate_relative_path)

    @field_validator("changed_lines")
    @classmethod
    def positive_lines(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if any(line < 1 for line in value):
            raise ValueError("Changed lines are one-based")
        return value


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)

    rule_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:/-]+$")
    severity: Severity
    path: str = Field(max_length=1024)
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    evidence: str = Field(min_length=1, max_length=4000)
    explanation: str = Field(min_length=1, max_length=8000)
    suggestion: str = Field(default="", max_length=8000)
    source: Literal["ast", "ruff", "ai"]
    confidence: float | None = Field(default=None, ge=0, le=1)

    _path = field_validator("path")(validate_relative_path)

    @model_validator(mode="after")
    def ordered_lines(self) -> Self:
        if self.line_end < self.line_start:
            raise ValueError("line_end precedes line_start")
        return self


class ReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    findings: list[Finding] = Field(default_factory=list, max_length=2000)
    summary: str = Field(max_length=16000)
    tokens_used: int = Field(default=0, ge=0)
    model: str = Field(default="static", max_length=200)
    estimated_cost_usd: float = Field(default=0, ge=0)


class ProposedArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    path: str = Field(max_length=1024)
    content: str = Field(max_length=200_000)
    _path = field_validator("path")(validate_relative_path)


class GeneratedProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    kind: ProposalKind
    summary: str = Field(max_length=16000)
    artifacts: list[ProposedArtifact] = Field(default_factory=list, max_length=30)
    diff: str = Field(default="", max_length=400_000)
    tokens_used: int = Field(default=0, ge=0)
    model: str = Field(default="", max_length=200)
    estimated_cost_usd: float = Field(default=0, ge=0)
