"""Short-lived owned provider process; no credentials in argv or caller results."""

import os
from dataclasses import asdict
from pathlib import Path

from llm_gateway.contracts import TokenUsage, parse_usd
from llm_gateway.isolation.network import proxy_destination
from llm_gateway.isolation.process import WorkerFailure, run_worker

from .runtime import ProviderEvidence, ProviderFailure

WORKER_KEY = "LLM_GATEWAY_CREDENTIAL_VALUE"
WORKER_KEY_FILE = "LLM_GATEWAY_CREDENTIAL_FILE"


def worker_provider(provider):
    """Rebind references without resolving values or forwarding their env names."""
    if provider.credential is None:
        return provider
    reference = provider.credential
    return provider.model_copy(
        update={
            "credential": reference.model_copy(
                update={
                    "env_var": WORKER_KEY if reference.env_var else None,
                    "file_env_var": WORKER_KEY_FILE if reference.file_env_var else None,
                }
            )
        }
    )


def worker_environment(provider, *, environment=None, private_home: str) -> dict:
    source = os.environ if environment is None else environment
    result = {
        "HOME": private_home,
        "TMPDIR": private_home,
        "PYTHONNOUSERSITE": "1",
        "LITELLM_MODE": "PRODUCTION",
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        "HF_HUB_OFFLINE": "1",
        "DO_NOT_TRACK": "1",
        "LANG": "C.UTF-8",
    }
    if provider is not None and provider.credential:
        for name, destination in (
            (provider.credential.env_var, WORKER_KEY),
            (provider.credential.file_env_var, WORKER_KEY_FILE),
        ):
            if name and name in source:
                result[destination] = source[name]
    return result


def decode_evidence(data):
    if data is None:
        return None
    return ProviderEvidence(
        payload=data["payload"],
        usage=TokenUsage.model_validate(data["usage"]),
        measured_usd=parse_usd(data["measured_usd"]) if data["measured_usd"] is not None else None,
        actual_model=data["actual_model"],
        actual_provider=data["actual_provider"],
        generation_id=data["generation_id"],
        billing_anomaly=data["billing_anomaly"],
    )


class OwnedProviderProcess:
    def __init__(self, state_root: Path, *, environment=None, deployment_proxy=None):
        self.root, self.environment = state_root, environment
        proxy_destination(deployment_proxy)
        self.deployment_proxy = deployment_proxy

    def invoke(self, prepared, provider, *, cancel=None, timeout=None):
        try:
            response = run_worker(
                Path(__file__).with_name("worker.py"),
                {
                    "prepared": asdict(prepared),
                    "provider": worker_provider(provider).model_dump(mode="json"),
                    "deployment_proxy": self.deployment_proxy,
                },
                state_root=self.root,
                environment=lambda private: worker_environment(
                    provider, environment=self.environment, private_home=private
                ),
                timeout=min(timeout or prepared.timeout_seconds, prepared.timeout_seconds),
                cancel=cancel,
            )
        except WorkerFailure as error:
            raise ProviderFailure(
                error.code.replace("worker_", "provider_"), dispatched=error.submitted
            ) from None
        except OSError:
            raise ProviderFailure("provider_process_unavailable", dispatched=False) from None
        try:
            if not isinstance(response, dict):
                raise ValueError()
            result = decode_evidence(response.get("evidence"))
            if "error" in response:
                raise ProviderFailure(
                    response["error"], dispatched=response.get("dispatched", True), evidence=result
                )
            if result is None:
                raise ValueError()
            return result
        except ProviderFailure:
            raise
        except Exception:
            raise ProviderFailure("provider_process_lost", dispatched=True) from None
