"""DataUpdateCoordinator for SAJ eSolar integration."""
from datetime import datetime, timedelta
import logging
from typing import Any
import re
import time
import json

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.exceptions import ConfigEntryAuthFailed

from .const import DEVICE_DETAIL_AGGREGATION, DEVICE_DETAIL_FIELDS, DOMAIN, ENDPOINTS, ENDPOINTS_V2, UPDATE_INTERVAL, REGIONS
from .elekeeper import calc_signature, encrypt, generatkey, sign_v2_request

_LOGGER = logging.getLogger(__name__)

# Inverter runningState values, as listed by the Elekeeper web device filter
RUNNING_STATE_NORMAL = 1
RUNNING_STATE_ALARM = 2

# Date formats the API uses for updateDate (v2 energy flow, v1 battery info)
UPDATE_DATE_FORMATS = ("%d/%m/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S")

# Keys already reported as unresolvable, so the warning is logged once per key
# instead of on every update.
_WARNED_FIELDS: set[str] = set()

# Detail values only some inverters/batteries report: missing is expected
_OPTIONAL_DETAIL = {
    "batteryCellTempMax", "batteryCellTempMin", "batteryCellVoltMax", "batteryCellVoltMin", "batteryCellCount",
}


def _normalise(key: str) -> str:
    """Reduce a field name to letters and digits for tolerant comparison."""
    return "".join(char for char in str(key).lower() if char.isalnum())


