"""One prompt, one gateway call, one printed answer. Runs in the released image."""

import argparse
import sys
import uuid

from llm_gateway.client import GatewayClient, GatewayError
from llm_gateway.contracts import ChatInput, Message, RequestContext

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("prompt", nargs="?")
parser.add_argument("--request-id", default=None, help="Reuse to recover the same invocation")
args = parser.parse_args()
prompt = args.prompt if args.prompt is not None else input("Prompt: ")
if not prompt.strip():
    parser.error("provide a nonempty prompt")
request_id = args.request_id or uuid.uuid4().hex
print(f"Request ID: {request_id}", file=sys.stderr, flush=True)
try:
    with GatewayClient(
        "http://gateway:8080", token_file="/run/gateway/client.token", timeout_seconds=160
    ) as client:
        result = client.chat(
            "kilo-auto-free",
            ChatInput(messages=[Message(role="user", content=prompt)]),
            request_id=request_id,
            context=RequestContext(run_id=request_id),
        )
    if (
        result.execution_status != "completed"
        or result.output_status != "valid"
        or not result.content
    ):
        raise SystemExit(
            f"Inference did not return text ({result.execution_status}/{result.output_status}). "
            f"Keep request ID {request_id}; inspect its receipt before retrying."
        )
    print(result.content)
except GatewayError as error:
    raise SystemExit(
        f"{error}. Keep request ID {request_id}; "
        "check gateway readiness, quota, and the request receipt."
    ) from None
