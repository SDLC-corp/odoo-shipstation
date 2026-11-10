# S02H–K staging acceptance — ShipStation Odoo 19

**Do not run this module upgrade against the production database.** The ZIP runner only edits module source and runs offline tests. It never connects to the Odoo database or ShipStation API.

## Changes

- One incremental order import pipeline replaces three duplicate code paths.
- Each imported order runs inside its own PostgreSQL savepoint, keyed by a transaction-scoped advisory lock for `(instance, order ID)`.
- Matching a sale order requires correct Odoo company and ShipStation instance; legacy unowned rows can only be adopted by exact external order ID and matching store. Conflicts are rejected rather than guessed.
- New imports attach `sale.order.shipstation_instance_id` and need an Odoo module upgrade to create that field.
- Invalid order identity, store mismatch, ambiguous customers/SKUs/mirror records and stale payloads are handled explicitly.
- Confirmed/cancelled Odoo orders get remote metadata updates only; no automatic line rewrite or fulfilment cancellation.
- Record failures are reported outside the savepoint; page failures or partial failures prevent cursor advancement and are retried from the previous checkpoint. A no-date manual fallback also holds the cursor.
- Remote POST/PUT/PATCH/DELETE calls are attempted once. A timeout after a write has an **unknown remote outcome**; a person must check ShipStation before retrying.
- Another label purchase is blocked if the local order already contains a shipment ID/tracking/label URL; this does not independently confirm remote state, so it is a guard, not complete duplicate-purchase prevention.

## Preupgrade preparation

1. Back up staging database and filestore and check they can be restored. Sanitize customer data and credentials. Use a dedicated staging ShipStation account or disable external cron and webhooks.
2. Use the local Odoo 19 virtual environment, actual `odoo-bin`, staging configuration, custom add-ons path and staging-only database. Do not paste DB passwords, API keys, or webhook URL secrets in shared logs.
3. Check existing duplicates and ownership gaps **read-only** before upgrading: 
   - `sale.order` rows with a ShipStation order ID but no owner (existing rows are initially unowned by the new field).
   - `shipstation.order.sync` rows repeated for the same instance + external order ID.
   - Two different stores using the same order number; different companies using identical remote IDs.
   - Duplicate shared/company customer emails or SKUs.
4. Run `-u shipstation_connector --stop-after-init` **only on staging** with the confirmed Odoo executable/config and record the full upgrade log. Do not guess the staging database name or Odoo config.

## Runtime acceptance scenarios

1. Import an order with a new `(company, instance, store, ID)` and verify exactly one draft sale order and one sync mirror.
2. Import the same order again: verify no duplicate sale order and no duplicated order lines.
3. Simulate two workers processing the same remote identity; verify advisory locks serialize and no duplicate sale order is created.
4. Import the same order ID from a different company or instance: never attach it to the other record. Test an existing legacy row with both matching and nonmatching store IDs.
5. Test missing order ID, changed store ID and repeated external ID; check reports and preserved checkpoint.
6. Make one order on a page fail due to ambiguous SKU/customer. Verify prior successful imports remain, the bad order is rolled back, and the checkpoint does **not** move. Correct the ambiguity and rerun; the successful orders stay idempotent.
7. Simulate a GET page timeout after a successful page; verify no advanced checkpoint.
8. Process older `modifyDate` than the Odoo record; verify local data stays newer.
9. Import a cancelled status on a *confirmed* Odoo sale/delivery. Verify no automatic cancellation or line replacement. Draft cancellations can still be applied.
10. Create an outgoing order in one company and verify no cross-instance echo. Confirm `sale.order.shipstation_instance_id` is set on successful export.
11. Mock a label POST timeout/503. Verify only one HTTP call occurs and the operator must reconcile remote state before retry.
12. Revalidate S01A–G ACL, V1/V2 connection diagnostics, webhooks, company dashboard and inventory no-write policy after module upgrade.

## Release blockers

- All offline unit tests must pass **and** staging registry/module upgrade must pass.
- Real Odoo ACL/record rules, UI views, and multi-company behavior need testing with three different role accounts.
- Actual ShipStation API calls need a controlled account. Do not buy paid labels on production to test.
- Shipping pickings and split shipments are not fully rebuilt by this batch; they remain a future major chunk.
- Existing duplicate records need manual reconciliation before enforcing any future SQL uniqueness constraints.
