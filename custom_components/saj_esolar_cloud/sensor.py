"""SAJ eSolar sensor platform."""
from __future__ import annotations
from datetime import datetime
from zoneinfo import ZoneInfo

from typing import Any, cast

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfEnergy,
    UnitOfPower,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
)
from homeassistant.util import dt as dt_util

from .const import (
    BATTERY_STATES,
    DEVICE_DETAIL_FIELDS,
    DEVICE_INFO,
    DIRECTION_STATES,
    DOMAIN,
    H1_SENSORS,
    CURRENCY_FIELDS,
    INVERTER_SENSOR_KEYS,
    REVENUE_FIELDS,
)
from .coordinator import (
    RUNNING_STATE_ALARM,
    RUNNING_STATE_NORMAL,
    SAJeSolarDataUpdateCoordinator,
    parse_update_date,
    resolve_field,
    to_float,
)

# Device class mapping
DEVICE_CLASS_MAP = {
    "power": SensorDeviceClass.POWER,
    "energy": SensorDeviceClass.ENERGY,
    "battery": SensorDeviceClass.BATTERY,
    "timestamp": SensorDeviceClass.TIMESTAMP,
    "temperature": SensorDeviceClass.TEMPERATURE,
    "voltage": SensorDeviceClass.VOLTAGE,
    "energy_storage": SensorDeviceClass.ENERGY_STORAGE,
    "duration": SensorDeviceClass.DURATION,
    "monetary": SensorDeviceClass.MONETARY,
}

# State class mapping
STATE_CLASS_MAP = {
    "measurement": SensorStateClass.MEASUREMENT,
    "total_increasing": SensorStateClass.TOTAL_INCREASING,
    "total": SensorStateClass.TOTAL,
}

# Generation sensors and the inverter field used when the plant total is missing
PLANT_GENERATION_KEYS = {
    "todayElectricity": "todayEnergy",
    "monthElectricity": "monthEnergy",
    "yearElectricity": "yearEnergy",
    "totalElectricity": "totalEnergy",
}

# Sensors read from the energy flow
FLOW_SENSOR_KEYS = {
    "gridPower",
    "gridPowerAbsolute",
    "batteryPower",
    "batteryPowerAbsolute",
    "outPower",
    "totalLoadPower",
    "batteryDirection",
    "pvDirection",
    "gridDirection",
    "outPutDirection",
}

def optional_float(data: dict[str, Any], key: str) -> float | None:
    """Read a numeric field, returning None (unknown) when it is missing."""
    value = data.get(key)
    if value in (None, ""):
        return None
    return float(str(value).rstrip("%"))

def is_running(running_state: Any) -> bool:
    """Whether an inverter runningState means it is online (normal or alarm)."""
    return int(running_state) in (RUNNING_STATE_NORMAL, RUNNING_STATE_ALARM)

def energy_stat(plant_stats: dict[str, Any], data_type: str, field: str) -> float:
    """Read a value from the plant statistics energyDataList.

    Entries are keyed by dataType (PV_ENERGY, LOAD_ENERGY, BUY_AND_SELL,
    CHARGE_AND_DISCHARGE). For the paired ones energy1 is buy/charge and
    energy2 is sell/discharge.
    """
    for entry in plant_stats.get("energyDataList") or []:
        if entry.get("dataType") == data_type:
            return float(entry.get(field) or 0)
    return 0

def environmental_stat(plant_stats: dict[str, Any], icon_key: str) -> float | None:
    """Read a value from the plant statistics environmentalInformation.

    Entries only carry a localized description, so match them by icon name
    (PLANT_CO2, PLANT_TREE, PLANT_COAL).
    """
    for entry in plant_stats.get("environmentalInformation") or []:
        if icon_key in (entry.get("iconUrl") or ""):
            return float(entry.get("value") or 0)
    return None

def _inverter_label(device_entry: dict[str, Any], device_sn: str) -> str:
    """Human readable name for one inverter of a multi-inverter plant."""
    device_info = device_entry.get("device_info", {}) or {}
    for key in ("deviceName", "aliasName", "deviceModel"):
        name = device_info.get(key)
        if name:
            return str(name)
    return device_sn


# Home Assistant 2026.8+ links a device to its parent by device id (via_device is deprecated)
_VIA_DEVICE_ID = "via_device_id" in getattr(DeviceInfo, "__annotations__", {})


