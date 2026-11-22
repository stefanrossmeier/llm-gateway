"""Separate opt-in free/approved-paid execution over the authenticated gateway."""

import argparse
import json
import sys
from pathlib import Path

from llm_gateway.client import GatewayClient, GatewayError
from llm_gateway.config import ConfigError, load_project_config

from .preflight import PreflightError, load_approval, load_plan
from .runner import ReportFile, run_plan


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--project-config", type=Path, required=True)
    parser.add_argument("--gateway-origin", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--gateway-token-file", type=Path)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--enable-free", action="store_true")
    group.add_argument("--enable-paid", action="store_true")
    parser.add_argument("--paid-approval", type=Path)
    args = parser.parse_args(argv)
    try:
        plan = load_plan(args.plan)
        if args.paid_approval is not None and (not args.enable_paid or plan.group != "paid"):
            raise PreflightError("paid_approval_requires_paid_group_enablement")
        approval = load_approval(args.paid_approval) if args.paid_approval is not None else None
        snapshot = load_project_config(args.project_config, check_credentials=False)
        principal = snapshot.config.access.principals.get(plan.principal_id)
        if principal is None:
            raise PreflightError("live_scoped_principal_required")
        evidence = ReportFile(args.report)  # Must succeed before any gateway call.
        timeout = (
            max(
                (
                    snapshot.config.aliases[c.request.alias].limits.request_timeout_seconds
                    for c in plan.cases
                    if c.request.alias in snapshot.config.aliases
                ),
                default=10,
            )
            + 10
        )
        with GatewayClient(
            args.gateway_origin,
            token_file=args.gateway_token_file or principal.token_file,
            timeout_seconds=min(timeout, 3610),
        ) as client:
            report = run_plan(
                plan,
                snapshot,
                client,
                enabled=bool(
                    args.enable_free
                    and plan.group == "free"
                    or args.enable_paid
                    and plan.group == "paid"
                ),
                paid_enabled=args.enable_paid,
                approval=approval,
                report_sink=evidence.write,
            )
        print(
            json.dumps(
                {
                    "status": report["status"],
                    "plan_digest": plan.digest,
                    "inference_posts_attempted": report["inference_posts_attempted"],
                }
            )
        )
        return (
            0
            if report["status"]
            in {"passed_execution", "passed_with_unsupported_capabilities", "recovered_existing"}
            else 2
        )
    except (ConfigError, PreflightError, GatewayError, OSError, ValueError):
        print(
            json.dumps({"status": "error", "code": "live_run_failed_preserve_ids_and_ledger"}),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
