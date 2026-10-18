from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field


@dataclass
class LogRecord:
    lineno: int
    ts: str
    level: str
    logger: str
    trace_id: str
    text: str


@dataclass
class Incident:
    signature: str
    exc_type: str
    exc_message: str
    frames: list[tuple[str, int, str]]  # only frames from our own code: (file, line, func)
    records: list[LogRecord] = field(default_factory=list)

    @property
    def trace_ids(self) -> list[str]:
        return list(dict.fromkeys(record.trace_id for record in self.records if record.trace_id != "-"))

    @property
    def location(self) -> str:
        return f"{self.frames[-1][0]}:{self.frames[-1][1]}" if self.frames else "unknown"


class Triage(BaseModel):
    reasoning: str = Field(description="One sentence: why this category and severity")
    category: Literal["code_bug", "bad_input", "infrastructure", "unknown"]
    severity: Literal["low", "medium", "high", "critical"]


class Report(BaseModel):
    title: str = Field(description="Short incident title")
    summary: str = Field(description="1-2 sentences for a non-engineer")
    root_cause_hypothesis: str
    evidence: list[str] = Field(description="One self-contained fact per item, plain text: file:line, a trace_id with "
                                            "its data, or a quoted log line. No markdown, no nested bullets, "
                                            "no code blocks.")
    confidence: Literal["low", "medium", "high"]
    suggested_fix: str = Field(description="Fixed Python code in a ```python block, plus one sentence why")
    next_steps: list[str]