def _via(coordinator: SAJeSolarDataUpdateCoordinator, plant_uid: str) -> dict[str, Any]:
    """The device_info key linking an inverter or battery to its plant."""
    device_id = getattr(coordinator, "plant_device_ids", {}).get(plant_uid)
    if _VIA_DEVICE_ID:
        return {"via_device_id": device_id} if device_id else {}
    return {"via_device": (DOMAIN, plant_uid)}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up SAJ eSolar sensors based on a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]

    # Wait for first update to ensure we have plant data
    await coordinator.async_config_entry_first_refresh()

    entities = []

    # Create sensor entities for each monitored plant
    for plant_uid in coordinator.monitored_plants:
        # Get plant info for naming
        plant_data = coordinator.data.get(plant_uid, {})
        plant_info = plant_data.get("plant_info", {})
        plant_name = plant_info.get("plantName", plant_uid)
        if _VIA_DEVICE_ID:
            # The plant device must exist before its inverters and batteries point to it
            plant_device = dr.async_get(hass).async_get_or_create(
                config_entry_id=entry.entry_id, identifiers={(DOMAIN, plant_uid)}, name=plant_name, manufacturer="SAJ"
            )
            if not hasattr(coordinator, "plant_device_ids"):
                coordinator.plant_device_ids = {}
            coordinator.plant_device_ids[plant_uid] = plant_device.id

        # Plant-level sensors. With several inverters under one plant these
        # report the aggregate built by the coordinator.
        for sensor_key, sensor_config in H1_SENSORS.items():
            entities.append(
                SAJeSolarSensor(
                    coordinator=coordinator,
                    sensor_key=sensor_key,
                    sensor_config=sensor_config,
                    plant_uid=plant_uid,
                    plant_name=plant_name,
                )
            )

        # A plant can group several inverters. Expose each one as its own
        # device so the individual contributions stay visible; a single
        # inverter plant keeps only the plant-level entities it always had.
        devices = plant_data.get("devices", {})
        if len(devices) > 1:
            for device_sn, device_entry in devices.items():
                device_label = _inverter_label(device_entry, device_sn)

                for sensor_key in INVERTER_SENSOR_KEYS:
                    sensor_config = H1_SENSORS.get(sensor_key)
                    if not sensor_config:
                        continue
                    entities.append(
                        SAJeSolarSensor(
                            coordinator=coordinator,
                            sensor_key=sensor_key,
                            sensor_config=sensor_config,
                            plant_uid=plant_uid,
                            plant_name=plant_name,
                            device_sn=device_sn,
                            device_label=device_label,
                        )
                    )

        # Create individual battery devices and sensors
        battery_list = plant_data.get("battery_list", {}).get("data", {}).get("list", [])
        for i, battery in enumerate(battery_list):
            battery_sn = battery.get("batSn", f"battery_{i+1}")
            battery_name = f"{plant_name} Battery {i+1}"

            # Import BATTERY_SENSORS from const
            from .const import BATTERY_SENSORS

            # Create sensors for each battery
            for sensor_key, sensor_config in BATTERY_SENSORS.items():
                entities.append(
                    SAJeBatterySensor(
                        coordinator=coordinator,
                        sensor_key=sensor_key,
                        sensor_config=sensor_config,
                        plant_uid=plant_uid,
                        plant_name=plant_name,
                        battery_sn=battery_sn,
                        battery_name=battery_name,
                        battery_index=i,
                    )
                )

    async_add_entities(entities)

