"""Inference-free runtime acceptance. Requires a locally built image and Docker."""

import argparse
import json
import subprocess
import tempfile
import time
import uuid
from pathlib import Path


def docker(*args, **kwargs):
    result = subprocess.run(["docker", *args], capture_output=True, text=True, **kwargs)
    if result.returncode:
        raise RuntimeError("fake container check failed: " + result.stderr)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    name = "gateway-check-" + uuid.uuid4().hex[:12]
    with tempfile.TemporaryDirectory(prefix="gateway-container-") as directory:
        root = Path(directory)
        root.chmod(0o755)
        (root / "reply.txt").write_text("container fixture")
        config = {
            "schema_version": 1,
            "project_id": "container-example",
            "mode": "fake",
            "providers": {"router": {"kind": "openrouter"}},
            "access": {"owner_only_ipc": True},
            "aliases": {
                "example": {
                    "provider_ref": "router",
                    "upstream_model": "synthetic",
                    "operation": "chat",
                    "capabilities": ["plain_text"],
                    "limits": {
                        "max_input_bytes": 1024,
                        "max_input_tokens": 4096,
                        "max_output_tokens": 32,
                        "attempt_timeout_seconds": 5,
                        "request_timeout_seconds": 10,
                        "max_concurrency": 1,
                    },
                }
            },
            "fake": {
                "fixture_root": "/config",
                "responses": {"example": {"default": ["reply.txt"]}},
            },
        }
        (root / "project.yaml").write_text(json.dumps(config))
        docker(
            "run",
            "-d",
            "--name",
            name,
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--tmpfs",
            "/state:uid=10001,gid=10001,mode=0700",
            "--tmpfs",
            "/run/gateway:uid=10001,gid=10001,mode=0700",
            "--mount",
            f"type=bind,src={root},dst=/config,readonly",
            args.image,
        )
        try:
            for _ in range(100):
                status = json.loads(docker("inspect", name).stdout)[0]["State"]
                if not status["Running"]:
                    raise RuntimeError("container startup failed: " + docker("logs", name).stdout)
                if "listening" in docker("logs", name).stdout:
                    break
                time.sleep(0.1)
            check = """
import os
from llm_gateway.client import GatewayClient
from llm_gateway.contracts import ChatInput, Message
assert os.getuid() == 10001
assert not os.path.exists('/var/run/docker.sock')
assert not os.path.exists('/app/src')
try:
    open('/app/forbidden', 'w')
except OSError as error:
    assert error.errno in {13, 30}
else:
    raise AssertionError('code was writable')
with GatewayClient('http://localhost', unix_socket='/run/gateway/gateway.sock') as gateway:
    assert gateway.ready().status == "ready"
    data = ChatInput(messages=[Message(role='user',content='public')])
    result = gateway.chat('example', data, request_id='container-check')
    assert result.content == 'container fixture'
    assert result.receipts[0].evidence_status == 'simulated'
print('container fake inference and isolation passed')
"""
            print(docker("exec", name, "python", "-I", "-c", check).stdout.strip())
            docker("stop", "--timeout", "10", name)
            state = json.loads(docker("inspect", name).stdout)[0]["State"]
            assert state["ExitCode"] == 0
            print(json.dumps({"image": args.image, "shutdown_exit_code": state["ExitCode"]}))
        finally:
            docker("rm", "-f", name)


if __name__ == "__main__":
    main()
