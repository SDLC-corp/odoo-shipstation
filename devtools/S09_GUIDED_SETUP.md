# S09 — Guided Setup and Troubleshooting (admin only)

These steps are for an isolated Odoo 16 database. No live integration testing is performed by the S09 source patch.

1. Open **ShipStation → Instances** as a ShipStation administrator.
2. Select a connection; open **Setup Checklist**. Review red/action-required items before running synchronization.
3. Enter a V1 API key and secret for legacy orders and shipments. Keep a separate V2 key for V2 operations. **Never put credentials in screenshots, tickets or logs.**
4. Configure the company and correct ShipStation `store_id`. Avoid using a shared store identity across unrelated companies.
5. If inventory reconciliation is enabled, select an Odoo warehouse in the same company. This connector reads ShipStation quantities; it does **not** adjust Odoo stock automatically.
6. Only if needed: configure the correct HTTPS `web.base.url`, enable V1 webhook event types, and register the generated callback URL. Treat the URL as a credential. Do not use V1 token URLs for V2 events.
7. Run **Test V1 Connection** and, if configured, **Test V2 Connection**. They are remote read-only checks and may consume API quota.
8. Open **Operations Home** to inspect local exceptions, batch preparation and manual returns. Bulk preparation never buys a label.
9. For connection errors, review admin-only logs; never paste raw responses publicly. Status 401 suggests invalid credentials; 403 permissions; 429 throttling; 5xx remote service issues.

**Important:** Local setup status confirms only configuration fields, not authentication, API access, production fitness or end-to-end shipping success. After finishing source development, separately verify staging install/upgrade, three roles, assets, webhooks, rate calculations, order imports, and label safeguards.