class SAJeSolarSensor(CoordinatorEntity[SAJeSolarDataUpdateCoordinator], SensorEntity):
    """Representation of a SAJ eSolar sensor."""

    def __init__(
        self,
        coordinator: SAJeSolarDataUpdateCoordinator,
        sensor_key: str,
        sensor_config: dict[str, Any],
        plant_uid: str,
        plant_name: str,
        device_sn: str | None = None,
        device_label: str | None = None,
    ) -> None:
        """Initialize the sensor.

        With ``device_sn`` unset the sensor reports the whole plant (the sum
        of every inverter); with it set it reports that one inverter.
        """
        super().__init__(coordinator)

        self._sensor_key = sensor_key
        self._config = sensor_config
        self._plant_uid = plant_uid
        self._plant_name = plant_name
        self._device_sn = device_sn

        plant_data = coordinator.data.get(plant_uid, {})

        if device_sn is None:
            # Set up entity properties with plant-specific naming
            self._attr_name = f"{plant_name} {sensor_config['name']}"
            self._attr_unique_id = f"{DOMAIN}_{plant_uid}_{sensor_key}"

            # Create plant-specific device info. Use the model reported by the
            # device list (e.g. R5-xxK-xxxx) when available, falling back to "H1".
            device_list = plant_data.get("device_list", {}).get("data", {}).get("list", [])
            device_data = device_list[0] if device_list else {}
            device_model = device_data.get("deviceModel") or "H1"

            self._attr_device_info = {
                "identifiers": {(DOMAIN, plant_uid)},
                "name": plant_name,
                "manufacturer": "SAJ",
                "model": device_model,
            }
        else:
            inverter_name = f"{plant_name} {device_label or device_sn}"
            self._attr_name = f"{inverter_name} {sensor_config['name']}"
            self._attr_unique_id = f"{DOMAIN}_{plant_uid}_{device_sn}_{sensor_key}"

            device_data = (
                plant_data.get("devices", {}).get(device_sn, {}).get("device_info", {})
            )

            self._attr_device_info = {
                "identifiers": {(DOMAIN, f"{plant_uid}_{device_sn}")},
                "name": inverter_name,
                "manufacturer": "SAJ",
                "model": device_data.get("deviceModel") or "H1",
                "serial_number": device_sn,
                **_via(coordinator, plant_uid),
            }

        self._attr_icon = sensor_config["icon"]

        # Set device class from mapping
        if sensor_config["device_class"]:
            self._attr_device_class = DEVICE_CLASS_MAP.get(sensor_config["device_class"])

        # Set state class from mapping
        if sensor_config["state_class"]:
            self._attr_state_class = STATE_CLASS_MAP.get(sensor_config["state_class"])

        # Set unit of measurement
        if sensor_config["unit"]:
            self._attr_native_unit_of_measurement = sensor_config["unit"]

    def _sources(self) -> tuple[dict[str, Any], ...]:
        """Return the data buckets this sensor reads from.

        Plant-level sensors read the coordinator's aggregate (every inverter
        summed); per-inverter sensors read that one device's own responses.
        """
        plant_data = self.coordinator.data.get(self._plant_uid, {}) or {}

        if self._device_sn is None:
            aggregate = plant_data.get("aggregate", {}) or {}
            return (
                plant_data,
                aggregate.get("device", {}) or {},
                aggregate.get("energy_flow", {}) or {},
                aggregate.get("battery_info", {}) or {},
                aggregate.get("device_alarms", {}) or {},
            )

        device_entry = (plant_data.get("devices", {}) or {}).get(self._device_sn, {}) or {}
        return (
            plant_data,
            device_entry.get("device_info", {}) or {},
            (device_entry.get("energy_flow", {}) or {}).get("data", {}) or {},
            (device_entry.get("battery_info", {}) or {}).get("data", {}) or {},
            (device_entry.get("device_alarms", {}) or {}).get("data", {}) or {},
        )

    def _detail_value(self, key: str | None = None) -> float | None:
        """Resolve a device-detail sensor (string and AC voltages).

        The plant aggregate holds values already resolved and averaged by the
        coordinator; a per-inverter sensor resolves from that inverter's own
        device info response.
        """
        plant_data = self.coordinator.data.get(self._plant_uid, {}) or {}

        if self._device_sn is None:
            aggregate = plant_data.get("aggregate", {}) or {}
            return (aggregate.get("device_detail") or {}).get(key or self._sensor_key)

        device_entry = (plant_data.get("devices", {}) or {}).get(self._device_sn, {}) or {}
        return SAJeSolarDataUpdateCoordinator.detail_value(
            device_entry.get("device_detail") or {}, key or self._sensor_key
        )

    def _plant_generation(self, plant_data: dict[str, Any], plant_stats: dict[str, Any]) -> float | None:
        """Plant PV energy for today, this month, this year or in total, if the API reported it."""
        if self._sensor_key in ("todayElectricity", "totalElectricity"):
            field = "energy1Today" if self._sensor_key == "todayElectricity" else "energy1Total"
            for entry in plant_stats.get("energyDataList") or []:
                if entry.get("dataType") == "PV_ENERGY" and entry.get(field) not in (None, ""):
                    return float(entry[field])
            return None
        period = "month" if self._sensor_key == "monthElectricity" else "year"
        energy = (plant_data.get("plant_energy") or {}).get(period) or {}
        return optional_float(energy, "pvEnergy")

    @staticmethod
    def _battery_remaining(
        device_data: dict[str, Any], battery_info: dict[str, Any]
    ) -> tuple[float, float] | None:
        """Energy stored in the battery and its capacity, in kWh."""
        soc = optional_float(device_data, "batEnergyPercent")
        capacity = optional_float(battery_info, "batCapacity")
        if soc is None or not capacity:
            return None
        return capacity * soc / 100, capacity

    def _revenue(
        self, plant_data: dict[str, Any], device_data: dict[str, Any] | None = None
    ) -> tuple[float | None, str | None]:
        """Revenue for this sensor and its currency.

        The inverter energy detail reports it (summed over the inverters for
        the plant); plant statistics and detail are checked as fallback.
        """
        sources = [
            device_data or {},
            (plant_data.get("plant_statistics", {}) or {}).get("data", {}) or {},
            (plant_data.get("plant_details", {}) or {}).get("data", {}) or {},
        ]
        value = next(
            (v for source in sources if (v := resolve_field(source, REVENUE_FIELDS[self._sensor_key])) is not None),
            None,
        )
        currency = next(
            (source[key] for source in sources for key in CURRENCY_FIELDS if source.get(key)),
            None,
        )
        return value, currency

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Revenue sensors use the currency the plant reports."""
        if self._sensor_key in REVENUE_FIELDS:
            try:
                plant_data, device_data, _, _, _ = self._sources()
                # Monetary sensors need an ISO 4217 code, the API may send a symbol
                currency = str(self._revenue(plant_data, device_data)[1] or "").strip()
                currency = {"€": "EUR", "$": "USD", "£": "GBP", "¥": "CNY", "₹": "INR"}.get(currency, currency)
                return currency.upper() if len(currency) == 3 and currency.isalpha() else "EUR"
            except (AttributeError, TypeError):
                return "EUR"
        return getattr(self, "_attr_native_unit_of_measurement", None)

    @property
    def native_value(self) -> StateType:
        """Return the sensor value."""
        try:
            plant_data, device_data, energy_flow, battery_info, device_alarms = (
                self._sources()
            )

            # Plant-scoped data, shared by every inverter of the plant
            plant_details = plant_data.get("plant_details", {}).get("data", {})
            plant_stats = plant_data.get("plant_statistics", {}).get("data", {})

            # An inverter sharing a plant-wide energy flow (e.g. one EMS
            # module) has no flow of its own: unknown rather than 0.
            if not energy_flow and self._sensor_key in FLOW_SENSOR_KEYS:
                return None

            # Plant Detail Sensors - map from new plant data structure
            if self._sensor_key == "nowPower":
                return optional_float(device_data, "powerNow")
            elif self._sensor_key in PLANT_GENERATION_KEYS and self._device_sn is None:
                # The plant counts every inverter it ever had (e.g. a replaced
                # one); the inverter counters restart with each new inverter
                plant_value = self._plant_generation(plant_data, plant_stats)
                if plant_value is not None:
                    return plant_value
                return optional_float(device_data, PLANT_GENERATION_KEYS[self._sensor_key])
            elif self._sensor_key == "todayElectricity":
                return optional_float(device_data, "todayEnergy")
            elif self._sensor_key == "monthElectricity":
                return optional_float(device_data, "monthEnergy")
            elif self._sensor_key == "yearElectricity":
                return optional_float(device_data, "yearEnergy")
            elif self._sensor_key == "totalElectricity":
                return optional_float(device_data, "totalEnergy")
            elif self._sensor_key == "totalConsumpElec":
                return energy_stat(plant_stats, "LOAD_ENERGY", "energy1Total")
            elif self._sensor_key == "totalBuyElec":
                return energy_stat(plant_stats, "BUY_AND_SELL", "energy1Total")
            elif self._sensor_key == "totalSellElec":
                return energy_stat(plant_stats, "BUY_AND_SELL", "energy2Total")
            elif self._sensor_key == "totalPlantTreeNum":
                return environmental_stat(plant_stats, "PLANT_TREE")
            elif self._sensor_key == "totalReduceCo2":
                return environmental_stat(plant_stats, "PLANT_CO2")
            elif self._sensor_key == "lastUploadTime":
                # Prefer the latest energy flow update time (real last update).
                # Fall back to the inverter's firstOnlineTime if it is missing.
                naive_dt = parse_update_date(energy_flow.get("updateDate"))
                if naive_dt:
                    tz_name = plant_details.get("timeZone") or plant_details.get("timezone")
                    if tz_name:
                        try:
                            return dt_util.as_utc(naive_dt.replace(tzinfo=ZoneInfo(tz_name)))
                        except Exception:
                            pass
                    return dt_util.as_utc(naive_dt)

                # Fallback to original behaviour (first online time)
                timestamp = device_data.get("firstOnlineTime", "")
                if timestamp:
                    try:
                        naive_dt = datetime.strptime(timestamp, "%d/%m/%Y %H:%M:%S")
                        return dt_util.as_utc(naive_dt)
                    except ValueError:
                        return None
                return None
            elif self._sensor_key == "selfUseRate":
                # Calculate PV self-use rate from today's PV generation and grid
                # export. Prefer plant statistics, fall back to the inverter's
                # own today energy so it also works on H1 setups.
                today_pv_energy = energy_stat(plant_stats, "PV_ENERGY", "energy1Today")
                if today_pv_energy <= 0:
                    today_pv_energy = float(device_data.get("todayEnergy", 0) or 0)
                today_sell_energy = energy_stat(plant_stats, "BUY_AND_SELL", "energy2Today")

                if today_pv_energy > 0:
                    self_used_energy = max(today_pv_energy - today_sell_energy, 0)
                    return round((self_used_energy / today_pv_energy) * 100, 1)

                return 0

            # Device Power Sensors - map from energy flow and device data
            elif self._sensor_key == "pvPower":
                # Use device powerNow for real-time PV power (not solarPower which is capacity)
                return optional_float(device_data, "powerNow")
            elif self._sensor_key == "gridPower":
                # Grid power from energy flow - correct field name
                grid_power = float(energy_flow.get("sysGridPowerwatt", 0))
                grid_direction = energy_flow.get("gridDirection", 0)
                # Make negative when exporting
                return -grid_power if int(grid_direction) == 1 else grid_power
            elif self._sensor_key == "gridPowerAbsolute":
                return float(energy_flow.get("sysGridPowerwatt", 0))
            elif self._sensor_key == "batteryPower":
                # Battery power from energy flow - correct field name
                bat_power = float(energy_flow.get("batPower", 0))
                bat_direction = energy_flow.get("batteryDirection", 0)
                # Make negative when charging
                return -bat_power if int(bat_direction) == -1 else bat_power
            elif self._sensor_key == "batteryPowerAbsolute":
                return float(energy_flow.get("batPower", 0))
            elif self._sensor_key == "outPower":
                # Output power - house consumption from energy flow
                return float(energy_flow.get("totalLoadPowerwatt", 0))
            elif self._sensor_key == "totalLoadPower":
                # Total load power - correct field name
                return float(energy_flow.get("totalLoadPowerwatt", 0))
            elif self._sensor_key == "batCapcity":
                # Battery capacity from battery info (system total in kWh)
                return optional_float(battery_info, "batCapacity")
            elif self._sensor_key == "batCurr":
                # Battery current from battery info (system total)
                return optional_float(battery_info, "batCurrent")
            elif self._sensor_key == "batVoltage":
                # Battery voltage from battery info (system voltage)
                return optional_float(battery_info, "batVoltage")
            elif self._sensor_key == "usableBatCapacity":
                # Usable battery capacity from battery info
                return optional_float(battery_info, "usableBatCapacity")
            elif self._sensor_key == "batteryWorkTime":
                # Battery remaining time in hours (convert from minutes)
                work_time_min = optional_float(battery_info, "batteryWorkTime")
                if work_time_min is None:
                    return None
                return round(work_time_min / 60, 1)  # Convert to hours
            elif self._sensor_key == "operatingMode":
                # Operating mode from battery info
                return battery_info.get("userModeName") or energy_flow.get("userModeName") or "Unknown"
            elif self._sensor_key == "batEnergyPercent":
                return optional_float(device_data, "batEnergyPercent")
            elif self._sensor_key == "batteryDirection":
                direction = energy_flow.get("batteryDirection", 0)
                return BATTERY_STATES.get(int(direction), f"Unknown ({direction})")
            elif self._sensor_key == "isOnline":
                running_state = device_data.get("runningState")
                if running_state is None:
                    return None
                return "Yes" if is_running(running_state) else "No"
            elif self._sensor_key == "inverterStatus":
                # Inverter status based on the active alarms of the device(s)
                # (plant statistics no longer report a deviceStatus)
                if device_alarms.get("list") or device_data.get("runningState") == RUNNING_STATE_ALARM:
                    return "Alarm"
                if "list" not in device_alarms:
                    # Alarms could not be fetched
                    return None
                return "OK"

            # Daily Values from plant statistics and device data - correct field names
            elif self._sensor_key == "dailyConsumption":
                return energy_stat(plant_stats, "LOAD_ENERGY", "energy1Today")
            elif self._sensor_key == "dailyGridImport":
                return energy_stat(plant_stats, "BUY_AND_SELL", "energy1Today")
            elif self._sensor_key == "dailyGridExport":
                return energy_stat(plant_stats, "BUY_AND_SELL", "energy2Today")
            elif self._sensor_key == "dailyBatteryCharge":
                return energy_stat(plant_stats, "CHARGE_AND_DISCHARGE", "energy1Today")
            elif self._sensor_key == "dailyBatteryDischarge":
                return energy_stat(plant_stats, "CHARGE_AND_DISCHARGE", "energy2Today")
            elif self._sensor_key == "dailyTreesPlanted":
                # No longer provided by the plant statistics endpoint
                return None
            elif self._sensor_key == "dailyReduceCo2":
                # No longer provided by the plant statistics endpoint
                return None

            # Direction sensors from energy flow
            elif self._sensor_key == "pvDirection":
                value = int(energy_flow.get("pvDirection", 0))
                return DIRECTION_STATES.get(value, f"Unknown ({value})")
            elif self._sensor_key == "gridDirection":
                value = int(energy_flow.get("gridDirection", 0))
                return DIRECTION_STATES.get(value, f"Unknown ({value})")
            elif self._sensor_key == "outPutDirection":
                value = int(energy_flow.get("outputDirection", 0))
                return DIRECTION_STATES.get(value, f"Unknown ({value})")

            # Values the Elekeeper portal derives from the readings above
            elif self._sensor_key == "selfSufficiencyRate":
                # Share of today's consumption not bought from the grid
                consumption = energy_stat(plant_stats, "LOAD_ENERGY", "energy1Today")
                if consumption <= 0:
                    return None
                imported = energy_stat(plant_stats, "BUY_AND_SELL", "energy1Today")
                return round(max(consumption - imported, 0) / consumption * 100, 1)
            elif self._sensor_key == "totalBatteryCharge":
                return energy_stat(plant_stats, "CHARGE_AND_DISCHARGE", "energy1Total")
            elif self._sensor_key == "totalBatteryDischarge":
                return energy_stat(plant_stats, "CHARGE_AND_DISCHARGE", "energy2Total")
            elif self._sensor_key == "batteryRemainingEnergy":
                remaining = self._battery_remaining(device_data, battery_info)
                return round(remaining[0], 2) if remaining else None
            elif self._sensor_key == "batteryTimeToFull":
                remaining = self._battery_remaining(device_data, battery_info)
                bat_power = optional_float(energy_flow, "batPower")
                charging = int(energy_flow.get("batteryDirection", 0) or 0) == -1
                if not remaining or not charging or not bat_power:
                    return None
                stored, capacity = remaining
                return round(max(capacity - stored, 0) / (bat_power / 1000), 2)
            elif self._sensor_key in REVENUE_FIELDS:
                return self._revenue(plant_data, device_data)[0]

            elif self._sensor_key == "batteryCellVoltAvg":
                # Battery voltage over the cells in series (e.g. 53.5 V / 16)
                voltage = optional_float(battery_info, "batVoltage")
                cells = self._detail_value("batteryCellCount")
                if not voltage or not cells:
                    return None
                return round(voltage / cells * 1000)
            elif self._sensor_key in ("batteryCellTempMax", "batteryCellTempMin"):
                # BMS cell readings when the API has them, else the battery packs
                value = self._detail_value()
                if value is not None:
                    return value
                temps = [
                    t for battery in plant_data.get("battery_list", {}).get("data", {}).get("list", []) or []
                    if (t := to_float(battery.get("batTemperature"))) is not None
                ]
                if not temps:
                    return None
                return max(temps) if self._sensor_key == "batteryCellTempMax" else min(temps)

            # String and AC voltages from the per-inverter device info
            elif self._sensor_key in DEVICE_DETAIL_FIELDS:
                return self._detail_value()

            # Battery Info from battery list
            elif self._sensor_key in ["batVoltage", "batTemperature"]:
                battery_list = plant_data.get("battery_list", {}).get("data", {}).get("list", [])
                if battery_list:
                    battery = battery_list[0]  # Use first battery for now
                    if self._sensor_key == "batTemperature":
                        temp_str = battery.get("batTemperature", "0℃")
                        return float(temp_str.rstrip("℃"))
                return 0

            # For sensors we haven't mapped yet, return 0 or appropriate default
            return 0

        except (KeyError, TypeError, ValueError) as e:
            return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return additional state attributes for main plant sensors."""
        if self._sensor_key == "lastUploadTime" and self._device_sn is None:
            # Plant coordinates from the SAJ cloud (e.g. for a location map)
            plant_data = self.coordinator.data.get(self._plant_uid, {}) or {}
            details = (plant_data.get("plant_details", {}) or {}).get("data", {}) or {}
            lat, lon = to_float(details.get("latitude")), to_float(details.get("longitude"))
            if lat is None or lon is None or (lat == 0 and lon == 0):
                return None
            return {"latitude": lat, "longitude": lon}
        if self._sensor_key == "operatingMode":
            try:
                _, _, _, battery_info, _ = self._sources()

                # Add system-level attributes to operating mode sensor
                attributes = {}

                battery_quantity = battery_info.get("batteryQuantity")
                if battery_quantity:
                    attributes["battery_quantity"] = battery_quantity

                update_date = battery_info.get("updateDate")
                if update_date:
                    attributes["last_update"] = update_date

                return attributes if attributes else None

            except (KeyError, TypeError, ValueError):
                pass

        elif self._sensor_key == "inverterStatus":
            try:
                plant_data, _, _, _, device_alarms = self._sources()

                attributes = {}

                # Inverter identity, as listed by the portal device table
                devices = plant_data.get("device_list", {}).get("data", {}).get("list", [])
                if self._device_sn is not None:
                    devices = [d for d in devices if d.get("deviceSn") == self._device_sn]
                if devices:
                    attributes["serial_number"] = devices[0].get("deviceSn")
                    attributes["model"] = devices[0].get("deviceModel")
                    if len(devices) > 1:
                        attributes["serial_numbers"] = [d.get("deviceSn") for d in devices]

                alarm_list = device_alarms.get("list", [])
                if alarm_list:
                    # Get the first (most recent) alarm
                    alarm = alarm_list[0]
                    attributes["alarm_name"] = alarm.get("alarmName", "Unknown")
                    attributes["alarm_level"] = alarm.get("alarmLevelName", "Unknown")
                    attributes["alarm_duration"] = alarm.get("alarmDuration", "Unknown")
                    attributes["alarm_start_time"] = alarm.get("alarmStartTime", "Unknown")
                    attributes["alarm_state"] = alarm.get("alarmStateName", "Unknown")
                    if self._device_sn is None and len(alarm_list) > 1:
                        attributes["alarm_count"] = len(alarm_list)

                return attributes if attributes else None

            except (KeyError, TypeError, ValueError):
                pass

        return None

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        if self.coordinator.data is None:
            return False

        try:
            # For the plant this is the aggregate state (online when at least
            # one inverter is), for an inverter sensor it is its own state.
            _, device_data, _, _, _ = self._sources()
            if not device_data or device_data.get("deviceCount") == 0:
                return False
            # Unknown state (e.g. the request failed) keeps the entity available
            running_state = device_data.get("runningState")
            return running_state is None or bool(int(running_state))
        except (KeyError, TypeError, ValueError):
            return False


