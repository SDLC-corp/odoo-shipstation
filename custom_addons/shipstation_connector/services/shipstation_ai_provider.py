import json
import logging

import requests


_logger = logging.getLogger(__name__)


class ShipStationAIProviderError(Exception):
    pass


class ShipStationAIProviderDisabled(Exception):
    pass


class ShipStationAIProvider:
    DEFAULT_ENDPOINT = "https://api.openai.com/v1/chat/completions"

    def __init__(self, env, prefix="shipstation_ai"):
        self.env = env
        self.params = env["ir.config_parameter"].sudo()
        self.prefix = prefix

    def _param(self, suffix, default=None):
        return self.params.get_param("%s.%s" % (self.prefix, suffix), default)

    def get_settings(self):
        enabled = self._param("enabled", "False")
        return {
            "enabled": str(enabled).lower() in ("1", "true", "yes", "on"),
            "provider": (self._param("provider") or "openai").strip(),
            "api_key": (self._param("api_key") or "").strip(),
            "model": (self._param("model") or "gpt-4o-mini").strip(),
            "max_tokens": int(self._param("max_tokens", "800") or 800),
            "endpoint": (self._param("endpoint") or self.DEFAULT_ENDPOINT).strip(),
        }

    def ensure_enabled(self):
        settings = self.get_settings()
        if not settings["enabled"]:
            raise ShipStationAIProviderDisabled("ShipStation AI is disabled.")
        if not settings["api_key"]:
            raise ShipStationAIProviderDisabled("ShipStation AI API key is not configured.")
        return settings

    def generate_json(self, system_prompt, user_prompt, temperature=0.2):
        settings = self.ensure_enabled()
        if settings["provider"] not in ("openai", "openai_compatible"):
            raise ShipStationAIProviderError(
                "Unsupported AI provider '%s'." % settings["provider"]
            )

        payload = {
            "model": settings["model"],
            "temperature": temperature,
            "max_tokens": settings["max_tokens"],
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        headers = {
            "Authorization": "Bearer %s" % settings["api_key"],
            "Content-Type": "application/json",
        }
        _logger.info(
            "ShipStation AI request provider=%s model=%s max_tokens=%s",
            settings["provider"],
            settings["model"],
            settings["max_tokens"],
        )
        try:
            response = requests.post(
                settings["endpoint"],
                headers=headers,
                data=json.dumps(payload),
                timeout=45,
            )
        except requests.RequestException as exc:
            raise ShipStationAIProviderError(str(exc)) from exc

        if response.status_code >= 400:
            raise ShipStationAIProviderError(
                "AI provider request failed with status %s." % response.status_code
            )

        try:
            data = response.json()
            return (
                data.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
                .strip()
            )
        except Exception as exc:
            raise ShipStationAIProviderError("Invalid AI provider response.") from exc
