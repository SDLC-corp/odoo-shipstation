from odoo import fields, models, _
from odoo.exceptions import UserError


class ShipStationCustomerSync(models.Model):
    _name = "shipstation.customer.sync"
    _description = "ShipStation Customer Sync"
    _order = "modify_date desc"

    instance_id = fields.Many2one("shipstation.instance", ondelete="cascade", required=True)
    company_id = fields.Many2one(related="instance_id.company_id", store=True)
    shipstation_customer_id = fields.Char(index=True)
    name = fields.Char()
    email = fields.Char()
    phone = fields.Char()
    modify_date = fields.Datetime()
    synced_on = fields.Datetime(default=fields.Datetime.now)
    payload = fields.Text()

    def _payload_get(self, payload, key, default=None):
        if not isinstance(payload, dict) or not key:
            return default
        current = payload
        for part in str(key).split("."):
            if not isinstance(current, dict) or part not in current:
                return default
            current = current.get(part)
        return current

    def _extract_listing_overrides_from_mapping(self, instance, payload):
        overrides = {}
        if not instance or not isinstance(payload, dict):
            return overrides

        field_to_listing = {
            "name": "name",
            "email": "email",
            "phone": "phone",
            "mobile": "phone",
        }
        for mapping in instance._get_field_mappings("customer"):
            listing_field = field_to_listing.get(mapping.odoo_field_id.name)
            if not listing_field:
                continue
            value = self._payload_get(payload, mapping.shipstation_field_key.name)
            if value in (None, ""):
                continue
            overrides[listing_field] = value
        return overrides

    def _build_candidate_push_calls(self):
        self.ensure_one()
        customer_id = str(self.shipstation_customer_id or "").strip()
        has_numeric_id = customer_id.isdigit()

        calls = []
        if has_numeric_id:
            # Common REST-style update endpoint.
            calls.append(("PUT", f"/customers/{int(customer_id)}"))
            # Legacy-style update endpoint used by some clusters.
            calls.append(("POST", "/customers/updatecustomer"))
        # Common REST-style create/upsert endpoint.
        calls.append(("POST", "/customers"))
        # Legacy-style create endpoint used by some clusters.
        calls.append(("POST", "/customers/createcustomer"))
        return calls

    def _is_not_found_endpoint_error(self, message):
        text = str(message or "").lower()
        return (
            "error 404" in text
            or "no http resource was found" in text
            or "no action was found on the controller" in text
        )

    def _build_payload(self):
        self.ensure_one()
        payload = {
            "customerId": self.shipstation_customer_id or None,
            "name": self.name or "",
            "email": self.email or "",
            "phone": self.phone or "",
        }
        if not payload["customerId"]:
            payload.pop("customerId", None)
        return payload

    def action_push_to_shipstation(self):
        self.ensure_one()
        if not self.instance_id:
            raise UserError(_("Missing ShipStation instance."))
        payload = self._build_payload()
        response = None
        last_error = None
        for method, endpoint in self._build_candidate_push_calls():
            try:
                response = self.instance_id._ss_request(method, endpoint, data=payload)
                break
            except UserError as exc:
                last_error = exc
                if self._is_not_found_endpoint_error(exc):
                    continue
                raise
        if response is None:
            if last_error and self._is_not_found_endpoint_error(last_error):
                raise UserError(
                    _(
                        "ShipStation customer write endpoints are not available on this API cluster. "
                        "Customer pull can still work, but push/update customers is not supported."
                    )
                )
            if last_error:
                raise last_error
            raise UserError(_("Unable to push customer to ShipStation."))

        customer_id = response.get("customerId") or response.get("customer_id")
        if customer_id:
            self.shipstation_customer_id = str(customer_id)
        if payload.get("name"):
            self.name = str(payload.get("name")).strip()
        if payload.get("email"):
            self.email = str(payload.get("email")).strip()
        if payload.get("phone"):
            self.phone = str(payload.get("phone")).strip()
        self.synced_on = fields.Datetime.now()
        self.payload = str(response or payload)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Customer Pushed"),
                "message": _("Customer updated in ShipStation."),
                "type": "success",
                "sticky": False,
            },
        }

    def action_pull_from_shipstation(self):
        self.ensure_one()
        if not self.instance_id:
            raise UserError(_("Missing ShipStation instance."))
        customer_id = str(self.shipstation_customer_id or "").strip()
        email = str(self.email or "").strip()
        if not ((customer_id and customer_id.isdigit()) or email):
            raise UserError(_("Set a valid numeric ShipStation customer ID or email before pulling."))

        customer_data = {}
        if customer_id and customer_id.isdigit():
            # More reliable and officially documented lookup by id.
            customer_data = self.instance_id._ss_request("GET", f"/customers/{int(customer_id)}")
        else:
            data = self.instance_id._ss_request("GET", "/customers", params={"email": email})
            customers = data.get("customers", [])
            if customers:
                customer_data = customers[0]

        if not customer_data:
            raise UserError(_("No customer found in ShipStation for the given ID/email."))
        synced = self._upsert_from_payload(self.instance_id, customer_data)
        if synced and synced.id != self.id:
            self.write(
                {
                    "shipstation_customer_id": synced.shipstation_customer_id,
                    "name": synced.name,
                    "email": synced.email,
                    "phone": synced.phone,
                    "modify_date": synced.modify_date,
                    "synced_on": fields.Datetime.now(),
                    "payload": synced.payload,
                }
            )
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Customer Pulled"),
                "message": _("Customer refreshed from ShipStation."),
                "type": "success",
                "sticky": False,
            },
        }

    def _upsert_from_payload(self, instance, customer_data):
        customer_id = str(customer_data.get("customerId") or customer_data.get("customer_id") or "")
        overrides = self._extract_listing_overrides_from_mapping(instance, customer_data)
        name_val = overrides.get("name", customer_data.get("name") or customer_data.get("customerName"))
        email_val = overrides.get("email", customer_data.get("email") or customer_data.get("customerEmail"))
        phone_val = overrides.get("phone", customer_data.get("phone"))
        vals = {
            "instance_id": instance.id,
            "shipstation_customer_id": customer_id or False,
            "name": str(name_val).strip() if name_val not in (None, "") else False,
            "email": str(email_val).strip() if email_val not in (None, "") else False,
            "phone": str(phone_val).strip() if phone_val not in (None, "") else False,
            "modify_date": instance._parse_ss_datetime(customer_data.get("modifyDate")),
            "synced_on": fields.Datetime.now(),
            "payload": str(customer_data),
        }
        existing = False
        if customer_id:
            existing = self.search(
                [
                    ("shipstation_customer_id", "=", customer_id),
                    ("instance_id", "=", instance.id),
                ],
                limit=1,
            )
        if not existing and vals.get("email"):
            existing = self.search(
                [
                    ("email", "=", vals.get("email")),
                    ("instance_id", "=", instance.id),
                ],
                limit=1,
            )
        if existing:
            existing.write(vals)
            return existing
        return self.create(vals)
