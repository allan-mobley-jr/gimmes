"""FastAPI server for the GIMMES Clubhouse dashboard."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from gimmes.clubhouse import data
from gimmes.clubhouse.models import (
    ActivityItem,
    CandidateItem,
    ConfigResponse,
    ErrorItem,
    MetricsResponse,
    PortfolioResponse,
    PositionItem,
    RecommendationItem,
    RiskResponse,
    StatusResponse,
    TradeItem,
)
from gimmes.config import GIMMES_HOME

DEFAULT_PORT = 1919
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"

app = FastAPI(title="GIMMES Clubhouse", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Resolved at startup
_db_path: Path = GIMMES_HOME / "gimmes.db"
_pause_seconds: int = 0


def set_db_path(path: Path) -> None:
    global _db_path
    _db_path = path


def set_pause_seconds(seconds: int) -> None:
    global _pause_seconds
    _pause_seconds = seconds


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "clubhouse.html")


# ---------------------------------------------------------------------------
# REST API
# ---------------------------------------------------------------------------


@app.get("/api/status")
async def api_status() -> StatusResponse:
    return await data.get_status(_db_path, _pause_seconds)


@app.get("/api/portfolio")
async def api_portfolio() -> PortfolioResponse:
    return await data.get_portfolio(_db_path)


@app.get("/api/positions")
async def api_positions() -> list[PositionItem]:
    return await data.get_positions(_db_path)


@app.get("/api/trades")
async def api_trades() -> list[TradeItem]:
    return await data.get_trades(_db_path)


@app.get("/api/candidates")
async def api_candidates() -> list[CandidateItem]:
    return await data.get_candidates(_db_path)


@app.get("/api/metrics")
async def api_metrics() -> MetricsResponse:
    return await data.get_metrics(_db_path)


@app.get("/api/risk")
async def api_risk() -> RiskResponse:
    return await data.get_risk(_db_path)


@app.get("/api/activity")
async def api_activity() -> list[ActivityItem]:
    return await data.get_activity(_db_path)


@app.get("/api/errors")
async def api_errors() -> list[ErrorItem]:
    return await data.get_errors_data(_db_path)


@app.get("/api/recommendations")
async def api_recommendations() -> list[RecommendationItem]:
    return await data.get_recommendations_data(_db_path)


@app.get("/api/config")
async def api_config() -> ConfigResponse:
    return await data.get_config_data()


# ---------------------------------------------------------------------------
# SSE stream
# ---------------------------------------------------------------------------


@app.get("/api/stream")
async def api_stream() -> StreamingResponse:
    async def event_generator():
        import logging

        sse_logger = logging.getLogger("gimmes.clubhouse.sse")
        last_fingerprint = ""
        while True:
            try:
                fp = await data.get_change_fingerprint(_db_path)
                if fp != last_fingerprint:
                    last_fingerprint = fp
                    # Gather all data for the update
                    status = await data.get_status(_db_path, _pause_seconds)
                    portfolio = await data.get_portfolio(_db_path)
                    positions = await data.get_positions(_db_path)
                    risk = await data.get_risk(_db_path)
                    activity = await data.get_activity(_db_path, limit=20)
                    trades = await data.get_trades(_db_path, limit=20)
                    candidates = await data.get_candidates(_db_path, limit=10)
                    metrics = await data.get_metrics(_db_path)
                    errors = await data.get_errors_data(_db_path, limit=10)
                    recs = await data.get_recommendations_data(_db_path, limit=10)

                    payload = json.dumps({
                        "status": status.model_dump(),
                        "portfolio": portfolio.model_dump(),
                        "positions": [p.model_dump() for p in positions],
                        "risk": risk.model_dump(),
                        "activity": [a.model_dump() for a in activity],
                        "trades": [t.model_dump() for t in trades],
                        "candidates": [c.model_dump() for c in candidates],
                        "metrics": metrics.model_dump(),
                        "errors": [e.model_dump() for e in errors],
                        "recommendations": [r.model_dump() for r in recs],
                    })

                    yield f"data: {payload}\n\n"
            except asyncio.CancelledError:
                sse_logger.info("SSE stream cancelled (client disconnected)")
                raise
            except ConnectionResetError:
                sse_logger.info("SSE client disconnected")
                return
            except Exception:
                sse_logger.warning("SSE event generation failed", exc_info=True)

            await asyncio.sleep(2)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Server lifecycle helpers
# ---------------------------------------------------------------------------


def _find_port(start: int = DEFAULT_PORT, max_tries: int = 11) -> int | None:
    """Find an available port starting from `start`."""
    for offset in range(max_tries):
        port = start + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return None


def run_standalone(
    port: int = DEFAULT_PORT,
    db_path: Path | None = None,
    open_browser: bool = True,
) -> None:
    """Run the dashboard server in the foreground (blocking)."""
    import os
    import signal
    import webbrowser

    import uvicorn

    if db_path:
        set_db_path(db_path)

    actual_port = _find_port(port)
    if actual_port is None:
        raise RuntimeError(f"No available port found (tried {port}-{port + 10})")

    if actual_port != port:
        print(f"Port {port} in use, using {actual_port}")

    url = f"http://127.0.0.1:{actual_port}"
    print(f"\n  GIMMES Clubhouse: {url}\n")

    if open_browser:
        def _open_browser() -> None:
            try:
                webbrowser.open(url)
            except Exception:
                print("  (Could not auto-open browser. Open the URL above manually.)")

        timer = threading.Timer(1.0, _open_browser)
        timer.daemon = True
        timer.start()

    def _handle_sigint(*_: object) -> None:
        print("\n  Clubhouse stopped.")
        os._exit(0)

    signal.signal(signal.SIGINT, _handle_sigint)

    uvicorn.run(app, host="127.0.0.1", port=actual_port, log_level="warning")


def start_background(
    port: int = DEFAULT_PORT,
    db_path: Path | None = None,
    pause_seconds: int = 0,
) -> int | None:
    """Start the dashboard as a daemon thread. Returns the port or None on failure."""
    import uvicorn

    if db_path:
        set_db_path(db_path)
    set_pause_seconds(pause_seconds)

    actual_port = _find_port(port)
    if actual_port is None:
        return None

    config = uvicorn.Config(
        app, host="127.0.0.1", port=actual_port, log_level="warning",
    )
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    return actual_port
