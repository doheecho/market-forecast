/**
 * market-forecast 워크플로 트리거용 Cloudflare Worker (무료 티어).
 *
 * GET /dispatch?wf=run  → GitHub Actions "Market Forecasting" (run.yml) 실행
 *   (대시보드의 "↻ AI 분석 갱신" 버튼이 이 경로를 호출)
 *
 * 배포:
 *   npm i -g wrangler
 *   cd proxy && wrangler deploy
 *   wrangler secret put GH_DISPATCH_TOKEN   # fine-grained PAT · 이 리포 · Actions: Read and write
 * 배포 후 나온 URL 을 dashboard.js 의 PROXY_BASE 에 넣는다.
 */

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, OPTIONS",
  "Access-Control-Allow-Headers": "*",
  "Cache-Control": "no-store",
};

const WF_FILE = { run: "run.yml" };
const DEFAULT_REPO = "doheecho/market-forecast";

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") return new Response(null, { headers: CORS });

    const url = new URL(request.url);
    if (url.pathname.replace(/\/+$/, "") !== "/dispatch") {
      return json({ error: "use /dispatch?wf=run" }, 404);
    }

    const wf = url.searchParams.get("wf") || "run";
    const file = WF_FILE[wf];
    if (!file) return json({ error: `unknown workflow: ${wf}` }, 400);

    const token = env && env.GH_DISPATCH_TOKEN;
    const repo = (env && env.GH_REPO) || DEFAULT_REPO;
    if (!token) {
      return json({ error: "GH_DISPATCH_TOKEN 미설정 — wrangler secret put GH_DISPATCH_TOKEN" }, 501);
    }

    let res;
    try {
      res = await fetch(
        `https://api.github.com/repos/${repo}/actions/workflows/${file}/dispatches`,
        {
          method: "POST",
          headers: {
            Authorization: `Bearer ${token}`,
            Accept: "application/vnd.github+json",
            "User-Agent": "market-forecast-proxy",
            "X-GitHub-Api-Version": "2022-11-28",
          },
          body: JSON.stringify({ ref: "main" }),
        }
      );
    } catch (e) {
      return json({ ok: false, error: `github fetch 실패: ${String((e && e.message) || e)}` }, 502);
    }

    if (res.status === 204) return json({ ok: true, status: 204, repo, workflow: file }, 200);
    const detail = await res.text().catch(() => "");
    return json({ ok: false, status: res.status, repo, workflow: file, detail: detail.slice(0, 400) }, 502);
  },
};

function json(obj, status) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", ...CORS },
  });
}
