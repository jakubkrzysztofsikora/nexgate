"""Disposable local database/proxy fixtures. Never read the operator overlay."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import time
import uuid

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
IMAGE = "nexgate-litellm:a2a-test"
BASELINE_IMAGE = "nexgate-litellm:a2a-rollback-1.95.0"
MASTER_KEY = "sk-disposable-a2a-spike-only"


def docker(*args: str) -> str:
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
    return (result.stdout + result.stderr).strip() if args[0] == "logs" else result.stdout.strip()


@pytest.fixture(scope="session")
def pinned_image():
    if os.environ.get("NEXGATE_RUN_A2A_SPIKE") != "1":
        pytest.skip("real Docker A2A spike requires NEXGATE_RUN_A2A_SPIKE=1")
    build = subprocess.run(
        ["docker", "build", "--progress=plain", "-f", "runtime/Dockerfile.litellm", "-t", IMAGE, "."],
        cwd=ROOT, capture_output=True, text=True, timeout=1200,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    versions = docker("run", "--rm", "--entrypoint", "python", IMAGE, "-c",
        "from importlib.metadata import version; import litellm, a2a; "
        "assert version('litellm') == '1.100.1'; "
        "assert version('a2a-sdk') == '1.1.2'; "
        "assert version('fastapi') == '0.139.0'; print('verified')")
    assert versions == "verified"
    return IMAGE


class RealProxy:
    def __init__(self, container: str):
        self.container = container

    def request(self, path: str, *, key=MASTER_KEY, body=None):
        script = '''
import json, sys
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
path, key, body = json.loads(sys.argv[1])
headers = {"Content-Type": "application/json"}
if key is not None:
    headers["Authorization"] = "Bearer " + key
request = Request("http://127.0.0.1:4000" + path, headers=headers,
                  data=None if body is None else json.dumps(body).encode())
try:
    with urlopen(request, timeout=15) as response:
        payload = (response.read().decode() if "text/event-stream" in response.headers.get("Content-Type", "")
                   else json.load(response))
        print(json.dumps([response.status, payload]))
except HTTPError as error:
    print(json.dumps([error.code, json.loads(error.read())]))
except URLError:
    print(json.dumps([0, {}]))
'''
        return json.loads(docker("exec", self.container, "python", "-S", "-c", script,
                                 json.dumps([path, key, body])))


def completed_response(payload):
    """Read native ChatGPT's SSE completion as a real Responses client does."""
    if isinstance(payload, dict):
        response = payload
    else:
        events = [json.loads(line[5:].strip()) for line in payload.splitlines()
                  if line.startswith("data:") and line[5:].strip() != "[DONE]"]
        completed = [event["response"] for event in events if event.get("type") == "response.completed"]
        assert len(completed) == 1, "native Responses stream must complete exactly once"
        response = completed[0]
    assert response["status"] == "completed", response
    return response


def start_proxy(container, image, network, mounts, db):
    docker("run", "-d", "--name", container, "--network", network, *mounts,
           "-e", f"LITELLM_MASTER_KEY={MASTER_KEY}", "-e", "PYTHONPATH=/app",
           "-e", "LITELLM_LOCAL_MODEL_COST_MAP=True",
           "-e", "CHATGPT_API_BASE=http://agent:9000/v1",
           "-e", "CHATGPT_TOKEN_DIR=/app", "-e", "CHATGPT_AUTH_FILE=test-chatgpt-auth.json",
           "-e", f"DATABASE_URL=postgresql://spike:disposable-spike@{db}:5432/spike",
           image, "--config=/app/config.yaml", "--host=0.0.0.0", "--port=4000")
    client = RealProxy(container)
    for _ in range(120):
        if docker("inspect", "-f", "{{.State.Running}}", container) != "true":
            break
        status, _ = client.request("/health/liveliness", key=None)
        if status == 200:
            return client
        time.sleep(1)
    pytest.fail("real proxy failed startup:\n" + docker("logs", container))


