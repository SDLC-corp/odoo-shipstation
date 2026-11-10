"""Internal return case tracking. This never issues return labels/refunds/stock moves."""
import ast

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError, ValidationError


class ShipStationReturnCase(models.Model):
    _name = "shipstation.return.case"
    _description = "ShipStation Return Case (Manual Tracking)"
    _inherit = ["mail.thread"]
    _order = "create_date desc, id desc"

    name = fields.Char(default="New", required=True, copy=False, readonly=True)
    instance_id = fields.Many2one("shipstation.instance", required=True, ondelete="restrict", index=True)
    company_id = fields.Many2one(related="instance_id.company_id", store=True)
    order_id = fields.Many2one("shipstation.order.sync", ondelete="restrict", index=True)
    shipment_id = fields.Many2one("shipstation.shipment.sync", ondelete="restrict", index=True)
    reason = fields.Selection([
        ("damaged", "Damaged"), ("wrong_item", "Incorrect Item"),
        ("not_needed", "No Longer Needed"), ("other", "Other")], required=True, default="other")
    state = fields.Selection([
        ("draft", "Draft"), ("review", "Awaiting Approval"),
        ("approved", "Approved for Manual Handling"),
        ("closed", "Closed"), ("rejected", "Rejected")],
        default="draft", required=True, index=True, tracking=True,
    )
    owner_id = fields.Many2one("res.users", default=lambda self: self.env.user)
    requested_at = fields.Datetime(default=fields.Datetime.now, readonly=True)
    approved_by_id = fields.Many2one("res.users", readonly=True, copy=False, tracking=True)
    approved_at = fields.Datetime(readonly=True, copy=False)
    return_tracking_number = fields.Char(help="Enter tracking obtained outside this connector; no label is created here.")
    customer_note = fields.Text(help="Describe the return request. Do not include secrets.")
    resolution_note = fields.Text(help="Record the external return/refund resolution; no Odoo stock or accounting entries are created.")
    line_ids = fields.One2many("shipstation.return.case.line", "case_id", string="Items")

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get("order_id") and not vals.get("shipment_id"):
                raise ValidationError(_("Link this return to a ShipStation order or shipment."))
            if vals.get("state") not in (None, "draft"):
                raise AccessError(_("Return cases must start as drafts."))
            if vals.get("approved_at") or vals.get("approved_by_id"):
                raise AccessError(_("Return approval metadata is set only by approval actions."))
        records = super().create(vals_list)
        for rec in records:
            if rec.name == "New":
                rec.name = "SHIP-RETURN-%06d" % rec.id
        return records

    @api.constrains("instance_id", "order_id", "shipment_id", "owner_id")
    def _check_related_scope(self):
        for rec in self:
            if rec.order_id and rec.order_id.instance_id != rec.instance_id:
                raise ValidationError(_("Return order belongs to another ShipStation instance."))
            if rec.shipment_id and rec.shipment_id.instance_id != rec.instance_id:
                raise ValidationError(_("Return shipment belongs to another ShipStation instance."))
            if (rec.order_id and rec.shipment_id and rec.order_id.shipstation_order_id
                    and rec.shipment_id.shipstation_order_id
                    and rec.order_id.shipstation_order_id != rec.shipment_id.shipstation_order_id):
                raise ValidationError(_("Return order and shipment do not refer to the same ShipStation order."))
            if rec.owner_id and rec.company_id not in rec.owner_id.company_ids:
                raise ValidationError(_("Return operator must belong to the return company."))

    def _ordered_quantities(self):
        """SKU -> ordered quantity from the ShipStation order snapshot, or None if unknowable."""
        self.ensure_one()
        order = self.order_id
        if not order and self.shipment_id and self.shipment_id.shipstation_order_id:
            order = self.env["shipstation.order.sync"].search([
                ("instance_id", "=", self.instance_id.id),
                ("shipstation_order_id", "=", self.shipment_id.shipstation_order_id),
            ], limit=1)
        if not order or not order.payload:
            return None
        try:
            items = ast.literal_eval(order.payload).get("items")
        except (ValueError, SyntaxError, AttributeError):
            return None
        if not isinstance(items, list) or not items:
            return None
        ordered = {}
        for item in items:
            if isinstance(item, dict) and item.get("sku"):
                key = str(item["sku"]).strip().lower()
                try:
                    ordered[key] = ordered.get(key, 0.0) + float(item.get("quantity") or 0.0)
                except (TypeError, ValueError):
                    continue
        return ordered or None

    def _require_operator(self, admin=False):
        group = ("shipstation_connector.group_shipstation_admin" if admin else
                 "shipstation_connector.group_shipstation_manager")
        if not self.env.su and not self.env.user.has_group(group):
            raise AccessError(_("You do not have the required ShipStation return permissions."))
        self.check_access_rights("write")
        self.check_access_rule("write")
        for rec in self:
            if rec.company_id not in self.env.companies:
                raise AccessError(_("Return case belongs to another company."))

    def write(self, vals):
        # A direct write/RPC call cannot bypass approval or forge audit fields.
        if any(key in vals for key in ("state", "approved_by_id", "approved_at")):
            raise AccessError(_("Use the controlled return workflow buttons to change status."))
        if any(r.state in ("closed", "rejected") for r in self):
            raise UserError(_("Closed and rejected return cases cannot be edited."))
        if any(r.state == "approved" for r in self) and set(vals).intersection(
                {"instance_id", "order_id", "shipment_id", "reason"}):
            raise AccessError(_("Approved return identities cannot be changed."))
        return super().write(vals)

    def action_submit_review(self):
        self.ensure_one()
        self._require_operator()
        if self.state != "draft" or not self.line_ids:
            raise UserError(_("Add return items to a draft before submitting for review."))
        # Transition is validated here; non-admin direct RPC writes to state are blocked.
        super(ShipStationReturnCase, self).write({"state": "review"})
        return True

    def action_approve_manual_handling(self):
        self.ensure_one()
        self._require_operator(admin=True)
        if self.state != "review" or not self.line_ids:
            raise UserError(_("Only reviewed returns with items can be approved."))
        super(ShipStationReturnCase, self).write({
            "state": "approved", "approved_by_id": self.env.user.id,
            "approved_at": fields.Datetime.now(),
        })
        return True

    def action_close_manual_case(self):
        self.ensure_one()
        self._require_operator(admin=True)
        if self.state != "approved" or not (self.resolution_note or "").strip():
            raise UserError(_("Add a resolution note to an approved return before closing."))
        super(ShipStationReturnCase, self).write({"state": "closed"})
        return True

    def action_reject_case(self):
        self.ensure_one()
        self._require_operator(admin=True)
        if self.state != "review" or not (self.resolution_note or "").strip():
            raise UserError(_("A reviewed return needs a rejection reason in Resolution Notes."))
        super(ShipStationReturnCase, self).write({"state": "rejected"})
        return True

    def action_open_related_shipment(self):
        self.ensure_one()
        self.check_access_rights("read")
        self.check_access_rule("read")
        if not self.shipment_id:
            raise UserError(_("No shipment is linked to this return."))
        self.shipment_id.check_access_rights("read")
        self.shipment_id.check_access_rule("read")
        return {"type": "ir.actions.act_window", "res_model": "shipstation.shipment.sync",
                "view_mode": "form", "res_id": self.shipment_id.id, "target": "current"}


