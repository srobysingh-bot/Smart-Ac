"""Canonical energy configuration resolver.

Runtime code should use this module to decide whether a room is manually
configured, device-discovery based, or unconfigured. Legacy request aliases and
migrations may live at ingestion boundaries; runtime should not branch on them.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Dict, Mapping, Optional, Tuple

from . import ha_client


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


def _entity_score(attrs: Mapping[str, Any], *, kind: str) -> int:
    device_class = _normal_class(attrs.get("device_class"))
    state_class = _normal_class(attrs.get("state_class"))
    unit = _normal_unit(attrs.get("unit_of_measurement"))

    if kind == "power":
        if device_class == "power" and unit in {"w", "kw"}:
            return 100
        if device_class == "power":
            return 80
        if unit in {"w", "kw"} and state_class in {"measurement", ""}:
            return 60
        return 0

    if device_class == "energy" and state_class in {"total", "total_increasing"}:
        return 110
    if device_class == "energy":
        return 90
    if unit in {"wh", "kwh", "mwh"} and state_class in {"total", "total_increasing"}:
        return 70
    return 0


def _best_entity(
    entity_ids: set[str],
    state_map: Mapping[str, Mapping[str, Any]],
    *,
    kind: str,
) -> Tuple[str, str]:
    candidates = []
    for entity_id in entity_ids:
        state_obj = state_map.get(entity_id) or {}
        attrs = state_obj.get("attributes") or {}
        if not isinstance(attrs, Mapping):
            attrs = {}
        score = _entity_score(attrs, kind=kind)
        if score <= 0:
            continue
        candidates.append((score, entity_id, _clean(attrs.get("unit_of_measurement"))))
    if not candidates:
        return "", ""
    candidates.sort(key=lambda item: (-item[0], item[1]))
    _, entity_id, unit = candidates[0]
    return entity_id, unit


async def discover_energy_entities_for_device(device_id: str) -> Dict[str, str]:
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
    power_entity, power_unit = _best_entity(device_entity_ids, state_map, kind="power")
    kwh_entity, kwh_unit = _best_entity(device_entity_ids, state_map, kind="energy")
    return {
        "power_entity": power_entity,
        "kwh_entity": kwh_entity,
        "power_unit": power_unit,
        "kwh_unit": kwh_unit,
    }


async def resolve_runtime_energy_config(room_cfg: Mapping[str, Any]) -> ResolvedEnergyConfig:
    """Resolve runtime entities, performing HA discovery only for legacy device mode."""
    resolved = resolve_energy_config(room_cfg)
    if resolved.mode != EnergyConfigMode.AUTO_DISCOVERY:
        return resolved

    discovered = await discover_energy_entities_for_device(resolved.device_id)
    return replace(
        resolved,
        power_entity=discovered.get("power_entity", ""),
        kwh_entity=discovered.get("kwh_entity", ""),
        power_unit=discovered.get("power_unit", ""),
        kwh_unit=discovered.get("kwh_unit", ""),
    )
