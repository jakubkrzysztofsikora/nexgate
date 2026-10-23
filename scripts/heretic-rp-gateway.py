#!/usr/bin/env python3
"""heretic-rp-gateway — OpenAI-compatible facade over a RunPod serverless
worker-ollama endpoint (qwen38-heretic-l4).

Why this exists: the RunPod serverless job API speaks Ollama-native payloads
({"input": {"messages": [...]}}), not OpenAI chat completions, and the sync
routes sit behind a ~100s edge timeout that a cold L4 worker (model download
+ GGUF registration) blows past. This shim:

  POST /v1/chat/completions  -> POST api.runpod.ai/v2/<id>/run  (async)
                             -> poll /status/<job> every 2s (non-stream)
                                or drain /stream/<job> snapshots (stream:true)
                             -> translate Ollama chunks/responses to OpenAI shape

Endpoints: /v1/chat/completions, /v1/models, /health.
Stdlib only (http.client) — no pip deps.

Env:
  RUNPOD_API_KEY              (required)
  RUNPOD_HERETIC_ENDPOINT_ID  (required)
  PORT            default 8311
  POLL_INTERVAL_S default 2.0
"""
import http.client
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

API_KEY = os.environ.get("RUNPOD_API_KEY", "")
ENDPOINT_ID = os.environ.get("RUNPOD_HERETIC_ENDPOINT_ID", "")
PORT = int(os.environ.get("PORT", "8311"))
POLL = float(os.environ.get("POLL_INTERVAL_S", "2.0"))
MODEL_LABEL = os.environ.get("HERETIC_MODEL_LABEL", "qwen38-heretic-rp")

RUNPOD_HOST = "api.runpod.ai"


def normalize_content(content):
    """Flatten OpenAI content-parts arrays to a plain string.

    Ollama's /api/chat rejects array content ("cannot unmarshal array into Go
    struct field ChatRequest.messages.content of type string"), and modern
    OpenAI clients (e.g. pi) send content as [{"type": "text", "text": ...}].
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and "text" in part:
                parts.append(part.get("text") or "")
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(p for p in parts if p)
    if content is None:
        return ""
    return str(content)


def rp_request(method, path, body=None, timeout=60):
    conn = http.client.HTTPSConnection(RUNPOD_HOST, timeout=timeout)
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    payload = json.dumps(body) if body is not None else None
    conn.request(method, path, body=payload, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    return resp.status, json.loads(raw) if raw else {}


def ollama_tool_calls_to_openai(tool_calls, job_id=""):
    """Map Ollama's message.tool_calls to the OpenAI tool_calls shape."""
    calls = []
    for i, tc in enumerate(tool_calls or []):
        fn = (tc or {}).get("function") or {}
        args = fn.get("arguments")
        if not isinstance(args, str):
            args = json.dumps(args if args is not None else {})
        calls.append({
            "id": (tc or {}).get("id") or f"call_{job_id[:8] or 'local'}_{i}",
            "type": "function",
            "function": {"name": fn.get("name") or "", "arguments": args},
        })
    return calls


