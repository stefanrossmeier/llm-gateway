"""Explicit live-plan execution over the shared client, never a provider SDK."""

import json
import os
import stat
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal, localcontext

from llm_gateway.client import GatewayError, GatewayHTTPError
from llm_gateway.contracts import ChatInput, FunctionChoice
from llm_gateway.storage.sqlite import verify_private_ancestors

from .preflight import PreflightError, preflight


def _forced_function(case):
    data = case.request.input
    return (
        isinstance(data, ChatInput)
        and bool(data.tools)
        and (data.tool_choice == "required" or isinstance(data.tool_choice, FunctionChoice))
    )


def _case_record(case, result, *, recovered=False):
    receipts = result.receipts
    recorded_output = result.provider_metadata.get("recorded_output_status", result.output_status)
    if recovered:
        status = (
            "recovered_existing"
            if result.execution_status == "completed" and recorded_output == "valid"
            else "recovered_failed_or_pending_request"
        )
    elif result.error and result.error.code in {
        "provider_auth",
        "provider_quota",
        "provider_unavailable",
        "provider_deadline_exceeded",
    }:
        status = "unavailable_provider"
    elif result.execution_status == "in_progress":
        status = "in_progress"
    elif result.execution_status != "completed":
        status = "failed_execution"
    elif result.output_status != "valid":
        status = "failed_output"
    elif not receipts:
        status = "failed_missing_receipt_evidence"
    elif result.model.actual_model is None:
        status = "failed_missing_model_evidence"
    else:
        status = "passed_execution"
    return {
        "case_id": case.case_id,
        "request_id": case.request.request_id,
        "status": status,
        "execution_status": result.execution_status,
        "output_status": result.output_status,
        "recorded_output_status": recorded_output,
        "function_call_count": None
        if recovered and result.output_status == "unavailable"
        else len(result.function_calls),
        "safe_error_code": result.error.code if result.error else None,
        "model": result.model.model_dump(mode="json"),
        "receipts": [receipt.model_dump(mode="json") for receipt in receipts],
    }


def _execute_case(
    plan,
    snapshot,
    client,
    case,
    index,
    old,
    report,
    checkpoint,
    lock,
    windows,
    stop,
    *,
    enabled,
    approval,
):
    """One logical POST; shared evidence/checkpoints are synchronized, HTTP is not."""
    started_at = finished_at = None
    try:
        with lock:
            if stop.is_set():
                report["cases"][index]["status"] = "unrun_execution_stopped"
                raise PreflightError("live_execution_stopped_before_submission")
            if plan.group == "paid":
                preflight(plan, snapshot, enabled=enabled, approval=approval)
            report["inference_posts_attempted"] += 1
            report["cases"][index]["status"] = "submitted_outcome_unknown"
            checkpoint()
            if stop.is_set():
                report["inference_posts_attempted"] -= 1
                report["cases"][index]["status"] = "unrun_execution_stopped"
                raise PreflightError("live_execution_stopped_before_submission")
            if plan.group == "paid":
                try:
                    preflight(plan, snapshot, enabled=enabled, approval=approval)
                except PreflightError:
                    stop.set()
                    report["inference_posts_attempted"] -= 1
                    report["cases"][index]["status"] = "unrun_approval_expired_or_invalid"
                    raise
        started_at = datetime.now(UTC).isoformat()
        beginning = time.monotonic()
        try:
            result = client.infer(case.request, expected_config_digest=snapshot.digest)
        finally:
            end = time.monotonic()
            finished_at = datetime.now(UTC).isoformat()
            with lock:
                windows[index] = (beginning, end)
                report["cases"][index].update(
                    client_inference_started_at=started_at,
                    client_inference_finished_at=finished_at,
                )
        record = _case_record(case, result, recovered=old)
        if (
            result.project_id != plan.project_id
            or result.config_digest != snapshot.digest
            or result.request_id != case.request.request_id
            or any(
                receipt.project_id != plan.project_id
                or receipt.principal_id != plan.principal_id
                or receipt.request_id != case.request.request_id
                or receipt.config_digest != snapshot.digest
                for receipt in result.receipts
            )
        ):
            record["status"] = "failed_configuration_identity"
        if plan.group == "free" and any(
            receipt.protected_debit_usd != 0
            or receipt.measured_usd not in {None, Decimal("0")}
            or receipt.evidence_status not in {"measured", "explicit_free", "not_dispatched"}
            for receipt in result.receipts
        ):
            record["status"] = "failed_zero_cost_boundary"
        followup = any(receipt.reconciliation_required for receipt in result.receipts)
        if plan.group == "free" and _forced_function(case):
            record["optional_capability"] = "function_calls"
            if (
                not old
                and record["status"] == "failed_execution"
                and record["safe_error_code"]
                in {
                    "provider_http_404_tools_unavailable",
                    "provider_http_404_parameters_unavailable",
                }
                and result.receipts
                and all(
                    receipt.evidence_status in {"measured", "explicit_free"}
                    for receipt in result.receipts
                )
                and not followup
            ):
                record["status"] = "unsupported_capability"
        failure = None
        if record["status"] not in {
            "passed_execution",
            "recovered_existing",
            "unsupported_capability",
        }:
            failure = (
                "unavailable_precondition"
                if record["status"] == "unavailable_provider"
                else "failed_acceptance"
            )
        elif any(
            receipt.reconciliation_required and receipt.evidence_status != "estimated"
            for receipt in result.receipts
        ):
            failure = "failed_billing_boundary"
        with lock:
            record.update(
                client_inference_started_at=started_at, client_inference_finished_at=finished_at
            )
            report["cases"][index] = record
            if followup:
                report["billing_followup_required"] = True
            checkpoint()
        return failure
    except GatewayError as error:
        with lock:
            record = dict(report["cases"][index])
        record.update(
            status="failed_gateway_operation",
            error_type=type(error).__name__,
            client_inference_started_at=started_at,
            client_inference_finished_at=finished_at,
        )
        if isinstance(error, GatewayHTTPError):
            record["http_status"] = error.status
            record["receipts"] = [receipt.model_dump(mode="json") for receipt in error.receipts]
        try:
            recovered = client.get_request(case.request.request_id)
            record["receipts"] = [receipt.model_dump(mode="json") for receipt in recovered.receipts]
            record["recorded_execution_status"] = recovered.execution_status
        except GatewayError:
            record["receipt_recovery"] = "unavailable_query_later_with_same_id"
        with lock:
            report["cases"][index] = record
            if any(receipt.get("reconciliation_required") for receipt in record["receipts"]):
                report["billing_followup_required"] = True
            checkpoint()
        return "failed_gateway_operation"


