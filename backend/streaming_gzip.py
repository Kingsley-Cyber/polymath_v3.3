"""GZip middleware that never compresses streaming SSE responses.

FastAPI's stock ``GZipMiddleware`` buffers a response so it can compress it. For
a Server-Sent-Events stream (``text/event-stream``) that is fatal: every event
is held in the gzip buffer and delivered as a single blob when the stream
closes, so the live trace + answer "pop in" all at once at the end instead of
streaming. (Browsers send ``Accept-Encoding: gzip`` by default, so this hit
every real user even though a header-less curl/urllib probe streamed fine.)

This subclass compresses normal JSON exactly like the stock middleware — the hot
graph/corpora/batch payloads still gzip 5-10× — but leaves ``text/event-stream``
responses uncompressed so each event flushes immediately.

Pinned to Starlette's GZipResponder internals (0.27.x); the override calls
``super()`` so a future Starlette change degrades to plain gzip rather than
breaking the response.
"""
from starlette.datastructures import Headers
from starlette.middleware.gzip import GZipResponder
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class _StreamSafeGZipResponder(GZipResponder):
    async def send_with_gzip(self, message: Message) -> None:
        if message["type"] == "http.response.start":
            # Let the parent stash the initial message and detect any existing
            # Content-Encoding, then force passthrough for SSE. GZipResponder
            # treats ``content_encoding_set`` as "already encoded" and streams
            # the body untouched — exactly what an event stream needs.
            await super().send_with_gzip(message)
            content_type = Headers(raw=message["headers"]).get("content-type", "")
            if content_type.startswith("text/event-stream"):
                self.content_encoding_set = True
            return
        await super().send_with_gzip(message)


class StreamSafeGZipMiddleware:
    def __init__(
        self, app: ASGIApp, minimum_size: int = 500, compresslevel: int = 9
    ) -> None:
        self.app = app
        self.minimum_size = minimum_size
        self.compresslevel = compresslevel

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            if "gzip" in Headers(scope=scope).get("Accept-Encoding", ""):
                responder = _StreamSafeGZipResponder(
                    self.app, self.minimum_size, compresslevel=self.compresslevel
                )
                await responder(scope, receive, send)
                return
        await self.app(scope, receive, send)
