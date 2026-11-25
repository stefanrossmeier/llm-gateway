"""Reproduce SDK/native-library evidence in a network-none, key-free container."""

import argparse
import ast
import hashlib
import json
import subprocess
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PROBE = r"""
from pathlib import Path
import hashlib, json, os, platform, ssl, struct
import llm_gateway
for kind in ["openai", "openrouter"]:
    for operation in ["chat", "responses", "embeddings", "decisions"]:
        test_owned_transport_all_operations(kind, operation)
loaded = sorted({line.split()[-1] for line in Path("/proc/self/maps").read_text().splitlines()
                 if "/" in line.split()[-1]})
linked = []
sensitive = []
for root in [Path("/app/.venv/lib/python3.12/site-packages"),
             Path("/usr/local/lib/python3.12/lib-dynload")]:
    for path in sorted(root.rglob("*.so")):
        data = path.read_bytes()
        if not data.startswith(b"\x7fELF"):
            continue
        if data[:6] != b"\x7fELF\x02\x01":
            raise ValueError("Only little-endian 64-bit ELF is reviewed by this probe")
        offset = struct.unpack_from("<Q", data, 40)[0]
        size, count = struct.unpack_from("<HH", data, 58)
        sections = [struct.unpack_from("<IIQQQQIIQQ", data, offset+i*size) for i in range(count)]
        imported = []
        for section in sections:
            if section[1] != 11:
                continue
            strings = sections[section[6]]
            names = data[strings[4]:strings[4]+strings[5]]
            for position in range(section[4], section[4]+section[5], section[9]):
                name, info, other, index, value, length = struct.unpack_from(
                    "<IBBHQQ", data, position)
                symbol = names[name:].split(b"\0", 1)[0].decode()
                if index == 0 and symbol.split("@")[0] in {
                    "regcomp", "regexec", "regfree", "wscanf", "fwscanf", "swscanf",
                    "vwscanf", "vfwscanf", "vswscanf", "__isoc99_wscanf", "__isoc99_fwscanf",
                    "__isoc99_swscanf", "__isoc99_vwscanf", "__isoc99_vfwscanf",
                    "__isoc99_vswscanf",
                }:
                    sensitive.append({"object": str(path.relative_to(root)), "symbol": symbol})
                if index == 0 and symbol.startswith("_Z"):
                    imported.append(symbol)
        if b"libstdc++.so.6\0" not in data:
            continue
        linked.append({"object": str(path.relative_to(root)),
                       "sha256": hashlib.sha256(data).hexdigest(),
                       "imported_cpp_symbols": sorted(imported)})
package_root = Path(llm_gateway.__file__).resolve().parent
authored_paths = sorted([*package_root.rglob("*.py"),
                         *package_root.glob("config/providers/*.json")])
authored = [[str(path.relative_to(package_root)), hashlib.sha256(path.read_bytes()).hexdigest()]
            for path in authored_paths]
status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines()
              if ":" in line)
root_mount = [line.split() for line in Path("/proc/mounts").read_text().splitlines()
              if line.split()[1] == "/"]
print("NATIVE_PROFILE " + json.dumps({
    "sensitive_native_imports": sensitive,
    "adapter_calls_over_mock_http": 8,
    "loaded_libstdcpp": [path for path in loaded if "libstdc++" in path],
    "native_objects_referencing_libstdcpp": linked,
    "authored_source_sha256": hashlib.sha256(json.dumps(
        authored, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest(),
    "authored_source_files": len(authored),
    "architecture": platform.machine(),
    "python_version": platform.python_version(),
    "openssl_version": ssl.OPENSSL_VERSION,
    "tls_cipher_names": sorted(
        cipher["name"] for cipher in ssl.create_default_context().get_ciphers()),
    "effective_uid": os.geteuid(),
    "effective_capabilities": status["CapEff"].strip(),
    "no_new_privileges": status["NoNewPrivs"].strip() == "1",
    "read_only_root": len(root_mount) == 1 and "ro" in root_mount[0][3].split(","),
}))
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    source = (ROOT / "tests/offline/test_provider_runtime.py").read_bytes()
    tree = ast.parse(source)
    selected = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            node.names = [name for name in node.names if name.name != "pytest"]
            if node.names:
                selected.append(node)
        elif isinstance(node, ast.ImportFrom):
            selected.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in {
            "alias",
            "payload",
            "test_owned_transport_all_operations",
        }:
            node.decorator_list = []
            selected.append(node)
    script = ast.unparse(ast.Module(body=selected, type_ignores=[])) + "\n" + PROBE
    name = "gateway-native-" + uuid.uuid4().hex[:12]
    try:
        result = subprocess.run(
            [
                "docker",
                "run",
                "--pull=never",
                "--name",
                name,
                "-i",
                "--network",
                "none",
                "--read-only",
                "--tmpfs",
                "/state:uid=10001,gid=10001,mode=0700",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--entrypoint",
                "python",
                args.image,
                "-I",
                "-",
            ],
            input=script,
            capture_output=True,
            text=True,
            timeout=90,
        )
        if result.returncode:
            raise SystemExit("Native profile probe failed; no applicability approval produced")
        lines = [
            line[len("NATIVE_PROFILE ") :]
            for line in result.stdout.splitlines()
            if line.startswith("NATIVE_PROFILE ")
        ]
        if len(lines) != 1:
            raise SystemExit("Native profile evidence missing")
        container = json.loads(
            subprocess.check_output(["docker", "inspect", name], text=True, stderr=subprocess.PIPE)
        )[0]
        image_id = container["Image"]
        installed_image = json.loads(
            subprocess.check_output(
                ["docker", "image", "inspect", image_id], text=True, stderr=subprocess.PIPE
            )
        )[0]
        runtime_identity = {
            "architecture": installed_image["Architecture"],
            "os": installed_image["Os"],
            "rootfs_diff_ids": installed_image["RootFS"]["Layers"],
            "config": installed_image["Config"],
        }
        report = json.loads(lines[0]) | {
            "resolved_container_image": image_id,
            # Modern Docker returns an index ID in .Image. Match the platform
            # manifest too, since scanners describe its config/manifest IDs.
            "platform_manifest_digest": container.get("ImageManifestDescriptor", {}).get("digest"),
            # docker save can transcode OCI -> Docker manifests. Executed layer
            # bytes and the entire execution config remain identical.
            "image_runtime_identity_sha256": hashlib.sha256(
                json.dumps(runtime_identity, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            "inference_performed": False,
            "source_fixture_sha256": hashlib.sha256(source).hexdigest(),
            "applicability_approved": False,
        }
        local_root = ROOT / "src" / "llm_gateway"
        authored = [
            [str(path.relative_to(local_root)), hashlib.sha256(path.read_bytes()).hexdigest()]
            for path in sorted(
                [*local_root.rglob("*.py"), *local_root.glob("config/providers/*.json")]
            )
        ]
        expected_source = hashlib.sha256(
            json.dumps(authored, separators=(",", ":"), ensure_ascii=True).encode()
        ).hexdigest()
        if report["authored_source_sha256"] != expected_source:
            raise SystemExit("Image authored source differs from the reviewed checkout")
        if (
            report["effective_uid"] != 10001
            or int(report["effective_capabilities"], 16) != 0
            or not report["no_new_privileges"]
            or not report["read_only_root"]
        ):
            raise SystemExit("Native probe runtime isolation differs from the reviewed profile")
        body = json.dumps(report, indent=2) + "\n"
        if args.output:
            with args.output.open("x") as destination:
                destination.write(body)
        print(body, end="")
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)


if __name__ == "__main__":
    main()
