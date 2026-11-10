"""Human-reviewed bulk fulfillment preparation; deliberately no label purchases."""
from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError, ValidationError

MAX_FULFILLMENT_ORDERS = 100


class ShipStationFulfillmentBatch(models.Model):
    _name = "shipstation.fulfillment.batch"
    _description = "ShipStation Fulfillment Preparation Batch"
    _inherit = ["mail.thread"]
    _order = "create_date desc, id desc"

    name = fields.Char(required=True, default="New", copy=False, readonly=True)
    instance_id = fields.Many2one("shipstation.instance", required=True, ondelete="restrict", index=True)
    company_id = fields.Many2one(related="instance_id.company_id", store=True)
    operator_id = fields.Many2one("res.users", default=lambda self: self.env.user, required=True)
    state = fields.Selection(
        [("draft", "Draft"), ("review", "Needs Review"),
         ("ready", "Ready for Manual Shipping"), ("closed", "Closed")],
        default="draft", required=True, index=True, tracking=True,
    )
    line_ids = fields.One2many("shipstation.fulfillment.batch.line", "batch_id", string="Orders")
    note = fields.Text(help="Internal notes only; never store credentials here.")
    last_reviewed_at = fields.Datetime(readonly=True, copy=False)

    @api.model_create_multi
    def create(self, vals_list):
        if any(vals.get("state", "draft") != "draft" for vals in vals_list):
            raise AccessError(_("New fulfillment batches must begin in Draft."))
        records = super().create(vals_list)
        for record in records:
            if record.name == "New":
                record.name = "SHIP-BATCH-%06d" % record.id
        return records

    def write(self, vals):
        if any(field in vals for field in ("state", "instance_id", "company_id", "last_reviewed_at")):
            raise AccessError(_("Use batch workflow actions to change the readiness state."))
        return super().write(vals)

    def _require_manager(self):
        if not self.env.su and not self.env.user.has_group("shipstation_connector.group_shipstation_manager"):
            raise AccessError(_("ShipStation Manager role required for fulfillment preparation."))
        self.check_access_rights("write")
        self.check_access_rule("write")
        for batch in self:
            if batch.company_id not in self.env.companies:
                raise AccessError(_("Batch belongs to a company outside the active companies."))

    def action_refresh_readiness(self):
        """Recheck only local readiness. No ShipStation calls or label transactions."""
        self.ensure_one()
        self._require_manager()
        if self.state == "closed":
            raise UserError(_("Reopen the batch before reviewing readiness."))
        if not self.line_ids:
            raise UserError(_("Add ShipStation orders before validating this batch."))
        if len(self.line_ids) > MAX_FULFILLMENT_ORDERS:
            raise UserError(_("Fulfillment batches are limited to %s orders.") % MAX_FULFILLMENT_ORDERS)
        counts = {"ready": 0, "blocked": 0, "shipped": 0}
        for line in self.line_ids:
            state, note = line._review_local_preparation()
            super(ShipStationFulfillmentBatchLine, line).write({
                "state": state, "review_message": note, "last_reviewed_at": fields.Datetime.now(),
            })
            counts[state] += 1
        next_state = "ready" if counts["ready"] and not counts["blocked"] else "review"
        super(ShipStationFulfillmentBatch, self).write({
            "state": next_state, "last_reviewed_at": fields.Datetime.now(),
        })
        return {
            "type": "ir.actions.client", "tag": "display_notification",
            "params": {"title": _("Fulfillment Readiness"),
                       "message": _("Ready: %(ready)s | Blocked: %(blocked)s | Already Shipped: %(shipped)s") % counts,
                       "type": "warning" if counts["blocked"] else "success", "sticky": bool(counts["blocked"])},
        }

    def action_close_batch(self):
        self.ensure_one()
        self._require_manager()
        if not self.line_ids or any(line.state in ("pending", "ready", "blocked") for line in self.line_ids):
            raise UserError(_("Only batches with all orders recorded as shipped may be closed."))
        super(ShipStationFulfillmentBatch, self).write({"state": "closed"})
        return True

    def action_reopen_batch(self):
        self.ensure_one()
        self._require_manager()
        if self.state != "closed":
            raise UserError(_("Only closed batches may be reopened."))
        super(ShipStationFulfillmentBatch, self).write({"state": "review"})
        return True


