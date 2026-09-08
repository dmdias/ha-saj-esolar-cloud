# 🌞 SAJ eSolar Cloud (DMDIAS build)

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/custom-components/hacs)
[![License: GPLv3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)

Home Assistant integration for the SAJ eSolar Cloud (Elekeeper) API, focused on
H1 inverters with battery storage.

This is a fork of [elboletaire/ha-saj-esolar-cloud](https://github.com/elboletaire/ha-saj-esolar-cloud)
with support for plants that group several inverters.

## Features

- 🎛️ **Multi-plant support** — monitor several installations from one integration
- 🔌 **Multi-inverter plants** — values are summed at plant level, and every
  inverter is also exposed as its own device (power, generation, online state,
  alarm status)
- 🔋 **Per-battery devices** — SOC, SOH, temperature, voltage, current, power
- ⚡ **Power, voltage and energy sensors** — today / month / year / total for
  generation, consumption, grid import/export and battery charge/discharge
- 🌍 **Multi-region** — EU, India and China
- 🌐 **English and Catalan** translations
- 🔄 Updates every 5 minutes, ready for the Energy Dashboard

## Setup

Configuration is done entirely through the UI:

1. **Settings** → **Devices & Services** → **+ Add Integration**
2. Search for **SAJ eSolar Cloud (DMDIAS build)**
3. Enter your eSolar Portal username, password and region
4. Select the plants you want to monitor

Use **Configure** on the integration afterwards to add or remove plants.

## Troubleshooting

Enable debug logging to see the raw fields your inverter reports:

```yaml
logger:
  default: info
  logs:
    custom_components.saj_esolar_cloud: debug
```

Alternatively run `python3 scripts/dump_device_fields.py --username YOU --region eu`.

Full documentation, sensor list and troubleshooting:
[github.com/dmdias/ha-saj-esolar-cloud](https://github.com/dmdias/ha-saj-esolar-cloud)

## Credits

Maintained in this fork by [@dmdias](https://github.com/dmdias).
Original integration by [@elboletaire](https://github.com/elboletaire), which
originated from [SAJeSolar](https://github.com/djansen1987/SAJeSolar) by
[@djansen1987](https://github.com/djansen1987).
