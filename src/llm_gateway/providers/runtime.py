"""Owned, single-dispatch HTTP boundary around the exact reviewed LiteLLM pin.

No SDK object, exception, credential or default-filled usage crosses this module.
The Decisions seam is deliberately pin-specific: its public entrypoint does not
accept an owned HTTP client. Preparation and transformation still use LiteLLM.
"""

import importlib.metadata
import json
import logging
import os
import threading
from dataclasses import dataclass, field
from decimal import Decimal

import httpx

from llm_gateway.config.models import ProviderConfig
from llm_gateway.config.secrets import resolve_credential
from llm_gateway.contracts import TokenUsage, parse_usd
from llm_gateway.isolation.network import owned_network, proxy_destination

from . import LITELLM_VERSION
from .preparation import PreparedCall

_lock = threading.Lock()
_initialized = False
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


def initialize():
    global _initialized
    with _lock:
        if _initialized:
            return
        if importlib.metadata.version("litellm") != LITELLM_VERSION:
            raise RuntimeError("unapproved_litellm_version")
        os.environ["LITELLM_MODE"] = "PRODUCTION"
        os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["DO_NOT_TRACK"] = "1"
        import litellm

        litellm.telemetry = False
        litellm.set_verbose = False
        litellm.callbacks = []
        litellm.success_callback = []
        litellm.failure_callback = []
        litellm.input_callback = []
        litellm.cache = None
        litellm.drop_params = False
        litellm.num_retries = 0
        litellm.suppress_debug_info = True
        for name in ("LiteLLM", "LiteLLM Router", "LiteLLM Proxy", "httpx", "openai"):
            logging.getLogger(name).disabled = True
        _initialized = True


@dataclass(frozen=True)
class ProviderEvidence:
    payload: dict = field(repr=False)
    usage: TokenUsage
    measured_usd: Decimal | None
    actual_model: str | None
    actual_provider: str | None
    generation_id: str | None
    billing_anomaly: bool = False


class ProviderFailure(Exception):
    def __init__(self, code: str, *, dispatched: bool, evidence=None):
        self.code, self.dispatched, self.evidence = code, dispatched, evidence
        super().__init__(code)


def checked_wire_parameters(prepared, actual, expected):
    """Only the approved pin's explicit transformations may reach the wire."""
    if not isinstance(actual, dict) or expected is None:
        raise ProviderFailure("provider_request_invalid", dispatched=False)
    actual = dict(actual)
    expected = dict(expected)
    cap_names = {"max_tokens", "max_completion_tokens", "max_output_tokens"}
    caps = cap_names.intersection(actual)
    allowed = {"max_output_tokens"}
    if prepared.operation == "chat":
        allowed = (
            {"max_tokens", "max_completion_tokens"}
            if prepared.provider_kind == "openai"
            else {"max_tokens"}
        )
    if prepared.max_output_tokens is None:
        if caps:
            raise ProviderFailure("provider_output_bound_missing", dispatched=False)
    elif (
        len(caps) != 1
        or not caps.issubset(allowed)
        or any(
            type(actual[name]) is not int or not 0 < actual[name] <= prepared.max_output_tokens
            for name in caps
        )
    ):
        raise ProviderFailure("provider_output_bound_missing", dispatched=False)
    for name in cap_names:
        actual.pop(name, None)
        expected.pop(name, None)
    # OpenRouter's pinned chat adapter asks for billing evidence. This is the
    # only permitted added field; it does not enable a server-side tool.
    if prepared.provider_kind == "openrouter" and prepared.operation == "chat":
        if "usage" in actual:
            usage = actual.pop("usage")
            if (
                not isinstance(usage, dict)
                or set(usage) != {"include"}
                or usage["include"] is not True
            ):
                raise ProviderFailure("provider_parameters_changed", dispatched=False)

    def canonical(value):
        return json.dumps(
            value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
        )

    if canonical(actual) != canonical(expected):
        raise ProviderFailure("provider_parameters_changed", dispatched=False)


def _redact(value, key):
    if isinstance(value, str):
        return value.replace(key, "[REDACTED]")
    if isinstance(value, dict):
        return {k.replace(key, "[REDACTED]"): _redact(v, key) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v, key) for v in value]
    return value


