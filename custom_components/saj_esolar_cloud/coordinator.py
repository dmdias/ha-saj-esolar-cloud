"""DataUpdateCoordinator for SAJ eSolar integration."""
from datetime import datetime, timedelta
import logging
from typing import Any
import time
import json
import hashlib
import os

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
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

                # Get all data for this plant
                plant_details = await self._get_plant_details_for_plant(plant_uid)
                device_list = await self._get_device_list_for_plant(plant_uid)
                battery_list = await self._get_battery_list_for_plant(plant_uid)
                plant_statistics = await self._get_plant_statistics_for_plant(plant_uid)
                energy_flow = await self._get_energy_flow_for_plant(plant_uid)

                # Get battery system info for this plant
                battery_info = await self._get_battery_info_for_plant(plant_uid)

                plants_data[plant_uid] = {
                    "plant_info": plant_info,
                    "plant_details": plant_details,
                    "device_list": device_list,
                    "battery_list": battery_list,
                    "plant_statistics": plant_statistics,
                    "energy_flow": energy_flow,
                    "battery_info": battery_info,
                }

            return plants_data

        except aiohttp.ClientError as err:
            raise UpdateFailed(f"Error communicating with API: {err}")
        except Exception as err:
            raise UpdateFailed(f"Error fetching data: {err}")

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

    async def _get_plant_list(self) -> dict[str, Any]:
        """Get list of plants."""
        data = {
            "pageNo": 1,
            "pageSize": 500,
            'appProjectName': 'elekeeper',
            'clientDate': datetime.now().strftime("%Y-%m-%d"),
            'lang': 'en',
            'timeStamp': int(time.time() * 1000),
            'random': generatkey(32),
            'clientId': 'esolar-monitor-admin',
        }

        signed = calc_signature(data)

        async with self.session.get(
            f"{self.base_url}{ENDPOINTS['plant_list']}",
            params=signed,
            headers={'Authorization': self.auth_token},
        ) as resp:
            if resp.status != 200:
                raise UpdateFailed(f"Failed to get plant list: {resp.status}")
            return await resp.json()

    async def _get_plant_details_for_plant(self, plant_uid: str) -> dict[str, Any]:
        """Get plant details for specific plant."""
        data = {
            "plantUid": plant_uid,
            'appProjectName': 'elekeeper',
            'clientDate': datetime.now().strftime("%Y-%m-%d"),
            'lang': 'en',
            'timeStamp': int(time.time() * 1000),
            'random': generatkey(32),
            'clientId': 'esolar-monitor-admin',
        }

        signed = calc_signature(data)

        async with self.session.get(
            f"{self.base_url}{ENDPOINTS['plant_detail']}",
            params=signed,
            headers={'Authorization': self.auth_token},
        ) as resp:
            if resp.status != 200:
                raise UpdateFailed(f"Failed to get plant details: {resp.status}")
            return await resp.json()

    async def _get_device_list_for_plant(self, plant_uid: str) -> dict[str, Any]:
        """Get device list for specific plant."""
        data = {
            "plantUid": plant_uid,
            "pageSize": 100,
            "pageNo": 1,
            "searchOfficeIdArr": "1",
            'appProjectName': 'elekeeper',
            'clientDate': datetime.now().strftime("%Y-%m-%d"),
            'lang': 'en',
            'timeStamp': int(time.time() * 1000),
            'random': generatkey(32),
            'clientId': 'esolar-monitor-admin',
        }

        signed = calc_signature(data)

        async with self.session.get(
            f"{self.base_url}{ENDPOINTS['device_list']}",
            params=signed,
            headers={'Authorization': self.auth_token},
        ) as resp:
            if resp.status != 200:
                raise UpdateFailed(f"Failed to get device list: {resp.status}")
            return await resp.json()

    async def _get_battery_list_for_plant(self, plant_uid: str) -> dict[str, Any]:
        """Get battery list for specific plant."""
        data = {
            "plantUid": plant_uid,
            "pageSize": 100,
            "pageNo": 1,
            "searchOfficeIdArr": "1",
            'appProjectName': 'elekeeper',
            'clientDate': datetime.now().strftime("%Y-%m-%d"),
            'lang': 'en',
            'timeStamp': int(time.time() * 1000),
            'random': generatkey(32),
            'clientId': 'esolar-monitor-admin',
        }

        signed = calc_signature(data)

        async with self.session.get(
            f"{self.base_url}{ENDPOINTS['battery_list']}",
            params=signed,
            headers={'Authorization': self.auth_token},
        ) as resp:
            if resp.status != 200:
                raise UpdateFailed(f"Failed to get battery list: {resp.status}")
            return await resp.json()

    async def _get_plant_statistics_for_plant(self, plant_uid: str) -> dict[str, Any]:
        """Get plant statistics for specific plant."""
        # Get device list first to get deviceSn
        device_list = await self._get_device_list_for_plant(plant_uid)
        device_sn = None
        if device_list.get("data", {}).get("list"):
            device_sn = device_list["data"]["list"][0]["deviceSn"]

        if not device_sn:
            raise UpdateFailed(f"No device found for plant {plant_uid}")

        data = {
            "plantUid": plant_uid,
            "deviceSn": device_sn,
            'appProjectName': 'elekeeper',
            'clientDate': datetime.now().strftime("%Y-%m-%d"),
            'lang': 'en',
            'timeStamp': int(time.time() * 1000),
            'random': generatkey(32),
            'clientId': 'esolar-monitor-admin',
        }

        signed = calc_signature(data)

        async with self.session.get(
            f"{self.base_url}{ENDPOINTS['plant_statistics']}",
            params=signed,
            headers={'Authorization': self.auth_token},
        ) as resp:
            if resp.status != 200:
                raise UpdateFailed(f"Failed to get plant statistics: {resp.status}")
            return await resp.json()

    async def _get_energy_flow_for_plant(self, plant_uid: str) -> dict[str, Any]:
        """Get energy flow for specific plant."""
        # Get device list first to get deviceSn
        device_list = await self._get_device_list_for_plant(plant_uid)
        device_sn = None
        if device_list.get("data", {}).get("list"):
            device_sn = device_list["data"]["list"][0]["deviceSn"]

        if not device_sn:
            raise UpdateFailed(f"No device found for plant {plant_uid}")

        data = {
            "plantUid": plant_uid,
            "deviceSn": device_sn,
            'appProjectName': 'elekeeper',
            'clientDate': datetime.now().strftime("%Y-%m-%d"),
            'lang': 'en',
            'timeStamp': int(time.time() * 1000),
            'random': generatkey(32),
            'clientId': 'esolar-monitor-admin',
        }

        signed = calc_signature(data)

        async with self.session.get(
            f"{self.base_url}{ENDPOINTS['energy_flow']}",
            params=signed,
            headers={'Authorization': self.auth_token},
        ) as resp:
            if resp.status != 200:
                raise UpdateFailed(f"Failed to get energy flow: {resp.status}")
            return await resp.json()

    async def _get_battery_info_for_plant(self, plant_uid: str) -> dict[str, Any]:
        """Get battery system info for specific plant."""
        # Get device list first to get deviceSn
        device_list = await self._get_device_list_for_plant(plant_uid)
        device_sn = None
        if device_list.get("data", {}).get("list"):
            device_sn = device_list["data"]["list"][0]["deviceSn"]

        if not device_sn:
            raise UpdateFailed(f"No device found for plant {plant_uid}")

        data = {
            "deviceSn": device_sn,
            'appProjectName': 'elekeeper',
            'clientDate': datetime.now().strftime("%Y-%m-%d"),
            'lang': 'en',
            'timeStamp': int(time.time() * 1000),
            'random': generatkey(32),
            'clientId': 'esolar-monitor-admin',
        }

        signed = calc_signature(data)

        async with self.session.get(
            f"{self.base_url}{ENDPOINTS['battery_info']}",
            params=signed,
            headers={'Authorization': self.auth_token},
        ) as resp:
            if resp.status != 200:
                raise UpdateFailed(f"Failed to get battery info: {resp.status}")
            return await resp.json()

    async def _get_plant_statistics(self, plant_data: dict[str, Any]) -> dict[str, Any]:
        """Get plant statistics data."""
        plant = plant_data["data"]["list"][0]
        plant_uid = plant["plantUid"]

        data = {
            "plantUid": plant_uid,
            'appProjectName': 'elekeeper',
            'clientDate': datetime.now().strftime("%Y-%m-%d"),
            'lang': 'en',
            'timeStamp': int(time.time() * 1000),
            'random': generatkey(32),
            'clientId': 'esolar-monitor-admin',
        }

        signed = calc_signature(data)

        async with self.session.get(
            f"{self.base_url}{ENDPOINTS['plant_statistics']}",
            params=signed,
            headers={'Authorization': self.auth_token},
        ) as resp:
            if resp.status != 200:
                raise UpdateFailed(f"Failed to get plant statistics: {resp.status}")
            return await resp.json()

    async def _get_energy_flow(self, plant_data: dict[str, Any]) -> dict[str, Any]:
        """Get energy flow data."""
        plant = plant_data["data"]["list"][0]
        plant_uid = plant["plantUid"]

        data = {
            "plantUid": plant_uid,
            'appProjectName': 'elekeeper',
            'clientDate': datetime.now().strftime("%Y-%m-%d"),
            'lang': 'en',
            'timeStamp': int(time.time() * 1000),
            'random': generatkey(32),
            'clientId': 'esolar-monitor-admin',
        }

        signed = calc_signature(data)

        async with self.session.get(
            f"{self.base_url}{ENDPOINTS['energy_flow']}",
            params=signed,
            headers={'Authorization': self.auth_token},
        ) as resp:
            if resp.status != 200:
                raise UpdateFailed(f"Failed to get energy flow: {resp.status}")
            return await resp.json()

    async def _get_plant_details(self, plant_data: dict[str, Any]) -> dict[str, Any]:
        """Get plant details."""
        if not plant_data.get("data", {}).get("list"):
            raise UpdateFailed("No plants found")

        plant = plant_data["data"]["list"][0]  # Use first plant for now
        plant_uid = plant["plantUid"]

        data = {
            "plantUid": plant_uid,
            'appProjectName': 'elekeeper',
            'clientDate': datetime.now().strftime("%Y-%m-%d"),
            'lang': 'en',
            'timeStamp': int(time.time() * 1000),
            'random': generatkey(32),
            'clientId': 'esolar-monitor-admin',
        }

        signed = calc_signature(data)

        async with self.session.get(
            f"{self.base_url}{ENDPOINTS['plant_detail']}",
            params=signed,
            headers={'Authorization': self.auth_token},
        ) as resp:
            if resp.status != 200:
                raise UpdateFailed(f"Failed to get plant details: {resp.status}")
            return await resp.json()

    async def _get_device_list(self, plant_data: dict[str, Any]) -> dict[str, Any]:
        """Get device list."""
        plant = plant_data["data"]["list"][0]
        plant_uid = plant["plantUid"]

        data = {
            "plantUid": plant_uid,
            "pageSize": 100,
            "pageNo": 1,
            "searchOfficeIdArr": "1",
            'appProjectName': 'elekeeper',
            'clientDate': datetime.now().strftime("%Y-%m-%d"),
            'lang': 'en',
            'timeStamp': int(time.time() * 1000),
            'random': generatkey(32),
            'clientId': 'esolar-monitor-admin',
        }

        signed = calc_signature(data)

        async with self.session.get(
            f"{self.base_url}{ENDPOINTS['device_list']}",
            params=signed,
            headers={'Authorization': self.auth_token},
        ) as resp:
            if resp.status != 200:
                raise UpdateFailed(f"Failed to get device list: {resp.status}")
            return await resp.json()

    async def _get_battery_list(self, plant_data: dict[str, Any]) -> dict[str, Any]:
        """Get battery list."""
        plant = plant_data["data"]["list"][0]
        plant_uid = plant["plantUid"]

        data = {
            "plantUid": plant_uid,
            "pageSize": 100,
            "pageNo": 1,
            "searchOfficeIdArr": "1",
            'appProjectName': 'elekeeper',
            'clientDate': datetime.now().strftime("%Y-%m-%d"),
            'lang': 'en',
            'timeStamp': int(time.time() * 1000),
            'random': generatkey(32),
            'clientId': 'esolar-monitor-admin',
        }

        signed = calc_signature(data)

        async with self.session.get(
            f"{self.base_url}{ENDPOINTS['battery_list']}",
            params=signed,
            headers={'Authorization': self.auth_token},
        ) as resp:
            if resp.status != 200:
                raise UpdateFailed(f"Failed to get battery list: {resp.status}")
            return await resp.json()
