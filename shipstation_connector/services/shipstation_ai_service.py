import json
import logging

from odoo import fields

from .shipstation_ai_provider import (
    ShipStationAIProvider,
    ShipStationAIProviderDisabled,
    ShipStationAIProviderError,
)


_logger = logging.getLogger(__name__)


class ShipStationAIService:
    def __init__(self, env):
        self.env = env
        self.provider = ShipStationAIProvider(env)

    def _strip_code_fences(self, text):
        text = (text or "").strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        return text

    def _safe_json_loads(self, raw_text):
        cleaned = self._strip_code_fences(raw_text)
        return json.loads(cleaned) if cleaned else {}

    def _format_currency(self, amount):
        return "%.2f" % float(amount or 0.0)

    def _fallback_insights(self, metrics):
        shipments_7 = metrics.get("shipments_last_7_days", {})
        shipments_30 = metrics.get("shipments_last_30_days", {})
        tracking_gaps = metrics.get("pending_tracking_shipments", [])[:3]
        low_stock = metrics.get("low_stock_inventory", [])[:3]
        failing_instances = metrics.get("failing_instances", [])[:3]

        summary_parts = [
            "Last 7 days shipped %s orders with %s revenue."
            % (
                int(shipments_7.get("count", 0)),
                self._format_currency(shipments_7.get("revenue", 0.0)),
            ),
            "Last 30 days shipped %s orders with %s revenue."
            % (
                int(shipments_30.get("count", 0)),
                self._format_currency(shipments_30.get("revenue", 0.0)),
            ),
            "%s shipments are missing tracking details."
            % int(metrics.get("pending_tracking_count", 0)),
        ]
        if low_stock:
            summary_parts.append("%s inventory items are at low stock levels." % len(low_stock))
        if failing_instances:
            summary_parts.append(
                "Sync instability detected on %s instance(s)." % len(failing_instances)
            )

        recommendations = []
        for item in tracking_gaps:
            recommendations.append(
                "Review tracking for order %s because the shipment is still marked %s."
                % (
                    item.get("order_number") or item.get("order_id") or "unknown",
                    item.get("status") or "pending_tracking",
                )
            )
        for item in low_stock:
            recommendations.append(
                "Restock %s because available stock is %s."
                % (item.get("name") or item.get("sku") or "item", item.get("stock_level", 0))
            )
        for item in failing_instances:
            recommendations.append(
                "Investigate sync health for %s because it had %s failures in the last 24 hours."
                % (item.get("name") or "instance", item.get("failed_24h", 0))
            )
        if not recommendations:
            recommendations.append(
                "No urgent operational issues were detected. Continue monitoring shipment throughput and sync health."
            )

        return {
            "summary": " ".join(summary_parts),
            "pending_tracking_shipments": metrics.get("pending_tracking_shipments", []),
            "low_stock_inventory": metrics.get("low_stock_inventory", []),
            "failing_instances": metrics.get("failing_instances", []),
            "top_carriers": metrics.get("top_carriers", []),
            "shipment_summary": {
                "last_7_days": shipments_7,
                "last_30_days": shipments_30,
            },
            "actionable_recommendations": recommendations[:6],
        }

    def generate_operational_insights(self, metrics, context_meta):
        fallback = self._fallback_insights(metrics)
        system_prompt = (
            "You are a shipping operations analyst. Return valid JSON only with keys: "
            "summary, pending_tracking_shipments, low_stock_inventory, failing_instances, "
            "top_carriers, shipment_summary, actionable_recommendations."
        )
        user_prompt = json.dumps(
            {
                "context": context_meta,
                "metrics": metrics,
                "fallback_reference": fallback,
            },
            indent=2,
            default=str,
        )

        result = fallback
        status = "fallback"
        error_message = False
        try:
            raw = self.provider.generate_json(system_prompt, user_prompt, temperature=0.1)
            parsed = self._safe_json_loads(raw)
            if isinstance(parsed, dict):
                result = {
                    "summary": parsed.get("summary") or fallback["summary"],
                    "pending_tracking_shipments": parsed.get("pending_tracking_shipments")
                    or fallback["pending_tracking_shipments"],
                    "low_stock_inventory": parsed.get("low_stock_inventory")
                    or fallback["low_stock_inventory"],
                    "failing_instances": parsed.get("failing_instances")
                    or fallback["failing_instances"],
                    "top_carriers": parsed.get("top_carriers") or fallback["top_carriers"],
                    "shipment_summary": parsed.get("shipment_summary")
                    or fallback["shipment_summary"],
                    "actionable_recommendations": parsed.get("actionable_recommendations")
                    or fallback["actionable_recommendations"],
                }
                status = "success"
        except (ShipStationAIProviderDisabled, ShipStationAIProviderError, ValueError, TypeError) as exc:
            error_message = str(exc)
            _logger.warning("ShipStation AI fallback triggered: %s", exc)

        return {
            "status": status,
            "error_message": error_message,
            "generated_at": fields.Datetime.now(),
            "summary_text": result["summary"],
            "insight_payload": result,
        }
