/*
 * SAJ Elekeeper card for Home Assistant.
 *
 * A dashboard in the style of the SAJ Elekeeper portal (overview with the
 * energy flow, device list, inverter/battery detail, curve analysis and
 * energy comparison), fed by the saj_esolar_cloud integration sensors.
 * No dependencies: charts are drawn as inline SVG from the Home Assistant
 * history and long-term statistics.
 *
 *   type: custom:saj-elekeeper-card
 *   plant_uid: 87QA3F...       # optional, first plant by default
 *   view: all                  # all | overview | device | analysis | comparison
 *   pv_capacity: 4.5           # optional, kWp shown under the PV power
 *   weather: weather.home      # optional, temperature shown on the overview
 */

const CARD_VERSION = "1.0.3";
const DOMAIN = "saj_esolar_cloud";

// Sensor keys that identify each kind of device in the entity registry
const PLANT_ONLY_KEY = "totalConsumpElec";
const INVERTER_KEY = "nowPower";
const BATTERY_KEY = "batSoc";

const COLORS = {
  pv: "#e5c158",
  load: "#9a7ff0",
  grid: "#5ab8f5",
  battery: "#6fe3b0",
  soc: "#e0609e",
  import: "#c75282",
  export: "#77d0f8",
  charge: "#7ce8b5",
  discharge: "#728fcf",
  accent: "#5fd3a8",
  orange: "#e9a33d",
};

const TABS = [
  ["overview", "Overview"],
  ["device", "Device"],
  ["analysis", "Analysis"],
  ["comparison", "Comparison"],
];

const ANALYSIS_MODES = [
  ["day", "Day"],
  ["week", "Week"],
  ["month", "Month"],
  ["year", "Year"],
  ["total", "Total"],
];

// Energy counters used by the bar charts and summaries (total_increasing)
const ENERGY_SERIES = [
  ["totalElectricity", "PV Energy", COLORS.pv],
  ["totalConsumpElec", "Load consumption", COLORS.load],
  ["totalBuyElec", "Import Energy", COLORS.import],
  ["totalSellElec", "Export Energy", COLORS.export],
  ["totalBatteryCharge", "Charge", COLORS.charge],
  ["totalBatteryDischarge", "Discharge", COLORS.discharge],
];

const COMPARISON_METRICS = ENERGY_SERIES.map(([key, name]) => [key, name]);
const YEAR_COLORS = ["#b6e095", "#6a8af5", "#e5c158", "#e0609e", "#77d0f8"];

/* ---------- small helpers ---------- */

const esc = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

const isNum = (v) => v !== null && v !== undefined && v !== "" && !Number.isNaN(Number(v));

function fmtNumber(value, digits = 2) {
  if (!isNum(value)) return "--";
  const n = Number(value);
  return n.toLocaleString("en-US", { minimumFractionDigits: 0, maximumFractionDigits: digits });
}

/** Power in W -> [value, unit], switching to kW like the portal. */
function fmtPower(watts) {
  if (!isNum(watts)) return ["--", "W"];
  const w = Math.abs(Number(watts));
  return w >= 1000 ? [fmtNumber(w / 1000, 2), "kW"] : [fmtNumber(w, 0), "W"];
}

/** Energy in kWh -> [value, unit], switching to MWh like the portal. */
function fmtEnergy(kwh, digits = 2) {
  if (!isNum(kwh)) return ["--", "kWh"];
  const e = Number(kwh);
  return Math.abs(e) >= 1000 ? [fmtNumber(e / 1000, 2), "MWh"] : [fmtNumber(e, digits), "kWh"];
}

const pad = (n) => String(n).padStart(2, "0");
const isoDay = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const startOfDay = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate());

function fmtDuration(hours) {
  if (!isNum(hours)) return null;
  const total = Math.round(Number(hours) * 60);
  const h = Math.floor(total / 60);
  const m = total % 60;
  return h ? `${h} hours ${m} minutes` : `${m} minutes`;
}

/** Group entity registry entries of the integration by plant and device. */
function buildEntityMap(entries, wantedPlant) {
  const prefix = `${DOMAIN}_`;
  const ours = entries
    .filter((e) => e.platform === DOMAIN && e.unique_id && e.unique_id.startsWith(prefix) && !e.disabled_by)
    .map((e) => ({ ...e, rest: e.unique_id.slice(prefix.length) }));

  const plants = ours
    .filter((e) => e.rest.endsWith(`_${PLANT_ONLY_KEY}`))
    .map((e) => e.rest.slice(0, -PLANT_ONLY_KEY.length - 1));
  if (!plants.length) return null;
  const plant = wantedPlant && plants.includes(wantedPlant) ? wantedPlant : plants[0];

  const map = { plant, plants, entities: {}, inverters: {}, batteries: {} };
  const scoped = ours.filter((e) => e.rest.startsWith(`${plant}_`)).map((e) => ({ ...e, rest: e.rest.slice(plant.length + 1) }));

  // Plant-level sensors have no "_" left (sensor keys are camelCase).
  const nested = [];
  for (const e of scoped) {
    if (!e.rest.includes("_")) map.entities[e.rest] = e.entity_id;
    else nested.push(e);
  }

  // "<serial>_<key>": an inverter when it has nowPower, a battery when batSoc
  for (const e of nested) {
    const cut = e.rest.lastIndexOf("_");
    const sn = e.rest.slice(0, cut);
    const key = e.rest.slice(cut + 1);
    (map._bySerial ??= {});
    (map._bySerial[sn] ??= { keys: {}, device_id: e.device_id }).keys[key] = e.entity_id;
  }
  for (const [sn, info] of Object.entries(map._bySerial || {})) {
    if (info.keys[BATTERY_KEY]) map.batteries[sn] = info;
    else if (info.keys[INVERTER_KEY]) map.inverters[sn] = info;
  }
  delete map._bySerial;
  map.deviceId = entries.find((e) => e.entity_id === map.entities[INVERTER_KEY])?.device_id;
  return map;
}

/* ---------- SVG charts ---------- */

