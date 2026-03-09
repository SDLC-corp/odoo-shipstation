# ShipStation Enterprise Feature Matrix

This document maps requested enterprise features to the current `shipstation_connector` state and defines an implementation path compatible with your codebase.

Reference listing reviewed:
- https://apps.odoo.com/apps/modules/19.0/shipstation_ept

Important:
- That app is OPL/proprietary. We can implement equivalent behavior in this module, but not copy their source code.

## 1. Multi-Instance Support
- Status: Implemented
- Notes: `shipstation.instance` model exists and supports multiple instances.

## 2. Multi-Store Management
- Status: Partially Implemented
- Implemented: store id auto-fetch, store mapping model (store -> warehouse/team), mapping UI on instance.
- Pending: robust store import/list sync from ShipStation `/stores` into mapping lines.

## 3. Product Sync
- Status: Implemented
- Implemented: pull/push, mapping support, update/create handling.
- Pending: optional validation rules for mandatory SKU and duplicate SKU prevention hard checks.

## 4. Order Sync
- Status: Implemented
- Implemented: pull/push, status update, cancellation sync.
- Pending: tighter status transition controls for edge cases.

## 5. Customer Sync
- Status: Implemented (with endpoint fallback)
- Implemented: pull, matching by email, partner linking through order sync flow.
- Note: push support depends on API cluster capability.

## 6. Shipment Sync
- Status: Implemented
- Implemented: pull shipments, update delivery tracking/carrier/service.

## 7. Shipping Label Generation
- Status: Implemented
- Implemented: create label + open label.
- Implemented: label PDF attachment (best effort) to related sale/picking.

## 8. Shipping Rate Calculation
- Status: Implemented
- Implemented: rate fetch and best-rate selection action.
- Pending: auto-select mode toggle per instance.

## 9. Inventory Sync
- Status: Implemented
- Implemented: push/pull inventory actions and cron hook.
- Implemented: warehouse-based quantity scope.
- Implemented: optional ShipStation->Odoo stock writeback toggle.

## 10. Webhook Integration
- Status: Implemented
- Implemented: `/shipstation/webhook` endpoint, payload logging, realtime order/shipment upsert.

## 11. Automation Rules
- Status: Implemented (baseline)
- Implemented: rule model with weight/country criteria and warehouse/team/shipping method effects.
- Pending: richer condition engine (AND/OR groups, priorities by event type).

## 12. Bulk Operations
- Status: Partially Implemented
- Implemented: instance actions for bulk push/sync.
- Pending: dedicated list-view mass actions and queued batch processing with progress.

## 13. Advanced Field Mapping
- Status: Implemented
- Implemented: field mapping models, expected data hints, mapping used in push and listing refresh.

## 14. Sync Scheduler (Cron Jobs)
- Status: Implemented
- Implemented: orders, shipments, products, customers, inventory cron methods.
- Pending: UI-level frequency configurator per job.

## 15. Sync Logs & Error Monitoring
- Status: Implemented
- Implemented: `shipstation.sync.log`, request logging, failed/success status, retry button.

## 16. Sync Dashboard
- Status: Implemented
- Implemented: redesigned ShipStation-only dashboard with KPIs, instance table, failed sync panel.

## 17. Carrier Mapping
- Status: Implemented
- Implemented: ShipStation code -> Odoo carrier mapping model and sync usage.

## 18. Warehouse Mapping
- Status: Implemented
- Implemented: store -> warehouse mapping applied during order sync.

## 19. Security Roles
- Status: Implemented (baseline)
- Implemented: Admin/Manager/Read-only groups.
- Pending: full ACL split by role for every ShipStation model (currently broad `base.group_user` access still exists).

## 20. Retry Failed Sync
- Status: Implemented
- Implemented: retry action on failed sync logs.

---

## Priority Gaps To Finish Next
1. Full role-based ACL hardening (Admin/Manager/User read-only behavior across all models/menus/actions).
2. Store master synchronization UI (import all stores, maintain mapping table from authoritative store list).
3. Queue-based bulk operations (large dataset safety, retries, progress monitor).
4. Per-instance scheduler configurator (editable interval/frequency from UI).
5. Enhanced automation engine (multiple criteria sets, explicit execution order, dry-run).

## Recommended Delivery Phases
1. Phase A: Stability and Security
- Schema migration cleanup and strict ACL model split.
2. Phase B: Operations Scale
- Queue/batch framework for bulk sync and retry orchestration.
3. Phase C: Configuration UX
- Store import wizard + cron configurator + automation builder UI.
4. Phase D: Advanced Logistics
- Shipment cancel/return, mass label print, richer tracking updates.
