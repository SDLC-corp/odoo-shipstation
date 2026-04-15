# ShipStation Connector

This addon provides ShipStation instances, sync data listings, reports, cron jobs,
and a dashboard for Odoo 18.

## Setup
1) Install the module.
2) Go to ShipStation > Instances.
3) Configure API Key/Secret and optional Store ID.
4) Use Test Connection, Sync Orders, and Sync Shipments.

## Cron Jobs
- ShipStation Sync Orders
- ShipStation Sync Shipments

## Webhook (optional)
POST JSON payloads to `/shipstation/webhook`. The addon creates a report entry.
