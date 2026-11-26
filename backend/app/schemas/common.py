from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


NonEmptyString = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]

ConfidenceScore = Annotated[
    Decimal,
    Field(ge=Decimal("0"), le=Decimal("1")),
]

Percentage = Annotated[
    Decimal,
    Field(ge=Decimal("0"), le=Decimal("100")),
]


class ContractModel(BaseModel):
    """Base class for all inter-agent contracts."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
    )


class SourceCitation(ContractModel):
    """Reference to the source supporting a data point or finding."""

    source_id: NonEmptyString
    source_name: NonEmptyString
    url: str | None = None
    published_at: datetime | None = None
    retrieved_at: datetime


class AgentMetadata(ContractModel):
    """Common metadata returned by an agent."""

    schema_version: str = "2.0"
    status: str = "success"
    as_of_date: datetime
    warnings: list[str] = Field(default_factory=list)

class RetrievedPassage(ContractModel):
    """A retrieved document passage supplied to an agent."""

    passage_id: NonEmptyString

    text: NonEmptyString

    source_citation_id: NonEmptyString