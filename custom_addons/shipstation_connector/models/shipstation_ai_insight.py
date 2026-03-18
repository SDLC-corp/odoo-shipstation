import json

from odoo import api, fields, models


class ShipStationAIInsight(models.Model):
    _name = "shipstation.ai.insight"
    _description = "ShipStation AI Insight"
    _order = "generated_at desc, id desc"

    name = fields.Char(required=True, default="AI Insight")
    instance_id = fields.Many2one("shipstation.instance", string="Instance", ondelete="cascade")
    scope = fields.Selection(
        [("instance", "Instance"), ("all", "All Instances")],
        default="all",
        required=True,
    )
    range_days = fields.Integer(default=30, required=True)
    summary_text = fields.Text()
    insight_json = fields.Text()
    status = fields.Selection(
        [("draft", "Draft"), ("success", "Success"), ("fallback", "Fallback"), ("failed", "Failed")],
        default="draft",
        required=True,
    )
    generated_at = fields.Datetime()
    error_message = fields.Text()

    _sql_constraints = [
        (
            "shipstation_ai_insight_scope_uniq",
            "unique(instance_id, scope, range_days)",
            "Only one latest AI insight record is stored per scope and date range.",
        )
    ]

    @api.model
    def upsert_latest(self, values):
        domain = [
            ("scope", "=", values.get("scope")),
            ("range_days", "=", values.get("range_days")),
        ]
        if values.get("scope") == "instance":
            domain.append(("instance_id", "=", values.get("instance_id")))
        else:
            domain.append(("instance_id", "=", False))
        record = self.search(domain, limit=1)
        if record:
            record.write(values)
            return record
        return self.create(values)

    def get_payload(self):
        self.ensure_one()
        try:
            payload = json.loads(self.insight_json or "{}")
        except Exception:
            payload = {}
        payload.update(
            {
                "summary_text": self.summary_text or "",
                "status": self.status,
                "generated_at": self.generated_at,
                "error_message": self.error_message or "",
            }
        )
        return payload
