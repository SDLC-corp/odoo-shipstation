"""Operator workbench for shipment exceptions and safe local reconciliation.

No ShipStation write APIs are called by these actions. All changes occur in
Odoo and require explicit role/company permissions.
"""
import logging

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError, ValidationError

_logger = logging.getLogger(__name__)

MAX_WORKBENCH_BATCH = 100


class ShipStationShipmentWorkbench(models.Model):
    _inherit = "shipstation.shipment.sync"

    review_owner_id = fields.Many2one(
        "res.users", string="Assigned To", index=True, copy=False,
        domain="[('share', '=', False)]",
        help="Operator responsible for investigating an unlinked shipment.",
    )
    review_priority = fields.Selection(
        [("0", "Normal"), ("1", "High"), ("2", "Urgent")],
        string="Review Priority", default="0", index=True, copy=False,
    )
    review_note = fields.Text(
        string="Operator Notes", copy=False,
        help="Internal handover notes. Do not put API keys or customer passwords here.",
    )
    review_last_checked_at = fields.Datetime(
        string="Last Link Check", readonly=True, copy=False,
    )
    review_check_count = fields.Integer(
        string="Link Checks", readonly=True, default=0, copy=False,
    )

    @api.constrains("review_owner_id", "instance_id")
    def _check_review_owner_company(self):
        for shipment in self:
            owner = shipment.review_owner_id
            if owner and shipment.company_id not in owner.company_ids:
                raise ValidationError(_("Assigned operator must have access to the shipment company."))

    def _check_workbench_operator(self, admin_only=False):
        """Enforce role and company permissions on public RPC-callable methods."""
        user = self.env.user
        if self.env.su:
            return
        group = ("shipstation_connector.group_shipstation_admin" if admin_only
                 else "shipstation_connector.group_shipstation_manager")
        if not user.has_group(group):
            raise AccessError(_("You are not authorized for this shipping operation."))
        self.check_access_rights("write")
        self.check_access_rule("write")
        for shipment in self:
            if shipment.company_id not in self.env.companies:
                raise AccessError(_("Shipment belongs to a company outside your active companies."))

    def _check_workbench_batch_size(self):
        if not self:
            raise UserError(_("Select shipments before using this action."))
        if len(self) > MAX_WORKBENCH_BATCH:
            raise UserError(_("Select no more than %s shipments at once.") % MAX_WORKBENCH_BATCH)

    @staticmethod
    def _workbench_notice(title, message, level="info"):
        return {
            "type": "ir.actions.client", "tag": "display_notification",
            "params": {
                "title": title, "message": message, "type": level,
                "sticky": level == "warning",
            },
        }

    def action_claim_review(self):
        """Assign unresolved shipments to the current operator, without remote calls."""
        self._check_workbench_batch_size()
        self._check_workbench_operator()
        pending = self.filtered(lambda s: s.link_state in ("pending", "review"))
        if pending:
            pending.write({"review_owner_id": self.env.user.id})
        return self._workbench_notice(
            _("Shipping Workbench"),
            _("Assigned %s unresolved shipments to you; %s already linked shipments skipped.")
            % (len(pending), len(self) - len(pending)),
            "success" if pending else "info",
        )

    def action_release_review(self):
        """Owner may release own work; an administrator may release any work."""
        self._check_workbench_batch_size()
        self._check_workbench_operator()
        admin = self.env.su or self.env.user.has_group(
            "shipstation_connector.group_shipstation_admin"
        )
        for shipment in self:
            if shipment.review_owner_id and not admin and shipment.review_owner_id != self.env.user:
                raise AccessError(_("You can only release shipments assigned to you."))
        self.write({"review_owner_id": False})
        return self._workbench_notice(
            _("Shipping Workbench"),
            _("Released %s shipment assignments.") % len(self), "success",
        )

    def action_bulk_recheck_delivery_links(self):
        """Safely re-evaluate Odoo deliveries; never buy labels or call ShipStation.

        Savepoints allow the remaining shipments to continue if one local
        mapping is corrupt. Only existing local order/picking data is read.
        """
        self._check_workbench_batch_size()
        self._check_workbench_operator(admin_only=True)
        results = {"linked": 0, "pending": 0, "review": 0, "unmatched": 0, "voided": 0, "failed": 0}
        for shipment in self:
            try:
                with self.env.cr.savepoint():
                    # Role and company were verified by _check_workbench_operator above.
                    outcome = shipment.instance_id.sudo()._reconcile_single_shipment(shipment.sudo())
                    if outcome not in results or outcome == "failed":
                        raise UserError(_("Unexpected reconciliation result."))
                    shipment.write({
                        "review_last_checked_at": fields.Datetime.now(),
                        "review_check_count": shipment.review_check_count + 1,
                    })
                results[outcome] += 1
            except Exception:
                # Do not expose raw remote payloads or exception strings to the UI.
                _logger.exception("Local ShipStation delivery recheck failed for record %s", shipment.id)
                results["failed"] += 1
        message = _("Linked: %(linked)s · Pending: %(pending)s · Review: %(review)s · "
                    "No order: %(unmatched)s · Voided: %(voided)s · Failed: %(failed)s") % results
        return self._workbench_notice(
            _("Bulk Delivery Reconciliation"), message,
            "warning" if results["failed"] or results["review"] else "success",
        )

    def action_open_sales_order(self):
        self.ensure_one()
        self.check_access_rights("read")
        self.check_access_rule("read")
        order = self.instance_id._find_order(
            self.shipstation_order_id, self.shipstation_order_number,
            self.shipstation_store_id,
        )
        if (not order or order.company_id != self.company_id
                or not order.shipstation_instance_id
                or order.shipstation_instance_id != self.instance_id):
            raise UserError(_("No matching instance-owned Odoo sales order was found."))
        order.check_access_rights("read")
        order.check_access_rule("read")
        return {
            "type": "ir.actions.act_window", "name": _("Sales Order"),
            "res_model": "sale.order", "view_mode": "form", "res_id": order.id,
            "target": "current",
        }
