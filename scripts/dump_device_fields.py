#!/usr/bin/env python3
"""Print the fields each inverter reports, to confirm sensor field mappings.

SAJ spells the per-inverter measurement fields differently across inverter
families and firmware versions, so the voltage sensors match a list of
candidate names (``DEVICE_DETAIL_FIELDS`` in ``const.py``). Run this against
your own account to see the real names and, if a sensor stays unavailable,
report the output so the mapping can be corrected.

No credentials are stored or sent anywhere except the SAJ API.

Usage:
    pip install aiohttp pycryptodome
    python3 scripts/dump_device_fields.py --username USER --region eu
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import sys
from datetime import datetime
from time import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "custom_components"))

import aiohttp  # noqa: E402

from saj_esolar_cloud.const import DEVICE_DETAIL_FIELDS, ENDPOINTS, REGIONS  # noqa: E402
from saj_esolar_cloud.elekeeper import calc_signature, encrypt, generatkey  # noqa: E402


def signed(**extra):
    """Build a signed request payload."""
    return calc_signature({
        "appProjectName": "elekeeper",
        "clientDate": datetime.now().strftime("%Y-%m-%d"),
        "lang": "en",
        "timeStamp": int(time() * 1000),
        "random": generatkey(32),
        "clientId": "esolar-monitor-admin",
        **extra,
    })


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", help="prompted for if omitted")
    parser.add_argument("--region", default="eu", choices=sorted(REGIONS))
    parser.add_argument("--json", action="store_true", help="dump full responses")
    args = parser.parse_args()

    password = args.password or getpass.getpass("eSolar password: ")
    base = REGIONS[args.region]

    async with aiohttp.ClientSession() as session:
        async def get(endpoint, **params):
            async with session.get(
                f"{base}{ENDPOINTS[endpoint]}", params=signed(**params), headers=headers
            ) as resp:
                resp.raise_for_status()
                return await resp.json()

        # Login
        async with session.post(
            f"{base}{ENDPOINTS['login']}",
            data=signed() | {
                "username": args.username,
                "password": encrypt(password),
                "rememberMe": "false",
                "loginType": 1,
            },
        ) as resp:
            resp.raise_for_status()
            body = await resp.json()

        if body.get("errCode"):
            print(f"Login failed: {body.get('errMsg')}", file=sys.stderr)
            return 1
        token = body["data"]["tokenHead"] + body["data"]["token"]
        headers = {"Authorization": token}

        plants = (await get("plant_list", pageNo=1, pageSize=500))["data"]["list"]
        print(f"Found {len(plants)} plant(s)\n")

        for plant in plants:
            uid, name = plant["plantUid"], plant.get("plantName", "?")
            devices = (await get(
                "device_list", plantUid=uid, pageNo=1, pageSize=100, searchOfficeIdArr="1"
            ))["data"]["list"]
            print(f"=== Plant {name} ({uid}) - {len(devices)} inverter(s) ===")

            for device in devices:
                sn = device.get("deviceSn")
                print(f"\n--- Inverter {sn} ({device.get('deviceModel', '?')}) ---")
                print(f"    powerNow={device.get('powerNow')} todayEnergy={device.get('todayEnergy')}")

                detail = await get("device_info", deviceSn=sn)
                data = detail.get("data") or {}
                if not data:
                    print("    device_info returned no data")
                    continue

                if args.json:
                    print(json.dumps(data, indent=2, ensure_ascii=False))
                    continue

                # Anything that looks like a voltage is what we are after
                interesting = {
                    key: value for key, value in data.items()
                    if any(word in key.lower() for word in ("volt", "pv", "grid", "inv", "ac", "dc"))
                }
                print("    candidate fields:")
                for key, value in sorted(interesting.items()):
                    print(f"      {key} = {value}")

                print("    resolved by the integration:")
                normalised = {"".join(c for c in k.lower() if c.isalnum()): k for k in data}
                for sensor_key, candidates in DEVICE_DETAIL_FIELDS.items():
                    hit = next(
                        (normalised[n] for c in candidates
                         if (n := "".join(ch for ch in c.lower() if ch.isalnum())) in normalised),
                        None,
                    )
                    status = f"{hit} = {data[hit]}" if hit else "NO MATCH"
                    print(f"      {sensor_key}: {status}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
