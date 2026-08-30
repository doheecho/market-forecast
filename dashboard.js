/* 원자재 시황 대시보드
 * raw_materials_forecast.json (analyze.py 산출물) 을 로드해 6대 원자재별로
 * KPI · 메인 차트(실적+6개월 전망 팬) · 요인지표 · 시나리오 · 과거 유사국면 · 근거표를 렌더.
 */
"use strict";

const JSON_LOCAL = "./raw_materials_forecast.json";
const JSON_RAW =
  "https://raw.githubusercontent.com/doheecho/market-forecast/main/raw_materials_forecast.json";

const ORDER = ["wti", "copper", "aluminum", "gold", "silver", "platinum"];
const LABEL = {
  wti: "WTI 원유", copper: "전기동", aluminum: "알루미늄",
  gold: "금", silver: "은", platinum: "백금",
};
const RANGES = [
  ["3M", 3], ["6M", 6], ["1Y", 12], ["2Y", 24], ["3Y", 36], ["5Y", 60], ["ALL", 0],
];

const state = { data: null, key: "wti", months: 12, charts: {} };

/* ---------- 부트스트랩 ---------- */
if (window.Chart) {
  Chart.defaults.color = "#8b95a1";
  Chart.defaults.borderColor = "#2b333d40";
  Chart.defaults.font.family =
    '-apple-system, "Segoe UI", "Malgun Gothic", Roboto, sans-serif';
}
document.addEventListener("DOMContentLoaded", init);
document.getElementById("refreshBtn").addEventListener("click", () => load(true));

async function init() {
  await load(false);
}

async function load(bust) {
  const app = document.getElementById("app");
  const q = bust ? "?t=" + Date.now() : "";
  try {
    const data = await fetchFirst([JSON_LOCAL + q, JSON_RAW + "?t=" + Date.now()]);
    state.data = data;
    if (!ORDER.some((k) => data.forecast_data && data.forecast_data[k])) {
      throw new Error("forecast_data 가 비어 있습니다");
    }
    document.getElementById("asOf").textContent = "기준일 " + (data.update_date || "—");
    buildTabs();
    render();
  } catch (e) {
    app.innerHTML = `<div class="error">데이터 로드 실패: ${escapeHtml(String(e.message || e))}</div>`;
  }
}

async function fetchFirst(urls) {
  let lastErr;
  for (const u of urls) {
    try {
      const r = await fetch(u, { cache: "no-store" });
      if (r.ok) return await r.json();
      lastErr = new Error("HTTP " + r.status);
    } catch (e) {
      lastErr = e;
    }
  }
  throw lastErr || new Error("모든 경로 실패");
}

/* ---------- 탭 ---------- */
function buildTabs() {
  const nav = document.getElementById("tabs");
  const keys = ORDER.filter((k) => state.data.forecast_data[k]);
  nav.innerHTML = keys
    .map(
      (k) =>
        `<button data-key="${k}"${k === state.key ? ' class="active"' : ""}>${
          state.data.forecast_data[k].name || LABEL[k] || k
        }</button>`
    )
    .join("");
  nav.querySelectorAll("button").forEach((b) =>
    b.addEventListener("click", () => {
      state.key = b.dataset.key;
      nav.querySelectorAll("button").forEach((x) => x.classList.toggle("active", x === b));
      render();
    })
  );
}

