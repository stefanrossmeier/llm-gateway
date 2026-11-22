# Paid acceptance proposals

Prepare a private, concrete plan before inference: exact project/config digest,
provider/model/operation, invocation IDs, one or more finite attempts, token limits,
justified cost ceilings or explicitly accepted X allowances, expiry and receipts.
The proposal helper in `llm_gateway.testing.proposal` is key-free and does not
execute or authorize calls. Preflight requires matching explicit approval.

For chat/Responses/functions, verify parameter support and schema before spending.
For X, use the separate explicit [risk policy](X_SEARCH.md); never describe its
allowance as a guaranteed ceiling. Preserve failures, cost measurements and
unknown outcomes. No hidden retry, provider fallback or weakened validation may
be added merely to make an acceptance case pass.
