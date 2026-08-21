"""Shared Pydantic models (pagination, response format)."""

from pydantic import BaseModel, Field, ConfigDict
from enum import Enum
from typing import Optional


class ResponseFormat(str, Enum):
    """Output format for tool responses."""
    MARKDOWN = "markdown"
    JSON = "json"


class PaginationInput(BaseModel):
    """Shared pagination parameters for list operations."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    limit: Optional[int] = Field(
        default=25,
        description="Maximum number of results to return (1-100)",
        ge=1,
        le=100,
    )
    offset: Optional[int] = Field(
        default=0,
        description="Number of results to skip for pagination",
        ge=0,
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' for human-readable, 'json' for machine-readable",
    )
