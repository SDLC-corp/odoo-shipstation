# S13 Release-readiness assessment - shipstation_connector 19.0.1.1.0 (ported to 15.0.1.1.0)

## Odoo 15 port (this branch: 15.0.1.1.0)

This branch is the 19.0.1.1.0 hardening ported to Odoo 15 Community (Python 3.8). The assessment below was written for
19.0 and stays as the record of how the behaviour was verified there; for this build the evidence is:

- **source-tested:** `devtools/run_offline_tests.py` under Python 3.8: 236 tests, 0 failures (234 from 19.0, contract
  checks updated to the Odoo 15 spellings, plus 2 for the Odoo 15 webhook dispatch and JSON responses).
- **runtime-tested:** Odoo 15 test suite (169 tests: the 167 from 19.0 plus a secret-field oracle test and an asset
  bundle test) passes on a fresh database installed without demo data, and on a database installed from the published
  15.0.1.0.0 build, seeded through its own code and upgraded (`migrations/15.0.1.1.0`: distinct webhook secrets, provably
  linked historical shipments, blank order currencies filled). PostgreSQL 18, HTTP mocked.
- **not done on 15:** no browser pass (the Owl 1 Operations Home, dashboard and chatbot are checked by the asset test and a
  `node --check` syntax check only), no real ShipStation account (blocker B1 below applies unchanged).
- Odoo 15 specific code: field-group enforcement for searches/sort orders/`read_group` on the connection's credential
  fields (Odoo 15 does not do it), plain-HTTP dispatch for `/shipstation/webhook...` (Odoo 15 turns any JSON POST into
  JSON-RPC), explicit flush of the side-cursor label/void bookkeeping (Odoo 15 only flushes through a live environment),
  `numbercall = -1` on the webhook cron.
- Editions: tested on Odoo 15 Community, on-premise; Enterprise and Odoo.sh expected, not yet verified.

Status: **one blocker remains: no real ShipStation account has ever been used (B1).** Everything that can be
verified without one has been, including an upgrade from the build published on the Store, behaviour at
100k-row scale and a real-browser pass over every screen. Licensing and edition wording were decided by the owner.

> **Correction (store-id contract).** The hardening commit `c47789c5` (S01-S12), and therefore every later commit up to
> `8cc619d6`, contained a critical defect, found afterwards by re-reading ShipStation's API reference: it required every
> order and shipment to carry a top-level `storeId` equal to the connection's store. ShipStation's orders carry no top-level
> `storeId` (only `advancedOptions.storeId`) and its shipments carry no store at all, so **every connection with a store id
> would have imported nothing**. The test fixtures encoded the same wrong assumption, so the suite was green. Fixed in the
> following commit (defect 16). The build published on the Store (`09e8866f`) predates the check; do not publish any of
> `c47789c5` ... `8cc619d6`.

## 1. What was verified, and how

Maturity levels: **implemented** - **source-tested** (offline contract tests) - **runtime-tested** (Odoo test
suite on a real, upgraded PostgreSQL 18 database, HTTP mocked) - **browser-tested** (real browser, isolated
server, synthetic data) - **production-verified** (real ShipStation account). *Nothing is production-verified.*

| Area | Source | Runtime | Browser | Production |
|---|:-:|:-:|:-:|:-:|
| V1/V2 routing, auth, retry/backoff, 429 handling, bounded waits | yes | yes | - | no |
| Credentials hidden from non-admins, ACLs for 6 roles, company isolation | yes | yes | yes | n/a |
| Order import / incremental sync / idempotent push | yes | yes | - | no |
| Shipment sync, partial shipments, multi-package, unmatched, voided | yes | yes | yes | no |
| Auto re-link of a replacement label after a void | - | yes | - | no |
| Rates, quote freshness, presets, carrier discovery | yes | yes | - | no |
| Label purchase (single order, durable claim, ambiguity lock, reset) | yes | yes | confirm dialog | no |
| **Void label (admin-only, off by default, single code path)** | AST test | yes | switch, button, confirm | no |
| Bulk fulfillment preparation (never buys) | yes | yes | yes | n/a |
| Return cases (quantities, approvals, audit trail) | yes | yes | yes | n/a |
| Webhook intake, replay protection, inbox retry, purge | yes | yes | inbox/retry | no |
| Operations Home, Dashboard, Exception Board, Workbench (Owl/kanban) | yes | views render | yes | n/a |
| Empty states on 16 list screens | - | yes | yes | n/a |
| AI assistant (ACL + company scoped, no `sudo`) | - | yes | yes | n/a |
| **Upgrade from the build published on the Store (09e8866f)** | - | yes | - | no |
| **Performance at 100k mirrors/shipments, 50k sale orders** | - | measured | - | no |

