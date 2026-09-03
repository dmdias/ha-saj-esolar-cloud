"""DataUpdateCoordinator for SAJ eSolar integration."""
from datetime import datetime, timedelta
import logging
from typing import Any
import time

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.exceptions import ConfigEntryAuthFailed

from .const import DOMAIN, ENDPOINTS, UPDATE_INTERVAL, REGIONS
from .elekeeper import calc_signature, encrypt, generatkey

_LOGGER = logging.getLogger(__name__)

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
        self.monitored_plants = monitored_plants or []
        self.auth_token = None

    async def _async_update_data(self) -> dict[str, Any]:
        """Update data via API."""
        try:
            # Authenticate using new Elekeeper system
            await self._authenticate()

            # Get plant list
            plant_data = await self._get_plant_list()

            # Get data for each monitored plant
            plants_data = {}

            for plant_uid in self.monitored_plants:
                # Find the plant in the plant list
                plant_info = None
                for plant in plant_data.get("data", {}).get("list", []):
                    if plant["plantUid"] == plant_uid:
                        plant_info = plant
                        break

                if not plant_info:
                    _LOGGER.warning(f"Plant {plant_uid} not found in plant list")
                    continue

                plants_data[plant_uid] = await self._async_update_plant(
                    plant_uid, plant_info
                )

            return plants_data

        except aiohttp.ClientError as err:
            raise UpdateFailed(f"Error communicating with API: {err}")
        except Exception as err:
            raise UpdateFailed(f"Error fetching data: {err}")

    async def _async_update_plant(
        self, plant_uid: str, plant_info: dict[str, Any]
    ) -> dict[str, Any]:
        """Fetch every inverter of a plant and build the plant-level aggregate.

        A plant can group several inverters (e.g. two 33kW units under one
        plant name). The device-scoped endpoints only ever report a single
        inverter, so they are queried once per device and the results summed
        into ``aggregate`` for the plant-level sensors.
        """
        plant_details = await self._get_plant_details_for_plant(plant_uid)
        device_list = await self._get_device_list_for_plant(plant_uid)
        battery_list = await self._get_battery_list_for_plant(plant_uid)

        plant_detail_data = plant_details.get("data") or {}
        devices = device_list.get("data", {}).get("list") or []

        if not devices:
            _LOGGER.warning(f"No devices found for plant {plant_uid}")

        # Per-device data, keyed by inverter serial.
        devices_data: dict[str, dict[str, Any]] = {}
        for index, device in enumerate(devices):
            device_sn = device.get("deviceSn") or f"device_{index + 1}"
            query_device = self._resolve_query_device(plant_detail_data, device, index)

            energy_flow = await self._get_energy_flow(plant_uid, query_device)
            battery_info = await self._get_battery_info(device.get("deviceSn"))
            device_alarms = await self._get_device_alarms(device.get("deviceSn"))

            devices_data[device_sn] = {
                "device_info": device,
                "device_index": index,
                "query_device": query_device,
                "energy_flow": energy_flow,
                "battery_info": battery_info,
                "device_alarms": device_alarms,
            }

        # Plant statistics are plant-scoped (they take plantUid); the device
        # identifier only selects which meter answers, so fetch them once.
        primary = devices_data[next(iter(devices_data))] if devices_data else {}
        plant_statistics = await self._get_plant_statistics(
            plant_uid, primary.get("query_device", {})
        )

        return {
            "plant_info": plant_info,
            "plant_details": plant_details,
            "device_list": device_list,
            "battery_list": battery_list,
            "plant_statistics": plant_statistics,
            # Kept for backwards compatibility: the primary inverter's own data.
            "energy_flow": primary.get("energy_flow", {}),
            "battery_info": primary.get("battery_info", {}),
            "device_alarms": primary.get("device_alarms", {}),
            "devices": devices_data,
            "aggregate": self._build_aggregate(devices_data),
        }

    @staticmethod
    def _build_aggregate(devices_data: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """Sum the per-inverter values into plant-wide totals.

        Powers and energies add up across inverters. Directional values are
        recombined from the signed sum so that, for example, one inverter
        importing while another exports reports the net plant flow.
        """
        def _num(source: dict[str, Any], key: str) -> float:
            value = source.get(key)
            if value is None or value == "":
                return 0.0
            try:
                return float(str(value).rstrip("%℃").strip())
            except (TypeError, ValueError):
                return 0.0

        device_agg: dict[str, Any] = {
            key: 0.0
            for key in (
                "powerNow",
                "todayEnergy",
                "monthEnergy",
                "yearEnergy",
                "totalEnergy",
                "todayBatChgEnergy",
                "todayBatDisChgEnergy",
            )
        }
        flow_agg: dict[str, Any] = {
            "sysGridPowerwatt": 0.0,
            "batPower": 0.0,
            "totalLoadPowerwatt": 0.0,
            "pvPower": 0.0,
        }
        battery_agg: dict[str, Any] = {
            key: 0.0
            for key in (
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
        }

        grid_signed = 0.0
        battery_signed = 0.0
        soc_values: list[float] = []
        voltages: list[float] = []
        work_times: list[float] = []
        pv_directions: list[int] = []
        out_directions: list[int] = []
        update_dates: list[str] = []
        online = False
        user_mode = None
        first_online_time = None

        for device_data in devices_data.values():
            device = device_data.get("device_info") or {}
            flow = (device_data.get("energy_flow") or {}).get("data") or {}
            battery = (device_data.get("battery_info") or {}).get("data") or {}

            for key in device_agg:
                device_agg[key] += _num(device, key)

            flow_agg["totalLoadPowerwatt"] += _num(flow, "totalLoadPowerwatt")
            flow_agg["pvPower"] += _num(flow, "pvPower")

            # Grid: negative means exporting (gridDirection == 1).
            grid_power = _num(flow, "sysGridPowerwatt")
            try:
                grid_direction = int(flow.get("gridDirection", 0) or 0)
            except (TypeError, ValueError):
                grid_direction = 0
            grid_signed += -grid_power if grid_direction == 1 else grid_power

            # Battery: negative means charging (batteryDirection == -1).
            bat_power = _num(flow, "batPower")
            try:
                bat_direction = int(flow.get("batteryDirection", 0) or 0)
            except (TypeError, ValueError):
                bat_direction = 0
            battery_signed += -bat_power if bat_direction == -1 else bat_power

            for key in battery_agg:
                battery_agg[key] += _num(battery, key)

            soc = device.get("batEnergyPercent")
            if soc not in (None, ""):
                soc_values.append(_num(device, "batEnergyPercent"))

            voltage = _num(battery, "batVoltage")
            if voltage:
                voltages.append(voltage)

            work_time = _num(battery, "batteryWorkTime")
            if work_time:
                work_times.append(work_time)

            if user_mode is None and battery.get("userModeName"):
                user_mode = battery["userModeName"]

            for values, key in ((pv_directions, "pvDirection"), (out_directions, "outPutDirection")):
                try:
                    values.append(int(flow.get(key, 0) or 0))
                except (TypeError, ValueError):
                    values.append(0)

            if flow.get("updateDate"):
                update_dates.append(flow["updateDate"])
            if battery.get("updateDate"):
                update_dates.append(battery["updateDate"])

            try:
                online = online or bool(int(device.get("runningState", 0) or 0))
            except (TypeError, ValueError):
                pass

            if first_online_time is None and device.get("firstOnlineTime"):
                first_online_time = device["firstOnlineTime"]

        def _direction(values: list[int]) -> int:
            """Collapse per-inverter directions: any active flow wins."""
            for candidate in (1, -1):
                if candidate in values:
                    return candidate
            return 0

        device_agg["runningState"] = 1 if online else 0
        device_agg["batEnergyPercent"] = (
            round(sum(soc_values) / len(soc_values), 1) if soc_values else 0
        )
        device_agg["firstOnlineTime"] = first_online_time or ""
        device_agg["deviceCount"] = len(devices_data)

        flow_agg["sysGridPowerwatt"] = abs(grid_signed)
        flow_agg["gridDirection"] = 1 if grid_signed < 0 else (-1 if grid_signed > 0 else 0)
        flow_agg["batPower"] = abs(battery_signed)
        flow_agg["batteryDirection"] = (
            -1 if battery_signed < 0 else (1 if battery_signed > 0 else 0)
        )
        device_agg["batteryDirection"] = flow_agg["batteryDirection"]
        flow_agg["pvDirection"] = _direction(pv_directions)
        flow_agg["outPutDirection"] = _direction(out_directions)
        flow_agg["updateDate"] = max(update_dates) if update_dates else ""

        # Voltage is a system property, not something to add up.
        battery_agg["batVoltage"] = (
            round(sum(voltages) / len(voltages), 2) if voltages else 0
        )
        # Remaining runtime of the plant is bounded by the shortest bank.
        battery_agg["batteryWorkTime"] = min(work_times) if work_times else 0
        battery_agg["userModeName"] = user_mode or "Unknown"
        battery_agg["updateDate"] = flow_agg["updateDate"]

        return {
            "device": device_agg,
            "energy_flow": flow_agg,
            "battery_info": battery_agg,
        }

    async def _authenticate(self) -> None:
        """Authenticate with the SAJ eSolar API using Elekeeper method."""
        data_to_sign = {
            "appProjectName": "elekeeper",
            "clientDate": datetime.now().strftime("%Y-%m-%d"),
            "lang": "en",
            "timeStamp": int(time.time() * 1000),
            "random": generatkey(32),
            "clientId": "esolar-monitor-admin"
        }

        login_data = {
            "username": self.username,
            "password": encrypt(self.password),
            "rememberMe": "false",
            "loginType": 1,
        }

        signed = calc_signature(data_to_sign)
        data = signed | login_data

        async with self.session.post(
            f"{self.base_url}{ENDPOINTS['login']}",
            data=data,
        ) as resp:
            if resp.status == 401:
                raise ConfigEntryAuthFailed("Invalid authentication")
            if resp.status != 200:
                raise UpdateFailed(f"Login failed with status {resp.status}")

            response_data = await resp.json()

            if "errCode" in response_data and response_data["errCode"] != 0:
                raise ConfigEntryAuthFailed(f"Login failed: {response_data.get('errMsg', 'Unknown error')}")

            if "data" in response_data and "token" in response_data['data']:
                self.auth_token = response_data['data']['tokenHead'] + response_data['data']['token']
            else:
                raise UpdateFailed("Token not found in login response")

    def _base_params(self, **extra: Any) -> dict[str, Any]:
        """Build the common signed-request payload."""
        data = {
            'appProjectName': 'elekeeper',
            'clientDate': datetime.now().strftime("%Y-%m-%d"),
            'lang': 'en',
            'timeStamp': int(time.time() * 1000),
            'random': generatkey(32),
            'clientId': 'esolar-monitor-admin',
        }
        data.update(extra)
        return data

    async def _get(self, endpoint: str, error: str, **params: Any) -> dict[str, Any]:
        """Perform a signed GET request."""
        signed = calc_signature(self._base_params(**params))

        async with self.session.get(
            f"{self.base_url}{ENDPOINTS[endpoint]}",
            params=signed,
            headers={'Authorization': self.auth_token},
        ) as resp:
            if resp.status != 200:
                raise UpdateFailed(f"{error}: {resp.status}")
            return await resp.json()

    async def _get_plant_list(self) -> dict[str, Any]:
        """Get list of plants."""
        return await self._get(
            "plant_list",
            "Failed to get plant list",
            pageNo=1,
            pageSize=500,
        )

    async def _get_plant_details_for_plant(self, plant_uid: str) -> dict[str, Any]:
        """Get plant details for specific plant."""
        return await self._get(
            "plant_detail",
            "Failed to get plant details",
            plantUid=plant_uid,
        )

    async def _get_device_list_for_plant(self, plant_uid: str) -> dict[str, Any]:
        """Get device list for specific plant."""
        return await self._get(
            "device_list",
            "Failed to get device list",
            plantUid=plant_uid,
            pageSize=100,
            pageNo=1,
            searchOfficeIdArr="1",
        )

    async def _get_battery_list_for_plant(self, plant_uid: str) -> dict[str, Any]:
        """Get battery list for specific plant."""
        return await self._get(
            "battery_list",
            "Failed to get battery list",
            plantUid=plant_uid,
            pageSize=100,
            pageNo=1,
            searchOfficeIdArr="1",
        )

    @staticmethod
    def _resolve_query_device(
        plant_details: dict[str, Any], device: dict[str, Any], index: int
    ) -> dict[str, str]:
        """Resolve the API query identifier for one inverter.

        Most inverters (e.g. H1) are queried by their inverter serial
        (``deviceSn``). R5 inverters paired with a SEC-C smart meter report
        ``queryDeviceDataType == 2`` and must be queried by the EMS module
        serial (``emsSn``) instead, otherwise load/grid values return 0.
        See ``elekeeper.prepare_data_for_query`` for the reference logic.
        """
        device_sn = device.get("deviceSn")

        # Prefer the module the device itself reports; fall back to the plant's
        # module list, paired by position so each inverter gets its own module.
        module_sn_list = plant_details.get("moduleSnList") or []
        ems_sn = device.get("moduleSn")
        if not ems_sn and index < len(module_sn_list):
            ems_sn = module_sn_list[index]
        if not ems_sn and module_sn_list:
            ems_sn = module_sn_list[0]

        # Only R5 + SEC-C (queryDeviceDataType == 2) queries by emsSn.
        # Everything else keeps the existing deviceSn behaviour.
        if plant_details.get("queryDeviceDataType", 1) == 2 and ems_sn:
            return {"emsSn": ems_sn}
        if device_sn:
            return {"deviceSn": device_sn}
        if ems_sn:
            return {"emsSn": ems_sn}

        raise UpdateFailed(
            f"No device or EMS module found for device index {index}"
        )

    async def _get_plant_statistics(
        self, plant_uid: str, query_device: dict[str, str]
    ) -> dict[str, Any]:
        """Get plant statistics for specific plant."""
        return await self._get(
            "plant_statistics",
            "Failed to get plant statistics",
            plantUid=plant_uid,
            **query_device,
        )

    async def _get_energy_flow(
        self, plant_uid: str, query_device: dict[str, str]
    ) -> dict[str, Any]:
        """Get energy flow for one inverter of a plant."""
        return await self._get(
            "energy_flow",
            "Failed to get energy flow",
            plantUid=plant_uid,
            **query_device,
        )

    async def _get_battery_info(self, device_sn: str | None) -> dict[str, Any]:
        """Get battery system info for one inverter."""
        if not device_sn:
            return {}

        return await self._get(
            "battery_info",
            "Failed to get battery info",
            deviceSn=device_sn,
        )

    async def _get_device_alarms(self, device_sn: str | None) -> dict[str, Any]:
        """Get device alarms for one inverter."""
        if not device_sn:
            return {}

        # Prepare form data for POST request
        form_data = self._base_params(
            deviceSn=device_sn,
            orderByIndex="1",
            pageNo="1",
            pageSize="10",
            alarmCommonState="1",
            searchOfficeIdArr="1",
        )
        form_data['timeStamp'] = str(form_data['timeStamp'])

        signed = calc_signature(form_data)

        async with self.session.post(
            f"{self.base_url}{ENDPOINTS['device_alarms']}",
            data=signed,
            headers={'Authorization': self.auth_token},
        ) as resp:
            if resp.status != 200:
                raise UpdateFailed(f"Failed to get device alarms: {resp.status}")
            return await resp.json()