@pytest.fixture(scope="session")
def real_proxy(tmp_path_factory, pinned_image):
    if os.environ.get("NEXGATE_RUN_A2A_SPIKE") != "1":
        pytest.skip("real Docker A2A spike requires NEXGATE_RUN_A2A_SPIKE=1")
    docker("run", "--rm", "--entrypoint", "python", BASELINE_IMAGE, "-c",
           "from importlib.metadata import version; assert version('litellm') == '1.95.0'")
    name = "nexgate-a2a-spike-" + uuid.uuid4().hex[:10]
    db, proxy, agent, baseline, redis = (name + suffix for suffix in ("-db", "-proxy", "-agent", "-baseline", "-redis"))
    render_root = tmp_path_factory.mktemp("a2a-rendered")
    for source in ("scripts/render-litellm-config.py", "runtime/config/litellm.yaml.tmpl"):
        target = render_root / source
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / source, target)
    overlay = render_root / "synthetic.env"
    overlay.write_text("\n".join([
        "NEXGATE_ENABLE_SUBSCRIPTION_ROUTES=true",
        "NEXGATE_CLAUDE_HAIKU_4_5_20251001_API_BASE=http://agent:9000",
        "CLAUDE_CODE_OAUTH_TOKEN=disposable-provider-key",
        f"LITELLM_MASTER_KEY={MASTER_KEY}",
        f"DATABASE_URL=postgresql://spike:disposable-spike@{db}:5432/spike",
    ]) + "\n")
    rendered = subprocess.run(
        [sys.executable, str(render_root / "scripts/render-litellm-config.py")],
        env={"PATH": os.environ["PATH"], "NEXGATE_ENV_FILE": str(overlay)},
        capture_output=True, text=True, timeout=30,
    )
    assert rendered.returncode == 0, rendered.stdout + rendered.stderr
    config_path = render_root / "runtime/state/litellm.yaml"
    auth_path = render_root / "test-chatgpt-auth.json"
    auth_path.write_text(json.dumps({"access_token": "disposable-chatgpt-token",
        "account_id": "disposable-account", "expires_at": time.time() + 3600}))
    # Prisma's startup toolchain may fetch npm packages. Use a dedicated bridge
    # with no published ports; no operator services or credentials are attached.
    docker("network", "create", name)
    try:
        docker("run", "-d", "--name", redis, "--network", name, "--network-alias", "redis",
               "redis:7-alpine", "redis-server", "--save", "", "--appendonly", "no")
        docker("run", "-d", "--name", agent, "--network", name,
               "--network-alias", "agent", "--entrypoint", "python",
               "-v", f"{ROOT / 'tests/integration/a2a_fixture_server.py'}:/fixture.py:ro",
               IMAGE, "/fixture.py")
        docker("run", "-d", "--name", db, "--network", name,
               "-e", "POSTGRES_USER=spike", "-e", "POSTGRES_DB=spike",
               "-e", "POSTGRES_PASSWORD=disposable-spike", "postgres:16-alpine")
        for _ in range(40):
            ready = subprocess.run(["docker", "exec", db, "pg_isready", "-U", "spike"],
                                   capture_output=True)
            if ready.returncode == 0:
                break
            time.sleep(1)
        else:
            pytest.fail("isolated Postgres did not become ready")
        mounts = ["-v", f"{config_path}:/app/config.yaml:ro",
                  "-v", f"{auth_path}:/app/test-chatgpt-auth.json:ro",
                  "-v", f"{ROOT / 'scripts/provision-research-agent.py'}:/provision.py:ro"]
        for filename in ("sitecustomize.py", "ccproxy_callback.py", "claude_aware_compression.py", "ccproxy.yaml"):
            mounts += ["-v", f"{ROOT / 'runtime/config' / filename}:/app/{filename}:ro"]
        old = start_proxy(baseline, BASELINE_IMAGE, name, mounts, db)
        status, marker = old.request("/key/generate", body={"key_alias": "migration-marker"})
        assert status == 200, {"status": status}
        docker("stop", baseline)
        client = start_proxy(proxy, IMAGE, name, mounts, db)
        status, _ = client.request("/v1/models", key=marker["key"])
        assert status == 200
        marker_count = docker("exec", db, "psql", "-U", "spike", "-d", "spike", "-Atc",
            'SELECT count(*) FROM "LiteLLM_VerificationToken" WHERE key_alias = \'migration-marker\'')
        assert marker_count == "1", "upgrade lost the prior-version key"
        client.agent_id = docker("exec", proxy, "python", "-S", "/provision.py",
            "--gateway", "http://127.0.0.1:4000",
            "--card-url", "http://agent:9000/.well-known/agent-card.json")
        client.db = db
        client.network = name
        client.mounts = mounts
        client.baseline = baseline
        client.marker_key = marker["key"]
        client.rendered_config = config_path
        client.redis = redis
        yield client
    finally:
        for container in (proxy, baseline, db, agent, redis):
            subprocess.run(["docker", "rm", "-f", "-v", container], capture_output=True)
        subprocess.run(["docker", "network", "rm", name], capture_output=True)
