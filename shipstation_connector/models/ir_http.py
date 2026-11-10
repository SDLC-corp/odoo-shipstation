from odoo import http, models
from odoo.http import request

from ..controllers.shipstation_webhook import _is_webhook_path


class IrHttp(models.AbstractModel):
    _inherit = "ir.http"

    @classmethod
    def _dispatch(cls):
        # The http.Root.get_request override that keeps ShipStation's application/json posts plain
        # HTTP requests is installed when this module's code is imported, i.e. when the first
        # registry containing the module loads. A notification that arrives in the request that
        # loads the registry (the first request after a restart when no database is preloaded) was
        # already turned into a JSON-RPC request; Odoo 15 would refuse it on the type="http" route
        # with an HTTP 200 JSON-RPC error that ShipStation takes as delivered, and the notification
        # would be lost. Dispatch it as the plain HTTP request it is instead.
        if request._request_type == "json" and _is_webhook_path(request.httprequest.path):
            with http.HttpRequest(request.httprequest):
                return super()._dispatch()
        return super()._dispatch()
