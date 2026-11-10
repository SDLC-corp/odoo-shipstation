from urllib.parse import urlsplit

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError


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
    voided = fields.Boolean(
        index=True, copy=False,
        help="The label was voided in ShipStation (refund requested); it is not a valid shipment.",
    )
    void_date = fields.Datetime(copy=False)
    picking_id = fields.Many2one("stock.picking", index=True, ondelete="set null", copy=False)
    link_state = fields.Selection(
        [("pending", "Pending"), ("linked", "Linked"), ("review", "Needs Review"),
         ("voided", "Voided"), ("unmatched", "No Matching Order")],
        default="pending", index=True, copy=False,
    )
    link_message = fields.Text(copy=False)
    synced_on = fields.Datetime(default=fields.Datetime.now)
    payload = fields.Text()

    @api.depends("shipstation_order_number", "shipstation_order_id", "tracking_number", "shipstation_shipment_id")
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = rec._shipstation_display_label()

    def _shipstation_display_label(self):
        order = self.shipstation_order_number or self.shipstation_order_id or _("Shipment")
        reference = self.tracking_number or self.shipstation_shipment_id
        return "%s · %s" % (order, reference) if reference else order

    def name_get(self):
        # Odoo 16 labels records (many2one, breadcrumbs) through name_get, not display_name.
        return [(rec.id, rec._shipstation_display_label()) for rec in self]

    def _find_existing_shipment(self, instance, shipment_id="", order_id="", order_number="", store_id="", tracking_number=""):
        """Never merge two real shipment IDs into the same package record.

        Use the exact remote shipment identifier first. A placeholder can be
        adopted only for the same instance/store/order and only if it has no
        assigned remote shipment ID or tracking number.
        """
        shipment_id = str(shipment_id or "").strip()
        order_id = str(order_id or "").strip()
        order_number = str(order_number or "").strip()
        store_id = str(store_id or "").strip()
        tracking_number = str(tracking_number or "").strip()
        if instance.store_id and store_id and store_id != str(instance.store_id):
            raise UserError(_("Shipment belongs to another ShipStation store."))
        store_id = store_id or str(instance.store_id or "").strip()  # shipments name no store: the filter did
        root = [("instance_id", "=", instance.id)]
        if shipment_id:
            matches = self.search(root + [("shipstation_shipment_id", "=", shipment_id)], limit=2)
            if len(matches) > 1:
                raise UserError(_("Duplicate ShipStation shipment IDs need manual reconciliation."))
            if matches:
                if store_id and matches.shipstation_store_id and matches.shipstation_store_id != store_id:
                    raise UserError(_("Shipment ID is already linked to a different store."))
                return matches
        if not order_id and not (order_number and store_id):
            return self.browse()
        # The instance already pins the store. Rows written by the published build have a blank store
        # (shipments name none), so a blank store is accepted besides the configured one: such a pending
        # placeholder is adopted (and its store repaired) instead of being orphaned next to the real shipment.
        scoped = root + [("shipstation_store_id", "in", [store_id, False] if store_id else [False])]
        scoped.append(("shipstation_order_id", "=", order_id) if order_id else ("shipstation_order_number", "=", order_number))
        # Without a remote shipment ID, matching an identical tracking number is safe.
        if not shipment_id and tracking_number:
            matches = self.search(scoped + [("tracking_number", "=", tracking_number)], limit=2)
            if len(matches) > 1:
                raise UserError(_("Ambiguous tracking number within this instance and order."))
            if matches:
                return matches
        # Real shipments may only adopt a pending placeholder, never another
        # package with its own label/tracking identity.
        placeholders = self.search(scoped + [
            ("shipstation_shipment_id", "=", False), ("tracking_number", "=", False)
        ], limit=2)
        if len(placeholders) > 1:
            raise UserError(_("Multiple shipment placeholders require manual reconciliation."))
        return placeholders or self.browse()

    def _upsert_from_payload(self, instance, shipment_data):
        if not isinstance(shipment_data, dict):
            raise UserError(_("Invalid ShipStation shipment payload."))
        shipment_id = str(shipment_data.get("shipmentId") or "").strip()
        order_id = str(shipment_data.get("orderId") or "")
        order_number = shipment_data.get("orderNumber") or ""
        store_id = instance._attributed_store_id(shipment_data)
        tracking_number = shipment_data.get("trackingNumber")
        voided = shipment_data.get("voided") is True
        if not shipment_id and not str(tracking_number or "").strip():
            raise UserError(_("Shipment ID or tracking number required; refusing ambiguous shipment."))
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
            "voided": voided,
            "void_date": instance._parse_ss_datetime(shipment_data.get("voidDate")) if voided else False,
            # V1 shipment objects have no status field; derive one from the documented ``voided`` flag.
            "status": "voided" if voided else (shipment_data.get("shipmentStatus") or "label_created"),
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
        store_id = instance._attributed_store_id(order_data)
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
        store_id = instance._attributed_store_id(order_data)
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
            if not isinstance(shipment, dict):
                raise UserError(_("Invalid shipment row from ShipStation."))
            shipment_order_id = str(shipment.get("orderId") or "").strip()
            shipment_order_number = str(shipment.get("orderNumber") or "").strip()
            shipment_store_id = instance._remote_store_id(shipment)
            if store_id and shipment_store_id and shipment_store_id != store_id:
                continue
            if order_id and shipment_order_id and shipment_order_id != order_id:
                continue
            if order_number and shipment_order_number and shipment_order_number != order_number:
                continue
            records |= self._upsert_from_payload(instance, shipment)

        if records or not create_placeholder:
            return records
        return self._upsert_placeholder_for_order(instance, order_data)

    def action_retry_delivery_link(self):
        self.ensure_one()
        if not self.env.su and not self.env.user.has_group("shipstation_connector.group_shipstation_admin"):
            raise AccessError(_("Only ShipStation administrators can reconcile deliveries."))
        self.check_access_rights("write")
        self.check_access_rule("write")
        if self.company_id not in self.env.companies:
            raise AccessError(_("Shipment belongs to a company outside your active companies."))
        # Role and company were verified above; the lookup of sale orders and deliveries must not
        # depend on the administrator also holding Sales/Inventory rights.
        outcome = self.instance_id.sudo()._reconcile_single_shipment(self.sudo())
        return {
            "type": "ir.actions.client", "tag": "display_notification",
            "params": {
                "title": _("Delivery Link Review"),
                "message": _("Shipment link status: %s") % outcome,
                "type": "success" if outcome == "linked" else "warning",
                "sticky": outcome != "linked",
            },
        }

    def action_acknowledge_void(self):
        """An operator confirms a voided label was handled (replacement bought or not needed)."""
        if not self.env.su and not self.env.user.has_group("shipstation_connector.group_shipstation_manager"):
            raise AccessError(_("ShipStation Manager role required."))
        self.check_access_rights("write")
        self.check_access_rule("write")
        for shipment in self:
            if shipment.company_id not in self.env.companies:
                raise AccessError(_("Shipment belongs to a company outside your active companies."))
            if not shipment.voided:
                raise UserError(_("Only voided labels can be acknowledged."))
        self.write({"link_state": "voided",
                    "link_message": _("Void acknowledged by %s.") % self.env.user.name})
        return True

    def action_open_tracking(self):
        self.ensure_one()
        url = str(self.tracking_url or "").strip()
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise UserError(_("A valid HTTPS tracking URL is required."))
        return {"type": "ir.actions.act_url", "url": url, "target": "new"}

    def action_open_delivery_order(self):
        self.ensure_one()
        if self.picking_id:
            pickings = self.picking_id
            if pickings.company_id != self.company_id or pickings.picking_type_code != "outgoing":
                raise UserError(_("The linked delivery is not valid for this shipment company."))
        else:
            order = self.instance_id._find_order(
                self.shipstation_order_id, self.shipstation_order_number, self.shipstation_store_id
            )
            if not order or order.company_id != self.company_id:
                raise UserError(_("No matching company-scoped sales order found for this shipment."))
            pickings = order.picking_ids.filtered(
                lambda p: p.company_id == self.company_id and
                p.picking_type_code == "outgoing" and p.state != "cancel"
            )
        if not pickings:
            raise UserError(_("No outgoing delivery found for this shipment."))
        action = self.env.ref("stock.action_picking_tree_all").read()[0]
        action["domain"] = [("id", "in", pickings.ids)]
        if len(pickings) == 1:
            action["view_mode"] = "form"
            action["res_id"] = pickings.id
        return action
