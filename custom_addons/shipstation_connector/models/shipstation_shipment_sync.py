from odoo import fields, models, _
from odoo.exceptions import UserError


class ShipStationShipmentSync(models.Model):
    _name = "shipstation.shipment.sync"
    _description = "ShipStation Shipment Sync"
    _order = "ship_date desc"

    instance_id = fields.Many2one("shipstation.instance", ondelete="cascade", required=True)
    company_id = fields.Many2one(related="instance_id.company_id", store=True)
    shipstation_shipment_id = fields.Char(index=True)
    shipstation_order_id = fields.Char(index=True)
    shipstation_order_number = fields.Char(index=True)
    shipstation_store_id = fields.Char(index=True)
    carrier_code = fields.Char()
    tracking_number = fields.Char()
    tracking_url = fields.Char()
    service_code = fields.Char()
    ship_date = fields.Datetime()
    status = fields.Char()
    synced_on = fields.Datetime(default=fields.Datetime.now)
    payload = fields.Text()

    def _upsert_from_payload(self, instance, shipment_data):
        shipment_id = str(shipment_data.get("shipmentId") or "")
        order_id = str(shipment_data.get("orderId") or "")
        order_number = shipment_data.get("orderNumber") or ""
        store_id = str(shipment_data.get("storeId") or "")
        vals = {
            "instance_id": instance.id,
            "shipstation_shipment_id": shipment_id or False,
            "shipstation_order_id": order_id or False,
            "shipstation_order_number": order_number or False,
            "shipstation_store_id": store_id or False,
            "carrier_code": shipment_data.get("carrierCode") or shipment_data.get("carrierName"),
            "tracking_number": shipment_data.get("trackingNumber"),
            "tracking_url": shipment_data.get("trackingUrl"),
            "service_code": shipment_data.get("serviceCode"),
            "ship_date": instance._parse_ss_datetime(shipment_data.get("shipDate")),
            "status": shipment_data.get("shipmentStatus"),
            "synced_on": fields.Datetime.now(),
            "payload": str(shipment_data),
        }
        existing = self.search(
            [
                ("shipstation_shipment_id", "=", shipment_id),
                ("instance_id", "=", instance.id),
            ],
            limit=1,
        )
        if existing:
            existing.write(vals)
        else:
            self.create(vals)

    def action_open_tracking(self):
        self.ensure_one()
        if not self.tracking_url:
            raise UserError(_("No tracking URL available for this shipment."))
        return {
            "type": "ir.actions.act_url",
            "url": self.tracking_url,
            "target": "new",
        }

    def action_open_delivery_order(self):
        self.ensure_one()
        domain = [("picking_type_code", "=", "outgoing")]
        if self.shipstation_order_id:
            domain = [("shipstation_order_id", "=", self.shipstation_order_id)] + domain
        elif self.shipstation_order_number:
            domain = [("origin", "ilike", self.shipstation_order_number)] + domain
        pickings = self.env["stock.picking"].search(domain)
        if not pickings:
            raise UserError(_("No delivery order found for this shipment."))
        action = self.env.ref("stock.action_picking_tree_all").read()[0]
        action["domain"] = [("id", "in", pickings.ids)]
        if len(pickings) == 1:
            action["view_mode"] = "form"
            action["res_id"] = pickings.id
        return action
