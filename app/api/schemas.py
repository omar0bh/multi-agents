from typing import Any

from pydantic import BaseModel, Field


class TaskRequest(BaseModel):
    task: str = Field(min_length=1, max_length=12000)


class ResearchRequest(BaseModel):
    topic: str = Field(min_length=1, max_length=3000)


class CodingRequest(BaseModel):
    task: str = Field(min_length=1, max_length=12000)
    language: str = Field(default="python", min_length=1, max_length=64)


class BusinessRequest(BaseModel):
    request: str = Field(min_length=1, max_length=12000)


class AgentResponse(BaseModel):
    ok: bool = True
    workflow: str
    result: dict[str, Any]
    meta: dict[str, Any] = Field(default_factory=dict)
