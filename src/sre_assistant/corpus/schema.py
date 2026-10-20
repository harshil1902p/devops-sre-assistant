"""Knowledge-base document model.

Every document is Markdown with a YAML front-matter header. The metadata is the contract that
retrieval relies on for *filtering* (service, environment, infrastructure, error_type) and that
evaluation relies on for *ground truth* (scenario).
"""

from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class DocType(StrEnum):
    RUNBOOK = "runbook"
    POSTMORTEM = "postmortem"
    KB = "kb"


class Infrastructure(StrEnum):
    DOCKER = "docker"
    KUBERNETES = "kubernetes"
    AZURE = "azure"


class Environment(StrEnum):
    PRODUCTION = "production"
    STAGING = "staging"
    ANY = "any"


class DocMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    doc_id: str = Field(pattern=r"^(RB|PM|KB)-[A-Z0-9-]+$")
    doc_type: DocType
    title: str = Field(min_length=10)
    scenario: str = Field(pattern=r"^[a-z0-9-]+$")
    service: str
    environment: Environment
    language: str
    infrastructure: Infrastructure
    error_type: str
    error_codes: list[str] = Field(min_length=1)
    severity: str = Field(pattern=r"^SEV[1-4]$")
    created: date
    tags: list[str] = Field(default_factory=list)

    @field_validator("tags")
    @classmethod
    def _lower_tags(cls, v: list[str]) -> list[str]:
        return sorted({t.lower() for t in v})


class Document(BaseModel):
    model_config = ConfigDict(frozen=True)

    metadata: DocMetadata
    body: str = Field(min_length=200)
    path: str

    @property
    def doc_id(self) -> str:
        return self.metadata.doc_id