/* ---------- 렌더 ---------- */
function render() {
  destroyCharts();
  const d = state.data;
  const f = d.forecast_data[state.key];
  if (!f) return;
  const sign = currencySign(f.unit);
  const rateUp = !String(f.forecast_change_rate || "").trim().startsWith("-");

  document.getElementById("app").innerHTML = `
    ${f.planning_advisor ? `<div class="advisor">${escapeHtml(stripAdvisorPrefix(f.planning_advisor))}</div>` : ""}

    <div class="cards">
      <div class="card">
        <div class="label">현재가</div>
        <div class="value">${sign}${fmtNum(f.current_price)}</div>
        <div class="sub">${escapeHtml(f.unit || "")}</div>
      </div>
      <div class="card">
        <div class="label">6개월 후 AI 목표가</div>
        <div class="value ${rateUp ? "up" : "down"}">${sign}${fmtNum(f.forecast_6m_target)}<span class="chip ${rateUp ? "up" : "down"}">${escapeHtml(f.forecast_change_rate || "")}</span></div>
        <div class="sub">기준 시나리오 (Base)</div>
      </div>
      <div class="card">
        <div class="label">변동성 지수</div>
        <div class="value">${fmtNum(f.volatility_score)}<span class="sub" style="margin-left:6px">pt</span></div>
        <div class="sub">0=안정 · 100=극심</div>
      </div>
    </div>

    <div class="block">
      <h3>가격 추이 · 6개월 전망 (Base / Bull / Bear)</h3>
      <div class="ctl-row" id="rangeRow">
        <span class="ctl-lbl">실적 기간</span>
        ${RANGES.map(([lbl, m]) => `<button data-m="${m}"${m === state.months ? ' class="on"' : ""}>${rangeText(lbl)}</button>`).join("")}
      </div>
      <div class="chart-box"><canvas id="mainChart"></canvas></div>
      <div class="src">실적(음영) · Base(굵은 선) · Bull/Bear(점선, 구름). 세로 점선 = 현재.</div>
    </div>

    <div class="block">
      <h3>요인지표 (Rationale)</h3>
      <div class="scroll-x" id="metrics"></div>
    </div>

    <div class="block">
      <h3>시나리오</h3>
      <div class="grid-3" id="scenarios"></div>
    </div>

    <div class="block" id="analogBlock" hidden>
      <h3>과거 유사 국면</h3>
      <div class="grid-2" id="analogs"></div>
    </div>

    <div class="block">
      <h3>6개월 가격 예측 근거</h3>
      <table>
        <thead><tr><th style="width:14%">대상월</th><th style="width:20%">전망 가격</th><th>예측 근거 · 주요 요인</th></tr></thead>
        <tbody id="rationaleBody"></tbody>
      </table>
      <div class="src">거시(DXY ${fmtNum(d.macro?.dxy)} · 美10Y ${fmtNum(d.macro?.us10y)}% · USD/CNY ${fmtNum(d.macro?.usdcny)} · USD/KRW ${fmtNum(d.macro?.usdkrw)}) 기준</div>
    </div>`;

  document.getElementById("rangeRow").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-m]");
    if (!b) return;
    state.months = +b.dataset.m;
    document.querySelectorAll("#rangeRow button").forEach((x) => x.classList.toggle("on", x === b));
    drawMainChart();
  });

  drawMainChart();
  renderMetrics(f);
  renderScenarios(f);
  renderAnalogs(f, sign);
  renderRationale(f, sign);
}

/* ---------- 메인 차트 ---------- */
function historyRows(key) {
  const d = state.data;
  return (d.history_3y || d.history || {})[key] || [];
}

