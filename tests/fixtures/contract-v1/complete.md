---
docsforge_contract: 1
harvest_id: "hv-20261118T101500Z-3f9a2c1d"
run_id: "run-20261118T101458Z-77e0b4a2"
job: "acme-sdk-1"
technology: "acme-sdk"
corpus: "acme-sdk"
corpus_role: "entry"
version: "2.4"
version_basis: "site-path"
source: "https://docs.acme.example/2.4/"
strategy: "sitemap"
store: "files"
scope: {"mode": "section", "start": "https://docs.acme.example/2.4/", "widened_to": null, "topic": null, "max_pages": null}
started_at: "2026-11-18T10:15:00Z"
harvested_at: "2026-11-18T10:15:41Z"
complete: true
complete_reason: null
count_basis: "sitemap"
expected_pages: 2
stored_pages: 2
not_stored: {"unextractable": {"count": 0, "urls": []}, "refused_429": {"count": 0, "urls": []}, "rejected_by_store": {"count": 0, "urls": []}, "off_topic": {"count": 0, "urls": []}, "dead_404": {"count": 1, "urls": ["https://docs.acme.example/2.4/old-quickstart"]}}
generator: "docsforge 1.1.0"
---

# acme-sdk 2.4 documentation

## Contents

1. [Getting Started](https://docs.acme.example/2.4/start)
2. [Client API](https://docs.acme.example/2.4/api/client)

---

## Getting Started

Source: <https://docs.acme.example/2.4/start>
<!-- docsforge:page n=1 captured_at="2026-11-18T10:15:12Z" sha256="cb2c406543db031ca8b9bb8ee355235f536b347793721dd07f0f4262a7b903e0" bytes=141 -->

Install the SDK with `pip install acme-sdk`, then create a client.

```python
from acme import Client
client = Client(token="YOUR_TOKEN")
```

---

## Client API

Source: <https://docs.acme.example/2.4/api/client>
<!-- docsforge:page n=2 captured_at="2026-11-18T10:15:13Z" sha256="bc58f88b5daf09a6743f2f734d94d66f84371a431d36e8397994db89f6d545e1" bytes=87 -->

`Client(token)` opens a session. Every method raises `AcmeError` on a non-2xx response.