function niceScale(min, max, ticks = 5) {
  if (min === max) {
    max = min + 1;
  }
  const span = max - min;
  const step0 = span / ticks;
  const mag = Math.pow(10, Math.floor(Math.log10(step0)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= step0) || step0;
  const lo = Math.floor(min / step) * step;
  const hi = Math.ceil(max / step) * step;
  const values = [];
  for (let v = lo; v <= hi + step / 2; v += step) values.push(Math.round(v * 1e6) / 1e6);
  return { lo, hi, values };
}

const CH = { w: 1000, h: 330, l: 58, r: 52, t: 30, b: 40 };

/** Line chart over a time range. series: {name,color,points:[[ms,v]],right?,area?} */
function lineChartSvg(id, series, start, end, leftUnit, rightUnit) {
  const left = series.filter((s) => !s.right);
  const right = series.filter((s) => s.right);
  const vals = (list) => list.flatMap((s) => s.points.map((p) => p[1]));
  const lv = vals(left);
  const ls = niceScale(Math.min(0, ...(lv.length ? lv : [0])), Math.max(1, ...(lv.length ? lv : [1])));
  const rs = { lo: 0, hi: 100, values: [0, 20, 40, 60, 80, 100] };
  const pw = CH.w - CH.l - CH.r;
  const ph = CH.h - CH.t - CH.b;
  const x = (t) => CH.l + ((t - start) / (end - start)) * pw;
  const yl = (v) => CH.t + ph - ((v - ls.lo) / (ls.hi - ls.lo)) * ph;
  const yr = (v) => CH.t + ph - ((v - rs.lo) / (rs.hi - rs.lo)) * ph;

  let svg = `<svg class="chart" viewBox="0 0 ${CH.w} ${CH.h}" data-chart="${id}">`;
  svg += `<text x="${CH.l - 10}" y="16" class="axis-unit" text-anchor="end">${esc(leftUnit)}</text>`;
  if (right.length) svg += `<text x="${CH.w - CH.r + 10}" y="16" class="axis-unit">${esc(rightUnit)}</text>`;
  for (const v of ls.values) {
    svg += `<line x1="${CH.l}" x2="${CH.w - CH.r}" y1="${yl(v)}" y2="${yl(v)}" class="${v === 0 ? "zero" : "grid"}"/>`;
    svg += `<text x="${CH.l - 8}" y="${yl(v) + 4}" class="tick" text-anchor="end">${fmtNumber(v, 1)}</text>`;
  }
  if (right.length) {
    for (const v of rs.values) svg += `<text x="${CH.w - CH.r + 8}" y="${yr(v) + 4}" class="tick">${v}</text>`;
  }
  // Hour labels
  const hours = (end - start) / 3600000;
  const every = hours > 12 ? 2 : 1;
  for (let h = 0; h <= hours; h += every) {
    const t = start + h * 3600000;
    svg += `<text x="${x(t)}" y="${CH.h - 14}" class="tick" text-anchor="middle">${pad(new Date(t).getHours())}:00</text>`;
  }
  for (const s of series) {
    if (!s.points.length) continue;
    const y = s.right ? yr : yl;
    const d = s.points.map((p, i) => `${i ? "L" : "M"}${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join("");
    if (s.area) {
      const base = s.right ? yr(0) : yl(Math.max(ls.lo, 0));
      const first = s.points[0];
      const last = s.points[s.points.length - 1];
      svg += `<path d="${d}L${x(last[0]).toFixed(1)},${base}L${x(first[0]).toFixed(1)},${base}Z" fill="${s.color}" opacity="0.08"/>`;
    }
    svg += `<path d="${d}" fill="none" stroke="${s.color}" stroke-width="2" stroke-linejoin="round"/>`;
  }
  svg += `<line class="cursor" x1="0" x2="0" y1="${CH.t}" y2="${CH.t + ph}" visibility="hidden"/>`;
  svg += `<rect class="hit" x="${CH.l}" y="${CH.t}" width="${pw}" height="${ph}" fill="transparent"/>`;
  svg += "</svg>";
  return svg;
}

/** Grouped bar chart. series: {name,color,values:[]} aligned with categories */
function barChartSvg(id, categories, series, unit) {
  const all = series.flatMap((s) => s.values.filter(isNum).map(Number));
  const sc = niceScale(0, Math.max(1, ...all));
  const pw = CH.w - CH.l - CH.r;
  const ph = CH.h - CH.t - CH.b;
  const y = (v) => CH.t + ph - ((v - sc.lo) / (sc.hi - sc.lo)) * ph;
  const slot = pw / Math.max(categories.length, 1);
  const groupW = Math.min(slot * 0.8, series.length * 34);
  const barW = groupW / Math.max(series.length, 1);

  let svg = `<svg class="chart" viewBox="0 0 ${CH.w} ${CH.h}" data-chart="${id}">`;
  svg += `<text x="${CH.l - 10}" y="16" class="axis-unit" text-anchor="end">${esc(unit)}</text>`;
  for (const v of sc.values) {
    svg += `<line x1="${CH.l}" x2="${CH.w - CH.r}" y1="${y(v)}" y2="${y(v)}" class="${v === 0 ? "zero" : "grid"}"/>`;
    svg += `<text x="${CH.l - 8}" y="${y(v) + 4}" class="tick" text-anchor="end">${fmtNumber(v, 1)}</text>`;
  }
  categories.forEach((cat, i) => {
    const cx = CH.l + slot * i + slot / 2;
    svg += `<text x="${cx}" y="${CH.h - 14}" class="tick" text-anchor="middle">${esc(cat)}</text>`;
    series.forEach((s, j) => {
      const v = s.values[i];
      if (!isNum(v) || Number(v) <= 0) return;
      const bx = cx - groupW / 2 + j * barW + 1;
      svg += `<rect x="${bx.toFixed(1)}" y="${y(v).toFixed(1)}" width="${Math.max(barW - 2, 1).toFixed(1)}" height="${(y(0) - y(v)).toFixed(1)}" fill="${s.color}" rx="1.5"/>`;
    });
    svg += `<rect class="hit-bar" data-index="${i}" x="${CH.l + slot * i}" y="${CH.t}" width="${slot}" height="${ph}" fill="transparent"/>`;
  });
  svg += "</svg>";
  return svg;
}

const legend = (series) =>
  `<div class="legend">${series.map((s) => `<span><i style="background:${s.color}"></i>${esc(s.name)}</span>`).join("")}</div>`;

/* ---------- the card ---------- */

class SajElekeeperCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._tab = "overview";
    this._detail = "inverter";
    this._analysis = { mode: "day", date: startOfDay(new Date()) };
    this._comparison = { metric: "totalElectricity", mode: "monthly" };
    this._cache = {};
    this._charts = {};
    this._signature = "";
  }

  static getStubConfig() {
    return { view: "all" };
  }

  setConfig(config) {
    this._config = { view: "all", ...config };
    if (this._config.view !== "all") this._tab = this._config.view;
    this._map = undefined;
    this._render();
  }

  getCardSize() {
    return 14;
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first || this._map === undefined) this._discover();
    const sig = this._stateSignature();
    if (sig !== this._signature) {
      this._signature = sig;
      this._render();
    }
  }

  connectedCallback() {
    this._resize = new ResizeObserver(() => {
      const narrow = this.clientWidth < 900;
      if (narrow !== this._narrow) {
        this._narrow = narrow;
        this.shadowRoot.host.classList.toggle("narrow", narrow);
      }
    });
    this._resize.observe(this);
  }

  disconnectedCallback() {
    this._resize?.disconnect();
  }

  async _discover() {
    if (this._discovering || !this._hass) return;
    this._discovering = true;
    try {
      const entries = await this._hass.callWS({ type: "config/entity_registry/list" });
      this._map = buildEntityMap(entries, this._config?.plant_uid) || null;
      if (this._map) {
        // Drop leftover devices (e.g. a replaced inverter) whose sensors are all unavailable
        for (const group of [this._map.inverters, this._map.batteries]) {
          for (const [sn, info] of Object.entries(group)) {
            const alive = Object.values(info.keys).some((id) => {
              const st = this._hass.states[id]?.state;
              return st !== undefined && st !== "unavailable";
            });
            if (!alive) delete group[sn];
          }
        }
      }
      this._error = this._map ? null : "No SAJ eSolar Cloud sensors found. Is the integration set up?";
    } catch (err) {
      this._map = null;
      this._error = `Could not read the entity registry: ${err.message || err}`;
    }
    this._discovering = false;
    this._signature = "";
    this._render();
  }

  _stateSignature() {
    if (!this._map || !this._hass) return String(this._map);
    const ids = [
      ...Object.values(this._map.entities),
      ...Object.values(this._map.inverters).flatMap((i) => Object.values(i.keys)),
      ...Object.values(this._map.batteries).flatMap((b) => Object.values(b.keys)),
    ];
    return ids.map((id) => this._hass.states[id]?.last_updated || "").join("|");
  }

  /* ----- state access ----- */

  _id(key, scope) {
    return (scope ? scope.keys[key] : this._map?.entities[key]) || null;
  }

  _st(key, scope) {
    const id = this._id(key, scope);
    return id ? this._hass.states[id] : undefined;
  }

  _val(key, scope) {
    const s = this._st(key, scope)?.state;
    return s === undefined || s === "unknown" || s === "unavailable" ? null : s;
  }

  _num(key, scope) {
    const v = this._val(key, scope);
    return isNum(v) ? Number(v) : null;
  }

  _plantName() {
    if (this._config.title) return this._config.title;
    const device = this._map?.deviceId && this._hass.devices?.[this._map.deviceId];
    if (device) return device.name_by_user || device.name;
    const fn = this._st(INVERTER_KEY)?.attributes?.friendly_name || "";
    return fn.replace(/ Current Power$/, "") || "SAJ Plant";
  }

  /* ----- rendering ----- */

  _render() {
    if (!this._config) return;
    const root = this.shadowRoot;
    let body;
    if (!this._hass || this._map === undefined) body = `<div class="empty">Loading…</div>`;
    else if (!this._map) body = `<div class="empty">${esc(this._error)}</div>`;
    else body = this._shell();
    root.innerHTML = `<style>${STYLES}</style><ha-card><div class="wrap">${body}</div></ha-card>`;
    this._bind();
    if (this._map) this._loadAsync();
  }

  _shell() {
    const status = this._val("inverterStatus");
    const badge =
      status === "Alarm" ? `<span class="badge alarm">Alarm</span>` : status === "OK" ? `<span class="badge ok">Normal</span>` : `<span class="badge">--</span>`;
    const updated = this._val("lastUploadTime");
    const updatedText = updated ? new Date(updated).toLocaleString("en-GB") : "--";
    const tabs =
      this._config.view === "all"
        ? `<div class="tabs">${TABS.map(([k, n]) => `<button data-tab="${k}" class="${k === this._tab ? "on" : ""}">${n}</button>`).join("")}</div>`
        : "";
    return `
      <div class="header">
        <div class="title"><span class="star">☆</span><b>${esc(this._plantName())}</b>
          <span class="muted">ID: ${esc(this._map.plant)}</span>${badge}</div>
        ${tabs}
        <div class="updated muted">⟳ Data Update Time: ${esc(updatedText)}</div>
      </div>
      <div class="content">${this._tabContent()}</div>`;
  }

  _tabContent() {
    switch (this._tab) {
      case "device":
        return this._deviceTab();
      case "analysis":
        return this._analysisTab();
      case "comparison":
        return this._comparisonTab();
      default:
        return this._overviewTab();
    }
  }

  /* ----- overview ----- */

  _overviewTab() {
    const grid = this._num("gridPower");
    const pv = this._num("pvPower");
    const load = this._num("totalLoadPower");
    const bat = this._num("batteryPower");
    const soc = this._num("batEnergyPercent");
    const dir = this._val("batteryDirection");
    const [gv, gu] = fmtPower(grid);
    const [pvv, pvu] = fmtPower(pv);
    const [lv, lu] = fmtPower(load);
    const [bv, bu] = fmtPower(bat);
    const batState = dir === "Charging" ? "Charge" : dir === "Discharging" ? "Discharge" : "Idle";
    const weatherTemp = this._config.weather && this._hass.states[this._config.weather]?.attributes?.temperature;
    const kwp = this._config.pv_capacity;

    const card = (icon, title, inner) => `
      <div class="stat-card"><div class="stat-head"><span class="ico">${icon}</span>${title}</div><div class="stat-body">${inner}</div></div>`;
    const pair = (label, totalKey, todayKey) => {
      const [tv, tu] = fmtEnergy(this._num(totalKey));
      const today = this._num(todayKey);
      return `<div class="pair">
        <div>${label ? `<div class="lbl">${label}</div>` : ""}<div class="big">${tv} <small>${tu}</small></div></div>
        <div class="right"><div class="plus">+${fmtNumber(today)} <small>kWh</small></div><div class="lbl">Today</div></div></div>`;
    };
    const selfUse = this._num("selfUseRate");
    const revenue = this._num("totalRevenue");
    // The API reports today's revenue; yesterday's only on some accounts
    const yesterday = this._num("yesterdayRevenue");
    const today = this._num("todayRevenue");
    const delta = yesterday ?? today;
    const deltaLabel = yesterday === null ? "Today" : "Yesterday";
    const currency = this._st("totalRevenue")?.attributes?.unit_of_measurement || "EUR";
    const symbol = { EUR: "€", USD: "$", GBP: "£" }[currency] || currency;

    return `
      <div class="overview">
        <div class="flow-panel">
          ${weatherTemp !== undefined ? `<div class="weather">☁ ${fmtNumber(weatherTemp, 0)}°C</div>` : ""}
          ${flowDiagram({ grid, pv, load, bat, dir })}
          <div class="flow-label" style="left:24%"><b>${gv}</b> ${gu}<div class="lbl">Grid</div></div>
          <div class="flow-label" style="left:45%"><b>${pvv}</b> ${pvu}${kwp ? `<div><b>${esc(kwp)}</b> kWp</div>` : ""}<div class="lbl">PV</div></div>
          <div class="flow-label" style="left:66%"><b>${lv}</b> ${lu}<div class="lbl">Load</div></div>
          <div class="flow-label bottom" style="left:41%"><b>${fmtNumber(soc, 0)}%</b> <span class="state ${batState.toLowerCase()}">${batState}</span>
            <div><b>${bv}</b> ${bu}</div><div class="lbl">${esc(this._inverterModelShort())}</div></div>
        </div>
        <div class="side">
          <div class="panel strategy"><div class="panel-title"><span class="ico">≋</span>Plant Strategy</div>
            <div class="muted">${esc(this._val("operatingMode") || "--")}</div></div>
          ${revenue === null ? "" : `<div class="panel revenue"><div class="inner">
            <div class="muted center">Revenue from Energy Production</div>
            <div class="money">${revenue === null ? "--" : fmtNumber(revenue, 1)}<small>${esc(symbol)}</small></div>
            ${delta === null ? "" : `<div class="center plus">+${fmtNumber(delta, 2)} <span class="up">↗</span></div><div class="center lbl">${deltaLabel}</div>`}
          </div></div>`}
        </div>
      </div>
      <div class="stats">
        ${card("▦", "Energy Generated", `${pair("", "totalElectricity", "todayElectricity")}
          <div class="foot"><b>${selfUse === null ? "--" : fmtNumber(selfUse, 2)}%</b> <span class="muted">Self-Consumption</span></div>`)}
        ${card("▤", "Energy Consumption", pair("", "totalConsumpElec", "dailyConsumption"))}
        ${card("⚡", "Grid", pair("Import", "totalBuyElec", "dailyGridImport") + pair("Export", "totalSellElec", "dailyGridExport"))}
        ${card("▭", "Battery", pair("Charge", "totalBatteryCharge", "dailyBatteryCharge") + pair("Discharge", "totalBatteryDischarge", "dailyBatteryDischarge"))}
      </div>`;
  }

  _inverterModelShort() {
    const model = this._st("inverterStatus")?.attributes?.model || "";
    return model.split("-")[0] || "H1";
  }

  /* ----- device ----- */

  _deviceTab() {
    const inverters = Object.keys(this._map.inverters).length
      ? Object.entries(this._map.inverters).map(([sn, scope]) => ({ sn, scope }))
      : [{ sn: this._st("inverterStatus")?.attributes?.serial_number, scope: null }];

    const state = (s, cls) => `<span class="badge ${cls}">${esc(s)}</span>`;
    const rows = [];
    for (const inv of inverters) {
      const attrs = this._st("inverterStatus", inv.scope)?.attributes || {};
      const status = this._val("inverterStatus", inv.scope);
      const online = this._val("isOnline", inv.scope);
      const st = status === "Alarm" ? state("Alarm", "alarm") : online === "No" ? state("Offline", "") : state("Normal", "ok");
      const [pv, pu] = fmtPower(this._num("pvPower", inv.scope));
      rows.push(`<tr><td>▾ ${esc(this._inverterModelShort())}</td><td class="sn">${esc(attrs.serial_number || inv.sn || "--")}</td>
        <td>${st}</td><td>${esc(attrs.model || "--")}</td><td>${pv} ${pu}</td>
        <td>${fmtNumber(this._num("todayElectricity", inv.scope))} kWh</td><td>${fmtNumber(this._num("batEnergyPercent", inv.scope), 0)} %</td>
        <td>${fmtNumber(this._num("dailyBatteryCharge"))} kWh</td></tr>`);
    }
    const dir = this._val("batteryDirection");
    for (const [sn, scope] of Object.entries(this._map.batteries)) {
      const running = this._val("runningState", scope);
      const label = running === "Offline" ? "Offline" : dir === "Charging" ? "Charge" : dir === "Discharging" ? "Discharge" : running || "--";
      const model = this._val("batModel", scope);
      rows.push(`<tr class="sub"><td>Battery pack</td><td class="sn">${esc(this._st("batModel", scope)?.attributes?.serial_number || sn)}</td>
        <td>${state(label, label === "Offline" ? "" : "ok")}</td><td>${esc(model || "--")}</td><td></td><td></td>
        <td>${fmtNumber(this._num("batSoc", scope), 0)} %</td><td></td></tr>`);
    }
    return `
      <div class="panel table-panel"><table>
        <thead><tr><th>Device Name</th><th>Device SN</th><th>State</th><th>Device Model</th><th>Power Generation</th>
          <th>Today's Energy Generation</th><th>SOC</th><th>Daily charging amount</th></tr></thead>
        <tbody>${rows.join("")}</tbody></table></div>
      ${this._detailPanel(inverters[0].scope)}
      ${this._batteryPacks()}`;
  }

  _detailPanel(scope) {
    const soc = this._num("batEnergyPercent", scope);
    const status = this._val("inverterStatus", scope);
    const dir = this._val("batteryDirection", scope);
    const [pv, pu] = fmtPower(this._num("pvPower", scope));
    const [bp, bu] = fmtPower(this._num("batteryPower", scope));
    const toFull = fmtDuration(this._num("batteryTimeToFull", scope));
    const item = (label, value, unit) =>
      `<div class="kv"><div class="lbl">${label}</div><div class="v">${value} <small>${unit}</small></div></div>`;
    const fmtOr = (key, digits = 2) => fmtNumber(this._num(key, scope), digits);

    const statusBox =
      this._detail === "inverter"
        ? `<div class="lbl">Inverter status</div><div class="status ${status === "Alarm" ? "alarm" : ""}">${status === "Alarm" ? "Alarm" : status === "OK" ? "Normal" : "--"}</div>
           <div class="two"><div><div class="lbl">SOC</div><div class="v accent">${fmtNumber(soc, 0)} <small>%</small></div></div>
           <div><div class="lbl">Power Generation</div><div class="v">${pv} <small>${pu}</small></div></div></div>`
        : `<div class="status">${dir === "Charging" ? "Charging" : dir === "Discharging" ? "Discharging" : "Idle"}</div>
           ${dir === "Charging" && toFull ? `<div class="muted">Estimated full charge in ${esc(toFull)}</div>` : ""}
           <div class="lbl" style="margin-top:14px">SOC</div><div class="v accent">${fmtNumber(soc, 0)} <small>%</small></div>`;

    const strip = (label, totalKey, todayKey) => {
      const [tv, tu] = fmtEnergy(this._num(totalKey, scope));
      return `<div class="strip-item"><span class="tag">${label}</span><div class="two">
        <div><div class="lbl">Total</div><div class="v">${tv} <small>${tu}</small></div></div>
        <div><div class="lbl">Today</div><div class="v">${fmtOr(todayKey)} <small>kWh</small></div></div></div></div>`;
    };

    return `
      <div class="panel detail">
        <div class="seg">${[["inverter", "Inverter"], ["battery", "Battery"]]
          .map(([k, n]) => `<button data-detail="${k}" class="${this._detail === k ? "on" : ""}">${n}</button>`)
          .join("")}</div>
        <div class="detail-grid">
          <div class="device-art">${this._detail === "inverter" ? INVERTER_ART : BATTERY_ART}</div>
          <div class="status-box">${statusBox}</div>
          <div class="kv-panel">
            <div class="kv wide"><div class="lbl">Strategy</div><div class="v sm">${esc(this._val("operatingMode", scope) || "--")}</div></div>
            ${item("Inverter Temperature", fmtOr("inverterTemperature", 1), "°C")}
            ${item("Battery power", bp, bu)}
            ${item("Battery capacity", fmtOr("batCapcity"), "kWh")}
            ${item("Battery voltage", fmtOr("batVoltage"), "V")}
            ${item("Battery current", fmtOr("batCurr"), "A")}
            ${item("Highest Cell Temperature", fmtOr("batteryCellTempMax"), "°C")}
            ${item("Lowest Cell Temperature", fmtOr("batteryCellTempMin"), "°C")}
            ${
              this._num("batteryCellVoltMax", scope) === null && this._num("batteryCellVoltAvg", scope) !== null
                ? item("Average Cell Voltage", fmtOr("batteryCellVoltAvg", 0), "mV")
                : item("Highest Cell Voltage", fmtOr("batteryCellVoltMax", 0), "mV")
            }
          </div>
        </div>
        <div class="strip">
          ${strip("Energy Generated", "totalElectricity", "todayElectricity")}
          ${strip("Charge Capacity", "totalBatteryCharge", "dailyBatteryCharge")}
          ${strip("Discharge", "totalBatteryDischarge", "dailyBatteryDischarge")}
          <div class="strip-item"><span class="tag">Remaining SOC</span><div class="two"><div><div class="lbl">&nbsp;</div>
            <div class="v">${fmtOr("batteryRemainingEnergy")} <small>kWh</small></div></div></div></div>
        </div>
      </div>`;
  }

  _batteryPacks() {
    const packs = Object.entries(this._map.batteries);
    if (!packs.length) return "";
    return `<div class="packs">${packs
      .map(([sn, scope], i) => {
        const soc = this._num("batSoc", scope);
        return `<div class="panel pack">
          <div class="panel-title">Battery ${i + 1} <span class="muted">${esc(this._val("batModel", scope) || "")}</span></div>
          <div class="soc-bar"><i style="width:${Math.max(0, Math.min(100, soc ?? 0))}%"></i></div>
          <div class="three">
            <div><div class="lbl">SOC</div><div class="v accent">${fmtNumber(soc, 0)} <small>%</small></div></div>
            <div><div class="lbl">Temperature</div><div class="v">${fmtNumber(this._num("batTemperature", scope), 1)} <small>°C</small></div></div>
            <div><div class="lbl">SOH</div><div class="v">${fmtNumber(this._num("batSoh", scope), 0)} <small>%</small></div></div>
          </div></div>`;
      })
      .join("")}</div>`;
  }

  /* ----- analysis ----- */

  _analysisTab() {
    const { mode, date } = this._analysis;
    const modes = `<div class="seg">${ANALYSIS_MODES.map(([k, n]) => `<button data-mode="${k}" class="${k === mode ? "on" : ""}">${n}</button>`).join("")}</div>`;
    const nav =
      mode === "total"
        ? ""
        : `<div class="date-nav"><button data-nav="-1">◀</button><span>${esc(this._rangeLabel())}</span><button data-nav="1" ${this._canGoForward() ? "" : "disabled"}>▶</button></div>`;
    const data = this._cache[this._analysisKey()];
    let summary = "";
    let chart = `<div class="chart-empty">Loading…</div>`;
    if (data?.error) chart = `<div class="chart-empty">${esc(data.error)}</div>`;
    else if (data) {
      summary = this._summary(data.totals);
      chart = mode === "day" ? this._dayChart(data) : this._barChart(data);
    }
    return `
      <div class="panel">
        <div class="panel-head"><h3>Curve Analysis</h3><div class="controls">${nav}${modes}</div></div>
        ${summary}
        <div class="chart-box">${chart}<div class="tooltip" hidden></div></div>
      </div>`;
  }

  _summary(t) {
    if (!t) return "";
    const gen = t.totalElectricity;
    const exp = t.totalSellElec;
    const cons = t.totalConsumpElec;
    const imp = t.totalBuyElec;
    const block = (title, total, rateLabel, a, aLabel, aIcon, b, bLabel, bIcon, bColor) => {
      const share = isNum(total) && total > 0 && isNum(a) ? Math.max(0, Math.min(100, (a / total) * 100)) : null;
      return `<div class="summary">
        <div class="summary-top"><span><b>${title}</b> <b class="num">${fmtNumber(total)}</b><small>kWh</small></span>
          <span>${rateLabel} ${share === null ? "--" : fmtNumber(share, 1)}%</span></div>
        <div class="summary-mid"><span>${aIcon} ${aLabel} <b>${fmtNumber(a)}</b> <small>kWh</small></span><span>${bIcon} ${bLabel} <b>${fmtNumber(b)}</b> <small>kWh</small></span></div>
        <div class="split"><i style="width:${share ?? 0}%"></i><i style="width:${share === null ? 0 : 100 - share}%;background:${bColor}"></i></div>
        <div class="summary-bot"><span class="accent">${share === null ? "--" : fmtNumber(share, 1)}%</span><span style="color:${bColor}">${share === null ? "--" : fmtNumber(100 - share, 1)}%</span></div>
      </div>`;
    };
    const selfUse = isNum(gen) && isNum(exp) ? Math.max(gen - exp, 0) : null;
    const selfSuff = isNum(cons) && isNum(imp) ? Math.max(cons - imp, 0) : null;
    return `<div class="summaries">
      ${block("Energy Generated", gen, "Self-Consumption Rate", selfUse, "Self-Consumption", "⌂", exp, "Export", "⚡", COLORS.grid)}
      ${block("Energy Consumption", cons, "Self-Sufficiency Rate", selfSuff, "Self-Sufficiency", "▦+▭", imp, "Import", "⚡", COLORS.orange)}
    </div>`;
  }

  _dayChart(data) {
    const series = [
      { name: "PV Power", color: COLORS.pv, points: data.lines.pvPower || [], area: true },
      { name: "Load power", color: COLORS.load, points: data.lines.totalLoadPower || [] },
      { name: "Grid power", color: COLORS.grid, points: data.lines.gridPower || [] },
      { name: "Battery power", color: COLORS.battery, points: data.lines.batteryPower || [], area: true },
      { name: "SOC", color: COLORS.soc, points: data.lines.batEnergyPercent || [], right: true },
    ];
    this._charts.analysis = { type: "line", series, start: data.start, end: data.end, units: ["W", "W", "W", "W", "%"] };
    return lineChartSvg("analysis", series, data.start, data.end, "W", "%") + legend(series);
  }

  _barChart(data) {
    const series = ENERGY_SERIES.map(([key, name, color]) => ({ name, color, values: data.bars[key] || [] }));
    this._charts.analysis = { type: "bar", series, categories: data.categories, unit: "kWh" };
    return barChartSvg("analysis", data.categories, series, "kWh") + legend(series);
  }

  _analysisKey() {
    const { mode, date } = this._analysis;
    return `analysis:${mode}:${isoDay(date)}`;
  }

  _range() {
    const { mode, date } = this._analysis;
    const d = startOfDay(date);
    if (mode === "day") return [d, new Date(d.getFullYear(), d.getMonth(), d.getDate() + 1)];
    if (mode === "week") return [new Date(d.getFullYear(), d.getMonth(), d.getDate() - 6), new Date(d.getFullYear(), d.getMonth(), d.getDate() + 1)];
    if (mode === "month") return [new Date(d.getFullYear(), d.getMonth(), 1), new Date(d.getFullYear(), d.getMonth() + 1, 1)];
    if (mode === "year") return [new Date(d.getFullYear(), 0, 1), new Date(d.getFullYear() + 1, 0, 1)];
    return [new Date(2015, 0, 1), new Date(new Date().getFullYear() + 1, 0, 1)];
  }

  _rangeLabel() {
    const { mode, date } = this._analysis;
    const [s, e] = this._range();
    if (mode === "day") return isoDay(date);
    if (mode === "week") return `${isoDay(s)} - ${isoDay(new Date(e - 86400000))}`;
    if (mode === "month") return `${s.getFullYear()}-${pad(s.getMonth() + 1)}`;
    return String(s.getFullYear());
  }

  _canGoForward() {
    return this._range()[1] <= new Date();
  }

  _shift(step) {
    const { mode, date } = this._analysis;
    const d = new Date(date);
    if (mode === "day") d.setDate(d.getDate() + step);
    else if (mode === "week") d.setDate(d.getDate() + 7 * step);
    else if (mode === "month") d.setMonth(d.getMonth() + step, 1);
    else if (mode === "year") d.setFullYear(d.getFullYear() + step, 0, 1);
    const today = startOfDay(new Date());
    this._analysis.date = d > today ? today : d;
  }

  /* ----- comparison ----- */

  _comparisonTab() {
    const { metric, mode } = this._comparison;
    const data = this._cache[`comparison:${metric}`];
    let chart = `<div class="chart-empty">Loading…</div>`;
    if (data?.error) chart = `<div class="chart-empty">${esc(data.error)}</div>`;
    else if (data) {
      const years = Object.keys(data.monthly).sort().slice(-5);
      let categories;
      let pick;
      if (mode === "monthly") {
        categories = Array.from({ length: 12 }, (_, i) => String(i + 1));
        pick = (y) => data.monthly[y];
      } else if (mode === "quarter") {
        categories = ["Q1", "Q2", "Q3", "Q4"];
        pick = (y) => [0, 1, 2, 3].map((q) => sumDefined(data.monthly[y].slice(q * 3, q * 3 + 3)));
      } else {
        categories = [COMPARISON_METRICS.find(([k]) => k === metric)[1]];
        pick = (y) => [sumDefined(data.monthly[y])];
      }
      const series = years.map((y, i) => ({ name: y, color: YEAR_COLORS[i % YEAR_COLORS.length], values: pick(y) }));
      this._charts.comparison = { type: "bar", series, categories, unit: "kWh" };
      chart = years.length ? barChartSvg("comparison", categories, series, "kWh") + legend(series) : `<div class="chart-empty">No statistics recorded yet.</div>`;
    }
    return `
      <div class="panel">
        <div class="panel-head"><h3>Energy Comparison</h3><div class="controls">
          <select data-metric>${COMPARISON_METRICS.map(([k, n]) => `<option value="${k}" ${k === metric ? "selected" : ""}>${n}</option>`).join("")}</select>
          <div class="seg">${[["monthly", "Monthly"], ["quarter", "Quarter"], ["annual", "Annual"]]
            .map(([k, n]) => `<button data-cmode="${k}" class="${k === mode ? "on" : ""}">${n}</button>`)
            .join("")}</div></div></div>
        <div class="chart-box">${chart}<div class="tooltip" hidden></div></div>
      </div>`;
  }

  /* ----- data loading ----- */

  _loadAsync() {
    if (this._tab === "analysis") {
      const key = this._analysisKey();
      if (!this._cache[key]) this._fetch(key, () => this._fetchAnalysis());
    } else if (this._tab === "comparison") {
      const key = `comparison:${this._comparison.metric}`;
      if (!this._cache[key]) this._fetch(key, () => this._fetchComparison());
    }
  }

  async _fetch(key, loader) {
    if (this._pending?.[key]) return;
    (this._pending ??= {})[key] = true;
    try {
      this._cache[key] = await loader();
    } catch (err) {
      this._cache[key] = { error: `Could not load data: ${err.message || err}` };
    }
    delete this._pending[key];
    // Today's data keeps changing, drop it after a few minutes
    if (key.includes(isoDay(new Date())) || key.startsWith("comparison")) setTimeout(() => delete this._cache[key], 5 * 60000);
    this._render();
  }

  async _statistics(keys, start, end, period) {
    const ids = keys.map((k) => this._id(k)).filter(Boolean);
    if (!ids.length) return {};
    const res = await this._hass.callWS({
      type: "recorder/statistics_during_period",
      start_time: start.toISOString(),
      end_time: end.toISOString(),
      statistic_ids: ids,
      period,
      types: ["change"],
    });
    const out = {};
    for (const k of keys) out[k] = res[this._id(k)] || [];
    return out;
  }

  async _fetchAnalysis() {
    const { mode } = this._analysis;
    const [start, end] = this._range();
    const keys = ENERGY_SERIES.map(([k]) => k);

    if (mode === "day") {
      const lineKeys = ["pvPower", "totalLoadPower", "gridPower", "batteryPower", "batEnergyPercent"];
      const ids = lineKeys.map((k) => this._id(k)).filter(Boolean);
      const hist = ids.length
        ? await this._hass.callWS({
            type: "history/history_during_period",
            start_time: start.toISOString(),
            end_time: end.toISOString(),
            entity_ids: ids,
            minimal_response: true,
            no_attributes: true,
          })
        : {};
      const lines = {};
      for (const k of lineKeys) {
        lines[k] = (hist[this._id(k)] || [])
          .map((p) => [p.lu !== undefined ? p.lu * 1000 : Date.parse(p.last_updated || p.last_changed), Number(p.s ?? p.state)])
          .filter((p) => Number.isFinite(p[1]));
      }
      // Daily totals straight from today's counters, like the portal summary
      const totals =
        isoDay(start) === isoDay(new Date())
          ? {
              totalElectricity: this._num("todayElectricity"),
              totalSellElec: this._num("dailyGridExport"),
              totalConsumpElec: this._num("dailyConsumption"),
              totalBuyElec: this._num("dailyGridImport"),
            }
          : sumStats(await this._statistics(keys, start, end, "day"));
      return { start: +start, end: +end, lines, totals };
    }

    const period = mode === "week" || mode === "month" ? "day" : "month";
    const stats = await this._statistics(keys, start, end, period);
    let categories;
    let slot;
    if (mode === "week") {
      categories = Array.from({ length: 7 }, (_, i) => {
        const d = new Date(start.getFullYear(), start.getMonth(), start.getDate() + i);
        return `${pad(d.getMonth() + 1)}/${pad(d.getDate())}`;
      });
      slot = (d) => Math.round((startOfDay(d) - start) / 86400000);
    } else if (mode === "month") {
      const days = new Date(start.getFullYear(), start.getMonth() + 1, 0).getDate();
      categories = Array.from({ length: days }, (_, i) => String(i + 1));
      slot = (d) => d.getDate() - 1;
    } else if (mode === "year") {
      categories = Array.from({ length: 12 }, (_, i) => String(i + 1));
      slot = (d) => d.getMonth();
    } else {
      const years = new Set();
      for (const rows of Object.values(stats)) for (const r of rows) years.add(new Date(r.start).getFullYear());
      categories = [...years].sort().map(String);
      slot = (d) => categories.indexOf(String(d.getFullYear()));
    }
    const bars = {};
    for (const k of keys) {
      bars[k] = categories.map(() => null);
      for (const r of stats[k] || []) {
        const i = slot(new Date(r.start));
        if (i >= 0 && i < categories.length && isNum(r.change)) bars[k][i] = (bars[k][i] || 0) + Number(r.change);
      }
    }
    return { categories, bars, totals: sumStats(stats) };
  }

  async _fetchComparison() {
    const metric = this._comparison.metric;
    const now = new Date();
    const stats = await this._statistics([metric], new Date(now.getFullYear() - 4, 0, 1), new Date(now.getFullYear() + 1, 0, 1), "month");
    const monthly = {};
    for (const r of stats[metric] || []) {
      const d = new Date(r.start);
      const y = String(d.getFullYear());
      (monthly[y] ??= Array(12).fill(null))[d.getMonth()] = (monthly[y][d.getMonth()] || 0) + (Number(r.change) || 0);
    }
    return { monthly };
  }

  /* ----- events ----- */

  _bind() {
    const root = this.shadowRoot;
    root.querySelectorAll("[data-tab]").forEach((b) => (b.onclick = () => ((this._tab = b.dataset.tab), this._render())));
    root.querySelectorAll("[data-detail]").forEach((b) => (b.onclick = () => ((this._detail = b.dataset.detail), this._render())));
    root.querySelectorAll("[data-mode]").forEach(
      (b) =>
        (b.onclick = () => {
          this._analysis = { mode: b.dataset.mode, date: startOfDay(new Date()) };
          this._render();
        })
    );
    root.querySelectorAll("[data-nav]").forEach((b) => (b.onclick = () => (this._shift(Number(b.dataset.nav)), this._render())));
    root.querySelectorAll("[data-cmode]").forEach((b) => (b.onclick = () => ((this._comparison.mode = b.dataset.cmode), this._render())));
    const sel = root.querySelector("[data-metric]");
    if (sel) sel.onchange = () => ((this._comparison.metric = sel.value), this._render());
    root.querySelectorAll("svg[data-chart]").forEach((svg) => this._bindTooltip(svg));
  }

  _bindTooltip(svg) {
    const chart = this._charts[svg.dataset.chart];
    const box = svg.parentElement;
    const tip = box.querySelector(".tooltip");
    if (!chart || !tip) return;
    const show = (html, clientX, clientY) => {
      const r = box.getBoundingClientRect();
      tip.innerHTML = html;
      tip.hidden = false;
      const x = clientX - r.left + 14;
      tip.style.left = `${Math.min(x, r.width - tip.offsetWidth - 8)}px`;
      tip.style.top = `${Math.max(clientY - r.top - 20, 0)}px`;
    };
    const hide = () => (tip.hidden = true);
    if (chart.type === "line") {
      const hit = svg.querySelector(".hit");
      const cursor = svg.querySelector(".cursor");
      hit.onmousemove = (ev) => {
        const r = svg.getBoundingClientRect();
        const vx = ((ev.clientX - r.left) / r.width) * CH.w;
        const t = chart.start + ((vx - CH.l) / (CH.w - CH.l - CH.r)) * (chart.end - chart.start);
        cursor.setAttribute("x1", vx);
        cursor.setAttribute("x2", vx);
        cursor.setAttribute("visibility", "visible");
        const lines = chart.series
          .map((s, i) => {
            let last = null;
            for (const p of s.points) {
              if (p[0] > t) break;
              last = p;
            }
            return last ? `<div><i style="background:${s.color}"></i>${esc(s.name)}: <b>${fmtNumber(last[1], 1)} ${chart.units[i]}</b></div>` : "";
          })
          .join("");
        const d = new Date(t);
        show(`<div class="tt-title">${pad(d.getHours())}:${pad(d.getMinutes())}</div>${lines}`, ev.clientX, ev.clientY);
      };
      hit.onmouseleave = () => (hide(), cursor.setAttribute("visibility", "hidden"));
    } else {
      svg.querySelectorAll(".hit-bar").forEach((rect) => {
        rect.onmousemove = (ev) => {
          const i = Number(rect.dataset.index);
          const lines = chart.series
            .map((s) => `<div><i style="background:${s.color}"></i>${esc(s.name)}: <b>${fmtNumber(s.values[i])} ${chart.unit}</b></div>`)
            .join("");
          show(`<div class="tt-title">${esc(chart.categories[i])}</div>${lines}`, ev.clientX, ev.clientY);
        };
        rect.onmouseleave = hide;
      });
    }
  }
}

function sumDefined(values) {
  const defined = values.filter(isNum);
  return defined.length ? defined.reduce((a, b) => a + Number(b), 0) : null;
}

function sumStats(stats) {
  const out = {};
  for (const [k, rows] of Object.entries(stats)) out[k] = sumDefined(rows.map((r) => r.change));
  return out;
}

/* ---------- artwork ---------- */

/** Stylised house with animated flows between PV, grid, battery, inverter and load. */
function flowDiagram({ grid, pv, load, bat, dir }) {
  const active = (v) => isNum(v) && Math.abs(Number(v)) > 5;
  // Flow direction: "fwd" runs along the path as drawn, "rev" the other way
  const flow = (id, d, on, reverse) =>
    `<path id="${id}" d="${d}" class="wire"/>${on ? `<path d="${d}" class="flow ${reverse ? "rev" : ""}"/>` : ""}`;
  const importing = isNum(grid) && Number(grid) > 0; // positive = importing
  const charging = dir === "Charging";
  return `
  <svg class="house" viewBox="0 0 1000 560" preserveAspectRatio="xMidYMid meet">
    <defs>
      <linearGradient id="roof" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#3b3f47"/><stop offset="1" stop-color="#25282e"/></linearGradient>
      <linearGradient id="wall" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#5a5650"/><stop offset="1" stop-color="#3e3b37"/></linearGradient>
      <linearGradient id="panel" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#2c3a52"/><stop offset="1" stop-color="#151c29"/></linearGradient>
      <radialGradient id="glow" cx="0.5" cy="0.5" r="0.5"><stop offset="0" stop-color="#5fd3a8" stop-opacity="0.35"/><stop offset="1" stop-color="#5fd3a8" stop-opacity="0"/></radialGradient>
    </defs>
    <!-- leader lines to the labels -->
    <line x1="240" y1="0" x2="240" y2="330" class="leader"/>
    <line x1="450" y1="0" x2="450" y2="150" class="leader"/>
    <line x1="660" y1="0" x2="660" y2="300" class="leader"/>
    <line x1="410" y1="430" x2="410" y2="560" class="leader"/>
    <!-- ground -->
    <polygon points="150,430 520,520 900,400 560,330" fill="#2e3036"/>
    <polygon points="150,430 520,520 520,532 150,442" fill="#1d1f24"/>
    <polygon points="520,520 900,400 900,412 520,532" fill="#24262b"/>
    <!-- grid pole -->
    <rect x="236" y="250" width="8" height="190" fill="#8b8f96"/>
    <rect x="214" y="262" width="52" height="6" fill="#8b8f96"/>
    <rect x="220" y="282" width="40" height="5" fill="#8b8f96"/>
    <!-- house body -->
    <polygon points="320,300 520,360 520,505 320,450" fill="url(#wall)"/>
    <polygon points="520,360 760,280 760,420 520,505" fill="#4a4743"/>
    <polygon points="300,305 470,165 540,365" fill="url(#roof)"/>
    <polygon points="470,165 700,105 785,285 540,365" fill="url(#roof)"/>
    <polygon points="490,180 690,128 760,272 548,340" fill="url(#panel)" stroke="#4a5876" stroke-width="2"/>
    <g stroke="#4a5876" stroke-width="1.4">
      <line x1="540" y1="167" x2="604" y2="317"/><line x1="590" y1="154" x2="656" y2="300"/><line x1="640" y1="141" x2="708" y2="287"/>
      <line x1="514" y1="226" x2="718" y2="176"/><line x1="530" y1="282" x2="740" y2="226"/>
    </g>
    <polygon points="660,170 720,150 740,195 680,215" fill="#3a3d44"/>
    <!-- windows -->
    <rect x="352" y="345" width="30" height="48" fill="#e2b36f" opacity="0.85"/>
    <rect x="432" y="368" width="30" height="48" fill="#e2b36f" opacity="0.85"/>
    <polygon points="600,350 640,337 640,395 600,408" fill="#e2b36f" opacity="0.8"/>
    <polygon points="690,322 720,312 720,360 690,370" fill="#e2b36f" opacity="0.8"/>
    <polygon points="560,375 585,367 585,450 560,458" fill="#22252a"/>
    <!-- inverter and battery on the wall -->
    <ellipse cx="410" cy="440" rx="70" ry="40" fill="url(#glow)"/>
    <rect x="385" y="330" width="52" height="58" rx="4" fill="#f2f2f2"/>
    <rect x="394" y="345" width="34" height="14" rx="2" fill="#1c1f24"/>
    <rect x="388" y="398" width="46" height="70" rx="4" fill="#e9e9e9"/>
    <line x1="388" y1="421" x2="434" y2="421" stroke="#cfcfcf"/><line x1="388" y1="444" x2="434" y2="444" stroke="#cfcfcf"/>
    <path d="M415,406 l-8,16 h7 l-5,14 l12,-19 h-7 z" fill="#5fd3a8"/>
    <!-- flows -->
    ${flow("f-grid", "M244,300 L320,322 L384,345", active(grid), !importing)}
    ${flow("f-pv", "M450,190 L450,310 L411,330", active(pv), false)}
    ${flow("f-bat", "M411,388 L411,398", active(bat), !charging)}
    ${flow("f-load", "M437,360 L520,385 L660,345", active(load), false)}
    <!-- car -->
    <ellipse cx="640" cy="450" rx="36" ry="14" fill="#9aa0a8"/>
  </svg>`;
}

const INVERTER_ART = `
  <svg viewBox="0 0 300 300" class="art">
    <defs><radialGradient id="ig" cx="0.5" cy="0.5" r="0.6"><stop offset="0" stop-color="#ffffff" stop-opacity="0.18"/><stop offset="1" stop-color="#ffffff" stop-opacity="0"/></radialGradient></defs>
    <ellipse cx="150" cy="150" rx="140" ry="130" fill="url(#ig)"/>
    <rect x="70" y="50" width="160" height="170" rx="18" fill="#fbfbfb"/>
    <text x="100" y="82" font-size="13" font-weight="700" fill="#d22">SAJ</text>
    <rect x="105" y="130" width="90" height="40" rx="4" fill="#15171b"/>
    <rect x="136" y="137" width="28" height="16" fill="#3a3f47"/>
    <rect x="80" y="220" width="30" height="34" rx="3" fill="#f0f0f0"/><rect x="190" y="220" width="30" height="34" rx="3" fill="#f0f0f0"/>
    <rect x="120" y="220" width="60" height="20" fill="#2a2d33"/>
  </svg>`;

const BATTERY_ART = `
  <svg viewBox="0 0 300 300" class="art">
    <defs><radialGradient id="bg" cx="0.5" cy="0.5" r="0.6"><stop offset="0" stop-color="#5fd3a8" stop-opacity="0.18"/><stop offset="1" stop-color="#5fd3a8" stop-opacity="0"/></radialGradient></defs>
    <ellipse cx="150" cy="150" rx="140" ry="130" fill="url(#bg)"/>
    <rect x="80" y="40" width="140" height="220" rx="14" fill="#f4f4f4"/>
    <line x1="80" y1="113" x2="220" y2="113" stroke="#d6d6d6" stroke-width="2"/>
    <line x1="80" y1="186" x2="220" y2="186" stroke="#d6d6d6" stroke-width="2"/>
    <text x="96" y="70" font-size="12" font-weight="700" fill="#d22">SAJ</text>
    <path d="M155,128 l-16,32 h14 l-10,28 l24,-38 h-14 z" fill="#5fd3a8"/>
  </svg>`;

/* ---------- styles ---------- */

const STYLES = `
  :host { --bg:#0d111a; --panel:#1b2130; --panel2:#2a3142; --line:#3a4256; --text:#e8ebf2; --muted:#9aa3b5; --accent:${COLORS.accent}; display:block; }
  ha-card { background:var(--bg); color:var(--text); border-radius:16px; overflow:hidden; border:none; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
  .wrap { padding:16px; }
  .empty { padding:40px; text-align:center; color:var(--muted); }
  .muted { color:var(--muted); } .center { text-align:center; } .accent { color:var(--accent) !important; }
  .lbl { color:var(--muted); font-size:13px; }
  small { font-size:0.62em; color:var(--muted); font-weight:400; }
  .header { display:grid; grid-template-columns:1fr auto 1fr; align-items:center; gap:12px; margin-bottom:14px; }
  .title { display:flex; align-items:center; gap:12px; font-size:16px; flex-wrap:wrap; }
  .star { color:var(--muted); }
  .updated { justify-self:end; font-size:13px; }
  .badge { border:1px solid var(--muted); color:var(--muted); border-radius:4px; padding:2px 7px; font-size:13px; }
  .badge.ok { border-color:#4bd36b; color:#4bd36b; } .badge.alarm { border-color:#f05a5a; color:#f05a5a; }
  .tabs, .seg { display:inline-flex; background:var(--panel2); border-radius:12px; padding:4px; gap:2px; }
  .tabs button, .seg button { background:none; border:1px solid transparent; color:var(--muted); font-size:15px; padding:7px 18px; border-radius:9px; cursor:pointer; font-family:inherit; }
  .tabs button.on, .seg button.on { color:var(--text); background:#3a4359; border-color:#6fd8c0; box-shadow:0 0 0 1px rgba(111,216,192,.25) inset; }
  .content { display:flex; flex-direction:column; gap:14px; }
  .panel { background:var(--panel); border-radius:16px; padding:18px; position:relative; }
  .panel-title { display:flex; gap:8px; align-items:center; font-size:16px; font-weight:600; margin-bottom:8px; }
  .ico { color:var(--accent); font-size:18px; }
  .overview { display:grid; grid-template-columns:2fr 1fr; gap:14px; background:var(--panel); border-radius:16px; padding:18px; position:relative; }
  .flow-panel { position:relative; min-height:360px; }
  .house { width:100%; height:auto; display:block; margin-top:60px; }
  .weather { position:absolute; top:0; left:0; font-size:14px; }
  .flow-label { position:absolute; top:0; font-size:15px; line-height:1.5; padding-left:8px; transform:translateX(-2px); }
  .flow-label b { font-size:19px; font-weight:500; }
  .flow-label.bottom { top:auto; bottom:-8px; }
  .state { font-size:13px; } .state.charge { color:#58b8ff; } .state.discharge { color:${COLORS.orange}; } .state.idle { color:var(--muted); }
  .leader { stroke:#6b7385; stroke-width:1; }
  .wire { fill:none; stroke:#7b8494; stroke-width:3; opacity:.55; }
  .flow { fill:none; stroke:#7ef0c6; stroke-width:3.5; stroke-dasharray:10 14; animation:dash 1.2s linear infinite; filter:drop-shadow(0 0 4px #5fd3a8); }
  .flow.rev { animation-direction:reverse; }
  @keyframes dash { to { stroke-dashoffset:-24; } }
  .side { display:flex; flex-direction:column; gap:14px; padding-top:50px; }
  .side .panel { background:var(--panel2); }
  .revenue .inner { background:#3a4254; border-radius:12px; padding:22px 10px; }
  .money { font-size:54px; font-weight:600; text-align:center; margin:10px 0; }
  .money small { font-size:22px; color:var(--text); margin-left:4px; }
  .plus { font-size:20px; font-weight:600; } .up { color:var(--accent); }
  .stats { display:grid; grid-template-columns:repeat(4,1fr); gap:12px; }
  .stat-card { background:var(--panel2); border-radius:14px; overflow:hidden; display:flex; flex-direction:column; }
  .stat-head { display:flex; gap:10px; align-items:center; padding:16px 20px; font-size:16px; }
  .stat-body { background:#3a4254; border-radius:14px; padding:14px 20px; flex:1; display:flex; flex-direction:column; gap:14px; }
  .pair { display:flex; justify-content:space-between; align-items:flex-start; gap:8px; }
  .pair .right { text-align:right; }
  .big { font-size:24px; font-weight:500; }
  .foot { margin-top:auto; font-size:16px; } .foot b { font-size:24px; font-weight:500; }
  table { width:100%; border-collapse:collapse; font-size:15px; }
  th { text-align:left; color:var(--muted); font-weight:500; padding:14px 12px; border-bottom:1px solid var(--line); background:var(--panel2); }
  td { padding:20px 12px; border-bottom:1px solid var(--line); }
  th, td { white-space:nowrap; }
  tr.sub td:first-child { padding-left:44px; }
  td.sn { color:#5aa9ff; }
  .table-panel { padding:0; overflow:auto; }
  .detail { display:flex; flex-direction:column; gap:12px; }
  .detail > .seg { align-self:flex-start; }
  .detail-grid { display:grid; grid-template-columns:1.3fr 1fr 1.2fr; gap:20px; align-items:center; }
  .art { width:100%; max-width:320px; display:block; margin:auto; }
  .status-box { background:var(--panel2); border-radius:12px; padding:20px; }
  .status { font-size:22px; color:var(--accent); margin:6px 0 16px; } .status.alarm { color:#f05a5a; }
  .two { display:grid; grid-template-columns:1fr 1fr; gap:12px; } .three { display:grid; grid-template-columns:repeat(3,1fr); gap:12px; }
  .v { font-size:26px; font-weight:500; margin-top:4px; } .v.sm { font-size:16px; }
  .kv-panel { background:var(--panel2); border-radius:16px; padding:18px; display:grid; grid-template-columns:1fr 1fr; gap:18px 14px; }
  .kv.wide { grid-column:1 / -1; }
  .strip { background:var(--panel2); border-radius:14px; padding:16px 22px; display:grid; grid-template-columns:repeat(3,1.4fr) 1fr; gap:16px; }
  .tag { background:#151a24; padding:3px 7px; border-radius:4px; font-size:13px; }
  .strip .two { margin-top:12px; }
  .packs { display:grid; grid-template-columns:repeat(auto-fit,minmax(240px,1fr)); gap:12px; }
  .soc-bar { height:8px; background:#3a4254; border-radius:4px; overflow:hidden; margin:6px 0 14px; }
  .soc-bar i { display:block; height:100%; background:linear-gradient(90deg,#5fd3a8,#7ef0c6); }
  .panel-head { display:flex; justify-content:space-between; align-items:center; gap:12px; flex-wrap:wrap; margin-bottom:14px; }
  .panel-head h3 { margin:0; font-size:22px; font-weight:500; }
  .controls { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
  .date-nav { display:flex; align-items:center; gap:10px; background:var(--panel2); border-radius:12px; padding:4px 6px; }
  .date-nav button { background:#3a4359; border:none; color:var(--text); width:34px; height:34px; border-radius:50%; cursor:pointer; }
  .date-nav button[disabled] { opacity:.3; cursor:default; }
  select { background:var(--panel2); color:var(--text); border:none; border-radius:10px; padding:10px 14px; font-size:15px; font-family:inherit; }
  .summaries { display:grid; grid-template-columns:1fr 1fr; gap:14px; margin-bottom:14px; }
  .summary { background:var(--panel2); border-radius:14px; padding:16px 20px; }
  .summary-top, .summary-mid, .summary-bot { display:flex; justify-content:space-between; gap:10px; }
  .summary-top { padding-bottom:12px; border-bottom:1px solid #1b1f29; font-size:17px; }
  .summary-top .num { margin-left:8px; }
  .summary-mid { padding:14px 0 10px; font-size:15px; }
  .split { display:flex; height:22px; border-radius:3px; overflow:hidden; gap:3px; }
  .split i { display:block; background:#5ec89e; } .split i:last-child { flex:1; }
  .summary-bot { margin-top:10px; font-size:15px; }
  .chart-box { background:var(--panel2); border-radius:14px; padding:14px 10px 10px; position:relative;
    background-image:radial-gradient(ellipse at 85% 0%, rgba(95,211,168,.22), transparent 45%), radial-gradient(ellipse at 100% 0%, rgba(90,140,240,.3), transparent 35%); }
  .chart { width:100%; height:auto; display:block; }
  .chart .grid { stroke:#3d4558; stroke-width:1; } .chart .zero { stroke:#cfd5e2; stroke-width:1; }
  .chart .tick, .chart .axis-unit { fill:#c5cbd8; font-size:13px; }
  .chart .cursor { stroke:#aab2c2; stroke-dasharray:3 3; }
  .chart-empty { height:240px; display:flex; align-items:center; justify-content:center; color:var(--muted); }
  .legend { display:flex; justify-content:center; flex-wrap:wrap; gap:18px; font-size:14px; padding:8px; }
  .legend i, .tooltip i { display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:6px; }
  .tooltip { position:absolute; background:rgba(40,44,54,.96); border-radius:8px; padding:10px 12px; font-size:13px; pointer-events:none; z-index:2; min-width:160px; box-shadow:0 4px 16px rgba(0,0,0,.4); }
  .tooltip .tt-title { margin-bottom:6px; color:var(--muted); }
  :host(.narrow) .header { grid-template-columns:1fr; } :host(.narrow) .updated { justify-self:start; }
  :host(.narrow) .overview, :host(.narrow) .detail-grid, :host(.narrow) .summaries { grid-template-columns:1fr; }
  :host(.narrow) .stats { grid-template-columns:1fr 1fr; } :host(.narrow) .strip { grid-template-columns:1fr 1fr; }
  :host(.narrow) .side { padding-top:0; }
`;

customElements.define("saj-elekeeper-card", SajElekeeperCard);
window.customCards = window.customCards || [];
window.customCards.push({
  type: "saj-elekeeper-card",
  name: "SAJ Elekeeper",
  description: "Elekeeper-style dashboard for the SAJ eSolar Cloud integration",
  preview: false,
});
console.info(`%c SAJ-ELEKEEPER-CARD %c ${CARD_VERSION} `, "background:#5fd3a8;color:#000", "background:#1b2130;color:#fff");
