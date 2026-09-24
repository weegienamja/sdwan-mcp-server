"""Policy Pydantic input models."""


from pydantic import BaseModel, ConfigDict, Field

from cisco_vmanage_mcp.models.common import ResponseFormat


class ListPoliciesInput(BaseModel):
    """Input parameters for listing vSmart policies."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    limit: int | None = Field(default=25, ge=1, le=100, description="Max results to return")
    offset: int | None = Field(default=0, ge=0, description="Results to skip for pagination")
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' or 'json'",
    )


class ListTemplatesInput(BaseModel):
    """Input parameters for listing device templates."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    device_type: str | None = Field(
        default=None,
        description="Optional filter by device type (e.g., 'vedge', 'vmanage').",
    )
    limit: int | None = Field(default=25, ge=1, le=100, description="Max results to return")
    offset: int | None = Field(default=0, ge=0, description="Results to skip for pagination")
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' or 'json'",
    )
