import os
import json
from typing import Dict

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse
import httpx

# Let's do it quick and dirty with streaming ... just for fun.
UPSTREAM_BASE_URL = os.getenv("UPSTREAM_BASE_URL", "https://any.host.exposing.chat.completion.api/v1")
UPSTREAM_API_KEY = os.getenv("UPSTREAM_API_KEY")

PROXY_HOST = os.getenv("PROXY_HOST", "0.0.0.0")
PROXY_PORT = int(os.getenv("PROXY_PORT", "8000"))

# yep
app = FastAPI()
client = httpx.AsyncClient(timeout=None)


def _filtered_headers_for_upstream(original_headers: Dict[str, str]) -> Dict[str, str]:
    hop_by_hop = {
        "host",
        "content-length",
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "accept-encoding",
    }

    new_headers = {}
    for k, v in original_headers.items():
        lk = k.lower()
        if lk in hop_by_hop:
            continue
        new_headers[k] = v

    return new_headers


def _filtered_headers_for_client(upstream_headers: Dict[str, str]) -> Dict[str, str]:
    hop_by_hop = {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "content-encoding",
    }

    new_headers = {}
    for k, v in upstream_headers.items():
        lk = k.lower()
        if lk in hop_by_hop:
            continue
        new_headers[k] = v

    return new_headers



# regular entry point /v1/chat/completions
@app.post("/v1/chat/completions")
async def proxy_chat_completions(request: Request):
    """
    Proxy for the OpenAI /v1/chat/completions endpoint.

    - Accepts the same JSON body as OpenAI.
    - Forwards it to UPSTREAM_BASE_URL + "/chat/completions".
    - If "stream": true, we stream to the client.
    - Otherwise, we return the JSON response.
    """

    body = await request.body()
    # It's cool to have access to the body, so you can tokenize the content and
    # count the tokens, ain't ya?
    print(f"DEBUG body = {body}")

    # Decide if this is a streaming request by inspecting the JSON body
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception:
        payload = {}

    is_stream = bool(payload.get("stream"))

    upstream_headers = _filtered_headers_for_upstream(dict(request.headers))

    upstream_url = f"{UPSTREAM_BASE_URL.rstrip('/')}/chat/completions"
    print(f"DEBUG url = {upstream_url}")
    print(f"DEBUG headers = {upstream_headers}")

    if is_stream:
        # Streaming path: keep the httpx stream open for the duration of iteration
        async def event_generator():
            try:
                async with client.stream(
                    "POST",
                    upstream_url,
                    headers=upstream_headers,
                    content=body,
                ) as r:
                    # We assume upstream uses text/event-stream for streaming
                    async for chunk in r.aiter_bytes():
                        if chunk:
                            # Pass through raw bytes (including "data: ...\\n\\n")
                            yield chunk
            except httpx.StreamClosed:
                # Upstream closed the stream; treat as normal end-of-stream
                print("DEBUG: upstream stream closed")
            except Exception as e:
                # Avoid crashing the ASGI app on client disconnect or other issues
                print(f"DEBUG: error while streaming from upstream: {e}")

        return StreamingResponse(
            event_generator(),
            status_code=200,  # will effectively be OK for streaming
            media_type="text/event-stream",
        )

    # Non-streaming path: simple request/response… boring!
    async with client.stream(
        "POST",
        upstream_url,
        headers=upstream_headers,
        content=body,
    ) as r:
        content_type = r.headers.get("content-type", "")
        client_headers = _filtered_headers_for_client(r.headers)
        resp_body = await r.aread()

        return Response(
            content=resp_body,
            status_code=r.status_code,
            headers=client_headers,
            media_type=content_type or None,
        )

# rocket launcher
if __name__ == "__main__":
    uvicorn.run(app, host=PROXY_HOST, port=PROXY_PORT)