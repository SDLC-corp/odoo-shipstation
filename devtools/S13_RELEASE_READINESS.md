# S13 Release-readiness assessment - shipstation_connector 19.0.1.1.0

Status: **not ready for the Apps Store until blockers B1-B3 are closed.** The code base is stable,
secure on the tested paths, and well covered by automated tests; what is missing is evidence from a
real ShipStation account and three decisions.

## 1. What was verified, and how

Maturity levels: **implemented** (code exists) - **source-tested** (offline text/contract tests) -
**runtime-tested** (Odoo test suite on a real, upgraded PostgreSQL 18 database, HTTP mocked) -
**browser-tested** (real browser against an isolated server, synthetic data) -
**production-verified** (real ShipStation account). *Nothing is production-verified.*

| Area | Implemented | Source | Runtime | Browser | Production |
|---|:-:|:-:|:-:|:-:|:-:|
| V1/V2 routing, auth header, retry/backoff, 429 handling | yes | yes | yes | - | no |
| Credentials hidden from non-admins, ACLs for 6 roles, company isolation | yes | yes | yes | yes | n/a |
| Order import (idempotent, stale-safe, draft-only) and incremental sync | yes | yes | yes | - | no |
| Order push (idempotent `orderKey`, open-delivery only, bounded) | yes | yes | yes | - | no |
| Shipment sync, partial shipments/backorders, multi-package | yes | yes | yes | - | no |
| Voided labels, unmatched shipments, auto re-link of a replacement | yes | - | yes | yes | no |
| Rates, quote freshness, presets, carrier discovery | yes | yes | yes | - | no |
| Label purchase (single order, claim, ambiguity lock, reset) | yes | yes | yes | confirm dialog | no |
| Bulk fulfillment preparation (never buys) | yes | yes | yes | yes | n/a |
| Return cases (quantities, approvals, audit trail) | yes | yes | yes | yes | n/a |
| Webhook intake, replay protection, inbox retry, purge | yes | yes | yes | inbox/retry | no |
| Operations Home, Dashboard, Exception Board, Workbench (Owl/kanban) | yes | yes | views render | yes | n/a |
| AI assistant (ACL + company scoped, no `sudo`) | yes | - | yes | yes | n/a |
| Upgrade 19.0.1.0.0 -> 19.0.1.1.0 (clone of an earlier working-tree install) | - | - | yes | - | no |

Evidence: offline suite **233/233**, Odoo runtime suite **127/127** (0 failed, 0 errors, 0 skipped),
0 view warnings, 0 translation warnings, 0 JavaScript console errors in three browser sessions
(Administrator, Manager, Reader).

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
11. Paid-label race: a later write from the request transaction would raise a serialization failure
    *after* the label was bought (reproduced against PostgreSQL REPEATABLE READ). All writes to the claimed row now use a side cursor.
12. Unbounded retry sleep (up to 180 s vs Odoo's 120 s limit); unbounded order push; chatbot crash on non-string input.
13. Documentation described a disabled endpoint, a removed write-back toggle and overclaimed features; store page said LGPL-3 while the manifest is OPL-1; `requests` was undeclared.
14. UX: records named `model,id`; buttons shown that can never work for the viewer; one accessibility warning.

## 3. Blockers before release

| # | Blocker | Why it matters | Action |
|---|---|---|---|
| B1 | **No real ShipStation call has ever been made.** | All behaviour is verified against mocks and ShipStation's published docs only. | Run the staging checklists in `devtools/` against a sandbox store with a **test label** (`testLabel` is USPS-only per the docs; confirm what it charges). |
| B2 | **License statement** | Manifest says OPL-1; the store page said LGPL-3 until this release. This release aligns the page to the manifest. | Confirm OPL-1 is intended (if not, change the manifest). |
| B3 | **Enterprise / Odoo.sh claimed, only Community tested** | The listing says "Community & Enterprise, Odoo.sh". | Smoke-test on Enterprise/Odoo.sh or drop the claim. |
| B4 | Commits are local; not pushed | The authenticated GitHub account on this machine (`tejaskamble-18`) differs from the commit author. | Push with the intended account: `git push origin 19.0` (fast-forward, no force). |

## 4. Remaining risks (not blockers)

- Upgrade was tested from a database installed from an earlier working tree, **not** from the exact 19.0.1.0.0 build published on the Store.
- Not measured at scale (100k+ orders). Order push, totals and the dashboard use bounded/grouped queries by design.
- The paid-label cursor logic is exercised in test mode (cursors merged); the production semantics were
  proven separately with a two-connection PostgreSQL probe, not through Odoo.
- Unverified assumptions: numbered V1 webhook hosts (`ssapiN`), `GET /v2/carriers` as a key check, `voidDate` semantics of `GET /shipments`.
- Inventory quantities are a read-only comparison; there is deliberately no write-back.

## 5. Next batch (priorities)

1. Run the sandbox checklist (B1) and fix whatever the real API contradicts.
2. Explicit, admin-only **Void Label** action (refund request) with confirmation and idempotency - needs a product decision on approvals.
3. Return cases: link to ShipStation return labels only after (1); keep manual until then.
4. Store/store-mapping import from `/stores`; scoped manual re-sync for a single order.
5. Load test at realistic volume; add indexes if the grouped queries show up in `pg_stat_statements`.

## 6. Reproduce

```
python -B -I devtools/run_offline_tests.py
odoo-bin -c <conf> -d <DISPOSABLE db> -u shipstation_connector --stop-after-init \
         --test-enable --test-tags /shipstation_connector
```
Use a throw-away database: the suite creates records inside rolled-back transactions, but the upgrade
step modifies the database it runs on. Never point it at a production database.
