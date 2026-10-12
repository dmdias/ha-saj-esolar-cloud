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

from saj_esolar_cloud.const import DEVICE_DETAIL_FIELDS, ENDPOINTS, ENDPOINTS_V2, REGIONS  # noqa: E402
from saj_esolar_cloud.elekeeper import calc_signature, encrypt, generatkey, sign_v2_request  # noqa: E402


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
    base_v2 = base.replace("/api/v1", "/api/v2")

    async with aiohttp.ClientSession() as session:
        async def get(endpoint, **params):
            async with session.get(
                f"{base}{ENDPOINTS[endpoint]}", params=signed(**params), headers=headers
            ) as resp:
                resp.raise_for_status()
                return await resp.json()

        async def post_v2(endpoint, token=None, **body):
            sign_headers, raw = sign_v2_request(body, token=token)
            async with session.post(
                f"{base_v2}{ENDPOINTS_V2[endpoint]}", data=raw, headers=sign_headers
            ) as resp:
                resp.raise_for_status()
                return json.loads(await resp.text())

        # Login (the v2 token works for both v1 and v2 endpoints)
        body = await post_v2(
            "login",
            username=args.username,
            password=encrypt(password),
            rememberMe=False,
            loginType=1,
        )

        if body.get("errCode"):
            print(f"Login failed: {body.get('errMsg')}", file=sys.stderr)
            return 1
        token = (body["data"].get("tokenHead") or "Bearer ") + body["data"]["token"]
        headers = {"Authorization": token}

        plants = (await post_v2("plant_list", token, pageNo=1, pageSize=500))["data"]["list"]
        print(f"Found {len(plants)} plant(s)\n")

        for plant in plants:
            uid, name = plant["plantUid"], plant.get("plantName", "?")
            tree = (await post_v2("device_tree", token, plantUid=uid)).get("data") or []
            devices = [d for d in tree if d.get("deviceType") == 1]
            print(f"=== Plant {name} ({uid}) - {len(devices)} inverter(s) ===")
            # Serials the energy flow is queried with: one per inverter lets
            # the integration sum them, a single shared one is plant-wide
            capability = (await post_v2("plant_capability", token, plantUid=uid)).get("data") or {}
            print(f"    snList: {capability.get('snList')}")

            for device in devices:
                sn = device.get("deviceSn")
                print(f"\n--- Inverter {sn} ({device.get('deviceModel', '?')}) ---")

                # The integration merges the v1 device info over the latest
                # v2 raw data record, so dump the same combination
                today = datetime.now().strftime("%Y-%m-%d")
                raw = await post_v2(
                    "raw_data", token, deviceSn=sn, deviceType=1, timeStr=today,
                    startTime=f"{today} 00:00:00", endTime=f"{today} 23:59:59", pageNo=1, pageSize=1,
                )
                records = (raw.get("data") or {}).get("list") or []
                data = dict(records[0]) if records else {}
                try:
                    data.update((await get("device_info", deviceSn=sn)).get("data") or {})
                except aiohttp.ClientError as err:
                    print(f"    device_info (api v1) failed: {err}")
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