class ShipStationReturnCaseLine(models.Model):
    _name = "shipstation.return.case.line"
    _description = "ShipStation Return Item (Manual Tracking)"

    case_id = fields.Many2one("shipstation.return.case", required=True, ondelete="cascade")
    company_id = fields.Many2one(related="case_id.company_id", store=True)
    sku = fields.Char(required=True)
    description = fields.Char()
    quantity = fields.Float(default=1.0, required=True)
    condition = fields.Selection([("unknown", "Not Inspected"), ("new", "New"),
                                  ("opened", "Opened"), ("damaged", "Damaged")], default="unknown", required=True)

    @api.constrains("quantity")
    def _check_positive_quantity(self):
        for line in self:
            if line.quantity <= 0:
                raise ValidationError(_("Returned quantity must be positive."))

    @api.constrains("sku", "quantity", "case_id")
    def _check_against_ordered_quantity(self):
        """A customer cannot return more of a SKU than was ordered (when the order is known)."""
        for case in self.mapped("case_id"):
            ordered = case._ordered_quantities()
            if ordered is None:
                continue  # the order snapshot lists no items: nothing to compare against
            returned = {}
            for line in case.line_ids:
                key = (line.sku or "").strip().lower()
                returned[key] = returned.get(key, 0.0) + line.quantity
            for key, quantity in returned.items():
                if key not in ordered:
                    raise ValidationError(_("SKU %s was not part of the original order.", key.upper()))
                if quantity > ordered[key] + 1e-6:
                    raise ValidationError(_(
                        "Cannot return %(returned)s of %(sku)s: only %(ordered)s were ordered.",
                        returned=quantity, sku=key.upper(), ordered=ordered[key]))

    @api.model_create_multi
    def create(self, vals_list):
        if any(not v.get("case_id") or self.env["shipstation.return.case"].browse(v["case_id"]).state not in ("draft", "review")
               for v in vals_list):
            raise UserError(_("Return items may only be added to drafts or reviews."))
        return super().create(vals_list)

    def write(self, vals):
        if any(line.case_id.state not in ("draft", "review") for line in self):
            raise UserError(_("Return items cannot be changed after approval."))
        return super().write(vals)

    def unlink(self):
        if any(line.case_id.state not in ("draft", "review") for line in self):
            raise UserError(_("Return items cannot be deleted after approval."))
        return super().unlink()


class ShipStationReturnSourceActions(models.Model):
    _inherit = "shipstation.shipment.sync"

    def action_start_return_case(self):
        self.ensure_one()
        if not self.env.su and not self.env.user.has_group("shipstation_connector.group_shipstation_manager"):
            raise AccessError(_("ShipStation Manager role required to start a return case."))
        self.check_access_rights("read")
        self.check_access_rule("read")
        if self.company_id not in self.env.companies:
            raise AccessError(_("Shipment is outside your active companies."))
        return {"type": "ir.actions.act_window", "res_model": "shipstation.return.case",
                "view_mode": "form", "target": "current",
                "context": {"default_instance_id": self.instance_id.id, "default_shipment_id": self.id}}
