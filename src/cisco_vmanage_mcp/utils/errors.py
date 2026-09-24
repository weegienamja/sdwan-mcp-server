"""Error handling utilities."""

import httpx

from cisco_vmanage_mcp.client import (
    AuthenticationError,
    ConnectionError,
    NotFoundError,
    PermissionError,
    RateLimitError,
    TimeoutError,
    VManageAPIError,
    VManageError,
)


def handle_api_error(e: Exception) -> str:
    """Convert exceptions into actionable, user-friendly error messages.

    These messages are returned to the LLM agent, so they should guide
    the agent toward a solution or suggest an alternative tool to try.
    """
    if isinstance(e, AuthenticationError):
        return (
            f"Error: {str(e)} "
            "Verify VMANAGE_USERNAME and VMANAGE_PASSWORD in your .env file."
        )

    if isinstance(e, RateLimitError):
        return "Error: Rate limit exceeded on vManage. Wait a moment and retry."

    if isinstance(e, NotFoundError):
        return (
            "Error: Endpoint or resource not found. This may mean the device "
            "system IP or UUID is incorrect. Use vmanage_list_devices first "
            "to find valid device identifiers."
        )

    if isinstance(e, PermissionError):
        return (
            "Error: Permission denied. The vManage user role may lack "
            "the required privilege level. The DevNet sandbox uses 'operator' "
            "role which has read-only access."
        )

    if isinstance(e, TimeoutError):
        return (
            f"Error: {str(e)} Check that the vManage host "
            "is reachable and VMANAGE_HOST/VMANAGE_PORT are correct."
        )

    if isinstance(e, ConnectionError):
        return (
            f"Error: {str(e)} Verify VMANAGE_HOST and "
            "VMANAGE_PORT are correct and the instance is running. "
            "The DevNet sandbox may be temporarily unavailable."
        )

    if isinstance(e, VManageAPIError):
        return f"Error: {str(e)}"

    if isinstance(e, VManageError):
        return f"Error: {str(e)}"

    # Legacy httpx exceptions (in case they leak through)
    if isinstance(e, httpx.HTTPStatusError):
        status = e.response.status_code
        if status in (401, 302):
            return (
                "Error: Authentication failed or session expired. "
                "Check VMANAGE_USERNAME and VMANAGE_PASSWORD environment variables."
            )
        elif status == 403:
            return (
                "Error: Permission denied. The vManage user role may lack "
                "the required privilege level."
            )
        elif status == 404:
            return (
                "Error: Endpoint or resource not found. Use vmanage_list_devices "
                "to find valid device identifiers."
            )
        elif status == 429:
            return "Error: Rate limit exceeded on vManage. Wait a moment and retry."
        return f"Error: vManage API returned HTTP {status}."

    if isinstance(e, httpx.TimeoutException):
        return (
            "Error: Request to vManage timed out. Check that the vManage host "
            "is reachable and VMANAGE_HOST/VMANAGE_PORT are correct."
        )

    if isinstance(e, httpx.ConnectError):
        return (
            "Error: Could not connect to vManage. Verify VMANAGE_HOST and "
            "VMANAGE_PORT are correct and the instance is running. "
            "The DevNet sandbox may be temporarily unavailable."
        )

    return f"Error: Unexpected error: {type(e).__name__} -- {str(e)}"
