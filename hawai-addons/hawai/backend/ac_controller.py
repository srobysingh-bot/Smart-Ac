"""
AC brand/model library + legacy ACController stub.

HawaAI control is climate-only (ac_adapter → Aerostate). IR/Broadlink is no
longer used from this add-on. The library remains for reference / future use.
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from . import config_manager

logger = logging.getLogger(__name__)

_LIBRARY_PATH = Path(__file__).parent / "ac_library" / "brands.json"
_library: Optional[Dict[str, Any]] = None


def _load_library() -> Dict[str, Any]:
    global _library
    if _library is None:
        try:
            _library = json.loads(_LIBRARY_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.error("Failed to load AC library: %s", exc)
            _library = {"brands": []}
    return _library


def get_brands() -> list:
    return _load_library().get("brands", [])


def get_models(brand_id: str) -> list:
    for brand in get_brands():
        if brand["id"] == brand_id:
            return brand.get("models", [])
    return []


def get_model(brand_id: str, model_id: str) -> Optional[Dict[str, Any]]:
    for model in get_models(brand_id):
        if model["id"] == model_id:
            return model
    return None


class ACController:
    """Legacy stub — IR/Broadlink disabled; configure Aerostate in Settings."""

    async def turn_on(self, mode: str = "cool", temp: int = 24, fan: str = "auto") -> bool:
        command = self._build_command("on", mode=mode, temp=temp, fan=fan)
        return await self._send(command)

    async def turn_off(self) -> bool:
        command = self._build_command("off")
        return await self._send(command)

    async def set_temperature(self, temp: int) -> bool:
        command = self._build_command("set_temp", temp=temp)
        return await self._send(command)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _build_command(self, action: str, **kwargs) -> str:
        """
        Build an IR command string.

        For Broadlink via HA, the command is typically a string like
        'b64:<base64_payload>' stored in the IR profile JSON, or a
        human-readable command name for learned codes.
        
        Falls back to a standardised command name that matches the
        profile keys if no base64 payload is found.
        """
        brand_id = config_manager.get("ac_brand", "")
        model_id = config_manager.get("ac_model", "")

        if brand_id and model_id:
            model = get_model(brand_id, model_id)
            if model:
                profile_file = model.get("broadlink_profile")
                if profile_file:
                    profile_path = (
                        Path(__file__).parent / "ac_library" / "ir_profiles" / profile_file
                    )
                    if profile_path.exists():
                        try:
                            profile = json.loads(profile_path.read_text(encoding="utf-8"))
                            # Build a key like "cool_24_auto" or "off"
                            if action == "off":
                                key = "off"
                            else:
                                mode = kwargs.get("mode", "cool")
                                temp = kwargs.get("temp", 24)
                                fan = kwargs.get("fan", "auto")
                                key = f"{mode}_{temp}_{fan}"
                            cmd = profile.get(key) or profile.get("off")
                            if cmd:
                                return cmd
                        except (OSError, json.JSONDecodeError) as exc:
                            logger.warning("Could not load IR profile %s: %s", profile_file, exc)

        # Fallback: plain command strings (works with Broadlink learned codes)
        if action == "off":
            return "off"
        mode = kwargs.get("mode", "cool")
        temp = kwargs.get("temp", 24)
        fan = kwargs.get("fan", "auto")
        return f"{mode}_{temp}_{fan}"

    async def _send(self, command: str) -> bool:
        logger.warning(
            "[HawaAI] ACController IR path disabled — use Climate entity (Aerostate). "
            "Ignored command key/payload (len=%s).",
            len(command) if command else 0,
        )
        return False
