# ShipStation Connector for Odoo 19

Connects Odoo 19 with ShipStation: orders, shipments, tracking, shipping rates, guarded label
purchase, fulfillment preparation, return tracking, inventory comparison and an operations console.

- Technical name: `shipstation_connector` - version `19.0.1.1.0` - license OPL-1
- Depends on: `base`, `web`, `mail`, `sale_management`, `stock`, `delivery`, `contacts`
- Python: `requests`

## What it does

| Area | Behaviour |
|---|---|
| Orders in | ShipStation orders become draft quotations (never auto-confirmed). Re-importing is idempotent; older updates are ignored; confirmed orders are never rewritten. |
| Orders out | Confirmed sales orders that still have an open outgoing delivery are sent with `orderKey` (and `orderId` once known), so sending again updates instead of duplicating. |
| Shipments | Imported by create-date and void-date windows. Tracking is written to the matching delivery only when it is unambiguous; a different package never overwrites existing tracking. Voided labels are detected and escalated. Shipments for orders unknown to Odoo are kept as "No Matching Order". |
| Rates | Quotes come from `POST /shipments/getrates` and expire after 30 minutes or when the address/package changes. |
| Labels | `POST /orders/createlabelfororder` for **one order at a time** after an explicit confirmation. The PDF is stored as an attachment on the order mirror and the delivery. |
| Void (refund) | Off by default. When an administrator enables **Allow Void Label Requests** on the instance, administrators get a confirmed, single-order **Void Label (Request Refund)** button (`POST /shipments/voidlabel`). A void made in ShipStation itself is detected by the shipment sync and frees the order the same way. After a void a replacement label can be bought; the first label's ids stay on the order as an audit trail. |
| Inventory | Read-only comparison of ShipStation and Odoo quantities. **Odoo stock is never changed.** |
| Returns | Manual case tracking (items validated against the order, approvals, audit trail). No return label, refund or stock move is created. |
| Operations | Operations Home (Owl), Shipping Workbench, Visual Exception Board, fulfillment batches, webhook inbox, guided setup. |

## What it never does on its own

- Buy a label from a bulk action, scheduled job or webhook.
- Retry a `POST` automatically (a retry could buy a second label).
- Change Odoo stock quantities.
- Request a refund (void) from a bulk action, scheduled job or webhook. The void endpoint is reachable
  from exactly one method, behind an instance switch, an administrator check and a confirmation.
- Create a return label or refund a return.

If ShipStation does not give a clear answer to a label purchase (timeout, HTTP 5xx, unreadable
reply) the order is locked in **Request Sent - Outcome Unknown**; the same applies to a void
(**Void Requested - Outcome Unknown**). Check the order in ShipStation, then an administrator uses
**Reset Label Request**. Only a definite rejection (HTTP 4xx, or `approved: false` for a void) can be
retried. For a void, the next shipment sync also resolves the doubt if ShipStation did void the label.

## Roles

| Group | Can |
|---|---|
| ShipStation User (Read Only) | See orders, shipments, queues, exception board, assistant answers. |
| ShipStation Manager | + get rates, buy a label, assign/recheck work, prepare batches, start return cases. |
| ShipStation Admin | + credentials, connection tests, manual syncs, approvals, webhook secret, inbox retry, reset label request, void requests (only when enabled on the instance). System administrators inherit this group. |

Manual operations run as the integration (like the scheduled jobs) after the role and company are
checked, so an administrator does not also need Sales or Inventory rights. Every model is
company-scoped by record rules.

## Setup

1. **ShipStation > Instances > New.** Enter the V1 API key and secret and the numeric store id.
   Optionally add a separate V2 key (never reuse the V1 key). Set **Order Currency** if the store does not
   sell in the company currency: ShipStation V1 orders do not state a currency, so order totals and dashboard
   revenue are reported in this one (empty = company currency).
2. **Check Setup** (local only), then **Test V1 Connection** / **Test V2 Connection** (read-only requests).
3. Optional: select an inventory warehouse for the read-only comparison.
4. Leave the base URL as `https://api.shipstation.com`. It must be `https://*.shipstation.com`
   because the V2 key is sent to it.
5. Run **Sync Orders** and **Sync Shipments**, then work the queues in **Operations Home**.

### How the store id is used

Every order, shipment and webhook look-up request is filtered by the numeric store id you enter, so a
connection only ever sees that store. ShipStation's V1 API is asymmetric about stores, and the connector
follows it exactly:

- **Orders** carry their store only at `advancedOptions.storeId` (there is no top-level `storeId`). An order
  that names a different store is rejected; one that names none is trusted to the request filter.
- **Shipments** carry no store at all, so they are scoped only by the `storeId` filter of the request.
  A shipment that explicitly names another store is rejected.
- **Pushing an order** assigns the store through `advancedOptions.storeId` **when the order is created**.
  ShipStation cannot change the store of an existing order, so updates never send it. Without a store id
  ShipStation puts a new order into the account's first manual store.
- An empty store id means no store filter: every store of the account is read, and new orders go to the
  account's first manual store. Set the id whenever the account has more than one store.

### Webhooks (V1)