function drawMainChart() {
  const f = state.data.forecast_data[state.key];
  const sign = currencySign(f.unit);
  let hist = historyRows(state.key);
  if (state.months > 0 && hist.length) {
    const last = new Date(hist[hist.length - 1].date).getTime();
    const cut = last - state.months * 30.4 * 864e5;
    hist = hist.filter((r) => new Date(r.date).getTime() >= cut);
  }

  const histPts = hist.map((r) => ({ x: r.date, y: r.price }));
  const anchor = histPts.length ? histPts[histPts.length - 1] : null;

  const fc = (arr) => (arr || []).map((r) => ({ x: r.month + "-15", y: r.price }));
  const base = fc(f.monthly_forecast_base);
  const bull = fc(f.monthly_forecast_bull);
  const bear = fc(f.monthly_forecast_bear);
  const lead = anchor ? [anchor] : [];

  const ds = [
    {
      label: "실적", data: histPts, borderColor: "#22d3ee",
      backgroundColor: "rgba(34,211,238,0.08)", borderWidth: 1.6,
      pointRadius: 0, fill: true, tension: 0.25, order: 5,
    },
    {
      label: "Bull", data: lead.concat(bull), borderColor: "#ef4444",
      borderWidth: 1.4, borderDash: [4, 3], pointRadius: 0, fill: false, tension: 0.3, order: 3,
    },
    {
      label: "Bear", data: lead.concat(bear), borderColor: "#3b82f6",
      backgroundColor: "rgba(99,110,140,0.10)", borderWidth: 1.4, borderDash: [4, 3],
      pointRadius: 0, fill: "-1", tension: 0.3, order: 3,
    },
    {
      label: "Base", data: lead.concat(base), borderColor: "#f59e0b",
      borderWidth: 2.6, pointRadius: 0, fill: false, tension: 0.3, order: 1,
    },
  ];

  state.charts.main = new Chart(document.getElementById("mainChart"), {
    type: "line",
    data: { datasets: ds },
    options: {
      responsive: true, maintainAspectRatio: false,
      parsing: true,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { labels: { boxWidth: 12, font: { size: 11 } } },
        tooltip: {
          callbacks: {
            label: (c) => `${c.dataset.label}: ${sign}${fmtNum(c.parsed.y)}`,
          },
        },
      },
      scales: {
        x: {
          type: "time",
          time: { unit: pickUnit(state.months), tooltipFormat: "yyyy-MM-dd" },
          grid: { color: "#2b333d40" },
          ticks: { maxRotation: 0, autoSkip: true, autoSkipPadding: 20 },
        },
        y: {
          position: "right", grid: { color: "#2b333d40" },
          ticks: { callback: (v) => sign + fmtNum(v) },
        },
      },
    },
    plugins: [nowLine(anchor && anchor.x)],
  });
}

function pickUnit(months) {
  if (months && months <= 6) return "week";
  if (months && months <= 36) return "month";
  return "quarter";
}

/* 현재 시점 세로 점선 */
function nowLine(xVal) {
  return {
    id: "nowLine",
    afterDatasetsDraw(chart) {
      if (!xVal || !chart.scales.x) return;
      const px = chart.scales.x.getPixelForValue(new Date(xVal).getTime());
      if (px < chart.chartArea.left || px > chart.chartArea.right) return;
      const { ctx, chartArea } = chart;
      ctx.save();
      ctx.strokeStyle = "#8b95a188";
      ctx.setLineDash([3, 3]);
      ctx.beginPath();
      ctx.moveTo(px, chartArea.top);
      ctx.lineTo(px, chartArea.bottom);
      ctx.stroke();
      ctx.restore();
    },
  };
}

/* ---------- 요인지표 ---------- */
function renderMetrics(f) {
  const box = document.getElementById("metrics");
  const list = f.metrics || [];
  if (!list.length) { box.innerHTML = "<span class='src'>지표 없음</span>"; return; }
  box.innerHTML = list
    .map(
      (m) => `<div class="metric">
        <div class="top">
          <span class="name">${escapeHtml(m.label || "")}</span>
          <span class="badge ${badgeClass(m.badge)}">${escapeHtml(m.cat || "")} · ${escapeHtml(m.status || "")}</span>
        </div>
        <div class="top" style="margin:0">
          <span class="val">${escapeHtml(m.val || "")}</span>
          <span class="date">${escapeHtml(m.date || "")}</span>
        </div>
      </div>`
    )
    .join("");
}

/* ---------- 시나리오 ---------- */
function renderScenarios(f) {
  const box = document.getElementById("scenarios");
  const row = (cls, head, txt) =>
    `<div class="scenario ${cls}"><div class="head">${head}</div><p>${escapeHtml(txt || "-")}</p></div>`;
  box.innerHTML =
    row("base", "기본 (Base) · 50%", f.rationale_base) +
    row("bull", "낙관 (Bull) · 25%", f.rationale_bull) +
    row("bear", "비관 (Bear) · 25%", f.rationale_bear);
}