def cancel_job(job_id):
    """Best-effort cancel so an abandoned request stops billing the worker."""
    try:
        rp_request("POST", f"/v2/{ENDPOINT_ID}/cancel/{job_id}", timeout=15)
    except Exception:
        pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print(f"[gateway] {self.address_string()} {fmt % args}", flush=True)

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _configured(self):
        return bool(API_KEY and ENDPOINT_ID)

    def do_GET(self):
        if self.path == "/health":
            ok = self._configured()
            self._json(200 if ok else 503, {"status": "ok" if ok else "misconfigured",
                                            "endpoint_id": ENDPOINT_ID or None})
        elif self.path == "/v1/models":
            self._json(200, {"object": "list", "data": [
                {"id": MODEL_LABEL, "object": "model", "owned_by": "runpod"}]})
        else:
            self._json(404, {"error": {"message": f"no route {self.path}"}})

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self._json(404, {"error": {"message": f"no route {self.path}"}})
            return
        if not self._configured():
            self._json(503, {"error": {"message": "gateway misconfigured (RUNPOD_API_KEY / RUNPOD_HERETIC_ENDPOINT_ID)"}})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"error": {"message": "invalid JSON body"}})
            return
        if not isinstance(req, dict):
            self._json(400, {"error": {"message": "request body must be a JSON object"}})
            return

        messages = req.get("messages")
        if not messages:
            self._json(400, {"error": {"message": "messages[] required"}})
            return
        messages = [{"role": m.get("role"), "content": normalize_content(m.get("content"))}
                    for m in messages if isinstance(m, dict)]

        stream = bool(req.get("stream", False))
        print(f"[gateway] POST messages={len(messages)} stream={stream} "
              f"tools={'tools' in req} keys={sorted(req.keys())}", flush=True)
        job_input = {"messages": messages, "stream": stream}
        if req.get("tools"):
            # worker-ollama forwards this to Ollama's /api/chat; tool_calls come
            # back on message.tool_calls and are mapped to the OpenAI shape.
            job_input["tools"] = req["tools"]
        options = {}
        if "temperature" in req:
            options["temperature"] = req["temperature"]
        if "top_p" in req:
            options["top_p"] = req["top_p"]
        if req.get("max_tokens"):
            options["num_predict"] = req["max_tokens"]
        if options:
            job_input["options"] = options

        status, resp = rp_request("POST", f"/v2/{ENDPOINT_ID}/run", {"input": job_input})
        if status not in (200, 201) or "id" not in resp:
            self._json(502, {"error": {"message": f"runpod /run failed: {status} {resp}"}})
            return
        job_id = resp["id"]

        if stream:
            self._serve_stream(job_id)
        else:
            self._serve_poll(job_id)

    def _openai_response(self, ollama, job_id):
        # worker-ollama returns its output as a list of responses; unwrap it.
        if isinstance(ollama, list):
            ollama = ollama[0] if ollama else {}
        message = ollama.get("message") or {}
        content = message.get("content", "")
        tool_calls = ollama_tool_calls_to_openai(message.get("tool_calls"), job_id)
        done_reason = ollama.get("done_reason") or "stop"
        if tool_calls:
            finish = "tool_calls"
        else:
            finish = "length" if done_reason == "length" else "stop"
        openai_message = {"role": "assistant", "content": content}
        if tool_calls:
            openai_message["tool_calls"] = tool_calls
        prompt_tokens = ollama.get("prompt_eval_count") or 0
        completion_tokens = ollama.get("eval_count") or 0
        return {
            "id": f"chatcmpl-{job_id[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": MODEL_LABEL,
            "choices": [{
                "index": 0,
                "message": openai_message,
                "finish_reason": finish,
            }],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }

    def _serve_poll(self, job_id):
        deadline = time.time() + 55 * 60  # generous: cold start + long generations
        terminal = False
        try:
            while time.time() < deadline:
                try:
                    status, resp = rp_request("GET", f"/v2/{ENDPOINT_ID}/status/{job_id}")
                except (OSError, ValueError, http.client.HTTPException):
                    time.sleep(POLL)  # transient transport/parse error: keep polling
                    continue
                state = resp.get("status")
                if state == "COMPLETED":
                    terminal = True
                    out = resp.get("output") or {}
                    if isinstance(out, list):
                        out = out[0] if out else {}
                    if isinstance(out, dict) and out.get("error"):
                        self._json(502, {"error": {"message": f"worker error: {out['error']}"}})
                    else:
                        self._json(200, self._openai_response(out, job_id))
                    return
                if state in ("FAILED", "CANCELLED", "TIMED_OUT"):
                    terminal = True
                    self._json(502, {"error": {"message": f"job {state}: {resp.get('error') or resp}"}})
                    return
                time.sleep(POLL)
            self._json(504, {"error": {"message": f"job {job_id} did not finish in time"}})
        finally:
            if not terminal:
                # client gone, timeout, or an unhandled error: stop the paid job
                cancel_job(job_id)

    def _serve_stream(self, job_id):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        def sse(obj):
            self.wfile.write(f"data: {json.dumps(obj)}\n\n".encode())
            self.wfile.flush()

        def chunk(delta, finish=None):
            return {
                "id": f"chatcmpl-{job_id[:12]}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": MODEL_LABEL,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }

        done_sent = False
        last_finish = "stop"

        def emit_ollama(obj):
            nonlocal done_sent, last_finish
            if not isinstance(obj, dict) or "message" not in obj:
                return
            message = obj.get("message") or {}
            content = message.get("content") or ""
            if content:
                sse(chunk({"content": content}))
            tool_calls = ollama_tool_calls_to_openai(message.get("tool_calls"), job_id)
            if tool_calls:
                last_finish = "tool_calls"
                sse(chunk({"tool_calls": [
                    {"index": i, "id": c["id"], "type": "function",
                     "function": c["function"]} for i, c in enumerate(tool_calls)]}))
            if obj.get("done") and not done_sent:
                sse(chunk({}, finish=last_finish))
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                done_sent = True

        def unwrap(item):
            # worker-ollama chunks arrive as {"output": "<json string>"}.
            if isinstance(item, dict) and isinstance(item.get("output"), str):
                try:
                    return json.loads(item["output"])
                except json.JSONDecodeError:
                    return None
            return item

        # RunPod's /stream is a draining snapshot endpoint: each GET returns the
        # chunks buffered since the last read (one JSON object, not a live SSE
        # feed), so poll it until the job reaches a terminal status.
        deadline = time.time() + 55 * 60
        prev_status = None
        terminal = False
        try:
            while time.time() < deadline:
                try:
                    code, evt = rp_request("GET", f"/v2/{ENDPOINT_ID}/stream/{job_id}")
                except (OSError, ValueError, http.client.HTTPException):
                    time.sleep(POLL)  # transient transport/parse error: keep polling
                    continue
                if code != 200:
                    sse({"error": {"message": f"stream read failed: {code} {evt}"}})
                    break
                if not isinstance(evt, dict):
                    break
                status = evt.get("status")
                if status != prev_status:
                    print(f"[gateway] stream job={job_id} status={status} "
                          f"items={len(evt.get('stream') or [])}", flush=True)
                    prev_status = status
                for item in evt.get("stream") or []:
                    emit_ollama(unwrap(item))
                if status in ("FAILED", "CANCELLED", "TIMED_OUT"):
                    terminal = True
                    sse({"error": {"message": f"job {status}"}})
                    break
                if status == "COMPLETED":
                    terminal = True
                    break
                time.sleep(POLL)
            if not done_sent:
                if not terminal:
                    # deadline hit or the read broke: surface an error rather
                    # than a clean-looking truncated answer.
                    sse({"error": {"message": f"stream ended before completion (job {job_id})"}})
                else:
                    sse(chunk({}, finish=last_finish))
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                    done_sent = True
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            if not terminal:
                # client gone, timeout, or an unhandled error: stop the paid job
                cancel_job(job_id)


def main():
    if not (API_KEY and ENDPOINT_ID):
        print("[gateway] WARNING: RUNPOD_API_KEY / RUNPOD_HERETIC_ENDPOINT_ID not set; /health reports misconfigured", flush=True)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"[gateway] listening on 127.0.0.1:{PORT} -> runpod endpoint {ENDPOINT_ID or '<unset>'}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
