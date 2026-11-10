from odoo import http
from odoo.http import request

from ..services.chatbot_service import ShipStationSimpleChatbotService


def active_company_ids(user, requested):
    """The companies the web client has active, limited to those the user may access.

    A JSON-RPC route gets no client context, so without this Odoo falls back to *all* of the
    user's companies and the assistant would ignore the company switcher. Anything that is not a
    company of the user is dropped; nothing usable means the Odoo default (all allowed companies).
    """
    if not isinstance(requested, list):
        return []
    allowed = set(user.company_ids.ids)
    result = []
    for cid in requested:
        if isinstance(cid, int) and not isinstance(cid, bool) and cid in allowed and cid not in result:
            result.append(cid)
    return result


class ShipStationSimpleChatbotController(http.Controller):
    @http.route("/shipstation/ai/chatbot/message", type="json", auth="user", methods=["POST"], csrf=False)
    def shipstation_ai_chatbot_message(self, message=None, company_ids=None, **kwargs):
        companies = active_company_ids(request.env.user, company_ids)
        if companies:
            request.update_context(allowed_company_ids=companies)
        payload = ShipStationSimpleChatbotService(request.env).get_reply(message)
        return {
            "intent": payload.get("intent", "fallback"),
            "reply": payload.get("reply", ShipStationSimpleChatbotService.FALLBACK_REPLY),
        }
