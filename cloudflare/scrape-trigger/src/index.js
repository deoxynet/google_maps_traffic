// Starts the Traffic Scraper workflow (.github/workflows/scrape.yml) on a fixed schedule.
// GitHub's own cron for it fires rarely (once in ~20 h on 2026-10-28, none of 8 slots in a 2 h watch),
// while workflow_dispatch runs start within seconds. The scrape itself still runs on GitHub's runners.
//
// Secret GH_TOKEN: a fine-grained PAT limited to this repo with Actions: read and write.
const API = "https://api.github.com";

function gh(env, method, path, body) {
  return fetch(`${API}/repos/${env.REPO}/actions/workflows/${env.WORKFLOW}${path}`, {
    method,
    headers: {
      Authorization: `Bearer ${env.GH_TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "gmaps-traffic-scrape-trigger",
    },
    body: body ? JSON.stringify(body) : undefined,
  });
}

async function dispatch(env) {
  const res = await gh(env, "POST", "/dispatches", { ref: env.REF });
  return { status: res.status, text: res.status === 204 ? "" : await res.text() };
}

export default {
  async scheduled(event, env) {
    let r = await dispatch(env);
    if (r.status !== 204 && /disabled/i.test(r.text)) {
      // GitHub disables workflows after 60 days without repo activity; re-enable and retry once
      await gh(env, "PUT", "/enable");
      r = await dispatch(env);
    }
    if (r.status !== 204) {
      // a thrown error marks this cron run as failed in the Cloudflare dashboard
      throw new Error(`dispatch failed: HTTP ${r.status} ${r.text.slice(0, 300)}`);
    }
  },
};
