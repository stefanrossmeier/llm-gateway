"""Two synthetic Docker stacks; public metadata GET only, never inference."""

import argparse
import json
import tempfile
import time
import uuid
from pathlib import Path

from check_container import docker


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    prefix = "gateway-egress-" + uuid.uuid4().hex[:10]
    containers, networks = [], []
    with tempfile.TemporaryDirectory(prefix="gateway-egress-") as directory:
        try:
            for project in ("a", "b"):
                network, proxy, gateway = (
                    f"{prefix}-{project}-{kind}" for kind in ("net", "proxy", "gateway")
                )
                docker("network", "create", "--internal", network)
                networks.append(network)
                docker(
                    "run",
                    "-d",
                    "--name",
                    proxy,
                    "--read-only",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges",
                    "--entrypoint",
                    "python",
                    args.image,
                    "-I",
                    "-m",
                    "llm_gateway.service.egress_proxy",
                )
                containers.append(proxy)
                docker("network", "connect", "--alias", "egress", network, proxy)
                root = Path(directory) / project
                root.mkdir(mode=0o755)
                Path(directory).chmod(0o755)
                (root / "reply.txt").write_text("project-" + project)
                config = {
                    "schema_version": 1,
                    "project_id": "project-" + project,
                    "mode": "fake",
                    "providers": {"router": {"kind": "openrouter"}},
                    "access": {"owner_only_ipc": True},
                    "aliases": {
                        "shared-name": {
                            "provider_ref": "router",
                            "upstream_model": "synthetic-" + project,
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
                        "responses": {"shared-name": {"default": ["reply.txt"]}},
                    },
                }
                (root / "project.yaml").write_text(json.dumps(config))
                docker(
                    "run",
                    "-d",
                    "--name",
                    gateway,
                    "--network",
                    network,
                    "--read-only",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges",
                    "--env",
                    "PROJECT_MARKER=" + project,
                    "--tmpfs",
                    "/state:uid=10001,gid=10001,mode=0700",
                    "--tmpfs",
                    "/run/gateway:uid=10001,gid=10001,mode=0700",
                    "--mount",
                    f"type=bind,src={root},dst=/config,readonly",
                    args.image,
                    "serve",
                    "--project-config",
                    "/config/project.yaml",
                    "--state-dir",
                    "/state",
                    "--unix-socket",
                    "/run/gateway/gateway.sock",
                    "--egress-proxy",
                    "http://egress:3128",
                )
                containers.append(gateway)
                for _ in range(100):
                    if "listening" in docker("logs", gateway).stdout:
                        break
                    time.sleep(0.1)
                check = """
import os, socket, httpx
from llm_gateway.client import GatewayClient
from llm_gateway.contracts import ChatInput, Message
project = os.environ['PROJECT_MARKER']
with GatewayClient('http://localhost', unix_socket='/run/gateway/gateway.sock') as client:
    assert client.ready().project_id == 'project-' + project
    answer = client.chat('shared-name', ChatInput(messages=[Message(role='user',content='public')]),
                         request_id='same-id')
    assert answer.content == 'project-' + project
    assert answer.model.configured_model == 'synthetic-' + project
    assert client.usage().attempts == 1
sock = socket.socket()
sock.settimeout(2)
try:
    sock.connect(('1.1.1.1', 443))
except OSError:
    pass
else:
    raise AssertionError('gateway had direct internet access')
finally:
    sock.close()
with httpx.Client(proxy='http://egress:3128', trust_env=False, timeout=15) as http:
    # Unauthenticated provider metadata GET has no inference/charge semantics.
    assert http.get('https://api.openai.com/v1/models').status_code == 401
    try:
        http.get('https://example.com/')
    except httpx.ProxyError:
        pass
    else:
        raise AssertionError('proxy allowed an unapproved host')
print('isolated project ' + project + ': fake inference and egress passed')
"""
                print(docker("exec", gateway, "python", "-I", "-c", check).stdout.strip())
                info = json.loads(docker("inspect", gateway).stdout)[0]
                assert list(info["NetworkSettings"]["Networks"]) == [network]
                assert info["HostConfig"]["ReadonlyRootfs"]
                assert all(
                    m["Destination"] not in {"/var/run/docker.sock", "/workspace"}
                    for m in info["Mounts"]
                )
            print("Two independent project namespaces/state/config/networks verified")
        finally:
            for container in reversed(containers):
                docker("rm", "-f", container)
            for network in networks:
                docker("network", "rm", network)


if __name__ == "__main__":
    main()
