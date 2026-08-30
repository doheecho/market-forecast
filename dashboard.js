/* 원자재 시황 대시보드
 * raw_materials_forecast.json (analyze.py 산출물) 을 로드해 6대 원자재별로
 * KPI · 메인 차트(실적+6개월 전망 팬) · 요인지표 · 시나리오 · 과거 유사국면 · 근거표를 렌더.
 */
"use strict";

const JSON_LOCAL = "./raw_materials_forecast.json";
const JSON_RAW =
  "https://raw.githubusercontent.com/doheecho/market-forecast/main/raw_materials_forecast.json";

// 배포한 Cloudflare Worker 주소 (proxy/). 비우면 "AI 분석 갱신" 은 데이터 재조회만 함.
const PROXY_BASE = ""; // 예: "https://market-forecast-proxy.<subdomain>.workers.dev"

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
document.getElementById("rerunBtn").addEventListener("click", rerunAnalysis);

async function init() {
  await load(false);
}

/* ---------- 토스트 ---------- */
let _toastT = null;
function toast(msg, ms = 2600) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(_toastT);
  if (ms) _toastT = setTimeout(() => (el.hidden = true), ms);
}

/* ---------- AI 분석 갱신: GitHub Actions(run.yml) 트리거 후 반영 대기 ---------- */
async function rerunAnalysis() {
  const btn = document.getElementById("rerunBtn");
  btn.disabled = true;
  const before = state.data && state.data.update_date;
  try {
    if (PROXY_BASE) {
      const r = await fetch(`${PROXY_BASE}/dispatch?wf=run`, { cache: "no-store" })
        .then((x) => x.json())
        .catch((e) => ({ error: String(e) }));
      if (r && r.ok) {
        toast("AI 분석 재생성 요청됨 · 1~3분 후 자동 반영", 4000);
        for (let i = 0; i < 20; i++) {
          await new Promise((s) => setTimeout(s, 12000));
          try {
            const d = await fetchFirst([JSON_LOCAL + "?t=" + Date.now(), JSON_RAW + "?t=" + Date.now()]);
            if (d && d.update_date && JSON.stringify(d) !== JSON.stringify(state.data)) {
              state.data = d;
              document.getElementById("asOf").textContent = "기준일 " + d.update_date;
              render();
              toast("AI 분석 갱신 완료");
              return;
            }
          } catch (_) {}
        }
        toast("아직 갱신 전입니다. 잠시 후 새로고침 해주세요.", 4000);
        return;
      }
      toast("워크플로 트리거 실패 · 최신 데이터만 다시 불러옵니다");
      await load(true);
      return;
    }
    // PROXY 미설정: GitHub Actions 실행 페이지를 새 탭으로 열어준다
    window.open(
      "https://github.com/doheecho/market-forecast/actions/workflows/run.yml",
      "_blank",
      "noopener"
    );
    toast("GitHub Actions 에서 'Run workflow' 를 눌러 갱신하세요 (PROXY_BASE 설정 시 자동)", 5000);
    await load(true);
  } finally {
    btn.disabled = false;
  }
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

  const advisorText = stripAdvisorPrefix(f.advisor || f.planning_advisor || "");
  document.getElementById("app").innerHTML = `
    ${advisorText ? `<div class="advisor"><span class="advisor-tag">AI Advisor</span>\n${escapeHtml(advisorText)}</div>` : ""}

    <div class="cards">
      <div class="card">
        <div class="label">현재가</div>
        <div class="value">${sign}${fmtNum(f.current_price)}</div>
        <div class="sub">${escapeHtml(f.unit || "")}</div>
      </div>
      <div class="card">
        <div class="label">6개월 후 AI 가격전망</div>
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
    </div>

    <div class="block">
      <h3>요인지표</h3>
      <div class="scroll-x" id="metrics"></div>
    </div>

    <div class="two-col">
      <div class="block" id="analogBlock">
        <h3>과거 유사 국면</h3>
        <div id="analogs"></div>
      </div>
      <div class="block">
        <h3>시나리오</h3>
        <div id="scenarios"></div>
      </div>
    </div>

    <div class="block scn-block">
      <h3>6개월 가격 시나리오</h3>
      <div class="tbl-scroll">
        <table class="scn-table">
          <thead>
            <tr>
              <th rowspan="2" class="col-month">대상월</th>
              <th colspan="2" class="grp base">기본</th>
              <th colspan="2" class="grp bull">낙관</th>
              <th colspan="2" class="grp bear">비관</th>
            </tr>
            <tr>
              <th>전망 가격</th><th>예측 근거 및 주요 요인</th>
              <th>전망 가격</th><th>예측 근거 및 주요 요인</th>
              <th>전망 가격</th><th>예측 근거 및 주요 요인</th>
            </tr>
          </thead>
          <tbody id="scnBody"></tbody>
        </table>
      </div>
      <div class="src">거시(DXY ${fmtNum(d.macro?.dxy)} · 美10Y ${fmtNum(d.macro?.us10y)}% · USD/CNY ${fmtNum(d.macro?.usdcny)} · USD/KRW ${fmtNum(d.macro?.usdkrw)}) 기준 · AI 생성</div>
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
  renderScenarioTable(f, sign);
}

/* ---------- 메인 차트 ---------- */
function historyRows(key) {
  const d = state.data;
  return (d.history_3y || d.history || {})[key] || [];
}

function drawMainChart() {
  if (state.charts.main) { try { state.charts.main.destroy(); } catch (_) {} state.charts.main = null; }
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
    row("base", "기본 (Base)", f.rationale_base) +
    row("bull", "낙관 (Bull)", f.rationale_bull) +
    row("bear", "비관 (Bear)", f.rationale_bear);
}

/* ---------- 과거 유사 국면 ---------- */
function renderAnalogs(f, sign) {
  const box = document.getElementById("analogs");
  const list = f.analogs || [];
  if (!list.length) {
    box.innerHTML = "<p class='src'>유사 국면 데이터 없음 (AI 분석 갱신 후 표시)</p>";
    return;
  }
  box.innerHTML = list
    .map((a, i) => {
      const past = a.actual != null && a.actual !== "" ? a.actual : "—";
      const now = f.forecast_change_rate || "—";
      return `<div class="analog">
        <div class="head">
          <span class="title">${escapeHtml(a.title || "유사 국면")}</span>
          <span class="badge success">유사도 ${escapeHtml(a.similarity || "-")}</span>
        </div>
        <div class="period">분석 기간 ${escapeHtml(a.period || "-")} · 월간 추이 상관도 기준</div>
        <p class="summary">${escapeHtml(a.summary || "")}</p>
        <div class="mini-box"><canvas id="mini${i}"></canvas></div>
        <div class="foot">
          <span class="kv"><small>이 국면 이후 6개월 실제</small><b>${escapeHtml(past)}</b></span>
          <span class="kv" style="text-align:right"><small>현재 모델 6개월 전망</small><b>${escapeHtml(now)}</b></span>
        </div>
      </div>`;
    })
    .join("");

  const curMonthly = monthlyCloses(historyRows(state.key));
  const curBase = (f.monthly_forecast_base || []).map((r) => Number(r.price)).filter(Number.isFinite);
  list.forEach((a, i) => drawMini(i, a, curMonthly, curBase));
}

/* 일/주봉 시계열 → 월별 마지막 종가 배열 */
function monthlyCloses(rows) {
  const m = new Map();
  for (const r of rows || []) if (r && r.date) m.set(String(r.date).slice(0, 7), r.price);
  return [...m.values()].map(Number).filter(Number.isFinite);
}

/* 과거 유사국면과 현재를 같은 평면에 정규화(첫 값=100)해서 겹쳐 본다.
   - 과거 실적(하늘색) / 과거 이후 실제(초록 점선)
   - 현재 실적(자홍) / 현재 전망(보라 점선)  ← 과거와 같은 구간 길이로 정렬 */
function drawMini(i, a, curMonthly, curBase) {
  const pastH = (a.miniHist || []).map(Number).filter(Number.isFinite);
  const pastF = (a.miniForecast || []).map(Number).filter(Number.isFinite);
  if (pastH.length < 2) return;
  const H = pastH.length;                 // 과거 실적 개월수 (보통 12)
  const F = pastF.length || curBase.length; // 이후 개월수 (보통 6)
  const rebase = (arr, b) => arr.map((v) => (b ? (v / b) * 100 : null));

  const pastHistN = rebase(pastH, pastH[0]);
  const pastForeN = Array(H - 1).fill(null).concat([pastHistN[H - 1]], rebase(pastF, pastH[0]));

  const curH = (curMonthly || []).slice(-H);
  const curF = (curBase || []).slice(0, F);
  let curHistN = [], curForeN = [];
  if (curH.length >= 2) {
    const b = curH[0];
    curHistN = Array(H - curH.length).fill(null).concat(rebase(curH, b));
    curForeN = Array(H - 1).fill(null).concat([curHistN[H - 1]], rebase(curF, b));
  }

  const labels = [];
  for (let k = 0; k < H + F; k++) labels.push(k < H ? `${k - H + 1}` : `+${k - H + 1}`);

  state.charts["mini" + i] = new Chart(document.getElementById("mini" + i), {
    type: "line",
    data: {
      labels,
      datasets: [
        { label: "과거 실적", data: pastHistN, borderColor: "#22d3ee", borderWidth: 1.6, pointRadius: 0, tension: 0.3 },
        { label: "과거 이후 실제", data: pastForeN, borderColor: "#22c55e", borderWidth: 1.6, borderDash: [4, 3], pointRadius: 0, tension: 0.3 },
        { label: "현재 실적", data: curHistN, borderColor: "#e879f9", borderWidth: 1.6, pointRadius: 0, tension: 0.3 },
        { label: "현재 전망", data: curForeN, borderColor: "#a855f7", borderWidth: 1.6, borderDash: [4, 3], pointRadius: 0, tension: 0.3 },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { display: true, position: "bottom", labels: { boxWidth: 8, font: { size: 9 }, padding: 6 } },
        tooltip: { enabled: false },
      },
      scales: { x: { display: false }, y: { display: false } },
    },
  });
}

/* ---------- 6개월 가격 시나리오 표 (대상월 | 기본 | 낙관 | 비관) ---------- */
function renderScenarioTable(f, sign) {
  const body = document.getElementById("scnBody");
  const base = f.monthly_forecast_base || [];
  const bull = f.monthly_forecast_bull || [];
  const bear = f.monthly_forecast_bear || [];
  const fb = {
    base: "매크로·수급 변동에 따른 기준 경로",
    bull: "상방 리스크(공급 차질·수요 서프라이즈) 현실화 시",
    bear: "하방 리스크(수요 둔화·재고 증가) 현실화 시",
  };
  const cell = (r, k) =>
    `<td class="mono">${r && r.price != null ? sign + fmtNum(r.price) : "—"}</td>
     <td class="why">${escapeHtml((r && r.rationale) || fb[k])}</td>`;

  body.innerHTML = base
    .map(
      (b, i) => `<tr>
        <td class="col-month"><span class="badge secondary">${escapeHtml(b.month || "")} (E)</span></td>
        ${cell(b, "base")}
        ${cell(bull[i], "bull")}
        ${cell(bear[i], "bear")}
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