Set the system parameter `web.base.url` to your public **https** address. The **Webhook** tab then
shows the callback URL (it embeds a secret; **Rotate** invalidates the old one). Enable the event types
and register the URL in ShipStation. ShipStation V1 notifications are not signed, so the secret URL is the
authentication. Notifications are queued, de-duplicated and fetched by a cron job with retries; failed ones
appear in **Webhook Inbox** (default filter "Needs Attention") and can be re-queued with **Retry Now**.
The old `/shipstation/webhook` endpoint is deliberately disabled and returns 404.
Odoo's access log shows the callback path with the secret replaced by `[redacted]`. A reverse proxy in front of
Odoo (nginx, Apache, a load balancer) logs the full URL in its own access log; restrict or filter that log too.

## Scheduled jobs

| Job | Default |
|---|---|
| Sync Orders | every 10 min, active |
| Sync Shipments | every 15 min, active |
| Sync Products | daily, active |
| Sync Customers / Inventory | inactive |
| Process Authenticated Webhooks | every minute, active |

None of them can buy a label or change stock. Each instance runs in its own savepoint, so one broken
connection cannot stop the others. Processed webhooks are purged after 30 days, request logs after 14
days (failures after 90).

## Time zones

ShipStation V1 uses Pacific time (PST/PDT) for every date and time. The connector converts both
ways. During the one repeated hour when daylight saving ends the sync window is widened so nothing is missed.

## Troubleshooting

| Symptom | Likely cause / action |
|---|---|
| "HTTP 401" on test | Wrong key or secret; V1 and V2 keys are different. |
| Shipment stays "Needs Review" | Several unclaimed deliveries, or the delivery already has other tracking. Open it in the Workbench. |
| "No Matching Order" | The shipment was created outside Odoo, or the order has not been imported yet. Recheck later. |
| "Request Sent - Outcome Unknown" | See the label rule above. |
| Webhook URL empty | `web.base.url` is not https. |
| 429 / rate limited | ShipStation allows 40 calls per minute per key; the connector waits at most 45 s per call. |
| "no Ship From location" when buying a label | ShipStation needs a Ship From location before it can create labels. Add one in ShipStation (Settings > Shipping > Ship From Locations). No label was requested. |
| Push fails with HTTP 404 for one order | The order was deleted in ShipStation; ShipStation will not accept it again under the same key. Recreate it in ShipStation or leave it. |

## Upgrading from 19.0.1.0.0 (the build published before this release)

Upgrade a **copy** first, then production: `odoo-bin -u shipstation_connector -d <db>`. This path was
tested on a database installed from the published build.

| What happens | What you do |
|---|---|
| **The old `/shipstation/webhook` endpoint is disabled (404).** It accepted anonymous posts. | **Action required if you used webhooks:** open each connection > Webhook tab, copy the new callback URL and register it in ShipStation. |
| Every connection receives its own webhook secret (a plain update would have given them all the same one). | Nothing. |
| Shipments whose delivery already carries the same ShipStation shipment id are marked Linked. Others show as Pending. | Work the Pending ones in the Shipping Workbench (or leave them if they predate Odoo). |
| Sale orders imported by the old version are recognised by exact ShipStation id and never get duplicated. The old version recorded no store on them, so a blank store is accepted when the company has only one connection (otherwise ownership is ambiguous and they are left alone). Pending shipment placeholders with a blank store are adopted by their real shipment. | Nothing. |
| Manual syncs now run as the integration, and only administrators can start them. | Give people the ShipStation Manager/Admin groups they need. |
| Request logs older than 14 days (failures 90) and processed webhooks older than 30 days are purged by Odoo's autovacuum. | Export anything you must keep before upgrading. |

## Tests

```
# offline source/contract tests (no Odoo needed)
python -B -I devtools/run_offline_tests.py

# database-backed tests: use a DISPOSABLE database, never a production one
odoo-bin -c <conf> -d <disposable_db> -u shipstation_connector --stop-after-init \
         --test-enable --test-tags /shipstation_connector
```

The database suite replaces `requests` with a router that fails on any URL a test did not mock, so
it can never reach ShipStation or buy a label.

## Known limitations

- Verified against mocked HTTP and ShipStation's published documentation only; **no real authenticated
  ShipStation account has been exercised.** Run the checklist in `devtools/` against a sandbox store first.
- V2 is used for the connection test and optional product/inventory reads; orders, shipments, rates and
  labels use V1. V2 webhooks (signed) are not supported.
- The numbered V1 hosts (`ssapiN.shipstation.com`) in webhook URLs are accepted on the strength of
  third-party reports; the supplied URL is never fetched, so this has no network impact.
- The void request (`POST /shipments/voidlabel`, reply `approved`/`message`) follows ShipStation's
  published documentation and is covered by mocked tests only; verify it with a sandbox label before enabling it.
  ShipStation may keep the order's status as shipped after a void; use **Pull from ShipStation** before buying a replacement.
- Tested on Odoo 19 Community, on-premise. Enterprise and Odoo.sh are expected to work (standard apps only) but are not verified.

## Release notes 19.0.1.1.0

Runtime-tested (Odoo test suite on an upgraded PostgreSQL 18 database, plus a browser pass). Fixes order
push on installs without `website_sale`, manager rate refresh, Pacific-time handling, shipment listing
parameters, assistant ACLs, hard-failing unmatched shipments, voided labels, and adds the guarded label
purchase, webhook retry and return validation described above.