Evidence at the time of writing:

- Offline suite **234/234**.
- Odoo runtime suite **167/167** on a clean database (0 failed, 0 errors, 0 skipped; 0 view and 0 translation warnings).
- The same suite **167/167** on a database installed from the published build and upgraded to this branch (pre-existing data kept).
- 0 JavaScript console errors across Administrator, Manager and Reader browser sessions.

### Upgrade from the published build

Installed the exact published commit on a fresh Odoo 19 database, seeded it through its own code, and upgraded to this branch.
It found two data problems that no source test could, both fixed by `migrations/19.0.1.1.0/post-migration.py`:

1. Every existing connection received the **same webhook secret** (Odoo evaluates a callable default once for all existing rows).
2. Every historical shipment became "Pending" even where the delivery already carried its tracking (3 false "awaiting delivery" items in the test; hundreds on a real database).

After the fix: 3 distinct secrets, provable shipments linked, ambiguous / foreign-company / already-triaged ones left alone, legacy
sale orders still adopted by id and store, views, ACLs, record rules and cron jobs intact. Users of webhooks must re-register the new URL
(see README "Upgrading").

### Scale (disposable clone; 100k order mirrors, 100k shipments, 50k request logs, 20k webhook items, 50k sale orders + deliveries)

| Operation | Median |
|---|---:|
| Operations Home overview | 52 ms |
| Legacy dashboard | 127 ms |
| Instance totals | 53 ms |
| Workbench queue page + count (33k rows) | 13 ms |
| Order mirror list / search | 37 / 29 ms |
| Order-push candidates: count (25k eligible) / first 200 | 60 / 3 ms |
| Per-record `_find_order` / shipment lookup | 3 / 0.6 ms |
| Webhook cron claim (`SKIP LOCKED`) | 4 ms |
| Autovacuum purge, one run | 271 ms |

The query plans use indexes on every hot path. Nothing exceeded 130 ms; the largest tables were 31-34 MB.

## 2. Defects found by real execution (all fixed, each with a regression test)

1. Order push crashed on every install without `website_sale` (`sale.order.amount_delivery`).
2. Managers could not refresh rates (no delete right on `shipstation.rate`).
3. Pacific time: UTC was sent as if Pacific, so incremental windows started 7-8 h late; DST repeated hour handled.
4. `GET /shipments` has no `modifyDate` filter; now uses documented `createDate`/`voidDate` windows.
5. Assistant used `sudo()` and ignored ACLs and companies.
6. Manual syncs, delivery rechecks and label-PDF storage failed for a ShipStation Admin/Manager without Sales/Inventory rights.
7. A shipment for an order unknown to Odoo hard-failed and held the checkpoint forever.
8. Voided labels were treated as valid shipments (and counted on the dashboard).
9. ShipStation identity fields were copied by "Duplicate" / backorder splitting.
10. Return quantities were unchecked; no audit trail on batches/returns.
11. Paid-label race: a later write from the request transaction would raise a serialization failure *after* the label was bought
    (reproduced against PostgreSQL REPEATABLE READ). All writes to the claimed row now use a side cursor.