def evidence(payload: dict, key: str) -> ProviderEvidence:
    payload = _redact(payload, key)
    raw = payload.get("usage")
    raw = raw if isinstance(raw, dict) else {}

    def count(*names):
        for name in names:
            value = raw.get(name)
            if type(value) is int and 0 <= value <= 10**12:
                return value
        return None

    details = raw.get("output_tokens_details", raw.get("completion_tokens_details")) or {}
    cached = raw.get("input_tokens_details", raw.get("prompt_tokens_details")) or {}
    details = details if isinstance(details, dict) else {}
    cached = cached if isinstance(cached, dict) else {}
    x_details = raw.get("server_side_tool_usage_details")
    x_details = x_details if isinstance(x_details, dict) else {}

    def item_count(name):
        value = x_details.get(name)
        return value if type(value) is int and 0 <= value <= 10**12 else None

    usage = TokenUsage(
        x_posts_fetched=item_count("x_posts_fetched"),
        x_profiles_fetched=item_count("x_users_fetched"),
        input_tokens=count("input_tokens", "prompt_tokens"),
        output_tokens=count("output_tokens", "completion_tokens"),
        total_tokens=count("total_tokens"),
        reasoning_tokens=(
            details.get("reasoning_tokens")
            if type(details.get("reasoning_tokens")) is int
            and 0 <= details["reasoning_tokens"] <= 10**12
            else None
        ),
        cache_tokens=(
            cached.get("cached_tokens")
            if type(cached.get("cached_tokens")) is int and 0 <= cached["cached_tokens"] <= 10**12
            else None
        ),
    )
    amount, anomaly = None, False
    if "cost" in raw:
        try:
            # JSON integers are valid provider evidence; configuration still
            # requires decimal strings. Never convert through binary float.
            value = raw["cost"]
            if not isinstance(value, (str, Decimal)) and type(value) is not int:
                raise ValueError()
            amount = parse_usd(value if isinstance(value, Decimal) else str(value))
        except ValueError:
            anomaly = True

    def identifier(name):
        value = payload.get(name)
        if not isinstance(value, str) or len(value) > 512 or "[REDACTED]" in value:
            return None
        try:
            value.encode("utf-8")
        except UnicodeError:
            return None
        return value

    return ProviderEvidence(
        payload,
        usage,
        amount,
        identifier("model"),
        identifier("provider"),
        identifier("id"),
        anomaly,
    )