/* ---------- 과거 유사 국면 ---------- */
function renderAnalogs(f, sign) {
  const block = document.getElementById("analogBlock");
  const box = document.getElementById("analogs");
  const list = f.analogs || [];
  if (!list.length) { block.hidden = true; return; }
  block.hidden = false;
  box.innerHTML = list
    .map(
      (a, i) => `<div class="analog">
        <div class="head">
          <span class="title">${escapeHtml(a.title || "유사 국면")}</span>
          <span class="badge success">유사도 ${escapeHtml(a.similarity || "-")}</span>
        </div>
        <div class="period">분석 기간 ${escapeHtml(a.period || "-")}</div>
        <p class="summary">${escapeHtml(a.summary || "")}</p>
        <div class="mini-box"><canvas id="mini${i}"></canvas></div>
        <div class="foot">
          <span class="badge danger">전망 정확도 ${escapeHtml(a.acc || "-")}</span>
          <span class="kv" style="text-align:right"><small>모델 전망 / 실제</small><b>${escapeHtml(a.forecast || "-")} / ${escapeHtml(a.actual || "-")}</b></span>
        </div>
      </div>`
    )
    .join("");

  list.forEach((a, i) => drawMini(i, a));
}

function drawMini(i, a) {
  const hist = (a.miniHist || []).map(Number);
  const fore = (a.miniForecast || []).map(Number);
  if (!hist.length) return;
  const n = hist.length;
  const labels = [];
  for (let k = 0; k < n + fore.length; k++) labels.push(k - n + 1);

  const histLine = hist.concat(Array(fore.length).fill(null));
  const foreLine = Array(n - 1).fill(null).concat([hist[n - 1]], fore);

  state.charts["mini" + i] = new Chart(document.getElementById("mini" + i), {
    type: "line",
    data: {
      labels,
      datasets: [
        { label: "과거 실적", data: histLine, borderColor: "#22d3ee", borderWidth: 1.5, pointRadius: 0, tension: 0.3 },
        { label: "이후 실제", data: foreLine, borderColor: "#22c55e", borderWidth: 1.5, borderDash: [3, 3], pointRadius: 0, tension: 0.3 },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false }, tooltip: { enabled: false } },
      scales: { x: { display: false }, y: { display: false } },
    },
  });
}

/* ---------- 근거 표 ---------- */
function renderRationale(f, sign) {
  const body = document.getElementById("rationaleBody");
  const list = f.monthly_forecast_base || [];
  body.innerHTML = list
    .map(
      (r) => `<tr>
        <td class="m"><span class="badge secondary">${escapeHtml(r.month || "")} (E)</span></td>
        <td class="mono">${sign}${fmtNum(r.price)}</td>
        <td>${escapeHtml(r.rationale || "매크로 및 원자재 스프레드 변동에 따라 조정될 전망")}</td>
      </tr>`
    )
    .join("");
}

/* ---------- 유틸 ---------- */
function destroyCharts() {
  for (const k of Object.keys(state.charts)) {
    try { state.charts[k].destroy(); } catch (_) {}
  }
  state.charts = {};
}
function currencySign(unit) {
  return /[￠¢]/.test(unit || "") ? "¢" : "$";
}
function fmtNum(v) {
  const n = Number(v);
  if (!Number.isFinite(n)) return "—";
  return n.toLocaleString("en-US", { maximumFractionDigits: Math.abs(n) >= 100 ? 0 : 2 });
}
function rangeText(l) {
  return { "3M": "3개월", "6M": "6개월", "1Y": "1년", "2Y": "2년", "3Y": "3년", "5Y": "5년", ALL: "전체" }[l] || l;
}
function badgeClass(b) {
  return ["danger", "warning", "success", "secondary"].includes(b) ? b : "secondary";
}
function stripAdvisorPrefix(s) {
  return String(s).replace(/^\s*Planning Advisor\s*[:：]\s*/i, "").trim();
}
function escapeHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}