12. Unbounded retry sleep (up to 180 s vs Odoo's 120 s limit); unbounded order push; chatbot crash on non-string input.
13. Documentation described a disabled endpoint and a removed write-back toggle and overclaimed features; the store page said LGPL-3
    while the manifest is OPL-1; `requests` was undeclared; Enterprise/Odoo.sh were claimed but untested.
14. UX: records named `model,id`; buttons shown that can never work for the viewer; an accessibility warning; no empty-state guidance.
15. **Upgrade:** shared webhook secret across connections; all historical shipments "Pending".
16. **Store-id contract (critical; the rejection was introduced by `c47789c5`, the published build only had the push half).** Orders and
    shipments were rejected unless they carried a top-level `storeId`; ShipStation sends none (orders: `advancedOptions.storeId`;
    shipments: no store). Orders were pushed with a top-level `storeId` (already in the published build), which ShipStation
    ignores, and the buyer name was read from a `customerName` field that V1 orders do not have. The published build imported
    orders but recorded a blank store on them, which is why upgraded databases carry blank-store rows. Now: the store is read from `advancedOptions.storeId` when
    present, a payload that names *no* store is trusted to the request's `storeId` filter, one that names a *different* store is
    rejected, pushes set `advancedOptions.storeId` on creation only, and the name falls back to `billTo.name`. Rows the published
    build wrote with a blank store are adopted (orders: when the company has a single connection; pending shipment placeholders:
    always, as the instance already pins the store). **How it was proven:** 16 new tests (`tests/test_store_contract.py`) use the
    documented payload shapes; against the previous code 11 of them fail (e.g. order sync `0 processed / 1 failed`, shipment sync
    `0 linked / 1 failed`, webhook items dropped, push sends a top-level `storeId`); with the fix all pass. The shared fixtures were
    changed to the documented shapes and a test guards them against drifting back.
17. **Revenue never available for real orders.** V1 orders state no currency, so every mirror's currency was blank and the dashboard
    reported revenue as unavailable. Each connection now has an optional **Order Currency** (empty = company currency) used when a
    payload states none; the upgrade fills blank mirror currencies from it.

## 3. Decisions made by the owner

- License: **OPL-1** is correct (store page aligned to the manifest).
- Editions: the claim now reads "tested on Odoo 19 Community, on-premise; Enterprise and Odoo.sh expected, not yet verified".
- Void Label: build it, **off by default**, administrator-only, one order at a time.

## 4. Remaining blocker

| # | Blocker | Why it matters | Action |
|---|---|---|---|
| B1 | **No real ShipStation call has ever been made.** | All behaviour is verified against mocks and ShipStation's published docs only. | Run the staging checklists in `devtools/` against a sandbox store with a **test label** (`testLabel` is USPS-only per the docs; confirm what it charges), and exercise the void request before enabling it for customers. |

## 5. Residual risks (not blockers)

- Payload shapes follow ShipStation's published reference, **not observed live**. The store contract (defect 16) is the clearest
  example of why B1 matters: confirm on a sandbox store that orders import with `advancedOptions.storeId`, shipments sync under the
  `storeId` filter, and a pushed order lands in the intended store.
- Unverified API assumptions: numbered V1 webhook hosts (`ssapiN`), `GET /v2/carriers` as a key check, `voidDate` semantics of
  `GET /shipments`, whether ShipStation reverts an order to awaiting shipment after a void (the README tells users to Pull first).
- The paid-label cursor logic runs in test mode (cursors merged); the production semantics were proven separately with a two-connection
  PostgreSQL probe, not through Odoo.
- Enterprise and Odoo.sh are untested (claim reworded accordingly).
- Scale was measured to 100k rows per table on one machine; behaviour beyond that is extrapolated from the index plans.
- After any upgrade, Odoo 19 browsers serve cached action definitions until revalidated; users may need a reload.
- Inventory quantities are a read-only comparison; there is deliberately no write-back.

## 6. Next batch

1. Sandbox verification (B1); fix whatever the real API contradicts.
2. Return cases: ShipStation return labels only after (1); keep manual until then.
3. Store / store-mapping import from `/stores`; scoped manual re-sync for a single order.
4. Verify on Enterprise / Odoo.sh, then restore the broader edition claim if it passes.

## 7. Reproduce

```
python -B -I devtools/run_offline_tests.py
odoo-bin -c <conf> -d <DISPOSABLE db> -u shipstation_connector --stop-after-init \
         --test-enable --test-tags /shipstation_connector
```
Use a throw-away database: the upgrade step modifies the database it runs on. Never point it at production.
To reproduce the published-build upgrade: `git checkout --detach 09e8866f`, install the module on a fresh database,
seed it, `git checkout 19.0`, then `-u shipstation_connector`.
