
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class ShipStationPackagePreset(models.Model):
    _name = "shipstation.package.preset"
    _description = "ShipStation Package Preset"
    _order = "name, id"

    name = fields.Char(required=True)
    instance_id = fields.Many2one("shipstation.instance", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(related="instance_id.company_id", store=True, index=True)
    active = fields.Boolean(default=True)
    package_code = fields.Char(required=True)
    weight_value = fields.Float(required=True)
    weight_units = fields.Selection(
        [("ounces", "Ounces"), ("pounds", "Pounds"), ("grams", "Grams")],
        required=True, default="ounces",
    )
    length = fields.Float()
    width = fields.Float()
    height = fields.Float()
    dimension_units = fields.Selection(
        [("inches", "Inches"), ("centimeters", "Centimeters")],
        required=True, default="inches",
    )

    _sql_constraints = [
        ("unique_preset", "unique(instance_id, name)",
         "Package preset names must be unique per ShipStation instance."),
    ]

    @api.constrains("weight_value", "length", "width", "height")
    def _validate_package_values(self):
        for preset in self:
            dims = [preset.length, preset.width, preset.height]
            if preset.weight_value <= 0 or any(v < 0 for v in dims):
                raise ValidationError(_("Package weight must be positive and dimensions must not be negative."))
            if any(dims) and not all(v > 0 for v in dims):
                raise ValidationError(_("Enter all three positive dimensions or leave all dimensions empty."))