class ShipStationFulfillmentBatchLine(models.Model):
    _name = "shipstation.fulfillment.batch.line"
    _description = "ShipStation Fulfillment Batch Order"
    _order = "id"

    batch_id = fields.Many2one("shipstation.fulfillment.batch", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(related="batch_id.company_id", store=True)
    instance_id = fields.Many2one(related="batch_id.instance_id", store=True)
    order_id = fields.Many2one("shipstation.order.sync", required=True, ondelete="restrict", index=True)
    state = fields.Selection(
        [("pending", "Not Checked"), ("ready", "Prepared"),
         ("blocked", "Needs Attention"), ("shipped", "Label Recorded")],
        default="pending", required=True, index=True,
    )
    review_message = fields.Char(readonly=True, copy=False)
    last_reviewed_at = fields.Datetime(readonly=True, copy=False)

    _sql_constraints = [
        (
            "unique_order_in_batch",
            "UNIQUE(batch_id, order_id)",
            "An order may appear only once in the same fulfillment batch.",
        ),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        if any(vals.get("state", "pending") != "pending" or
               "review_message" in vals or "last_reviewed_at" in vals or
               not vals.get("batch_id") for vals in vals_list):
            raise AccessError(_("New batch orders must start as pending."))
        for vals in vals_list:
            batch = self.env["shipstation.fulfillment.batch"].browse(vals["batch_id"])
            if batch.state != "draft":
                raise UserError(_("Orders may be added only while the batch is in Draft."))
        return super().create(vals_list)

    def write(self, vals):
        if any(k in vals for k in ("batch_id", "order_id", "state", "review_message", "last_reviewed_at")):
            raise AccessError(_("Use batch readiness review to update line status."))
        return super().write(vals)

    @api.constrains("batch_id", "order_id")
    def _check_order_instance(self):
        for line in self:
            if line.order_id.instance_id != line.batch_id.instance_id:
                raise ValidationError(_("The order must belong to this batch's ShipStation instance."))

    def _review_local_preparation(self):
        """Never make shipping network calls; only inspect a live local order snapshot."""
        self.ensure_one()
        order = self.order_id
        if order.instance_id != self.batch_id.instance_id:
            return "blocked", _("Order belongs to another instance or company.")
        if order.shipstation_shipment_id or order.label_url or order.tracking_number:
            return "shipped", _("Shipping identity already recorded; do not purchase again.")
        if (order.status or "").lower() != "awaiting_shipment":
            return "blocked", _("Order is not awaiting shipment; confirm its current status.")
        if not order.shipstation_order_id:
            return "blocked", _("No remote ShipStation order ID.")
        if not (order.carrier_code and order.service_code and order.package_code):
            return "blocked", _("Carrier, service and package must be selected.")
        try:
            order._ensure_fresh_rate_quote()
            match = order.rate_ids.filtered(lambda r:
                r.carrier_code == order.carrier_code and
                r.service_code == order.service_code and
                (r.package_code or "") == (order.package_code or ""))
            if not match:
                return "blocked", _("The selected shipping service is not among current quotes.")
        except UserError:
            return "blocked", _("Rates are missing, expired, or shipment details changed. Refresh rates.")
        return "ready", _("Prepared; open the order to purchase its label manually.")

    def action_open_shipping_order(self):
        self.ensure_one()
        self.check_access_rights("read")
        self.check_access_rule("read")
        self.order_id.check_access_rights("read")
        self.order_id.check_access_rule("read")
        return {
            "type": "ir.actions.act_window", "res_model": "shipstation.order.sync",
            "view_mode": "form", "res_id": self.order_id.id, "target": "current",
        }


class ShipStationOrderBulkPreparation(models.Model):
    _inherit = "shipstation.order.sync"

    def action_create_fulfillment_batch(self):
        """Bound Odoo list action. Operator explicitly selects up to 100 orders."""
        if not self or len(self) > MAX_FULFILLMENT_ORDERS:
            raise UserError(_("Select between 1 and %s ShipStation orders.") % MAX_FULFILLMENT_ORDERS)
        if not self.env.su and not self.env.user.has_group("shipstation_connector.group_shipstation_manager"):
            raise AccessError(_("Only ShipStation Managers can prepare fulfillment batches."))
        self.check_access_rights("read")
        self.check_access_rule("read")
        instances = self.mapped("instance_id")
        if len(instances) != 1:
            raise UserError(_("Select orders from one ShipStation instance only."))
        instance = instances[0]
        if instance.company_id not in self.env.companies:
            raise AccessError(_("The selected instance is outside your active companies."))
        if any(order.instance_id != instance for order in self):
            raise UserError(_("Cross-instance order selection is not permitted."))
        if self.env["shipstation.fulfillment.batch.line"].search_count([
            ("order_id", "in", self.ids),
            ("batch_id.state", "in", ["draft", "review", "ready"]),
        ]):
            raise UserError(_("One or more selected orders are already in an open fulfillment batch."))
        batch = self.env["shipstation.fulfillment.batch"].create({"instance_id": instance.id})
        self.env["shipstation.fulfillment.batch.line"].create([
            {"batch_id": batch.id, "order_id": order.id} for order in self
        ])
        batch.action_refresh_readiness()
        return {"type": "ir.actions.act_window", "res_model": "shipstation.fulfillment.batch",
                "view_mode": "form", "res_id": batch.id, "target": "current"}
