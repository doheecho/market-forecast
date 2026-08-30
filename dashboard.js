/* 원자재 시황 대시보드
 * raw_materials_forecast.json (analyze.py 산출물) 을 로드해 원자재별로
 * KPI · 메인 차트(실적+6개월 전망 팬) · 요인지표 · 시나리오 · 과거 유사국면 · 근거표를 렌더.
 */
"use strict";

const JSON_LOCAL = "./raw_materials_forecast.json";
const JSON_RAW =
  "https://raw.githubusercontent.com/doheecho/market-forecast/main/raw_materials_forecast.json";

// 배포한 Cloudflare Worker 주소 (proxy/). 비우면 "AI 분석 갱신" 은 데이터 재조회만 함.
const PROXY_BASE = ""; // 예: "https://market-forecast-proxy.<subdomain>.workers.dev"

const ORDER = ["wti", "copper", "aluminum", "gold", "silver", "platinum",
  "steel", "ironore", "nickel", "zinc", "tungsten"];
const LABEL = {
  wti: "WTI 원유", copper: "전기동", aluminum: "알루미늄",
  gold: "금", silver: "은", platinum: "백금",
  steel: "열연강판", ironore: "철광석", nickel: "니켈", zinc: "아연", tungsten: "텅스텐",
};
// 현재가 카드에서 단위 옆에 표기할 시장/기준 (제목에서는 뺀다)
const VENUE = {
  wti: "CME", copper: "LME Cash", aluminum: "LME Cash",
  gold: "LBMA", silver: "LBMA", platinum: "LBMA / NYMEX",
  steel: "CME HRC", ironore: "CFR China", nickel: "LME Cash", zinc: "LME Cash",
  tungsten: "APT Europe",
};
// f.name / 라벨에서 괄호 부속(예: " (CME)") 제거
const stripVenue = (s) => String(s || "").replace(/\s*\([^)]*\)\s*/g, " ").trim();
const RANGES = [
  ["3M", 3], ["6M", 6], ["1Y", 12], ["2Y", 24], ["3Y", 36], ["5Y", 60], ["ALL", 0],
];

const state = { data: null, key: "wti", months: 12, analogIdx: 0, analogWin: 12, charts: {} };

/* ---------- 부트스트랩 ---------- */
if (window.Chart) {
  Chart.defaults.color = "#8b95a1";
  Chart.defaults.borderColor = "#2b333d40";
  Chart.defaults.font.family =
    '-apple-system, "Segoe UI", "Malgun Gothic", Roboto, sans-serif';
}
document.addEventListener("DOMContentLoaded", init);
document.getElementById("refreshBtn").addEventListener("click", () => load(true));

function setAsOf(d) {
  const px = d && d.prices_date;
  const fc = d && d.update_date;
  document.getElementById("asOf").textContent =
    px && fc && px !== fc ? `시세 ${px} · 전망 ${fc}`
    : `기준일 ${fc || px || "—"}`;
}
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
              setAsOf(d);
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
    setAsOf(data);
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
          escapeHtml(LABEL[k] || stripVenue(state.data.forecast_data[k].name) || k)
        }</button>`
    )
    .join("");
  nav.querySelectorAll("button").forEach((b) =>
    b.addEventListener("click", () => {
      state.key = b.dataset.key;
      state.analogIdx = 0;
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
  const venue = VENUE[state.key] || (String(f.name || "").match(/\(([^)]+)\)/) || [])[1] || "";

  // 현재가·전망을 '실적 마지막 정상값(spot)' 기준으로 재계산 → 차트와 KPI 일관.
  const hist = historyRows(state.key); // 이미 despike 됨
  const base = f.monthly_forecast_base || [];
  const spot =
    hist.length ? hist[hist.length - 1].price
    : Number(f.current_price) || null;
  state._spot = spot; // 메인 차트가 마지막 실적점을 이 값으로 맞춤
  const target = base.length ? Number(base[base.length - 1].price) : Number(f.forecast_6m_target);
  const rate = spot && target ? (target / spot - 1) * 100 : null;
  const rateStr = rate == null ? (f.forecast_change_rate || "") : `${rate > 0 ? "+" : ""}${rate.toFixed(1)}%`;
  const rateUp = rate == null ? !String(f.forecast_change_rate || "").startsWith("-") : rate >= 0;

  // AI Advisor 는 상단 고정 영역(#advisor)에 별도로 렌더 (스크롤해도 위치 그대로)
  const advisorText = stripAdvisorPrefix(f.advisor || f.planning_advisor || "").replace(/\s*\n\s*/g, " ").trim();
  const advEl = document.getElementById("advisor");
  if (advisorText) {
    advEl.innerHTML = `<span class="advisor-tag">AI Advisor</span> ${escapeHtml(advisorText)}`;
    advEl.hidden = false;
  } else {
    advEl.innerHTML = "";
    advEl.hidden = true;
  }

  document.getElementById("app").innerHTML = `
    <div class="cards">
      <div class="card">
        <div class="label">현재가</div>
        <div class="value">${sign}${fmtNum(spot ?? f.current_price)}</div>
        <div class="sub">${escapeHtml(f.unit || "")}${venue ? " · " + escapeHtml(venue) : ""}</div>
      </div>
      <div class="card">
        <div class="label">6개월 후 AI 가격전망</div>
        <div class="value ${rateUp ? "up" : "down"}">${sign}${fmtNum(target)}<span class="chip ${rateUp ? "up" : "down"}">${escapeHtml(rateStr)}</span></div>
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

  // 한 섹션이 터져도 나머지는 그리도록 각각 격리
  const safe = (name, fn) => {
    try { fn(); } catch (e) {
      console.error(`[render:${name}]`, e);
      const box = { chart: "mainChart", metrics: "metrics", scenarios: "scenarios",
        analogs: "analogs", table: "scnBody" }[name];
      const el = box && document.getElementById(box);
      if (el) el.innerHTML = `<div class="error" style="padding:16px">${name} 표시 오류: ${escapeHtml(String(e && e.message || e))}</div>`;
    }
  };
  safe("chart", drawMainChart);
  safe("metrics", () => renderMetrics(f));
  safe("scenarios", () => renderScenarios(f));
  safe("analogs", () => renderAnalogs(f, sign, rateStr));
  safe("table", () => renderScenarioTable(f, sign));
}

