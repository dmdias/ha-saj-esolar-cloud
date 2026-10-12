"""Rebuild the long-term statistics of the energy counters from the SAJ cloud.

Home Assistant only keeps statistics from the day a sensor started, and a
counter that jumps between values (e.g. when an inverter is replaced and the
plant total briefly reads as the new inverter's own counter) is taken for a
meter reset, adding its whole value again. The SAJ cloud keeps the plant
energy of every day since the plant was created, so the statistics of the six
lifetime energy counters can be rewritten from it: one row per day (at 23:00)
whose sum is the energy since the first day, plus a last row for the current
hour that matches the counter's present value, so later hours continue
without a jump.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
import logging
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMeanType, StatisticMetaData
from homeassistant.components.recorder.statistics import async_import_statistics
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .const import CHART_DAY, CHART_MONTH, DOMAIN

_LOGGER = logging.getLogger(__name__)

# Sensor key -> field of the plant energy (getSelfUseEnergyData)
HISTORY_FIELDS = {
    "totalElectricity": "pvEnergy",
    "totalConsumpElec": "loadEnergy",
    "totalBuyElec": "buyEnergy",
    "totalSellElec": "sellEnergy",
    "totalBatteryCharge": "chargeEnergy",
    "totalBatteryDischarge": "dischargeEnergy",
}

# Sensor key -> lifetime value in the plant statistics, to match the present reading
LIFETIME_FIELDS = {
    "totalElectricity": ("PV_ENERGY", "energy1Total"),
    "totalConsumpElec": ("LOAD_ENERGY", "energy1Total"),
    "totalBuyElec": ("BUY_AND_SELL", "energy1Total"),
    "totalSellElec": ("BUY_AND_SELL", "energy2Total"),
    "totalBatteryCharge": ("CHARGE_AND_DISCHARGE", "energy1Total"),
    "totalBatteryDischarge": ("CHARGE_AND_DISCHARGE", "energy2Total"),
}

PARALLEL_REQUESTS = 4


def _plant_start(details: dict[str, Any]) -> date | None:
    """Day the plant was created, from the plant detail createDate (dd/mm/yyyy hh:mm:ss)."""
    value = (details.get("data") or {}).get("createDate")
    for fmt in ("%d/%m/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(str(value), fmt).date()
        except (TypeError, ValueError):
            continue
    return None


def _lifetime(plant_stats: dict[str, Any], key: str) -> float | None:
    data_type, field = LIFETIME_FIELDS[key]
    for entry in plant_stats.get("energyDataList") or []:
        if entry.get("dataType") == data_type and entry.get(field) not in (None, ""):
            return float(entry[field])
    return None


def _reconcile(days: list[date], daily: dict[date, dict[str, Any]], monthly: dict[str, dict[str, Any]], field: str) -> list[tuple[date, float]]:
    """Daily values adjusted so that every month adds up to the month total.

    The difference goes to the days that reported nothing, or, when every day
    reported something, is spread in proportion to the daily values. The
    current month is left as reported (its total is still growing).
    """
    values = {day: float(daily[day].get(field) or 0) for day in days}
    current = dt_util.now().strftime("%Y-%m")
    for month, totals in monthly.items():
        if month == current or totals.get(field) in (None, ""):
            continue
        month_days = [day for day in days if day.strftime("%Y-%m") == month]
        diff = float(totals[field]) - sum(values[day] for day in month_days)
        if abs(diff) < 0.05 or not month_days:
            continue
        empty = [day for day in month_days if values[day] == 0]
        if empty and diff > 0:
            for day in empty:
                values[day] += diff / len(empty)
        else:
            reported = sum(values[day] for day in month_days)
            for day in month_days:
                values[day] += diff * (values[day] / reported if reported else 1 / len(month_days))
        _LOGGER.debug("Adjusted %s of %s by %.2f kWh to match the month total", field, month, diff)
    return [(day, max(values[day], 0.0)) for day in days]


async def async_import_history(hass: HomeAssistant, coordinator, plant_uid: str, start: date | None = None) -> dict[str, Any]:
    """Fetch the daily plant energy since ``start`` and rewrite the statistics. Returns a summary."""
    registry = er.async_get(hass)
    entity_ids = {
        key: registry.async_get_entity_id("sensor", DOMAIN, f"{DOMAIN}_{plant_uid}_{key}") for key in HISTORY_FIELDS
    }
    entity_ids = {key: entity_id for key, entity_id in entity_ids.items() if entity_id}
    if not entity_ids:
        raise HomeAssistantError(f"No energy sensors found for plant {plant_uid}")

    await coordinator._authenticate()
    sn_list = await coordinator._get_plant_sn_list(plant_uid)
    if start is None:
        start = _plant_start(await coordinator._get_plant_details_for_plant(plant_uid))
    if start is None:
        raise HomeAssistantError("Could not find the plant creation date; pass start_date")

    today = dt_util.now().date()
    days = [start + timedelta(days=n) for n in range((today - start).days + 1)]
    semaphore = asyncio.Semaphore(PARALLEL_REQUESTS)

    async def fetch(day: date) -> tuple[date, dict[str, Any]]:
        async with semaphore:
            for attempt in range(3):
                try:
                    return day, await coordinator.get_plant_energy(plant_uid, sn_list, CHART_DAY, day.isoformat())
                except Exception as err:  # noqa: BLE001 - retried, then reported
                    if attempt == 2:
                        raise HomeAssistantError(f"Could not fetch the energy of {day}: {err}") from err
                    await asyncio.sleep(2 * (attempt + 1))
        raise AssertionError  # unreachable

    _LOGGER.info("Fetching the daily energy of plant %s for %d days since %s", plant_uid, len(days), start)
    daily = dict(await asyncio.gather(*(fetch(day) for day in days)))

    # Month totals: the daily values miss the energy of days the inverter did
    # not report (e.g. 0 kWh), which the month total still includes
    months = sorted({day.strftime("%Y-%m") for day in days})

    async def fetch_month(month: str) -> tuple[str, dict[str, Any]]:
        async with semaphore:
            return month, await coordinator.get_plant_energy(plant_uid, sn_list, CHART_MONTH, month)

    monthly = dict(await asyncio.gather(*(fetch_month(month) for month in months)))

    plant_stats = ((coordinator.data or {}).get(plant_uid, {}).get("plant_statistics") or {}).get("data") or {}
    tz = dt_util.get_default_time_zone()
    now = dt_util.now()
    current_hour = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    recorder = get_instance(hass)
    recorder.async_clear_statistics(list(entity_ids.values()))

    summary: dict[str, Any] = {"plant": plant_uid, "start": start.isoformat(), "days": len(days), "sensors": {}}
    for key, entity_id in entity_ids.items():
        field = HISTORY_FIELDS[key]
        values = _reconcile(days, daily, monthly, field)
        total = sum(value for _, value in values)
        lifetime = _lifetime(plant_stats, key)
        # The counter reads the lifetime total: offset the sums so the last row matches it
        offset = (lifetime - total) if lifetime is not None else 0.0

        rows: list[StatisticData] = []
        cumulative = 0.0
        for day, value in values:
            cumulative += value
            if day < today:
                start_dt = datetime(day.year, day.month, day.day, 23, tzinfo=tz)
            else:
                # Today so far, at the last complete hour (after yesterday's 23:00 row)
                start_dt = max(current_hour, datetime(day.year, day.month, day.day, 0, tzinfo=tz))
            if rows and start_dt <= rows[-1]["start"]:
                rows[-1] = StatisticData(start=rows[-1]["start"], state=offset + cumulative, sum=cumulative)
                continue
            rows.append(StatisticData(start=start_dt, state=offset + cumulative, sum=cumulative))

        metadata = StatisticMetaData(
            has_sum=True,
            mean_type=StatisticMeanType.NONE,
            name=None,
            source="recorder",
            statistic_id=entity_id,
            unit_class="energy",
            unit_of_measurement="kWh",
        )
        async_import_statistics(hass, metadata, rows)
        summary["sensors"][entity_id] = round(total, 2)

    _LOGGER.info("Imported the energy history of plant %s: %s", plant_uid, summary)
    return summary


async def async_run_import(hass: HomeAssistant, coordinator, plant_uid: str, start: date | None) -> None:
    """Run the import in the background and report the outcome as a notification."""
    try:
        summary = await async_import_history(hass, coordinator, plant_uid, start)
    except Exception as err:  # noqa: BLE001 - shown to the user
        _LOGGER.exception("Energy history import failed for plant %s", plant_uid)
        persistent_notification.async_create(
            hass, f"Energy history import failed for plant {plant_uid}: {err}", title="SAJ eSolar", notification_id=f"{DOMAIN}_import_{plant_uid}"
        )
        return
    lines = "\n".join(f"- `{entity_id}`: {value} kWh" for entity_id, value in summary["sensors"].items())
    persistent_notification.async_create(
        hass,
        f"Imported {summary['days']} days of energy since {summary['start']} for plant {plant_uid}:\n{lines}",
        title="SAJ eSolar",
        notification_id=f"{DOMAIN}_import_{plant_uid}",
    )
