"""Controlled HTTP agent; the gateway under test remains real LiteLLM.

Serialize and validate with the installed 1.1.2 SDK's v0.3 compatibility API,
which is also the API imported by LiteLLM 1.100.1's A2A implementation.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json

from a2a.compat.v0_3.types import (
    AgentCard, AgentCapabilities, AgentSkill, Artifact, DataPart, Message,
    Part, Task, TaskNotFoundError, TaskState, TaskStatus,
)


CARD = AgentCard(
    name="quick-research", description="Disposable compatibility probe",
    url="http://agent:9000/", version="1.0.0", protocolVersion="0.3.0",
    capabilities=AgentCapabilities(streaming=False),
    defaultInputModes=["application/json"], defaultOutputModes=["application/json"],
    skills=[AgentSkill(id="probe", name="probe", description="Probe", tags=["probe"])],
)
TASKS = {}


def dump(model):
    return model.model_dump(mode="json", by_alias=True, exclude_none=True)


class Handler(BaseHTTPRequestHandler):
    def reply(self, body, status=200):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.reply(dump(CARD))

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path.endswith("/messages"):
            message = {"id": "msg_spike", "type": "message", "role": "assistant",
                       "model": "claude-haiku-4-5", "content": [], "stop_reason": None,
                       "stop_sequence": None, "usage": {"input_tokens": 5, "output_tokens": 0}}
            if request.get("stream"):
                events = [
                    {"type": "message_start", "message": message},
                    {"type": "content_block_start", "index": 0,
                     "content_block": {"type": "text", "text": ""}},
                    {"type": "content_block_delta", "index": 0,
                     "delta": {"type": "text_delta", "text": "spike"}},
                    {"type": "content_block_stop", "index": 0},
                    {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                     "usage": {"output_tokens": 1}},
                    {"type": "message_stop"},
                ]
                if request.get("tools"):
                    events[4:4] = [
                        {"type": "content_block_start", "index": 1,
                         "content_block": {"type": "tool_use", "id": "tool_spike", "name": "lookup", "input": {}}},
                        {"type": "content_block_delta", "index": 1,
                         "delta": {"type": "input_json_delta", "partial_json": '{"query":"spike"}'}},
                        {"type": "content_block_stop", "index": 1},
                    ]
                    events[-2]["delta"]["stop_reason"] = "tool_use"
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for event in events:
                    self.wfile.write(("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n").encode())
                self.wfile.flush()
                return
            message.update(content=[{"type": "text", "text": str(request["max_tokens"])}],
                           stop_reason="end_turn", usage={"input_tokens": 5, "output_tokens": 1})
            self.reply(message)
            return
        if self.path.endswith("/responses"):
            self.reply({"id": "resp_spike", "object": "response", "created_at": 1,
                        "status": "completed", "error": None, "incomplete_details": None,
                        "model": "gpt-5.2", "output": [{"id": "msg_spike", "type": "message",
                            "role": "assistant", "status": "completed", "content": [
                                {"type": "output_text", "text": "spike", "annotations": []}]}],
                        "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
                        "usage": {"input_tokens": 5, "output_tokens": 1, "total_tokens": 6}})
            return
        response = {"jsonrpc": "2.0", "id": request["id"]}
        if request["method"] == "message/send":
            message = Message.model_validate(request["params"]["message"])
            part = Part(root=DataPart(data={"query": "spike"}))
            task = Task(
                id="spike-task", contextId="spike-context",
                status=TaskStatus(state=TaskState.completed), history=[message],
                artifacts=[Artifact(artifactId="spike-artifact", parts=[part])],
            )
            TASKS[task.id] = task
            response["result"] = dump(task)
        elif request["method"] == "tasks/get":
            task = TASKS.get(request["params"]["id"])
            response["result" if task else "error"] = dump(task or TaskNotFoundError())
        else:
            response["error"] = {"code": -32601, "message": "Method not found"}
        self.reply(response)


ThreadingHTTPServer(("0.0.0.0", 9000), Handler).serve_forever()
