from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    shipstation_ai_enabled = fields.Boolean(
        string="Enable ShipStation AI",
        config_parameter="shipstation_ai.enabled",
    )
    shipstation_ai_provider = fields.Char(
        string="AI Provider",
        config_parameter="shipstation_ai.provider",
    )
    shipstation_ai_api_key = fields.Char(
        string="AI API Key",
        config_parameter="shipstation_ai.api_key",
    )
    shipstation_ai_model = fields.Char(
        string="AI Model",
        config_parameter="shipstation_ai.model",
    )
    shipstation_ai_max_tokens = fields.Integer(
        string="AI Max Tokens",
        config_parameter="shipstation_ai.max_tokens",
    )
    shipstation_ai_endpoint = fields.Char(
        string="AI Endpoint",
        config_parameter="shipstation_ai.endpoint",
    )
