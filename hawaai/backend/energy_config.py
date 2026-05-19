"""Canonical energy configuration resolver.

Runtime code should use this module to decide whether a room is manually
configured, device-discovery based, or unconfigured. Legacy request aliases and
migrations may live at ingestion boundaries; runtime should not branch on them.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Dict, Mapping, Optional, Tuple

from . import ha_client

logger = logging.getLogger(__name__)


class EnergyConfigMode(str, Enum):
    AUTO_DISCOVERY = "auto_discovery"
    MANUAL_OVERRIDE = "manual_override"
    UNCONFIGURED = "unconfigured"


@dataclass(frozen=True)
class ResolvedEnergyConfig:
    mode: EnergyConfigMode
    power_entity: str = ""
    kwh_entity: str = ""
    device_id: str = ""
    device_name: str = ""
    device_lookup_skipped: bool = True
    power_unit: str = ""
    kwh_unit: str = ""

    @property
    def configured(self) -> bool:
        return self.mode != EnergyConfigMode.UNCONFIGURED


@dataclass(frozen=True)
class EnergyEntityValidation:
    valid: bool
    reason: str = "ok"
    score: int = 0
    unit: str = ""
    numeric_state: Optional[float] = None


def _clean(value: object) -> str:
    return str(value or "").strip()


def resolve_energy_config(room_cfg: Mapping[str, Any]) -> ResolvedEnergyConfig:
    """Resolve the room energy config mode without touching Home Assistant."""
    power_entity = _clean(room_cfg.get("energy_power_entity"))
    kwh_entity = _clean(room_cfg.get("energy_kwh_entity"))
    device_id = _clean(room_cfg.get("energy_device_id"))
    device_name = _clean(room_cfg.get("energy_device_name"))

    if power_entity:
        return ResolvedEnergyConfig(
            mode=EnergyConfigMode.MANUAL_OVERRIDE,
            power_entity=power_entity,
            kwh_entity=kwh_entity,
            device_lookup_skipped=True,
        )

    if device_id:
        return ResolvedEnergyConfig(
            mode=EnergyConfigMode.AUTO_DISCOVERY,
            device_id=device_id,
            device_name=device_name,
            device_lookup_skipped=False,
        )

    return ResolvedEnergyConfig(mode=EnergyConfigMode.UNCONFIGURED)


def _normal_unit(value: object) -> str:
    return str(value or "").strip().lower().replace(" ", "")


def _normal_class(value: object) -> str:
    return str(value or "").strip().lower()


def _entity_domain(entity_id: str) -> str:
    return entity_id.split(".", 1)[0].lower() if "." in entity_id else ""


def static_energy_entity_rejection_reason(entity_id: object, *, kind: str) -> str:
    entity_id = _clean(entity_id)
    if not entity_id:
        return ""
    if _entity_domain(entity_id) != "sensor":
        return "invalid_domain"
    object_id = entity_id.split(".", 1)[-1].lower()
    if object_id.endswith(("_behaviour", "_behavior", "_configuration", "_setting")):
        return "configuration_entity"
    return ""


def parse_numeric_state(value: object) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text or text.lower() in {"unavailable", "unknown", "none", "nan"}:
            return None
        value = text
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _suffix_rank(entity_id: str, *, kind: str) -> int:
    object_id = entity_id.split(".", 1)[-1].lower()
    if kind == "power" and object_id.endswith("_power"):
        return 1000
    if kind == "energy" and object_id.endswith("_total_energy"):
        return 1000
    return 0


def validate_energy_entity(
    entity_id: str,
    state_obj: Optional[Mapping[str, Any]],
    *,
    kind: str,
) -> EnergyEntityValidation:
    entity_id = _clean(entity_id)
    if not entity_id:
        return EnergyEntityValidation(False, reason="missing_entity")
    static_rejection = static_energy_entity_rejection_reason(entity_id, kind=kind)
    if static_rejection:
        return EnergyEntityValidation(False, reason=static_rejection)
    if not state_obj:
        return EnergyEntityValidation(False, reason="state_unavailable")

    attrs = state_obj.get("attributes") or {}
    if not isinstance(attrs, Mapping):
        attrs = {}
    entity_category = _normal_class(attrs.get("entity_category"))
    if entity_category in {"config", "diagnostic"}:
        return EnergyEntityValidation(False, reason=f"entity_category_{entity_category}")

    device_class = _normal_class(attrs.get("device_class"))
    state_class = _normal_class(attrs.get("state_class"))
    unit = _normal_unit(attrs.get("unit_of_measurement"))
    numeric_state = parse_numeric_state(state_obj.get("state"))
    if numeric_state is None:
        return EnergyEntityValidation(False, reason="non_numeric")

    if kind == "power":
        if device_class != "power" and unit not in {"w", "kw"}:
            return EnergyEntityValidation(False, reason="invalid_power_metadata")
        score = _suffix_rank(entity_id, kind=kind)
        if device_class == "power":
            score += 500
        if unit in {"w", "kw"}:
            score += 250
        if state_class in {"measurement", ""}:
            score += 25
        return EnergyEntityValidation(True, score=score, unit=unit, numeric_state=numeric_state)

    if device_class != "energy" and unit not in {"wh", "kwh"}:
        return EnergyEntityValidation(False, reason="invalid_energy_metadata")
    score = _suffix_rank(entity_id, kind=kind)
    if device_class == "energy":
        score += 500
    if unit in {"wh", "kwh"}:
        score += 250
    if state_class == "total_increasing":
        score += 100
    elif state_class == "total":
        score += 75
    return EnergyEntityValidation(True, score=score, unit=unit, numeric_state=numeric_state)


def log_energy_validation(room_id: str, entity_id: str, reason: str) -> None:
    if reason == "ok":
        return
    logger.warning(
        "[ENERGY_VALIDATE] room=%s entity=%s reason=%s",
        room_id or "unknown",
        entity_id or "none",
        reason,
    )


async def read_validated_energy_state(
    room_id: str,
    entity_id: str,
    *,
    kind: str,
) -> Tuple[object, Optional[float], EnergyEntityValidation]:
    if not entity_id:
        validation = EnergyEntityValidation(False, reason="missing_entity")
        return None, None, validation
    full = await ha_client.get_entity_state_full(entity_id)
    validation = validate_energy_entity(entity_id, full, kind=kind)
    if not validation.valid:
        log_energy_validation(room_id, entity_id, validation.reason)
        return (full or {}).get("state") if full else None, None, validation
    return full.get("state"), validation.numeric_state, validation


def _best_entity(
    entity_ids: set[str],
    state_map: Mapping[str, Mapping[str, Any]],
    *,
    kind: str,
    room_id: str = "",
) -> Tuple[str, str]:
    candidates = []
    for entity_id in entity_ids:
        state_obj = state_map.get(entity_id) or {}
        validation = validate_energy_entity(entity_id, state_obj, kind=kind)
        if not validation.valid:
            log_energy_validation(room_id, entity_id, validation.reason)
            continue
        candidates.append((validation.score, entity_id, validation.unit))
    if not candidates:
        return "", ""
    candidates.sort(key=lambda item: (-item[0], item[1]))
    _, entity_id, unit = candidates[0]
    return entity_id, unit


async def discover_energy_entities_for_device(device_id: str, *, room_id: str = "") -> Dict[str, str]:
    """Discover energy entities for a HA device using registry + state metadata."""
    clean_device_id = _clean(device_id)
    if not clean_device_id:
        return {}

    registry, all_states = await asyncio.gather(
        ha_client.get_entity_registry(),
        ha_client.get_all_entities(),
    )
    device_entity_ids = {
        _clean(entry.get("entity_id"))
        for entry in registry
        if _clean(entry.get("device_id")) == clean_device_id and _clean(entry.get("entity_id"))
    }
    if not device_entity_ids:
        return {}

    state_map = {
        _clean(state.get("entity_id")): state
        for state in all_states
        if _clean(state.get("entity_id"))
    }
    power_entity, power_unit = _best_entity(
        device_entity_ids,
        state_map,
        kind="power",
        room_id=room_id,
    )
    kwh_entity, kwh_unit = _best_entity(
        device_entity_ids,
        state_map,
        kind="energy",
        room_id=room_id,
    )
    return {
        "power_entity": power_entity,
        "kwh_entity": kwh_entity,
        "power_unit": power_unit,
        "kwh_unit": kwh_unit,
    }


async def resolve_runtime_energy_config(
    room_cfg: Mapping[str, Any],
    *,
    room_id: str = "",
) -> ResolvedEnergyConfig:
    """Resolve runtime entities, performing HA discovery only for legacy device mode."""
    resolved = resolve_energy_config(room_cfg)
    if resolved.mode != EnergyConfigMode.AUTO_DISCOVERY:
        return resolved

    discovered = await discover_energy_entities_for_device(resolved.device_id, room_id=room_id)
    return replace(
        resolved,
        power_entity=discovered.get("power_entity", ""),
        kwh_entity=discovered.get("kwh_entity", ""),
        power_unit=discovered.get("power_unit", ""),
        kwh_unit=discovered.get("kwh_unit", ""),
    )
