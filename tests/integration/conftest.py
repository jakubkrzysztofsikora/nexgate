"""Disposable local database/proxy fixtures. Never read the operator overlay."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
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


def start_proxy(container, image, network, mounts, db):
    docker("run", "-d", "--name", container, "--network", network, *mounts,
           "-e", f"LITELLM_MASTER_KEY={MASTER_KEY}", "-e", "PYTHONPATH=/app",
           "-e", "LITELLM_LOCAL_MODEL_COST_MAP=True",
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
    db, proxy, agent, baseline = name + "-db", name + "-proxy", name + "-agent", name + "-baseline"
    config_path = tmp_path_factory.mktemp("a2a") / "config.yaml"
    config_path.write_text(yaml.safe_dump({
        "model_list": [
            {"model_name": "spike-claude", "litellm_params": {
                "model": "anthropic/claude-haiku-4-5", "api_base": "http://agent:9000",
                "api_key": "disposable-provider-key"}},
            {"model_name": "spike-codex", "litellm_params": {
                "model": "openai/gpt-5.2", "api_base": "http://agent:9000/v1",
                "api_key": "disposable-provider-key"}},
        ],
        "litellm_settings": {"callbacks": [
            "claude_aware_compression.claude_aware_compression",
            "ccproxy_callback.ccproxy_handler",
        ]},
        "general_settings": {
            "master_key": "os.environ/LITELLM_MASTER_KEY",
            "database_url": "os.environ/DATABASE_URL",
        },
    }))
    # Prisma's startup toolchain may fetch npm packages. Use a dedicated bridge
    # with no published ports; no operator services or credentials are attached.
    docker("network", "create", name)
    try:
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
        yield client
    finally:
        for container in (proxy, baseline, db, agent):
            subprocess.run(["docker", "rm", "-f", "-v", container], capture_output=True)
        subprocess.run(["docker", "network", "rm", name], capture_output=True)
