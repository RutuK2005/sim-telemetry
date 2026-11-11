"""FastAPI app that pushes live telemetry to a browser dashboard over a WebSocket."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from importlib.resources import files
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from .logger import frame_to_row
from .reader import TelemetryReader
from .stats import RollingStats
from .transport import open_transport


class LiveFeed:
    """Bridges the reader thread and the asyncio world of the web server.

    Every ``push_interval`` seconds it drains the reader's queue, updates the
    rolling stats and sends one JSON message to every connected browser.
    Batching (instead of one message per frame) keeps the browser responsive.
    """

    def __init__(self, reader: TelemetryReader, push_interval: float = 0.1):
        self.reader = reader
        self.push_interval = push_interval
        self.stats = RollingStats()
        self.clients: set[WebSocket] = set()
        self.messages_sent = 0

    def build_message(self) -> dict[str, Any]:
        frames = self.reader.drain()
        for frame in frames:
            self.stats.update(frame)
        return {
            "samples": [frame_to_row(f) for f in frames],
            "stats": asdict(self.stats.snapshot()),
            "link": self.reader.health(),
        }

    async def run(self) -> None:
        while True:
            message = self.build_message()
            for ws in list(self.clients):
                try:
                    await ws.send_json(message)
                except Exception:  # the browser went away mid-send
                    self.clients.discard(ws)
            self.messages_sent += 1
            await asyncio.sleep(self.push_interval)


def create_app(
    source: str = "sim", rate_hz: float = 50.0, noise: float = 0.0, push_interval: float = 0.1
) -> FastAPI:
    """Build the dashboard app; the transport and reader start with the app's lifespan."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        transport = open_transport(source, rate_hz=rate_hz, noise=noise)
        reader = TelemetryReader(transport)
        reader.start()
        feed = LiveFeed(reader, push_interval)
        app.state.feed = feed
        task = asyncio.create_task(feed.run())
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            reader.stop()
            transport.close()

    app = FastAPI(title="sim-telemetry", lifespan=lifespan)
    index_html = (files("sim_telemetry") / "static" / "index.html").read_text(encoding="utf-8")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return index_html

    @app.get("/api/stats")
    async def stats() -> dict[str, Any]:
        feed: LiveFeed = app.state.feed
        return {
            "source": source,
            "stats": asdict(feed.stats.snapshot()),
            "link": feed.reader.health(),
            "clients": len(feed.clients),
        }

    @app.websocket("/ws")
    async def websocket(ws: WebSocket) -> None:
        feed: LiveFeed = app.state.feed
        await ws.accept()
        feed.clients.add(ws)
        try:
            while True:
                # We never expect messages from the browser; this just waits for disconnect.
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            feed.clients.discard(ws)

    return app
