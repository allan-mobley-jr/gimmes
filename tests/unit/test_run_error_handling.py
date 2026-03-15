"""Tests for _run() error handling in CLI."""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest
from click.exceptions import Exit as ClickExit

from gimmes.cli import _run


def _make_response(status: int, *, json_data=None, text="") -> httpx.Response:
    """Build a fake httpx.Response for testing."""
    request = httpx.Request("GET", "https://api.example.com/test")
    if json_data is not None:
        return httpx.Response(status, json=json_data, request=request)
    return httpx.Response(status, text=text, request=request)


async def _raise(exc: Exception):
    raise exc


def _run_expecting_exit(exc: Exception) -> str:
    """Run _run() with an exception-raising coroutine, return the console output."""
    with patch("gimmes.cli.console") as mock_console:
        with pytest.raises(ClickExit) as exc_info:
            _run(_raise(exc))
    assert exc_info.value.exit_code == 1
    return mock_console.print.call_args[0][0]


class TestRunHTTPStatusError:
    def test_json_message_field(self) -> None:
        resp = _make_response(400, json_data={"message": "Insufficient balance"})
        exc = httpx.HTTPStatusError("error", request=resp.request, response=resp)
        output = _run_expecting_exit(exc)
        assert "400" in output
        assert "Insufficient balance" in output

    def test_json_error_field(self) -> None:
        resp = _make_response(401, json_data={"error": "Unauthorized"})
        exc = httpx.HTTPStatusError("error", request=resp.request, response=resp)
        output = _run_expecting_exit(exc)
        assert "401" in output
        assert "Unauthorized" in output

    def test_non_json_body(self) -> None:
        resp = _make_response(502, text="<html>Bad Gateway</html>")
        exc = httpx.HTTPStatusError("error", request=resp.request, response=resp)
        output = _run_expecting_exit(exc)
        assert "502" in output
        assert "Bad Gateway" in output

    def test_non_dict_json_body(self) -> None:
        resp = _make_response(422, json_data=["validation", "error"])
        exc = httpx.HTTPStatusError("error", request=resp.request, response=resp)
        output = _run_expecting_exit(exc)
        assert "422" in output

    def test_empty_body(self) -> None:
        resp = _make_response(500, text="")
        exc = httpx.HTTPStatusError("Server Error", request=resp.request, response=resp)
        output = _run_expecting_exit(exc)
        assert "500" in output


class TestRunTimeoutError:
    def test_timeout_exception(self) -> None:
        exc = httpx.ReadTimeout("Connection read timed out")
        output = _run_expecting_exit(exc)
        assert "timed out" in output.lower()


class TestRunExistingErrors:
    def test_connection_error_still_caught(self) -> None:
        exc = ConnectionError("Connection refused")
        output = _run_expecting_exit(exc)
        assert "Connection refused" in output
        assert "API error" not in output
