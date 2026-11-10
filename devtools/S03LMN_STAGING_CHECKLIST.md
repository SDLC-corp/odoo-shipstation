# S03L–N: Staging acceptance gate (not yet performed)

This source patch adds shipment-to-picking metadata and review fields (`picking_id`, `link_state`, `link_message`). Passing offline mocks and XML parsing is **not** evidence of successful Odoo 16 module installation, user-role authorization, or carrier integration.

## Before any database upgrade

1. Confirm the Odoo 16 executable, Python environment, configuration file, addon path and PostgreSQL endpoint with the read-only `devtools/Run-S03-Staging-Preflight.ps1`. Do not paste credential values, passwords or API keys into chat.
2. Create/choose an **isolated disposable staging DB**, independent of production, and take a verified backup. Do not run a module upgrade against your live DB or a shared test DB without a rollback.
3. Remove or disable real ShipStation credentials and outbound automations on the staging clone, and prevent scheduled jobs and webhooks from reaching external systems. When testing real ShipStation, use a separate authorized test account and never purchase a shipping label inadvertently.
4. Review the accumulated S01A–S03 changes, Git status, and manifest for new models/rules/cron entries before upgrading. Check existing permission XML IDs and data migration implications.
5. Use `odoo-bin -c <staging-config> -d <disposable-db> -u shipstation_connector --stop-after-init --no-http` only after reviewing the exact CLI paths and staging config. Do not substitute production database names into this command. Check the logs for view/ACL/record-rule errors.

## Runtime checks to record

- Administrator, Manager, Read-Only roles: menus, model rights, protected credential fields, server actions, company A/B isolation.
- V1 and V2 connection tests using separate authorized credentials; no log reveals either secret.
- Webhook receives authorized V1 callback, rejects invalid token/store, deduplicates repeated events, queues without performing remote API calls in HTTP thread.
- Import same order twice in one instance/store: no duplicate sale order or lines. Two different stores cannot cross-match.
- Force one order record to fail: a savepoint rolls it back and the last-success checkpoint does not advance.
- Import one shipment twice: same package record is updated. Import two different shipments for one order: both exist; a single picking's tracking number is not overwritten by the second.
- Order without an outgoing picking: shipment remains **Pending** with reason, and can be rechecked after a delivery is created.
- Order with two eligible pickings: shipment is marked **Needs Review** and no delivery is arbitrarily chosen.
- Shipment with an existing different picking tracking number: no overwrite; state **Needs Review**.
- Wrong store, company, instance, or missing shipment identity: rejected without local mutation.
- Simulate a failed page / malformed API response: checkpoint is held, reports indicate failure, retry is safe.
- Inventory sync remains read-only; create no stock.quant adjustments. Dashboard only aggregates active company and compatible currencies.
- Verified shipment tracking opens via HTTPS; unsafe URL schemes are rejected.

## Acceptance criteria

No unhandled tracebacks during install or upgrade; no unintended label charge, stock change, company leakage, or destructive picking write. Log real outcome and any failures. If any check fails, stop further feature deployment and correct it before release.
