"""Private subprocess entrypoint, not a network-facing configuration surface."""

import contextlib
import json
import os
import sys
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

# -I excludes cwd/user paths. The only additional path is this installed package's
# verified location; it never comes from a request, environment variable or YAML.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def main():
    from llm_gateway.isolation.network import install

    install()
    from llm_gateway.config.models import ProviderConfig
    from llm_gateway.contracts import usd_string
    from llm_gateway.providers.preparation import PreparedCall
    from llm_gateway.providers.runtime import ProviderFailure, ProviderRuntime

    output = sys.stdout
    response = {"error": "provider_process_invalid", "dispatched": False}
    with (
        open(os.devnull, "w") as silent,
        contextlib.redirect_stdout(silent),
        contextlib.redirect_stderr(silent),
    ):
        try:
            raw = sys.stdin.buffer.read(20 * 1024 * 1024 + 1)
            if len(raw) > 20 * 1024 * 1024:
                raise ValueError("protocol_size")
            message = json.loads(raw)
            prepared = PreparedCall(**message["prepared"])
            provider = ProviderConfig.model_validate(message["provider"])
            try:
                result = ProviderRuntime(deployment_proxy=message.get("deployment_proxy")).invoke(
                    prepared, provider
                )
                data = asdict(result)
                data["usage"] = result.usage.model_dump(mode="json")
                data["measured_usd"] = (
                    usd_string(result.measured_usd) if result.measured_usd is not None else None
                )
                response = {"evidence": data}
            except ProviderFailure as error:
                data = asdict(error.evidence) if error.evidence else None
                if data is not None:
                    data["usage"] = error.evidence.usage.model_dump(mode="json")
                    data["measured_usd"] = (
                        usd_string(error.evidence.measured_usd)
                        if error.evidence.measured_usd is not None
                        else None
                    )
                response = {"error": error.code, "dispatched": error.dispatched, "evidence": data}
        except Exception:
            pass
    encoded = json.dumps(
        response,
        default=lambda value: float(value) if isinstance(value, Decimal) else str(value),
        allow_nan=False,
    )
    if len(encoded.encode()) > 20 * 1024 * 1024:
        response["evidence"]["payload"] = {}
        response.update(error="provider_response_too_large", dispatched=True)
        encoded = json.dumps(response, allow_nan=False)
    output.write(encoded)
    output.flush()


if __name__ == "__main__":
    main()
