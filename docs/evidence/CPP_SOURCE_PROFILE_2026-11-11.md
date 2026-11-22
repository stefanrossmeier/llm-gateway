# C++ source prerequisites in the restricted gateway profile

> Historical source analysis, not current artifact approval. Current behavior is
> specified in [the product specification](../SPECIFICATION.md) and [X support](../X_SEARCH.md).

Review date: 2026-11-11. This supplements, rather than replaces, the
[actual native probe](NATIVE_PROFILE_2026-11-11.md). It is a scoped source-path
assessment, **not a patched-library claim or full container release approval**.
The raw advisory gate is unchanged. Final binary/architecture evidence and
remaining container findings still require T126.

## Exact primary sources

The GitHub-verified signed tags resolve to immutable commits:

- [tokenizers v0.23.3](https://github.com/huggingface/tokenizers/tree/301277072263fb3830eb66eec7e47adadf76f1dc)
- [frozenlist v1.8.0](https://github.com/aio-libs/frozenlist/tree/48324affeecc2fff8f84f38368f720426c0354cf)

Only bounded source text was downloaded/read; no upstream build hook or source
was executed. The [provenance record](native-source-review-2026-11-11.json)
preserves archive/file hashes and verification outcomes. Tokenizers' exact
[Python Cargo lock](https://github.com/huggingface/tokenizers/blob/301277072263fb3830eb66eec7e47adadf76f1dc/bindings/python/Cargo.lock)
selects esaxx-rs 0.1.10, SHA-256
`d817e038c30374a4bcb22f94d0a8a0e216958d4c3dcde369b1439fec4bdda6e6`.
The official crates.io artifact was checked against that hash before inspection.

## Source-path observations

[Frozenlist's implementation](https://github.com/aio-libs/frozenlist/blob/48324affeecc2fff8f84f38368f720426c0354cf/frozenlist/_frozenlist.pyx)
uses Python list operations and a primitive atomic Boolean. It does not implement
C++ boolean vectors, wide bitset streams, numeric from-chars, priority-queue
erase-if or GNU parallel search. This agrees with its empty observed undefined
C++ symbol set; the symbol set alone was not treated as proof.

[Tokenizers' unigram trainer](https://github.com/huggingface/tokenizers/blob/301277072263fb3830eb66eec7e47adadf76f1dc/tokenizers/src/models/unigram/trainer.rs)
places the esaxx C++ call in seed construction reached by training. Its
[feature declaration](https://github.com/huggingface/tokenizers/blob/301277072263fb3830eb66eec7e47adadf76f1dc/tokenizers/Cargo.toml)
connects esaxx_fast to that native crate. In the checksum-verified crate, build.rs
selects C++11; the active source uses ordinary arrays and vectors of integer
pairs. The observed imports use ordinary allocation, not the C++17 aligned-new
overload. None of the six advisory-specific facilities below is used there.
Conditional OpenMP suffix-sort loops are distinct from GNU parallel search.

The pinned LiteLLM tokenizer adapter exposes read-only encode/decode/loading
operations. The gateway uses only its five typed inference operations, fixed
OpenAI/OpenRouter adapter selection and owned HTTP. It exposes no tokenizer
training, custom tokenizer, callbacks, custom provider or arbitrary SDK kwargs.
Schemas and returned user functions are data, never local execution.

## Scoped applicability inference

| Primary advisory | Required facility | Source-profile assessment |
| --- | --- | --- |
| [CVE-2026-95619](https://security-tracker.debian.org/tracker/CVE-2026-95619) | Aligned C++ allocation | Not reached by the observed ordinary allocations or the reviewed C++11/atomic paths. |
| [CVE-2026-102010](https://security-tracker.debian.org/tracker/CVE-2026-102010) | Priority-queue erase-if | Feature absent from the reviewed C++ sources; tokenizer training is additionally outside the gateway API. |
| [CVE-2026-114690](https://security-tracker.debian.org/tracker/CVE-2026-114690) | GNU parallel search | That facility is absent; conditional suffix-sort OpenMP code is a different operation. |
| [CVE-2026-114691](https://security-tracker.debian.org/tracker/CVE-2026-114691) | Oversized std::vector<bool> | No such container is instantiated by the reviewed sources; a primitive atomic Boolean is different. |
| [CVE-2026-115455](https://security-tracker.debian.org/tracker/CVE-2026-115455) | Wide-character bitset input | No bitset/stream-extraction facility in the reviewed paths. |
| [CVE-2026-115456](https://security-tracker.debian.org/tracker/CVE-2026-115456) | std::from_chars | No such conversion in the reviewed C++ sources or gateway dispatch controls. |

These are inferences for this exact reviewed dependency/source profile. They do
not authorize direct tokenizers training, arbitrary LiteLLM/custom SDK execution,
new native features, different wheel builds or a different compiler/dependency
graph. Changes require fresh artifact and call-path review. Prefer reviewed fixed
upstream artifacts when available even where a current path is not reached.

## Verification and remaining limits

API regressions reject custom tokenizer/trainer/callback/provider/base controls
at both request and operation-input levels before any attempt is admitted, and
reject operation=train. The owned runtime has no training route. Real pinned SDK
transformations still pass over mocked HTTP without network.

The CI service job now also records the native probe after the actual image build,
with no pulls, provider keys or inference. ARM64 local evidence already exists;
the new AMD64 CI evidence must be reviewed before final platform disposition.
Dynamic import observations still do not prove arbitrary inlined-code absence.
The source assessment narrows the identified prerequisites, while final image
provenance and all other raw matches remain mandatory. No CVE was suppressed.

## AMD64 evidence review

The [actual AMD64 CI probe](native-profile-amd64-2026-11-11.json) from
[run 43027993568](https://github.com/stefanrossmeier/llm-gateway/actions/runs/43027993568)
completed all eight mocked-HTTP adapter calls without network or inference.
It records image `sha256:e3c3a66fb308b2e40e288a30cda1bd91e405ecd607aaed041d55b9ee965de639`.
The loaded C++ library is 6.0.33. Frozenlist has no undefined C++ imports;
tokenizers has the same ten ordinary allocation/iostream/typeinfo imports
observed on ARM64. The source fixture SHA agrees. This confirms the observed
binary prerequisites on both reviewed architectures, subject to the preceding
source/inlined-code limitations. It does not dispose of unrelated CVEs or approve
a later image merely because it uses the same tag. T126 remains open.
