"""Create explicit synthetic consumer-owned configuration for an offline demo."""

import argparse
import json
from pathlib import Path


def create(directory):
    directory = directory.resolve()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    fixtures = directory / "fixtures"
    fixtures.mkdir(mode=0o700)
    (fixtures / "reply.txt").write_text("Hello from your project's fake fixture.\n")
    data = {
        "schema_version": 1,
        "project_id": "my-example",
        "mode": "fake",
        "providers": {"router": {"kind": "openrouter"}},
        "access": {"owner_only_ipc": True},
        "aliases": {
            "example-chat": {
                "provider_ref": "router",
                "upstream_model": "synthetic",
                "operation": "chat",
                "capabilities": ["plain_text"],
                "limits": {
                    "max_input_bytes": 4096,
                    "max_input_tokens": 8192,
                    "max_output_tokens": 64,
                    "attempt_timeout_seconds": 5,
                    "request_timeout_seconds": 10,
                    "max_concurrency": 1,
                },
            }
        },
        "fake": {
            "fixture_root": str(fixtures),
            "responses": {"example-chat": {"default": ["reply.txt"]}},
        },
    }
    with (directory / "project.yaml").open("x") as file:
        json.dump(data, file, indent=2)
        file.write("\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    create(args.directory)
    print("Created synthetic configuration; pass its project.yaml explicitly to serve.")


if __name__ == "__main__":
    main()
