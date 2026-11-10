"""Read-only, company-scoped data service for the S08 OWL operations console.

No `sudo()`, external API calls, paid label operations, or stock writes occur here.
All queries use the caller's own Odoo ACLs and record rules.
"""

from odoo import api, models, _
from odoo.exceptions import AccessError


class ShipStationOperationsHome(models.Model):
    _inherit = "shipstation.instance"

    @api.model
    def get_operations_overview(self, instance_id=False):
        """Return lightweight dashboard counts plus limited work queues.

        The company is always the current Odoo company, never chosen by the
        browser. Cross-company instance IDs are explicitly rejected.
        """
        env = self.env
        user = env.user
        if not (env.su or user.has_group("shipstation_connector.group_shipstation_user_readonly")):
            raise AccessError(_("ShipStation access is required to view shipping operations."))

        company = env.company
        available = self.search([("company_id", "=", company.id)], order="name, id", limit=200)
        selected = False
        if instance_id not in (False, None, 0):
            if type(instance_id) is not int or instance_id <= 0:
                raise AccessError(_("Invalid ShipStation instance selection."))
            selected = self.browse(instance_id).exists()
            if not selected or selected.company_id != company:
                raise AccessError(_("This ShipStation instance is unavailable in the current company."))
            selected.check_access("read")

        # Explicit company-scoping supplements the module's global record
        # rules. No model is accessed using elevated privileges.
        domain = [("company_id", "=", company.id)]
        if selected:
            domain.append(("instance_id", "=", selected.id))

        shipment = env["shipstation.shipment.sync"]
        batch = env["shipstation.fulfillment.batch"]
        returns = env["shipstation.return.case"]
        orders = env["shipstation.order.sync"]
        for model in (shipment, batch, returns, orders):
            model.check_access("read")

        def count(model, extra):
            return model.search_count(domain + extra)

        metrics = {
            "needs_review": count(shipment, [("link_state", "=", "review")]),
            "pending_links": count(shipment, [("link_state", "=", "pending")]),
            "linked": count(shipment, [("link_state", "=", "linked")]),
            "my_queue": count(shipment, [
                ("link_state", "in", ["pending", "review"]),
                ("review_owner_id", "=", user.id),
            ]),
            "unassigned": count(shipment, [
                ("link_state", "in", ["pending", "review"]),
                ("review_owner_id", "=", False),
            ]),
            "active_batches": count(batch, [("state", "!=", "closed")]),
            "returns_review": count(returns, [("state", "=", "review")]),
            "synced_orders": count(orders, []),
        }
        can_manage = bool(env.su or user.has_group("shipstation_connector.group_shipstation_manager"))
        can_admin = bool(env.su or user.has_group("shipstation_connector.group_shipstation_admin"))
        metrics["failed_syncs"] = 0
        if can_manage:
            logs = env["shipstation.sync.log"]
            logs.check_access("read")
            # Sync logs have instance_id but no stored company_id.
            log_domain = [("instance_id.company_id", "=", company.id)]
            if selected:
                log_domain.append(("instance_id", "=", selected.id))
            metrics["failed_syncs"] = logs.search_count(log_domain + [("status", "=", "failed")])

        recent_shipments = shipment.search(
            domain + [("link_state", "in", ["review", "pending"])],
            order="review_priority desc, ship_date desc, id desc", limit=8,
        )
        recent_returns = returns.search(
            domain + [("state", "in", ["draft", "review", "approved"])],
            order="requested_at desc, id desc", limit=6,
        )
        recent_batches = batch.search(
            domain + [("state", "!=", "closed")],
            order="create_date desc, id desc", limit=5,
        )
        # Do not expose raw API response/error payloads in a dashboard.
        return {
            "company_id": company.id,
            "company_name": company.name,
            "user_id": user.id,
            "can_manage": can_manage,
            "can_admin": can_admin,
            "instance_id": selected.id if selected else False,
            "instances": [{"id": item.id, "name": item.name} for item in available],
            "metrics": metrics,
            "shipments": [{
                "id": item.id,
                "order_number": item.shipstation_order_number or _("Unnamed order"),
                "link_state": item.link_state,
                "priority": item.review_priority or "0",
                "owner": item.review_owner_id.name if item.review_owner_id else _("Unassigned"),
                "instance": item.instance_id.name,
                "tracking": item.tracking_number or "",
            } for item in recent_shipments],
            "returns": [{
                "id": item.id,
                "name": item.name,
                "reason": dict(item._fields["reason"].selection).get(item.reason, item.reason or ""),
                "state": item.state,
                "owner": item.owner_id.name if item.owner_id else _("Unassigned"),
            } for item in recent_returns],
            "batches": [{
                "id": item.id,
                "name": item.name,
                "state": item.state,
                "operator": item.operator_id.name if item.operator_id else _("Unassigned"),
            } for item in recent_batches],
        }
