# S01E–G Staging Acceptance — required before production

## What changed
- Inventory sync **never modifies `stock.quant`**. The old `inventory_update_odoo_stock` field remains in the database only for backward compatibility; it has no writeback effect. Inventory view shows a comparison only when remote quantity was independently verified.
- Remote quantity unknown / credentials unavailable is **not** interpreted as a verified zero. Existing remote snapshots may be preserved but marked unverified.
- Warehouse reconciliation is read-only. Wrong-company warehouses cause an explicit error; ambiguous SKUs cannot silently select the first product.
- Dashboard SQL and AI insight storage/readback are scoped to the current Odoo company. Historical global AI insight records without a company are hidden, not silently reused.
- Revenue and instance totals do not sum mixed or unknown currencies and do not apply an arbitrary `$` prefix.
- Raw sync-log `Retry` allows safe listed GET endpoints only. POST/DELETE/PUT/PATCH replay is blocked, including label purchase operations.

## Preproduction gate
1. Do **not** run `-u` on production. Create a separate staging database based on a sanitized backup, and confirm restore succeeds.
2. Confirm `odoo-bin`, Odoo config, add-ons path, Python virtual environment, Postgres version, and database are from Odoo 19. Do not put DB passwords or ShipStation keys into chat output.
3. Run Odoo's module upgrade in staging with the known-good config / staging DB (the patch runner does *not* do this). Review logs for XML/view errors and schema changes; stop if errors occur.
4. Assign separate Odoo users for Administrator, Manager and Read-Only. Check menu visibility, API actions, protected fields, and company switching in **runtime**, not only mocks.
5. Simulate inventory: known zero, unknown quantity, same-company matched SKU, ambiguous SKU, missing SKU, wrong-company warehouse. Verify Odoo stock quants have *not changed* before/after even if legacy boolean was True.
6. Check dashboard with two companies and two currencies. Company B must never appear in A's instance counts, logs, low-stock or AI summary. Mixed currency revenue should show a warning rather than a fabricated total.
7. Retry GET `/orders` from a failed test log. Verify POST `/shipments/createlabel` cannot be replayed by this button. Never test paid label purchase on production.
8. Revalidate S01D webhook callback URL rotation, correct store ID, failed-token response, queue processing, duplicate event behavior, and partial failure reporting in staging.
9. Recheck job schedules, API request limits, retention/security of stored payloads, and source compatibility with any existing customer integrations.
10. Only after all gates pass, capture staging acceptance evidence and consider a verified commit/release.

## Compatibility notes
- The new `remote_stock_verified`, reconciliation fields, and `shipstation.ai.insight.company_id` need a **staging Odoo module upgrade** before dashboard/AI/inventory features are usable. Existing inventory records default to `remote_stock_verified=False` until re-synchronized.
- Stock numbers in ShipStation and Odoo may use different availability definitions/warehouses. Comparison is diagnostic, not an accounting inventory adjustment.
- The batch is offline-tested; no live ShipStation request, paid label, Odoo registry upgrade or PostgreSQL migration has been executed here.