class SAJeBatterySensor(CoordinatorEntity[SAJeSolarDataUpdateCoordinator], SensorEntity):
    """Representation of a SAJ eSolar battery sensor."""

    def __init__(
        self,
        coordinator: SAJeSolarDataUpdateCoordinator,
        sensor_key: str,
        sensor_config: dict[str, Any],
        plant_uid: str,
        plant_name: str,
        battery_sn: str,
        battery_name: str,
        battery_index: int,
    ) -> None:
        """Initialize the battery sensor."""
        super().__init__(coordinator)

        self._sensor_key = sensor_key
        self._config = sensor_config
        self._plant_uid = plant_uid
        self._plant_name = plant_name
        self._battery_sn = battery_sn
        self._battery_name = battery_name
        self._battery_index = battery_index

        # Set up entity properties with battery-specific naming
        self._attr_name = f"{battery_name} {sensor_config['name']}"
        self._attr_unique_id = f"{DOMAIN}_{plant_uid}_{battery_sn}_{sensor_key}"
        self._attr_icon = sensor_config["icon"]

        # Create battery-specific device info
        # Get battery model from coordinator data
        plant_data = coordinator.data.get(plant_uid, {})
        battery_list = plant_data.get("battery_list", {}).get("data", {}).get("list", [])
        battery_model = "Unknown"
        if battery_index < len(battery_list):
            battery_model = battery_list[battery_index].get("batModel", "Unknown")

        self._attr_device_info = {
            "identifiers": {(DOMAIN, f"{plant_uid}_{battery_sn}")},
            "name": battery_name,
            "manufacturer": "SAJ",
            "model": battery_model,
            **_via(coordinator, plant_uid),
        }

        # Set device class from mapping
        if sensor_config["device_class"]:
            self._attr_device_class = DEVICE_CLASS_MAP.get(sensor_config["device_class"])

        # Set state class from mapping
        if sensor_config["state_class"]:
            self._attr_state_class = STATE_CLASS_MAP.get(sensor_config["state_class"])

        # Set unit of measurement
        if sensor_config["unit"]:
            self._attr_native_unit_of_measurement = sensor_config["unit"]

    @property
    def native_value(self) -> StateType:
        """Return the battery sensor value."""
        try:
            # Get data for this specific plant
            plant_data = self.coordinator.data.get(self._plant_uid, {})
            battery_list = plant_data.get("battery_list", {}).get("data", {}).get("list", [])

            # Get this specific battery data
            if self._battery_index < len(battery_list):
                battery_data = battery_list[self._battery_index]
            else:
                return None

            # Map battery sensor values
            if self._sensor_key == "batSoc":
                soc_str = battery_data.get("batSoc", "0%")
                return float(soc_str.rstrip("%"))
            elif self._sensor_key == "batTemperature":
                temp_str = battery_data.get("batTemperature", "0℃")
                return float(temp_str.rstrip("℃"))
            elif self._sensor_key == "batSoh":
                soh_raw = battery_data.get("batSoh", "0")
                try:
                    # Try interpreting as degradation percentage (1% degraded = 99% health)
                    degradation = float(soh_raw)
                    health = 100 - degradation
                    return max(0, min(100, health))  # Clamp between 0-100%
                except (ValueError, TypeError):
                    return 0
            elif self._sensor_key == "runningState":
                state = battery_data.get("runningState", 0)
                state_map = {0: "Offline", 1: "Standby", 2: "Running"}
                return state_map.get(int(state), f"Unknown ({state})")
            elif self._sensor_key == "batModel":
                return battery_data.get("batModel", "Unknown")
            elif self._sensor_key == "batPower":
                # Individual battery power - need to calculate from system data
                # For now, use system power divided by number of batteries
                plant_data = self.coordinator.data.get(self._plant_uid, {})
                battery_info = plant_data.get("battery_info", {}).get("data", {})
                system_power = float(battery_info.get("batPower", 0))
                battery_quantity = battery_info.get("batteryQuantity", 2)
                return round(system_power / max(1, battery_quantity), 1)
            elif self._sensor_key == "batCurrent":
                # Individual battery current - use system current divided by batteries
                plant_data = self.coordinator.data.get(self._plant_uid, {})
                battery_info = plant_data.get("battery_info", {}).get("data", {})
                system_current = float(battery_info.get("batCurrent", 0))
                battery_quantity = battery_info.get("batteryQuantity", 2)
                return round(system_current / max(1, battery_quantity), 2)
            elif self._sensor_key == "batVoltage":
                # Individual battery voltage - use system voltage (same for all)
                plant_data = self.coordinator.data.get(self._plant_uid, {})
                battery_info = plant_data.get("battery_info", {}).get("data", {})
                return float(battery_info.get("batVoltage", 0))
            elif self._sensor_key == "todayBatCharge":
                # Today battery charge - use system value divided by batteries
                plant_data = self.coordinator.data.get(self._plant_uid, {})
                battery_info = plant_data.get("battery_info", {}).get("data", {})
                system_charge = float(battery_info.get("todayBatChgEnergy", 0))
                battery_quantity = battery_info.get("batteryQuantity", 2)
                return round(system_charge / max(1, battery_quantity), 2)
            elif self._sensor_key == "todayBatDischarge":
                # Today battery discharge - use system value divided by batteries
                plant_data = self.coordinator.data.get(self._plant_uid, {})
                battery_info = plant_data.get("battery_info", {}).get("data", {})
                system_discharge = float(battery_info.get("todayBatDisEnergy", 0))
                battery_quantity = battery_info.get("batteryQuantity", 2)
                return round(system_discharge / max(1, battery_quantity), 2)
            elif self._sensor_key == "totalBatCharge":
                # Total battery charge - use system value divided by batteries
                plant_data = self.coordinator.data.get(self._plant_uid, {})
                battery_info = plant_data.get("battery_info", {}).get("data", {})
                system_charge = float(battery_info.get("totalBatChgEnergy", 0))
                battery_quantity = battery_info.get("batteryQuantity", 2)
                return round(system_charge / max(1, battery_quantity), 2)
            elif self._sensor_key == "totalBatDischarge":
                # Total battery discharge - use system value divided by batteries
                plant_data = self.coordinator.data.get(self._plant_uid, {})
                battery_info = plant_data.get("battery_info", {}).get("data", {})
                system_discharge = float(battery_info.get("totalBatDisEnergy", 0))
                battery_quantity = battery_info.get("batteryQuantity", 2)
                return round(system_discharge / max(1, battery_quantity), 2)

            return None

        except (KeyError, TypeError, ValueError):
            return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return additional state attributes for battery sensors."""
        if self._sensor_key != "batModel":
            return None

        try:
            # Get data for this specific plant
            plant_data = self.coordinator.data.get(self._plant_uid, {})
            battery_list = plant_data.get("battery_list", {}).get("data", {}).get("list", [])

            # Get this specific battery data
            if self._battery_index < len(battery_list):
                battery_data = battery_list[self._battery_index]

                # Add hardware and software versions as attributes to battery model sensor
                attributes = {}

                hw_version = battery_data.get("bmsHardwareVersion")
                if hw_version:
                    attributes["hardware_version"] = hw_version

                sw_version = battery_data.get("bmsSoftwareVersion")
                if sw_version:
                    attributes["software_version"] = sw_version

                # Add other useful battery info
                battery_sn = battery_data.get("batSn")
                if battery_sn:
                    attributes["serial_number"] = battery_sn

                cluster_id = battery_data.get("clusterId")
                if cluster_id:
                    attributes["cluster_id"] = cluster_id

                return attributes if attributes else None

        except (KeyError, TypeError, ValueError):
            pass

        return None

    @property
    def available(self) -> bool:
        """Return if battery entity is available."""
        if self.coordinator.data is None:
            return False

        try:
            # Check if battery data is available
            plant_data = self.coordinator.data.get(self._plant_uid, {})
            battery_list = plant_data.get("battery_list", {}).get("data", {}).get("list", [])
            return self._battery_index < len(battery_list)
        except (KeyError, TypeError, ValueError):
            return False