def to_float(value: Any) -> float | None:
    """Parse a number that the API may have wrapped in units or spaces."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()
    if not text:
        return None

    match = re.match(r"[-+]?\d*\.?\d+", text)
    if not match:
        return None
    try:
        return float(match.group())
    except ValueError:
        return None


def resolve_field(source: dict[str, Any], candidates: tuple[str, ...]) -> float | None:
    """Read the first candidate field present in an API response.

    SAJ spells these fields differently across inverter families, so the
    candidates are matched ignoring case and separators (``pv1Volt`` also
    matches ``PV1_VOLT``). Returns ``None`` when nothing matches, which the
    sensor reports as unavailable rather than as a misleading zero.
    """
    if not source:
        return None

    normalised = {_normalise(key): value for key, value in source.items()}
    for candidate in candidates:
        if (value := to_float(normalised.get(_normalise(candidate)))) is not None:
            return value
    return None


def _flatten(data: dict[str, Any]) -> dict[str, Any]:
    """Lift scalar fields of nested objects (and first list items) to the top.

    Battery details nest the BMS readings (e.g. in a list of BMS records);
    top-level fields win over nested ones with the same name.
    """
    flat: dict[str, Any] = {}
    nested: list[dict[str, Any]] = []
    for key, value in data.items():
        if isinstance(value, dict):
            nested.append(value)
        elif isinstance(value, list):
            nested.extend(item for item in value[:1] if isinstance(item, dict))
        else:
            flat[key] = value
    for item in nested:
        for key, value in _flatten(item).items():
            flat.setdefault(key, value)
    return flat


def parse_update_date(value: Any) -> datetime | None:
    """Parse an API updateDate in any of the formats the API uses."""
    if not value:
        return None
    for fmt in UPDATE_DATE_FORMATS:
        try:
            return datetime.strptime(str(value), fmt)
        except ValueError:
            continue
    return None


class SAJeSolarDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from the SAJ eSolar API."""

    def __init__(
        self,
        hass: HomeAssistant,
        session: aiohttp.ClientSession,
        username: str,
        password: str,
        region: str = "eu",
        monitored_plants: list[str] | None = None,
    ) -> None:
        """Initialize."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )
        self.session = session
        self.username = username
        self.password = password
        self.region = region
        self.base_url = REGIONS[region]
        self.base_url_v2 = self.base_url.replace("/api/v1", "/api/v2")
        self.monitored_plants = monitored_plants or []
        self.auth_token = None
        self._warned: set[str] = set()

    async def _async_update_data(self) -> dict[str, Any]:
        """Update data via API."""
        try:
            await self._authenticate()

            plant_data = await self._get_plant_list()
            plants = (plant_data.get("data") or {}).get("list") or []

            plants_data = {}

            for plant_uid in self.monitored_plants:
                plant_info = next((p for p in plants if p.get("plantUid") == plant_uid), None)
                if not plant_info:
                    _LOGGER.warning(
                        "Plant %s not found in the account plant list (%d plants returned), skipping it",
                        plant_uid,
                        len(plants),
                    )
                    continue

                plants_data[plant_uid] = await self._async_update_plant(plant_uid, plant_info)

            return plants_data

        except ConfigEntryAuthFailed as err:
            # There is no reauth flow, keep retrying (the error may be temporary)
            raise UpdateFailed(f"Authentication with the SAJ API failed: {err}") from err
        except UpdateFailed:
            raise
        except aiohttp.ClientError as err:
            raise UpdateFailed(f"Error communicating with the SAJ API: {err!r}") from err
        except Exception as err:
            _LOGGER.exception("Unexpected error while fetching SAJ data")
            raise UpdateFailed(f"Unexpected error fetching SAJ data: {err!r}") from err

    async def _async_update_plant(
        self, plant_uid: str, plant_info: dict[str, Any]
    ) -> dict[str, Any]:
        """Fetch every inverter of a plant and build the plant-level aggregate.

        A plant can group several inverters (e.g. two 33kW units under one
        plant name). The device-scoped endpoints only ever report a single
        inverter, so they are queried once per device and the results summed
        into ``aggregate`` for the plant-level sensors.

        Failing to fetch the core data fails the whole update (so sensors
        become unavailable instead of reporting zeros); the extras only log a
        warning and are left empty.
        """
        plant_details = await self._get_plant_details_for_plant(plant_uid)
        sn_list = await self._get_plant_sn_list(plant_uid)
        device_list = await self._get_device_list_for_plant(plant_uid)
        battery_list = await self._optional(plant_uid, "battery list", self._get_battery_list_for_plant(plant_uid))

        devices = device_list["data"]["list"]
        if not devices:
            _LOGGER.warning("No inverter found for plant %s, inverter, battery and alarm data will be empty", plant_uid)

        # Plant statistics are plant-scoped; the device only selects which
        # meter answers, so fetch them once, the way the web client does.
        plant_statistics = await self._post_v2(
            "plant_statistics", {"plantUid": plant_uid, **self._query_device(sn_list, {}, 0)}, plant_uid
        )

        # Energy flow responses keyed by the queried serial. When several
        # inverters resolve to the same serial (e.g. one EMS module for the
        # whole plant) the flow is plant-wide: fetch it once and do not
        # attribute it to any single inverter.
        flows: dict[tuple, dict[str, Any]] = {}
        query_keys: list[tuple] = []
        for index, device in enumerate(devices or [{}]):
            query_device = self._query_device(sn_list, device, index)
            key = tuple(sorted(query_device.items()))
            query_keys.append(key)
            if key not in flows:
                flows[key] = await self._post_v2("energy_flow", {"plantUid": plant_uid, **query_device}, plant_uid)

        # Per-device data, keyed by inverter serial.
        devices_data: dict[str, dict[str, Any]] = {}
        for index, device in enumerate(devices):
            device_sn = device.get("deviceSn") or f"device_{index + 1}"
            key = query_keys[index]
            shared_flow = query_keys.count(key) > 1

            battery_info: dict[str, Any] = {}
            device_alarms: dict[str, Any] = {}
            device_detail: dict[str, Any] = {}
            if device.get("deviceSn"):
                battery_info = await self._optional(
                    plant_uid, "battery info", self._get_battery_info(device["deviceSn"], plant_uid)
                )
                device_alarms = await self._optional(
                    plant_uid, "device alarms", self._get_device_alarms(device["deviceSn"], plant_uid)
                )
                device_detail = await self._get_device_detail(device, plant_uid)

            devices_data[device_sn] = {
                "device_info": device,
                "device_index": index,
                "query_device": dict(key),
                "energy_flow": {} if shared_flow else flows[key],
                "battery_info": battery_info,
                "device_alarms": device_alarms,
                "device_detail": device_detail,
            }

        primary = devices_data[next(iter(devices_data))] if devices_data else {}
        # A single flow covering the whole plant (one inverter, or one EMS
        # module shared by all of them) is used as is instead of summed.
        plant_flow = flows[query_keys[0]] if len(flows) == 1 else None

        return {
            "plant_info": plant_info,
            "plant_details": plant_details,
            "device_list": device_list,
            "battery_list": battery_list,
            "plant_statistics": plant_statistics,
            # Kept for backwards compatibility: the primary inverter's own data.
            "energy_flow": plant_flow or primary.get("energy_flow", {}),
            "battery_info": primary.get("battery_info", {}),
            "device_alarms": primary.get("device_alarms", {}),
            "devices": devices_data,
            "aggregate": self._build_aggregate(devices_data, plant_flow),
        }

    @staticmethod
    def _build_aggregate(
        devices_data: dict[str, dict[str, Any]],
        plant_flow: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Sum the per-inverter values into plant-wide totals.

        Powers and energies add up across inverters. Directional values are
        recombined from the signed sum so that, for example, one inverter
        importing while another exports reports the net plant flow. A value
        no inverter reports stays ``None`` (unknown) instead of becoming 0,
        avoiding false resets of total_increasing sensors.
        """
        def _add(target: dict[str, Any], key: str, value: float | None) -> None:
            if value is not None:
                target[key] = (target.get(key) or 0.0) + value

        def _int(source: dict[str, Any], key: str) -> int | None:
            try:
                return int(source[key])
            except (KeyError, TypeError, ValueError):
                return None

        device_keys = (
            "powerNow",
            "todayEnergy",
            "monthEnergy",
            "yearEnergy",
            "totalEnergy",
            "todayBatChgEnergy",
            "todayBatDisChgEnergy",
            "todayIncome",
            "totalIncome",
            "yesterdayIncome",
        )
        battery_keys = (
            "batCapacity",
            "usableBatCapacity",
            "batCurrent",
            "batPower",
            "todayBatChgEnergy",
            "todayBatDisEnergy",
            "totalBatChgEnergy",
            "totalBatDisEnergy",
            "batteryQuantity",
        )
        device_agg: dict[str, Any] = dict.fromkeys(device_keys)
        battery_agg: dict[str, Any] = dict.fromkeys(battery_keys)
        flow_agg: dict[str, Any] = dict.fromkeys(("totalLoadPowerwatt", "pvPower"))

        grid_signed: float | None = None
        battery_signed: float | None = None
        soc_values: list[float] = []
        voltages: list[float] = []
        detail_values: dict[str, list[float]] = {key: [] for key in DEVICE_DETAIL_FIELDS}
        work_times: list[float] = []
        pv_directions: list[int] = []
        out_directions: list[int] = []
        flow_dates: list[str] = []
        battery_dates: list[str] = []
        running_states: list[int] = []
        alarms: list[dict[str, Any]] = []
        alarms_fetched = False
        user_mode = None
        first_online_time = None

        for device_data in devices_data.values():
            device = device_data.get("device_info") or {}
            flow = (device_data.get("energy_flow") or {}).get("data") or {}
            battery = (device_data.get("battery_info") or {}).get("data") or {}
            detail = device_data.get("device_detail") or {}
            device_alarms = (device_data.get("device_alarms") or {}).get("data") or {}

            for key in detail_values:
                value = SAJeSolarDataUpdateCoordinator.detail_value(detail, key)
                if value is not None:
                    detail_values[key].append(value)

            for key in device_keys:
                _add(device_agg, key, to_float(device.get(key)))

            for key in flow_agg:
                _add(flow_agg, key, to_float(flow.get(key)))

            # Grid: negative means exporting (gridDirection == 1).
            grid_power = to_float(flow.get("sysGridPowerwatt"))
            if grid_power is not None:
                grid_power = -grid_power if _int(flow, "gridDirection") == 1 else grid_power
                grid_signed = (grid_signed or 0.0) + grid_power

            # Battery: negative means charging (batteryDirection == -1).
            bat_power = to_float(flow.get("batPower"))
            if bat_power is not None:
                bat_power = -bat_power if _int(flow, "batteryDirection") == -1 else bat_power
                battery_signed = (battery_signed or 0.0) + bat_power

            for key in battery_keys:
                _add(battery_agg, key, to_float(battery.get(key)))

            if (soc := to_float(device.get("batEnergyPercent"))) is not None:
                soc_values.append(soc)

            if voltage := to_float(battery.get("batVoltage")):
                voltages.append(voltage)

            if work_time := to_float(battery.get("batteryWorkTime")):
                work_times.append(work_time)

            if user_mode is None:
                user_mode = battery.get("userModeName") or flow.get("userModeName")

            for values, key in ((pv_directions, "pvDirection"), (out_directions, "outputDirection")):
                if (direction := _int(flow, key)) is not None:
                    values.append(direction)

            if flow.get("updateDate"):
                flow_dates.append(flow["updateDate"])
            if battery.get("updateDate"):
                battery_dates.append(battery["updateDate"])

            if (running_state := _int(device, "runningState")) is not None:
                running_states.append(running_state)

            if "list" in device_alarms:
                alarms_fetched = True
                alarms.extend(device_alarms.get("list") or [])

            if first_online_time is None and device.get("firstOnlineTime"):
                first_online_time = device["firstOnlineTime"]

        def _direction(values: list[int]) -> int | None:
            """Collapse per-inverter directions: any active flow wins."""
            for candidate in (1, -1):
                if candidate in values:
                    return candidate
            return 0 if values else None

        def _latest(dates: list[str]) -> str:
            """Most recent updateDate, kept in the format the API sent it."""
            parsed = [(parse_update_date(d), d) for d in dates]
            parsed = [item for item in parsed if item[0] is not None]
            return max(parsed)[1] if parsed else ""

        # Online when any inverter is; in alarm when any inverter is.
        if RUNNING_STATE_ALARM in running_states:
            device_agg["runningState"] = RUNNING_STATE_ALARM
        elif RUNNING_STATE_NORMAL in running_states:
            device_agg["runningState"] = RUNNING_STATE_NORMAL
        elif running_states:
            device_agg["runningState"] = 0
        else:
            device_agg["runningState"] = None
        device_agg["batEnergyPercent"] = (
            round(sum(soc_values) / len(soc_values), 1) if soc_values else None
        )
        device_agg["firstOnlineTime"] = first_online_time or ""
        device_agg["deviceCount"] = len(devices_data)
        device_agg["currency"] = next(
            (d["device_info"]["currency"] for d in devices_data.values() if (d.get("device_info") or {}).get("currency")),
            None,
        )

        if plant_flow is not None:
            # One flow already describes the whole plant, do not sum it.
            flow_agg = dict(plant_flow.get("data") or {})
        else:
            flow_agg["sysGridPowerwatt"] = abs(grid_signed) if grid_signed is not None else None
            flow_agg["gridDirection"] = (
                None if grid_signed is None else 1 if grid_signed < 0 else -1 if grid_signed > 0 else 0
            )
            flow_agg["batPower"] = abs(battery_signed) if battery_signed is not None else None
            flow_agg["batteryDirection"] = (
                None if battery_signed is None else -1 if battery_signed < 0 else 1 if battery_signed > 0 else 0
            )
            flow_agg["pvDirection"] = _direction(pv_directions)
            flow_agg["outputDirection"] = _direction(out_directions)
            flow_agg["updateDate"] = _latest(flow_dates)
            flow_agg["userModeName"] = user_mode
        device_agg["batteryDirection"] = flow_agg.get("batteryDirection")

        # Voltage is a system property, not something to add up.
        battery_agg["batVoltage"] = (
            round(sum(voltages) / len(voltages), 2) if voltages else None
        )
        # Remaining runtime of the plant is bounded by the shortest bank.
        battery_agg["batteryWorkTime"] = min(work_times) if work_times else None
        battery_agg["userModeName"] = user_mode
        battery_agg["updateDate"] = _latest(battery_dates)

        # Voltages and temperatures are levels, not quantities: averaging the
        # inverters (or taking the extreme, for highest/lowest readings)
        # describes the plant, whereas summing them would be meaningless.
        def _combine(key: str, values: list[float]) -> float | None:
            if not values:
                return None
            mode = DEVICE_DETAIL_AGGREGATION.get(key)
            if mode == "max":
                return max(values)
            if mode == "min":
                return min(values)
            return round(sum(values) / len(values), 1)

        detail_agg = {key: _combine(key, values) for key, values in detail_values.items()}

        return {
            "device": device_agg,
            "energy_flow": flow_agg,
            "battery_info": battery_agg,
            "device_detail": detail_agg,
            # Every inverter's active alarms; empty when none could be fetched.
            "device_alarms": {"list": alarms} if alarms_fetched else {},
        }

    async def _optional(self, plant_uid: str, what: str, coro) -> dict[str, Any]:
        """Await a non essential request, logging and swallowing failures."""
        try:
            return await coro
        except (UpdateFailed, aiohttp.ClientError) as err:
            _LOGGER.warning("Could not fetch %s for plant %s, related sensors will be empty: %s", what, plant_uid, err)
            return {}

    async def _authenticate(self) -> None:
        """Authenticate with the SAJ eSolar API using the Elekeeper v2 login.

        The token returned by the v2 login is valid for both v1 and v2
        endpoints, whereas the old v1 login token makes v2 endpoints
        return empty data.
        """
        login_data = {
            "username": self.username,
            "password": encrypt(self.password),
            "rememberMe": False,
            "loginType": 1,
        }

        headers, body = sign_v2_request(login_data)
        url = f"{self.base_url_v2}{ENDPOINTS_V2['login']}"

        async with self.session.post(url, data=body, headers=headers) as resp:
            if resp.status == 401:
                raise ConfigEntryAuthFailed("Invalid authentication (HTTP 401)")
            if resp.status != 200:
                raise UpdateFailed(f"Login failed: HTTP {resp.status} from {url}")

            response_data = await self._read_json(resp, "login")

        err_code = response_data.get("errCode")
        _LOGGER.debug("login: errCode=%s", err_code)
        if err_code not in (None, 0):
            raise ConfigEntryAuthFailed(
                f"Login failed (errCode {err_code}): {response_data.get('errMsg') or 'Unknown error'}"
            )

        data = response_data.get("data") or {}
        if not data.get("token"):
            raise UpdateFailed(f"Login response did not include a token (keys: {sorted(data)})")
        self.auth_token = (data.get("tokenHead") or "Bearer ") + data["token"]

    @staticmethod
    async def _read_json(resp: aiohttp.ClientResponse, what: str) -> dict[str, Any]:
        """Parse a JSON response, regardless of the content type the API sends."""
        text = await resp.text()
        try:
            return json.loads(text)
        except ValueError as err:
            raise UpdateFailed(
                f"Unexpected non-JSON response for {what} (HTTP {resp.status}, {resp.content_type}): {text[:200]}"
            ) from err

    async def _post_v2(self, endpoint: str, body: dict[str, Any], plant_uid: str | None = None) -> dict[str, Any]:
        """POST a signed request to an api/v2 endpoint."""
        headers, raw = sign_v2_request(body, token=self.auth_token)
        url = f"{self.base_url_v2}{ENDPOINTS_V2[endpoint]}"
        where = f"{endpoint} (plant {plant_uid})" if plant_uid else endpoint

        async with self.session.post(url, data=raw, headers=headers) as resp:
            if resp.status != 200:
                raise UpdateFailed(f"Failed to get {where}: HTTP {resp.status} from {url}")
            response_data = await self._read_json(resp, where)

        err_code = response_data.get("errCode", 0)
        _LOGGER.debug("%s: errCode=%s", where, err_code)
        if err_code != 0:
            raise UpdateFailed(
                f"Failed to get {where}: errCode {err_code}, {response_data.get('errMsg') or 'Unknown error'}"
            )
        return response_data

    async def _get_plant_list(self) -> dict[str, Any]:
        """Get list of plants."""
        return await self._post_v2("plant_list", {"pageNo": 1, "pageSize": 500})

    async def _get_plant_details_for_plant(self, plant_uid: str) -> dict[str, Any]:
        """Get plant details for specific plant."""
        return await self._post_v2("plant_detail", {"plantUid": plant_uid}, plant_uid)

    async def _get_device_list_for_plant(self, plant_uid: str) -> dict[str, Any]:
        """Get the plant inverters, in the shape of the old v1 device list.

        The plant device tree gives the inverters, the inverter energy detail
        their power and energy counters, the device capability their running
        state and the latest raw data record the yearly energy.
        """
        tree = await self._post_v2("device_tree", {"plantUid": plant_uid}, plant_uid)
        inverters = [d for d in tree.get("data") or [] if d.get("deviceType") == 1]

        devices = []
        for inverter in inverters:
            device = {
                "deviceSn": inverter.get("deviceSn"),
                "deviceModel": inverter.get("deviceModel"),
                "firstOnlineTime": inverter.get("accessTime"),
            }
            for key in ("deviceName", "aliasName"):
                if inverter.get(key):
                    device[key] = inverter[key]
            # Every inverter is queried: multi-inverter plants sum them all
            if device["deviceSn"]:
                await self._add_inverter_data(plant_uid, device)
            devices.append(device)

        return {"data": {"list": devices}}

    async def _add_inverter_data(self, plant_uid: str, device: dict[str, Any]) -> None:
        """Add real time data to an inverter entry of the device list."""
        device_sn = device["deviceSn"]

        energy = (await self._post_v2("inverter_energy", {"deviceSn": device_sn}, plant_uid)).get("data") or {}
        for key in (
            "powerNow", "todayEnergy", "monthEnergy", "totalEnergy",
            "todayIncome", "totalIncome", "yesterdayIncome", "currency",
        ):
            if energy.get(key) not in (None, ""):
                device[key] = energy[key]
        if energy.get("batterySoc") is not None:
            device["batEnergyPercent"] = energy["batterySoc"]
        # Handed over to the field diagnostics by _get_device_detail
        device["_energy"] = energy

        capability = await self._optional(
            plant_uid, "inverter state", self._post_v2("device_capability", {"deviceSn": device_sn}, plant_uid)
        )
        if (capability.get("data") or {}).get("runningState") is not None:
            device["runningState"] = capability["data"]["runningState"]

        raw_data = await self._optional(plant_uid, "inverter raw data", self._get_latest_raw_data(device_sn, plant_uid))
        if raw_data.get("yearPVEnergy") is not None:
            device["yearEnergy"] = raw_data["yearPVEnergy"]
        # Handed over to the voltage sensors by _get_device_detail
        device["_raw_data"] = raw_data

    async def _get_latest_raw_data(self, device_sn: str, plant_uid: str) -> dict[str, Any]:
        """Get the latest raw data record of an inverter."""
        today = datetime.now().strftime("%Y-%m-%d")
        response = await self._post_v2(
            "raw_data",
            {
                "deviceSn": device_sn,
                "deviceType": 1,
                "timeStr": today,
                "startTime": f"{today} 00:00:00",
                "endTime": f"{today} 23:59:59",
                "pageNo": 1,
                "pageSize": 1,
            },
            plant_uid,
        )
        records = (response.get("data") or {}).get("list") or []
        return records[0] if records else {}

    async def _get_v1(self, endpoint: str, params: dict[str, Any], plant_uid: str) -> dict[str, Any]:
        """GET a signed request to an api/v1 endpoint."""
        data = {
            **params,
            'appProjectName': 'elekeeper',
            'clientDate': datetime.now().strftime("%Y-%m-%d"),
            'lang': 'en',
            'timeStamp': int(time.time() * 1000),
            'random': generatkey(32),
            'clientId': 'esolar-monitor-admin',
        }
        url = f"{self.base_url}{ENDPOINTS[endpoint]}"
        where = f"{endpoint} (plant {plant_uid}, api v1)"

        async with self.session.get(
            url,
            params=calc_signature(data),
            headers={'Authorization': self.auth_token},
        ) as resp:
            if resp.status != 200:
                raise UpdateFailed(f"Failed to get {where}: HTTP {resp.status} from {url}")
            response_data = await self._read_json(resp, where)

        err_code = response_data.get("errCode", 0)
        _LOGGER.debug("%s: errCode=%s", where, err_code)
        if err_code != 0:
            raise UpdateFailed(f"Failed to get {where}: errCode {err_code}, {response_data.get('errMsg') or 'Unknown error'}")
        return response_data

    async def _get_battery_list_for_plant(self, plant_uid: str) -> dict[str, Any]:
        """Get battery list for specific plant.

        Still on v1: the v2 battery list lacks the per battery SOC,
        temperature and SOH the battery sensors read.
        """
        return await self._get_v1(
            "battery_list",
            {"plantUid": plant_uid, "pageSize": 100, "pageNo": 1, "searchOfficeIdArr": "1"},
            plant_uid,
        )

    async def _get_battery_info(self, device_sn: str, plant_uid: str) -> dict[str, Any]:
        """Get the inverter battery system info.

        Prefer v1, which still reports the remaining battery time
        (batteryWorkTime); fall back to the v2 equivalent, with the same
        fields, if v1 stops working.
        """
        try:
            return await self._get_v1("battery_info", {"deviceSn": device_sn}, plant_uid)
        except (UpdateFailed, aiohttp.ClientError) as err:
            self._warn_once(
                "battery_info_v1",
                "Falling back to api v2 for battery info, the remaining battery time will not be available: %s",
                err,
            )
            return await self._post_v2("battery_info", {"deviceSn": device_sn}, plant_uid)

    def _warn_once(self, key: str, msg: str, *args: Any) -> None:
        """Log a warning only the first time it happens for this coordinator."""
        if key in self._warned:
            _LOGGER.debug(msg, *args)
            return
        self._warned.add(key)
        _LOGGER.warning(msg, *args)

    async def _get_plant_sn_list(self, plant_uid: str) -> list[dict[str, Any]]:
        """Get the device serials the plant home endpoints can be queried with.

        The plant capability ``snList`` holds inverter or EMS module serials,
        each with its type.
        """
        capability = await self._post_v2("plant_capability", {"plantUid": plant_uid}, plant_uid)
        sn_list = (capability.get("data") or {}).get("snList") or []
        if not sn_list:
            _LOGGER.warning("Plant %s capability has no device serials, querying it by plant only", plant_uid)
        return sn_list

    @staticmethod
    def _query_device(
        sn_list: list[dict[str, Any]], device: dict[str, Any], index: int
    ) -> dict[str, Any]:
        """Resolve the serial the plant home endpoints are queried with for one inverter.

        The web client takes the first ``snList`` entry, which covers single
        inverter plants and R5 + SEC-C setups (queried by the EMS module
        serial). With several inverters, each is queried by its own entry when
        the plant lists one, so their values can be summed.
        """
        if not sn_list:
            return {}

        device_sn = device.get("deviceSn")
        entry = next((item for item in sn_list if device_sn and item.get("sn") == device_sn), None)
        if entry is None and len(sn_list) > 1 and index < len(sn_list):
            entry = sn_list[index]
        if entry is None:
            entry = sn_list[0]
        return {"sn": entry.get("sn"), "snType": entry.get("snType")}

    async def _get_device_detail(self, device: dict[str, Any], plant_uid: str) -> dict[str, Any]:
        """Get the detailed per-inverter readings (string and AC voltages).

        Read from the v1 device info, with the latest v2 raw data record as
        fallback for the fields it lacks (or if v1 stops working). This only
        backs the extra diagnostic sensors, so a failure here must not take
        down the rest of the integration: the affected sensors simply report
        unavailable.
        """
        device_sn = device["deviceSn"]
        data = dict(device.pop("_raw_data", None) or {})

        # The battery energy detail (as the Elekeeper app reads it) carries the
        # BMS cell temperatures and voltages; only fill fields still missing.
        battery_detail = await self._optional(
            plant_uid, "battery detail", self._post_v2("battery_info", {"deviceSn": device_sn}, plant_uid)
        )
        # "More data" (as the portal names it) holds the BMS real-time
        # readings: highest/lowest cell temperature and voltage.
        more_data = await self._optional(
            plant_uid,
            "device more data",
            self._post_v2("device_more_data", {"deviceSn": device_sn, "searchFromRealData": 1}, plant_uid),
        )
        for source in (battery_detail, more_data):
            for key, value in _flatten(source.get("data") or {}).items():
                if data.get(key) in (None, ""):
                    data[key] = value

        self._log_field_values_once(
            device_sn,
            {
                "raw_data": data,
                "battery_detail": battery_detail.get("data"),
                "more_data": more_data.get("data"),
                "inverter_energy": device.pop("_energy", None),
            },
        )

        try:
            detail = await self._get_v1("device_info", {"deviceSn": device_sn}, plant_uid)
            data.update(detail.get("data") or {})
        except (UpdateFailed, aiohttp.ClientError) as err:
            self._warn_once(
                "device_info_v1",
                "Device info (api v1) unavailable, voltage sensors fall back to the raw data record: %s",
                err,
            )

        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("Device %s detail fields: %s", device_sn, sorted(data.keys()))

        return {"data": data}

    def _log_field_values_once(self, device_sn: str, sources: dict[str, Any]) -> None:
        """Log, once per inverter, the temperature/voltage/income fields and their values.

        Field names alone do not tell which ones are filled for a given
        inverter and battery, so this lists every matching value (including
        inside nested lists) to map the sensors to the right fields.
        """
        if f"fields_{device_sn}" in self._warned:
            return
        self._warned.add(f"fields_{device_sn}")

        pattern = re.compile(r"temp|vol|income|currency|cell|soh|maxt|mint", re.IGNORECASE)
        found: list[str] = []

        def walk(value: Any, path: str) -> None:
            if len(found) >= 150:
                return
            if isinstance(value, dict):
                for key, item in value.items():
                    walk(item, f"{path}.{key}" if path else key)
            elif isinstance(value, list):
                for index, item in enumerate(value[:4]):
                    walk(item, f"{path}[{index}]")
            elif pattern.search(path.rsplit(".", 1)[-1]) and not path.endswith("Str") and value not in (None, ""):
                found.append(f"{path}={value}")

        for name, source in sources.items():
            walk(source or {}, name)
        _LOGGER.debug("Field values reported for inverter %s: %s", device_sn, "; ".join(found) or "none")

    @staticmethod
    def detail_value(device_detail: dict[str, Any], sensor_key: str) -> float | None:
        """Resolve one detail sensor from an inverter's device info response."""
        candidates = DEVICE_DETAIL_FIELDS.get(sensor_key)
        if not candidates:
            return None

        data = (device_detail or {}).get("data") or {}
        value = resolve_field(data, candidates)

        if value is None and data and sensor_key not in _WARNED_FIELDS and sensor_key not in _OPTIONAL_DETAIL:
            _WARNED_FIELDS.add(sensor_key)
            _LOGGER.warning(
                "No field matching %s found in the device info response; "
                "available fields: %s. Please report these so the mapping can "
                "be corrected.",
                sensor_key,
                sorted(data.keys()),
            )

        return value

    async def _get_device_alarms(self, device_sn: str, plant_uid: str) -> dict[str, Any]:
        """Get the active alarms of a device."""
        return await self._post_v2(
            "device_alarms",
            {"deviceSn": device_sn, "alarmCommonState": 1, "pageNo": 1, "pageSize": 10},
            plant_uid,
        )
