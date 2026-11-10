"""Upgrade from the published 15.0.1.0.0 build.

Two things a plain module update gets wrong for existing databases (found by upgrading a database
installed from the published build):

1. ``webhook_token`` is a new field whose default is a function. Odoo evaluates a callable default
   ONCE when it adds the column and gives every existing row that same value, so all existing
   connections would share one webhook secret and a URL for one connection would authenticate against
   another. Every connection that shares (or lacks) a secret gets its own.
2. ``link_state`` is new and defaults to "pending", so every historical shipment would show up as an
   unresolved delivery link even though the published sync had already written its tracking onto the
   delivery. Shipments that are provably linked (exactly one outgoing delivery of the same company
   carries the same ShipStation shipment id) are marked linked. Everything else stays pending for the
   Shipping Workbench.

3. ShipStation V1 orders state no currency, so the published build stored order mirrors with a blank
   currency and the dashboard could not report revenue. Blank currencies are filled with the currency of
   the connection's company (the new per-connection "Order Currency" is empty after the upgrade).

All steps are idempotent and use plain SQL, so they are safe to re-run.
"""
import logging
import secrets

_logger = logging.getLogger(__name__)


def _give_every_connection_its_own_webhook_secret(cr):
    cr.execute(
        """
        SELECT id FROM shipstation_instance
         WHERE webhook_token IS NULL OR webhook_token = ''
            OR webhook_token IN (
                 SELECT webhook_token FROM shipstation_instance
                  WHERE webhook_token IS NOT NULL GROUP BY webhook_token HAVING count(*) > 1)
        """
    )
    ids = [row[0] for row in cr.fetchall()]
    for instance_id in ids:
        cr.execute("UPDATE shipstation_instance SET webhook_token = %s WHERE id = %s",
                   (secrets.token_urlsafe(32), instance_id))
    _logger.info("ShipStation upgrade: issued a distinct webhook secret to %s connection(s)", len(ids))
    return len(ids)


def _link_shipments_already_on_a_delivery(cr):
    cr.execute(
        """
        UPDATE shipstation_shipment_sync s
           SET picking_id = m.picking_id, link_state = 'linked'
          FROM (
                SELECT sh.id AS shipment_id, min(p.id) AS picking_id
                  FROM shipstation_shipment_sync sh
                  JOIN shipstation_instance i ON i.id = sh.instance_id
                  JOIN stock_picking p ON p.shipstation_shipment_id = sh.shipstation_shipment_id
                                      AND p.company_id = i.company_id
                  JOIN stock_picking_type t ON t.id = p.picking_type_id AND t.code = 'outgoing'
                 WHERE sh.picking_id IS NULL
                   AND sh.link_state = 'pending'
                   AND COALESCE(sh.shipstation_shipment_id, '') <> ''
                 GROUP BY sh.id
                HAVING count(p.id) = 1
               ) m
         WHERE s.id = m.shipment_id
        """
    )
    _logger.info("ShipStation upgrade: marked %s historical shipment(s) as already linked", cr.rowcount)
    return cr.rowcount


def _fill_blank_order_currencies(cr):
    cr.execute(
        """
        UPDATE shipstation_order_sync o
           SET currency = cur.name
          FROM shipstation_instance i
          JOIN res_company c ON c.id = i.company_id
          JOIN res_currency cur ON cur.id = COALESCE(i.order_currency_id, c.currency_id)
         WHERE o.instance_id = i.id
           AND COALESCE(TRIM(o.currency), '') = ''
        """
    )
    _logger.info("ShipStation upgrade: filled the currency of %s order mirror(s)", cr.rowcount)
    return cr.rowcount


def migrate(cr, version):
    if not version:  # fresh install: nothing to repair
        return
    _give_every_connection_its_own_webhook_secret(cr)
    _link_shipments_already_on_a_delivery(cr)
    _fill_blank_order_currencies(cr)