class ProviderRuntime:
    def __init__(self, *, transport_factory=None, environment=None, deployment_proxy=None):
        # Factory injection is an internal offline test seam, never HTTP/config input.
        self._transport_factory = transport_factory
        self._environment = environment
        self._proxy = deployment_proxy
        self._proxy_destination = proxy_destination(deployment_proxy)

    def invoke(self, prepared: PreparedCall, provider: ProviderConfig) -> ProviderEvidence:
        if (
            provider.kind != prepared.provider_kind
            or not provider.enabled
            or not provider.credential
        ):
            raise ProviderFailure("provider_unavailable", dispatched=False)
        key = resolve_credential(provider.credential, self._environment)
        initialize()
        import litellm
        from litellm.llms.custom_httpx.http_handler import HTTPHandler

        host = "api.openai.com" if provider.kind == "openai" else "openrouter.ai"
        prefix = "/v1" if provider.kind == "openai" else "/api/v1"
        endpoint = {
            "chat": prefix + "/chat/completions",
            "responses": prefix + "/responses",
            "x_search": prefix + "/responses",
            "embeddings": prefix + "/embeddings",
            "decisions": "/v1/decisions" if provider.kind == "openai" else "/api/alpha/decisions",
        }[prepared.operation]
        dispatched, captured, response_status = False, None, None
        routing_failure = None
        expected_wire = prepared.body
        expected_wire.update(expected_wire.pop("extra_body", {}))
        if provider.kind == "openai" and prepared.operation == "decisions":
            expected_wire = None

        def approve_decision_wire(body):
            nonlocal expected_wire
            if provider.kind != "openai":
                return
            expected_wire = json.loads(json.dumps(body, allow_nan=False))
            if (
                set(expected_wire) != {"model", "input", "questions"}
                or not isinstance(expected_wire["questions"], list)
                or len(expected_wire["questions"]) != len(prepared.body["questions"])
                or {question.get("name") for question in expected_wire["questions"]}
                != set(prepared.body["questions"])
            ):
                raise ProviderFailure("provider_parameters_changed", dispatched=False)

        def guard(request):
            nonlocal dispatched
            if dispatched:
                raise ProviderFailure("opaque_retry_denied", dispatched=True)
            if (
                str(request.url) != f"https://{host}{endpoint}"
                or request.method != "POST"
                or request.headers.get("authorization") != f"Bearer {key}"
            ):
                raise ProviderFailure("provider_destination_denied", dispatched=False)
            try:
                transformed = json.loads(request.content)
                size = len(
                    json.dumps(
                        transformed,
                        ensure_ascii=True,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ).encode()
                )
            except (ValueError, TypeError):
                raise ProviderFailure("provider_request_invalid", dispatched=False) from None
            if size > min(
                prepared.byte_limit, prepared.wire_byte_limit, prepared.input_token_bound
            ):
                raise ProviderFailure("transformed_bound_exceeded", dispatched=False)
            if not isinstance(transformed, dict):
                raise ProviderFailure("provider_request_invalid", dispatched=False)
            if transformed.get("model") != prepared.upstream_model:
                raise ProviderFailure("provider_model_mismatch", dispatched=False)
            checked_wire_parameters(prepared, transformed, expected_wire)
            dispatched = True

        def capture(response):
            nonlocal captured, response_status, routing_failure
            response_status = response.status_code
            if response.headers.get("content-encoding", "identity") not in {"identity", ""}:
                raise ProviderFailure("provider_compression_denied", dispatched=True)
            content = bytearray()
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content) > MAX_RESPONSE_BYTES:
                    raise ProviderFailure("provider_response_too_large", dispatched=True)
            response._content = bytes(content)
            try:
                payload = json.loads(content, parse_float=Decimal)
                if isinstance(payload, dict):
                    captured = evidence(payload, key)
                    # Classify public routing reasons inside the credential
                    # boundary. Never propagate the arbitrary upstream message.
                    if provider.kind == "openrouter" and response_status == 404:
                        error = payload.get("error")
                        message = error.get("message") if isinstance(error, dict) else None
                        if isinstance(message, str):
                            for prefix, reason in {
                                "no endpoints found that support tool use": "tools_unavailable",
                                "no endpoints found that can handle the requested parameters": (
                                    "parameters_unavailable"
                                ),
                                "no endpoints found matching your data policy": "data_policy",
                                "no endpoints found supporting your data region": "data_region",
                            }.items():
                                if message[:256].casefold().startswith(prefix):
                                    routing_failure = f"provider_http_404_{reason}"
                                    break
            except (ValueError, TypeError):
                pass

        transport = (
            self._transport_factory()
            if self._transport_factory
            else httpx.HTTPTransport(retries=0, trust_env=False, proxy=self._proxy)
        )

        class OwnedTransport(httpx.BaseTransport):
            def handle_request(self, request):
                with owned_network(*(self_destination or (host, 443))):
                    return transport.handle_request(request)

            def close(self):
                transport.close()

        self_destination = self._proxy_destination
        try:
            with httpx.Client(
                transport=OwnedTransport(),
                trust_env=False,
                follow_redirects=False,
                timeout=prepared.timeout_seconds,
                headers={"Accept-Encoding": "identity"},
                event_hooks={"request": [guard], "response": [capture]},
            ) as http_client:
                handler = HTTPHandler(client=http_client, follow_redirects=False)
                body = prepared.body
                body["model"] = prepared.sdk_model
                kwargs = dict(
                    api_key=key,
                    api_base=f"https://{host}{prefix}",
                    timeout=prepared.timeout_seconds,
                    max_retries=0,
                    num_retries=0,
                    fallbacks=[],
                )
                if prepared.operation == "decisions":
                    decision = self._decisions(body, key, prepared, handler, approve_decision_wire)
                    if captured is not None:
                        captured.payload["answers"] = _redact(
                            decision.model_dump(mode="json")["answers"], key
                        )
                elif prepared.operation in {"responses", "x_search"}:
                    litellm.responses(**body, **kwargs, client=handler)
                elif provider.kind == "openai":
                    from openai import OpenAI

                    sdk = OpenAI(
                        api_key=key,
                        base_url=kwargs["api_base"],
                        max_retries=0,
                        http_client=http_client,
                    )
                    if prepared.operation == "chat":
                        litellm.completion(**body, **kwargs, client=sdk)
                    else:
                        litellm.embedding(**body, **kwargs, client=sdk)
                elif prepared.operation == "chat":
                    litellm.completion(**body, **kwargs, client=handler)
                else:
                    litellm.embedding(**body, **kwargs, client=handler)
            if captured is None:
                raise ProviderFailure("provider_response_invalid", dispatched=dispatched)
            return captured
        except Exception as error:
            code = (
                error.code
                if isinstance(error, ProviderFailure)
                else {401: "provider_auth", 403: "provider_auth", 429: "provider_quota"}.get(
                    response_status,
                    routing_failure or f"provider_http_{response_status}"
                    if type(response_status) is int and 100 <= response_status <= 599
                    else "provider_transport_failed"
                    if dispatched
                    else "provider_before_dispatch_failed",
                )
            )
            raise ProviderFailure(code, dispatched=dispatched, evidence=captured) from None

    @staticmethod
    def _decisions(body, key, prepared, handler, approve_wire):
        from litellm.decisions.call import _format_response, _request_body
        from litellm.llms.custom_httpx.llm_http_handler import BaseLLMHTTPHandler
        from litellm.systemone.main import _prepare_systemone_call

        call = _prepare_systemone_call(
            model=prepared.sdk_model,
            state=body["state"],
            questions=body["questions"],
            images=None,
            assets=None,
            safety_identifier=None,
            audio=None,
            videos=None,
            api_key=key,
            api_base=(
                "https://openrouter.ai/api"
                if prepared.provider_kind == "openrouter"
                else "https://api.openai.com/v1"
            ),
            timeout=prepared.timeout_seconds,
            custom_llm_provider=prepared.provider_kind,
            extra_headers=None,
            kwargs={"num_retries": 0, "max_retries": 0},
        )
        request_body = _request_body(call)
        approve_wire(request_body)
        result = BaseLLMHTTPHandler().decisions(
            model=call.model,
            custom_llm_provider=call.custom_llm_provider,
            logging_obj=None,
            provider_config=call.provider_config,
            request=call.ir_request,
            body=request_body,
            logged_body=request_body,
            api_base=call.api_base,
            api_key=key,
            headers=call.headers,
            timeout=call.timeout,
            litellm_params=call.litellm_params,
            client=handler,
        )
        return _format_response(result, call)
