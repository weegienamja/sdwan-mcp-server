"""Alarm Pydantic input models."""


from pydantic import BaseModel, ConfigDict, Field

from cisco_vmanage_mcp.models.common import ResponseFormat


class ListAlarmsInput(BaseModel):
    """Input parameters for listing alarms."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    severity: str | None = Field(
        default=None,
        description="Filter by severity: 'Critical', 'Major', 'Medium', 'Minor'.",
        pattern=r"^(Critical|Major|Medium|Minor)$",
    )
    hours_back: int | None = Field(
        default=24,
        description="How many hours of alarm history to retrieve (1-168, default 24).",
        ge=1,
        le=168,
    )
    limit: int | None = Field(default=25, ge=1, le=100, description="Max results to return")
    offset: int | None = Field(default=0, ge=0, description="Results to skip for pagination")
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' or 'json'",
    )


class AlarmCountInput(BaseModel):
    """Input for alarm count query."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' or 'json'",
    )


class ListEventsInput(BaseModel):
    """Input parameters for listing events."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    hours_back: int | None = Field(
        default=24,
        description="How many hours of event history to retrieve (1-168, default 24).",
        ge=1,
        le=168,
    )
    system_ip: str | None = Field(
        default=None,
        description="Optional: filter events to a specific device by system IP.",
    )
    limit: int | None = Field(default=25, ge=1, le=100, description="Max results to return")
    offset: int | None = Field(default=0, ge=0, description="Results to skip for pagination")
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' or 'json'",
    )