/* ---------- 메인 차트 ---------- */
function historyRows(key) {
  const d = state.data;
  return despike((d.history_3y || d.history || {})[key] || []);
}

/* 시계열 꼬리에 튄 값(야후 마지막 봉 오류 등) 잘라냄:
   직전 8개 중앙값 대비 ±12% 초과면 꼬리에서 제거 */
function despike(rows) {
  if (!Array.isArray(rows) || rows.length < 10) return rows || [];
  let end = rows.length;
  while (end > 9) {
    const ref = rows.slice(end - 9, end - 1).map((r) => Number(r.price)).filter(Number.isFinite).sort((x, y) => x - y);
    const med = ref[Math.floor(ref.length / 2)];
    const a = Number(rows[end - 1].price);
    if (med && Number.isFinite(a) && Math.abs(a / med - 1) > 0.12) end--;
    else break;
  }
  return end === rows.length ? rows : rows.slice(0, end);
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
  if (!hist.length) return;

  // 마지막 실적점 = 상단 KPI 현재가(state._spot)와 동일하게 강제
  const spot = state._spot != null ? state._spot : hist[hist.length - 1].price;
  const anchorX = hist[hist.length - 1].date;
  // 전망 월 라벨: 마지막 실적일 이후만, 오름차순 정렬 + 중복 제거 (AI 월 꼬임 방어)
  const anchorMs = new Date(anchorX).getTime();
  const fcMonths = [...new Set((f.monthly_forecast_base || []).map((r) => r.month))]
    .filter((m) => new Date(m + "-15").getTime() > anchorMs)
    .sort();
  const fcX = fcMonths.map((m) => m + "-15");
  const priceByMonth = (arr) => {
    const m = new Map();
    for (const r of arr || []) if (!m.has(r.month)) m.set(r.month, Number(r.price));
    return m;
  };

  // 4개 데이터셋 모두 같은 x 격자(실적일들 + 전망월들). 겹치지 않는 구간은 null → 툴팁에서 제외.
  const pT = (r, i, isLast) => ({ x: r.date, y: isLast ? spot : r.price });
  const histData = hist
    .map((r, i) => pT(r, i, i === hist.length - 1))
    .concat(fcX.map((x) => ({ x, y: null })));

  const line = (arr) => {
    const by = priceByMonth(arr);
    return hist
      .map((r, i) => ({ x: r.date, y: i === hist.length - 1 ? spot : null }))
      .concat(fcMonths.map((m, i) => ({ x: fcX[i], y: by.has(m) ? by.get(m) : null })));
  };

  const ds = [
    {
      label: "실적", data: histData, borderColor: "#22d3ee",
      backgroundColor: "rgba(34,211,238,0.08)", borderWidth: 1.6,
      pointRadius: 0, fill: true, tension: 0.25, order: 5, spanGaps: false,
    },
    {
      label: "Bull", data: line(f.monthly_forecast_bull), borderColor: "#ef4444",
      borderWidth: 1.4, borderDash: [4, 3], pointRadius: 0, fill: false, tension: 0.3, order: 3,
    },
    {
      label: "Bear", data: line(f.monthly_forecast_bear), borderColor: "#3b82f6",
      backgroundColor: "rgba(99,110,140,0.10)", borderWidth: 1.4, borderDash: [4, 3],
      pointRadius: 0, fill: "-1", tension: 0.3, order: 3,
    },
    {
      label: "Base", data: line(f.monthly_forecast_base), borderColor: "#f59e0b",
      borderWidth: 2.6, pointRadius: 0, fill: false, tension: 0.3, order: 1,
    },
  ];
  const anchor = { x: anchorX, y: spot };

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
          filter: (it) => {
            const y = it && (it.parsed && it.parsed.y != null ? it.parsed.y
              : it.raw && typeof it.raw === "object" ? it.raw.y : it.raw);
            return Number.isFinite(y);
          },
          callbacks: {
            label: (c) => (Number.isFinite(c.parsed.y) ? `${c.dataset.label}: ${sign}${fmtNum(c.parsed.y)}` : ""),
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

/* ---------- 과거 유사 국면 (1년/6개월 비교창 · 여러 개면 ①②… 로 전환) ---------- */
const CIRCLED = (i) => String.fromCharCode(0x2460 + i); // ①②③④⑤

function renderAnalogs(f, sign, rateStr) {
  const box = document.getElementById("analogs");
  const h3 = document.querySelector("#analogBlock h3");
  const has12 = (f.analogs || []).length;
  const has6 = (f.analogs_6m || []).length;
  // 선택한 비교창에 데이터가 없으면 있는 쪽으로 자동 전환
  if (state.analogWin === 6 && !has6 && has12) state.analogWin = 12;
  if (state.analogWin === 12 && !has12 && has6) state.analogWin = 6;
  const win = state.analogWin === 6 ? 6 : 12;
  const list = win === 6 ? f.analogs_6m || [] : f.analogs || [];

  const idx = Math.min(Math.max(state.analogIdx || 0, 0), Math.max(list.length - 1, 0));
  state.analogIdx = idx;

  // 헤더: 좌(제목 + 1년/6개월) · 우(①②③)
  if (h3) {
    h3.innerHTML =
      `<span class="ana-h-left">과거 유사 국면` +
      `<span class="ana-win">` +
      `<button data-w="12"${win === 12 ? ' class="on"' : ""}>1년</button>` +
      `<button data-w="6"${win === 6 ? ' class="on"' : ""}>6개월</button>` +
      `</span></span>` +
      (list.length > 1
        ? `<span class="analog-nav">` +
          list
            .map((_, i) => `<button data-i="${i}"${i === idx ? ' class="on"' : ""}>${CIRCLED(i)}</button>`)
            .join("") +
          `</span>`
        : "");
    h3.querySelector(".ana-win").addEventListener("click", (e) => {
      const b = e.target.closest("button[data-w]");
      if (!b || +b.dataset.w === win) return;
      state.analogWin = +b.dataset.w;
      state.analogIdx = 0;
      renderAnalogs(f, sign, rateStr);
    });
    h3.querySelector(".analog-nav")?.addEventListener("click", (e) => {
      const b = e.target.closest("button[data-i]");
      if (!b) return;
      state.analogIdx = +b.dataset.i;
      renderAnalogs(f, sign, rateStr);
    });
  }

  if (!list.length) {
    box.innerHTML = `<p class='src'>${win === 6 ? "6개월" : "1년"} 비교창 유사 국면 데이터 없음 (AI 분석 갱신 후 표시)</p>`;
    return;
  }

  const a = list[idx];
  const past = a.actual != null && a.actual !== "" ? a.actual : "—";
  const now = rateStr || f.forecast_change_rate || "—";
  box.innerHTML = `<div class="analog">
      <div class="head">
        <span class="title">${list.length > 1 ? CIRCLED(idx) + " " : ""}${escapeHtml(a.title || "유사 국면")}</span>
        <span class="badge success">유사도 ${escapeHtml(a.similarity || "-")}</span>
      </div>
      <div class="period">분석 기간 ${escapeHtml(a.period || "-")} · 최근 ${win === 6 ? "6" : "12"}개월 추이 상관도 기준</div>
      <p class="summary">${escapeHtml(a.summary || "")}</p>
      <div class="mini-box"><canvas id="mini0"></canvas></div>
      <div class="foot">
        <span class="kv"><small>이 국면 이후 6개월 실제</small><b>${escapeHtml(past)}</b></span>
        <span class="kv" style="text-align:right"><small>현재 모델 6개월 전망</small><b>${escapeHtml(now)}</b></span>
      </div>
    </div>`;

  const curMonthly = monthlyCloses(historyRows(state.key));
  const curBase = (f.monthly_forecast_base || []).map((r) => Number(r.price)).filter(Number.isFinite);
  drawMini(0, a, curMonthly, curBase);
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
  const cid = "mini" + i;
  if (state.charts[cid]) { try { state.charts[cid].destroy(); } catch (_) {} delete state.charts[cid]; }
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
  const ym = new Date().toISOString().slice(0, 7);
  const bySort = (a) =>
    [...(a || [])]
      .filter((r) => String(r.month) >= ym)              // 과거월 라벨 방어
      .sort((x, y) => String(x.month).localeCompare(String(y.month)));
  const base = bySort(f.monthly_forecast_base);
  const bMon = base.map((r) => r.month);
  const idx = (a) => {
    const m = new Map((a || []).map((r) => [r.month, r]));
    return bMon.map((mm) => m.get(mm) || null);
  };
  const bull = idx(f.monthly_forecast_bull);
  const bear = idx(f.monthly_forecast_bear);
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
