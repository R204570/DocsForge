---
docsforge_contract: 1
harvest_id: "hv-20261118T101530Z-5e2d90aa"
run_id: "run-20261118T101458Z-77e0b4a2"
job: "acme-sdk-1"
technology: "acme-sdk"
corpus: "acme-sdk--sdk-acme-example"
corpus_role: "federated"
version: "undated"
version_basis: "undated"
source: "https://sdk.acme.example/"
strategy: "llms.txt (md manifest)"
store: "postgres"
scope: {"mode": "section", "start": "https://sdk.acme.example/", "widened_to": null, "topic": null, "max_pages": 2}
started_at: "2026-11-18T10:15:30Z"
harvested_at: "2026-11-18T10:15:36Z"
complete: false
complete_reason: {"code": "page-limit", "text": "stopped at the 2-page limit; 118 of the 120 pages the manifest lists were not fetched"}
count_basis: "manifest"
expected_pages: 120
stored_pages: 2
not_stored: {"unextractable": {"count": 0, "urls": []}, "refused_429": {"count": 0, "urls": []}, "rejected_by_store": {"count": 0, "urls": []}, "off_topic": {"count": 0, "urls": []}, "dead_404": {"count": 0, "urls": []}}
generator: "docsforge 1.1.0"
---

# acme-sdk--sdk-acme-example undated documentation

## Contents

1. [Streaming](https://sdk.acme.example/streaming.md)
2. [Retries](https://sdk.acme.example/retries.md)

---

## Streaming

Source: <https://sdk.acme.example/streaming.md>
<!-- docsforge:page n=1 captured_at="2026-11-18T10:15:34Z" sha256="f554e3eae27bb9b0dae6d338661f6d68c9124add3b83ff4eb13caa65530a487f" bytes=70 -->

Call `client.stream(resource)` to iterate over results as they arrive.

---

## Retries

Source: <https://sdk.acme.example/retries.md>
<!-- docsforge:page n=2 captured_at="2026-11-18T10:15:35Z" sha256="30e596673befa5143ce64d3a51c88a8f1915267edc1e720cd0059f4b8c618e90" bytes=93 -->

Requests are retried three times with exponential backoff; pass `retries=0` to turn that off.
