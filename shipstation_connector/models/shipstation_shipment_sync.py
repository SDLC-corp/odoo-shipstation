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

    def _find_existing_shipment(self, instance, shipment_id="", order_id="", order_number="", store_id="", tracking_number=""):
        domain = [("instance_id", "=", instance.id)]
        if shipment_id:
            domain.append(("shipstation_shipment_id", "=", shipment_id))
            existing = self.search(domain, limit=1)
            if existing:
                return existing

        fallback_domain = [("instance_id", "=", instance.id)]
        if order_id:
            fallback_domain.append(("shipstation_order_id", "=", order_id))
        elif order_number:
            fallback_domain.append(("shipstation_order_number", "=", order_number))
        else:
            return self.browse()

        if store_id:
            fallback_domain.append(("shipstation_store_id", "=", store_id))

        if tracking_number:
            with_tracking = self.search(fallback_domain + [("tracking_number", "=", tracking_number)], limit=1)
            if with_tracking:
                return with_tracking

        return self.search(
            fallback_domain + [("tracking_number", "=", False)],
            order="ship_date desc, id desc",
            limit=1,
        )

    def _upsert_from_payload(self, instance, shipment_data):
        shipment_id = str(shipment_data.get("shipmentId") or "")
        order_id = str(shipment_data.get("orderId") or "")
        order_number = shipment_data.get("orderNumber") or ""
        store_id = str(shipment_data.get("storeId") or "")
        tracking_number = shipment_data.get("trackingNumber")
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
        existing = self._find_existing_shipment(
            instance,
            shipment_id=shipment_id,
            order_id=order_id,
            order_number=order_number,
            store_id=store_id,
            tracking_number=tracking_number,
        )
        if existing:
            existing.write(vals)
            return existing
        return self.create(vals)

    def _upsert_placeholder_for_order(self, instance, order_data):
        order_id = str(order_data.get("orderId") or "")
        order_number = str(order_data.get("orderNumber") or "")
        store_id = str(order_data.get("storeId") or "")
        existing = self._find_existing_shipment(
            instance,
            order_id=order_id,
            order_number=order_number,
            store_id=store_id,
        )
        vals = {
            "instance_id": instance.id,
            "shipstation_shipment_id": False,
            "shipstation_order_id": order_id or False,
            "shipstation_order_number": order_number or False,
            "shipstation_store_id": store_id or False,
            "carrier_code": False,
            "tracking_number": False,
            "tracking_url": False,
            "service_code": False,
            "ship_date": instance._parse_ss_datetime(order_data.get("shipDate") or order_data.get("modifyDate")),
            "status": "shipped_pending_tracking",
            "synced_on": fields.Datetime.now(),
            "payload": str(order_data),
        }
        if existing:
            if existing.shipstation_shipment_id:
                return existing
            existing.write(vals)
            return existing
        return self.create(vals)

    def sync_for_order(self, instance, order_data, create_placeholder=True):
        order_id = str(order_data.get("orderId") or "").strip()
        order_number = str(order_data.get("orderNumber") or "").strip()
        store_id = str(order_data.get("storeId") or instance.store_id or "").strip()
        params = {}
        if order_id.isdigit():
            params["orderId"] = int(order_id)
        elif order_number:
            params["orderNumber"] = order_number
        if store_id.isdigit():
            params["storeId"] = int(store_id)
        if not params:
            return self.browse()

        data = instance._ss_request("GET", "/shipments", params=params)
        shipments = (data or {}).get("shipments") or []
        records = self.browse()
        for shipment in shipments:
            shipment_order_id = str(shipment.get("orderId") or "").strip()
            shipment_order_number = str(shipment.get("orderNumber") or "").strip()
            if order_id and shipment_order_id and shipment_order_id != order_id:
                continue
            if order_number and shipment_order_number and shipment_order_number != order_number:
                continue
            records |= self._upsert_from_payload(instance, shipment)

        if records or not create_placeholder:
            return records
        return self._upsert_placeholder_for_order(instance, order_data)

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
