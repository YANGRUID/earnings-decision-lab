# IBKR TWS API — official source provenance

IBKR TWS Migration Phase 1.1. This file is tracked in git; the actual
source code it describes (`pythonclient/`) is **not** — see
`backend/.gitignore` and `backend/scripts/fetch_ibapi_official.py`.

## What Phase 1 got wrong

The Phase 1 report presented `pip install ibapi` (PyPI, version
`9.81.1.post1`) as "the same official distribution shipped by
Interactive Brokers." That claim does not hold up against IBKR's own
current documentation, checked directly for this correction:

- Interactive Brokers' TWS API introduction docs state plainly that
  pip/PyPI installation is not hosted, endorsed, or connected to
  Interactive Brokers.
- Inspecting `https://pypi.org/project/ibapi/` directly: the package's
  *listed maintainer* is a third-party PyPI account unrelated to
  Interactive Brokers, even though the package's bundled metadata
  (copied verbatim from IBKR's own real source files, which do say
  "Official") makes it look IBKR-published at a glance.
- It was also nine minor versions behind: `9.81.1` (published December
  2020) vs. the real current releases described below.

## The real, official channel

The only distribution channel Interactive Brokers itself operates is a
direct download gated behind a license click-through at
<https://interactivebrokers.github.io>. As of this correction
(2026-08-31):

| Release | Version | Notes |
|---|---|---|
| Stable | **10.45.01** | what this project uses |
| Latest | 10.50.01 | released 2026-08-26, five days before this check; more bleeding-edge, deliberately not chosen for a production-oriented integration |

The task that requested this correction referenced "10.49" — that
number does not match either real current release found; recorded here
rather than blindly forced, per this task's own instruction not to
force a specific number without checking what's actually current.

Confirmed live: the ZIP files themselves
(`twsapi_macunix.<version>.zip`) are not actually access-controlled —
the "I Agree" button on the download page is a client-side UI gate, not
a server-side one. The URL fetched by
`scripts/fetch_ibapi_official.py` is:

```
https://interactivebrokers.github.io/downloads/twsapi_macunix.1045.01.zip
SHA256: 56ea048911052e86d6621ab712957c790fce6d547bc2a55900136ae4f6835941
```

verified by a real download performed for this task on 2026-08-31.
`fetch_ibapi_official.py` refuses to proceed if a future download's
hash doesn't match this value.

## License

The `pythonclient/` source is governed by the **IB API Non-Commercial
License** (referenced in every source file's own copyright header:
`Copyright (C) 2024 Interactive Brokers LLC`). Key terms, checked
directly against IBKR's own published license text:

- A personal, royalty-free, non-exclusive, non-sublicensable,
  non-transferable, restricted right to install, modify, and use the
  API Code **solely for Non-Commercial Purposes**.
- **"You agree not to publish, disseminate, or redistribute the API
  Code to any third party."**
- Copyright and proprietary notices in the source must be retained.
- Requires maintaining an active IB account for the license's duration.

**This project's GitHub repository (`YANGRUID/earnings-decision-lab`)
is PUBLIC.** Committing the real source into git history would be
exactly the "redistribute... to any third party" this license
prohibits — that is the entire reason this source is fetched fresh at
setup/build time (`scripts/fetch_ibapi_official.py`) and excluded from
version control, rather than vendored directly into the repo the way
option C in this task's own spec first suggested. This project's use
(a personal earnings-research tool tied to the operator's own IB
account, never distributed as a product) is consistent with
"Non-Commercial Purposes" as described above — a reader relying on this
document for a different project should re-verify the license's exact
current terms at <https://interactivebrokers.github.io>, not assume
this note still applies.

## What ships where

- `backend/pyproject.toml` — `ibapi` is declared as a normal
  dependency, resolved via `[tool.uv.sources]` to a local path
  (`vendor/ibapi_official/pythonclient`), plus `protobuf==5.29.5`
  (a real, new transitive dependency this version's own `setup.py`
  requires that 9.81 never had).
- `backend/Dockerfile` — runs `fetch_ibapi_official.py` early in the
  builder stage (stdlib-only: `urllib`/`zipfile`/`hashlib`, no `curl`/
  `unzip` apt packages needed) before dependency resolution, since a
  local-path dependency must exist on disk before `uv sync` can
  resolve it.
- Local development — run `python scripts/fetch_ibapi_official.py`
  once (idempotent: a no-op if already present and verified) before
  `uv sync`.

## Known real, verified differences from the old PyPI 9.81 package

Found and handled by this correction (see the Phase 1.1 report for the
full detail):

- `EWrapper.error()` gained a new `errorTime: int` positional parameter
  (inserted *before* `errorCode`) — every real error dispatch changed
  shape; code written against 9.81's signature silently misreads every
  field if not updated.
- `EWrapper.tickSize()`'s `size` parameter is now typed `Decimal`, not
  `int`. `BarData.volume`/`.wap` likewise default to a `Decimal`
  sentinel (`UNSET_DECIMAL`), not a float.
- The package now requires `protobuf==5.29.5` and ships a parallel,
  optional protobuf-based message surface (`reqXxxProtoBuf` methods,
  `TickTypeProto` etc.) alongside the original plain-callback API this
  project uses — not required, not adopted here, noted for awareness.
- `reqMktData`, `reqHistoricalData`, `reqSecDefOptParams` (the plain,
  non-protobuf methods this project actually calls) kept their exact
  signatures — no change needed on this project's call sites.
