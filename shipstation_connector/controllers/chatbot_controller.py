from odoo import http
from odoo.http import request

from ..services.chatbot_service import ShipStationSimpleChatbotService


class ShipStationSimpleChatbotController(http.Controller):
    @http.route("/shipstation/ai/chatbot/message", type="json", auth="user", methods=["POST"], csrf=False)
    def shipstation_ai_chatbot_message(self, message=None, **kwargs):
        payload = ShipStationSimpleChatbotService(request.env).get_reply(message)
        return {
            "intent": payload.get("intent", "fallback"),
            "reply": payload.get("reply", ShipStationSimpleChatbotService.FALLBACK_REPLY),
        }