def run_plan(
    plan, snapshot, client, *, enabled=False, paid_enabled=False, approval=None, report_sink=None
):
    """Return a safe cost/evidence report even when acceptance cannot dispatch."""
    report = {
        "schema_version": 1,
        "group": plan.group,
        "max_concurrency": plan.max_concurrency,
        "plan_digest": plan.digest,
        "config_digest": snapshot.digest,
        "project_id": plan.project_id,
        "principal_id": plan.principal_id,
        "run_id": plan.run_id,
        "started_at": datetime.now(UTC).isoformat(),
        "status": "unrun_precondition",
        "inference_posts_attempted": 0,
        "usage_before": None,
        "usage_after": None,
        "cases": [
            {
                "case_id": case.case_id,
                "request_id": case.request.request_id,
                "status": "unrun",
                "receipts": [],
            }
            for case in plan.cases
        ],
    }
    sink = report_sink or (lambda value: None)
    lock = threading.RLock()
    windows = {}
    stop = threading.Event()

    def checkpoint():
        with lock:
            try:
                sink(report)
            except BaseException:
                stop.set()
                raise

    checkpoint()
    checked = False
    try:
        if plan.group == "paid" and not paid_enabled:
            raise PreflightError("paid_execution_waits_for_final_owner_approval")
        preflight(plan, snapshot, enabled=enabled, approval=approval)
        ready, aliases = client.ready(), client.aliases()
        if not ready.supports_config_precondition:
            raise PreflightError("live_server_configuration_precondition_required")
        if (
            ready.project_id != plan.project_id
            or ready.principal_id != plan.principal_id
            or ready.config_digest != snapshot.digest
            or aliases.config_digest != snapshot.digest
        ):
            raise PreflightError("live_active_configuration_mismatch")
        granted = {item.alias for item in aliases.aliases}
        if not {case.request.alias for case in plan.cases}.issubset(granted):
            raise PreflightError("live_alias_inspection_mismatch")
        before = client.usage(filters={"run_id": plan.run_id})
        report["usage_before"] = before.model_dump(mode="json")
        checked = True
        existing = []
        for index, case in enumerate(plan.cases):
            try:
                original = client.get_request(case.request.request_id)
                if original.config_digest != snapshot.digest:
                    raise PreflightError("live_existing_configuration_mismatch")
                existing.append(True)
                report["cases"][index] = _case_record(case, original, recovered=True)
            except GatewayHTTPError as error:
                if error.status != 404:
                    raise
                existing.append(False)
        with localcontext() as context:
            context.prec = 50
            missing = [case for case, old in zip(plan.cases, existing, strict=True) if not old]
            extra = sum((case.request.limits.max_usd for case in missing), Decimal("0"))
            if (
                before.overrun
                or before.unknown_attempts
                or before.protected_debit_usd + extra > plan.total_ceiling_usd
                or before.attempts + len(missing) > plan.max_calls
            ):
                raise PreflightError("live_persistent_ledger_allowance_exhausted")
        report["status"] = "running"

        def execute(index):
            try:
                return _execute_case(
                    plan,
                    snapshot,
                    client,
                    plan.cases[index],
                    index,
                    existing[index],
                    report,
                    checkpoint,
                    lock,
                    windows,
                    stop,
                    enabled=enabled,
                    approval=approval,
                )
            except BaseException:
                stop.set()
                raise

        if plan.max_concurrency == 1:
            for index in range(len(plan.cases)):
                failure = execute(index)
                if failure:
                    report["status"] = failure
                    break
        else:
            # Preflight permits exactly two distinct paid-provider routes.
            # Already submitted calls finish and retain receipts if a peer fails.
            with ThreadPoolExecutor(max_workers=2) as pool:
                pending = []
                try:
                    try:
                        for index in range(2):
                            pending.append(pool.submit(execute, index))
                    except RuntimeError:
                        raise PreflightError("live_parallel_executor_unavailable") from None
                    failures = [failure for future in pending if (failure := future.result())]
                except BaseException:
                    stop.set()
                    for future in pending:
                        future.cancel()
                    raise
            if failures:
                priority = {
                    "unavailable_precondition": 1,
                    "failed_acceptance": 2,
                    "failed_billing_boundary": 3,
                    "failed_gateway_operation": 4,
                }
                report["status"] = max(failures, key=priority.__getitem__)
        if report["status"] == "running":
            unsupported = any(c["status"] == "unsupported_capability" for c in report["cases"])
            required_passed = any(
                c["status"] == "passed_execution" and "optional_capability" not in c
                for c in report["cases"]
            )
            report["status"] = (
                (
                    "passed_with_unsupported_capabilities"
                    if required_passed
                    else "unsupported_capability"
                )
                if unsupported
                else "passed_execution"
            )
        if plan.max_concurrency == 2:
            overlap = len(windows) == 2 and max(window[0] for window in windows.values()) < min(
                window[1] for window in windows.values()
            )
            report["concurrency_evidence"] = (
                "recovered_requests_not_fresh"
                if any(existing)
                else "client_call_overlap"
                if overlap
                else "no_fresh_client_overlap"
            )
            if report["status"] == "passed_execution":
                if any(existing) and not all(existing):
                    report["status"] = "partial_recovery_no_fresh_concurrency"
                elif not any(existing) and not overlap:
                    report["status"] = "failed_concurrency_evidence"
        if all(case["status"] == "recovered_existing" for case in report["cases"]):
            report["status"] = "recovered_existing"
        elif report["status"] == "passed_execution" and report.get("billing_followup_required"):
            report["status"] = "execution_passed_billing_unverified"
    except KeyboardInterrupt:
        report["status"] = "interrupted_preserve_ledger_and_ids"
    except PreflightError as error:
        report["status"] = (
            "failed_precondition_after_submission"
            if report["inference_posts_attempted"]
            else "unrun_precondition"
        )
        report["precondition"] = str(error)
    except GatewayError as error:
        report["precondition"] = "gateway_preflight_unavailable"
        report["error_type"] = type(error).__name__
    except Exception as error:
        stop.set()
        report.update(status="failed_runner_operation", error_type=type(error).__name__)
        raise
    finally:
        if checked:
            try:
                report["usage_after"] = client.usage(filters={"run_id": plan.run_id}).model_dump(
                    mode="json"
                )
            except GatewayError:
                report["usage_after_status"] = "unavailable_preserve_case_receipts"
        report["finished_at"] = datetime.now(UTC).isoformat()
        checkpoint()
    return report


class ReportFile:
    """Exclusive private evidence with atomic, fsynced checkpoints."""

    def __init__(self, path):
        if not path.is_absolute():
            raise PreflightError("live_report_absolute_path_required")
        verify_private_ancestors(path.parent)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            self.inode = os.fstat(fd).st_ino
            self.path = path
        finally:
            os.close(fd)

    def write(self, report):
        body = json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2).encode()
        if len(body) > 16 * 1024 * 1024:
            raise PreflightError("live_report_size_limit")
        verify_private_ancestors(self.path.parent)
        original = self.path.lstat()
        if (
            original.st_ino != self.inode
            or not stat.S_ISREG(original.st_mode)
            or original.st_uid != os.getuid()
            or original.st_mode & 0o077
        ):
            raise PreflightError("live_report_identity_changed")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=self.path.parent, prefix=".live-report-", delete=False
            ) as file:
                temporary = file.name
                file.write(body)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.path)
            self.inode = self.path.lstat().st_ino
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)


def write_report(path, report):
    ReportFile(path).write(report)
