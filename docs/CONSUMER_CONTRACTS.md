# Consumer compatibility

Consumers own configuration, credentials, network/state boundaries and logical run
identity. They use the canonical typed API or documented structured compatibility
wires. Sharing a gateway package does not require sharing another application's
checkout, database, provider secrets or deployment lifecycle.

A migrated consumer must route every inference path—including evaluation and smoke
scripts—through its configured authenticated gateway. Remove silent legacy URL
fallbacks and copied direct-provider transports. Require credentials and durable
run identity before paid calls. Recover uncertain calls with the original request
ID and preserve nullable usage through downstream summaries.

Provider/model defaults are not embedded in the shared runtime. Changes to model
aliases, pricing or capabilities are explicit reviewed consumer configuration.
Native X is a supported operation with the risk semantics in [X support](X_SEARCH.md).
