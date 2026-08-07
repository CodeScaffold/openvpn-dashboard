// Demo mode: intercepts window.fetch so the real, unmodified app.js runs
// against fake-but-evolving data instead of a real backend. No server, no
// build step — this is what makes a live demo possible as a static GitHub
// Pages site. Load this BEFORE app.js.
//
// Root-relative fetch() calls like fetch("/api/clients") are intercepted
// here regardless of what subpath this page itself is served from, because
// we replace window.fetch before app.js ever calls it — the browser never
// sees a real HTTP request. The two places app.js triggers a real page
// navigation instead of a fetch (downloading a .ovpn file) can't be mocked
// this way; see the click-guard at the bottom.
(() => {
  "use strict";

  const now = () => Date.now() / 1000;
  const GB = 1_000_000_000;

  // ---------- mutable demo state ----------

  const state = {
    clients: [
      {
        name: "alice-phone", created: iso(daysAgo(41)), expires: iso(daysFromNow(324)),
        max_connections: 2, quota_bytes: 5 * GB, used_bytes: 3.1 * GB,
        bandwidth_kbps: null, disabled: false, online: true,
      },
      {
        name: "bob-laptop", created: iso(daysAgo(19)), expires: iso(daysFromNow(346)),
        max_connections: 1, quota_bytes: null, used_bytes: 41.7 * GB,
        bandwidth_kbps: 20000, disabled: false, online: false,
      },
      {
        name: "work-desktop", created: iso(daysAgo(6)), expires: iso(daysFromNow(359)),
        max_connections: 1, quota_bytes: null, used_bytes: 2.4 * GB,
        bandwidth_kbps: null, disabled: false, online: true,
      },
      {
        name: "guest-tablet", created: iso(daysAgo(88)), expires: iso(daysFromNow(2)),
        max_connections: 1, quota_bytes: 2 * GB, used_bytes: 2 * GB,
        bandwidth_kbps: null, disabled: true, online: false,
      },
    ],
    endpoints: [
      { name: "server", port: 1194, protocol: "udp", cipher: "AES-128-GCM", dns1: "1.1.1.1", dns2: "1.0.0.1", created: iso(daysAgo(120)) },
      { name: "stealth-443", port: 443, protocol: "tcp", cipher: "CHACHA20-POLY1305", dns1: "1.1.1.1", dns2: "1.0.0.1", created: iso(daysAgo(30)) },
    ],
    settings: {
      session_timeout_minutes: 0, lockout_max_attempts: 5, lockout_seconds: 300,
      notify_webhook_enabled: false, notify_webhook_url: "",
      notify_telegram_enabled: false, notify_telegram_bot_token: "", notify_telegram_chat_id: "",
      client_default_max_connections: 1, client_default_quota_gb: null,
      client_default_bandwidth_mbps: null, client_default_validity_days: 365,
      retention_points: 60, panel_version: "1.0.0",
      panel_repo: "https://github.com/CodeScaffold/openvpn-dashboard",
    },
  };

  function daysAgo(n) { return now() - n * 86400; }
  function daysFromNow(n) { return now() + n * 86400; }
  function iso(unixSeconds) { return new Date(unixSeconds * 1000).toISOString(); }

  // ---------- live-looking system metrics ----------

  const HISTORY_LEN = 60;
  const sys = { cpu: [], mem: [], swap: [], disk: [], net: [], conn: [] };
  let cpuBase = 8, memBase = 42, netBase = { rx: 40000, tx: 210000 };

  function seedHistory() {
    let t = now() - HISTORY_LEN * 15;
    for (let i = 0; i < HISTORY_LEN; i++) {
      t += 15;
      tick(t);
    }
  }

  function tick(t = now()) {
    cpuBase = clamp(cpuBase + (Math.random() - 0.5) * 6, 3, 65);
    memBase = clamp(memBase + (Math.random() - 0.5) * 1.5, 30, 70);
    const swapPct = 9 + Math.sin(t / 400) * 1.5;
    const diskPct = 46;
    netBase.rx = clamp(netBase.rx + (Math.random() - 0.5) * 30000, 3000, 400000);
    netBase.tx = clamp(netBase.tx + (Math.random() - 0.5) * 60000, 5000, 900000);
    const tcp = state.clients.filter((c) => c.online).length;
    const udp = 0;

    sys.cpu.push({ t, v: round1(cpuBase) });
    sys.mem.push({ t, v: round1(memBase) });
    sys.swap.push({ t, v: round1(swapPct) });
    sys.disk.push({ t, v: diskPct });
    sys.net.push({ t, rx_bps: netBase.rx, tx_bps: netBase.tx });
    sys.conn.push({ t, tcp, udp });

    for (const k of Object.keys(sys)) if (sys[k].length > HISTORY_LEN) sys[k].shift();
  }
  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }
  function round1(v) { return Math.round(v * 10) / 10; }

  seedHistory();
  setInterval(tick, 4000); // faster than the real 15s poller — livelier for a demo

  function systemSnapshot() {
    const memTotal = 2_000_000_000, swapTotal = 2_147_000_000, diskTotal = 39_000_000_000;
    return {
      cpu_percent: sys.cpu.at(-1).v,
      memory: { total: memTotal, used: Math.round((memBase / 100) * memTotal) },
      swap: { total: swapTotal, used: Math.round((sys.swap.at(-1).v / 100) * swapTotal) },
      disk: { total: diskTotal, used: Math.round((sys.disk.at(-1).v / 100) * diskTotal) },
      uptime_seconds: 14 * 86400 + 3600 * 6,
      net_totals: { rx: 7_100_000_000, tx: 6_800_000_000 },
      network_history: sys.net, cpu_history: sys.cpu, mem_history: sys.mem,
      swap_history: sys.swap, disk_history: sys.disk, conn_history: sys.conn,
      process_mem_bytes: 54_000_000,
      openvpn_uptime_seconds: 14 * 86400 + 1500,
      vpn_endpoint: "vpn.example.com",
    };
  }

  function clientView(c) {
    return {
      name: c.name, created: c.created, expires: c.expires,
      max_connections: c.max_connections, quota_bytes: c.quota_bytes,
      used_bytes: c.used_bytes, bandwidth_kbps: c.bandwidth_kbps, disabled: c.disabled,
    };
  }

  function endpointView(e) {
    return {
      name: e.name, port: e.port, protocol: e.protocol, cipher: e.cipher,
      dns1: e.dns1, dns2: e.dns2, active: true,
      online: state.clients.filter((c) => c.online).length, created: e.created,
    };
  }

  function sessionsView() {
    return state.clients.filter((c) => c.online).map((c, i) => ({
      name: c.name, real_address: `203.0.113.${10 + i}`, virtual_address: `10.9.0.${10 + i}`,
      bytes_received: Math.round(c.used_bytes * 0.4), bytes_sent: Math.round(c.used_bytes * 0.6),
      connected_since: iso(now() - (1800 + i * 900)), client_id: String(i + 1), endpoint: "server",
    }));
  }

  // ---------- fetch mock ----------

  function jsonResponse(body, status = 200) {
    return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
  }

  const realFetch = window.fetch.bind(window);

  window.fetch = async (url, opts = {}) => {
    const path = new URL(url, location.href).pathname.replace(/^.*\/(api\/.*)$/, "/$1");
    const method = (opts.method || "GET").toUpperCase();
    let body = {};
    try { if (opts.body) body = JSON.parse(opts.body); } catch { /* ignore */ }

    await new Promise((r) => setTimeout(r, 120 + Math.random() * 180)); // feels like a real network

    if (method === "POST" && path === "/api/login") return jsonResponse({ ok: true });
    if (method === "POST" && path === "/api/logout") return jsonResponse({ ok: true });

    if (method === "GET" && path === "/api/clients") return jsonResponse(state.clients.map(clientView));
    if (method === "GET" && path === "/api/endpoints") return jsonResponse(state.endpoints.map(endpointView));
    if (method === "GET" && path === "/api/status") return jsonResponse(sessionsView());
    if (method === "GET" && path === "/api/system") return jsonResponse(systemSnapshot());
    if (method === "GET" && path === "/api/settings") return jsonResponse(state.settings);

    if (method === "POST" && path === "/api/clients") {
      if (state.clients.some((c) => c.name === body.name)) {
        return jsonResponse({ detail: "A client with that name already exists." }, 409);
      }
      const days = body.validity_days || 365;
      const c = {
        name: body.name, created: iso(now()), expires: iso(daysFromNow(days)),
        max_connections: body.max_connections || 1,
        quota_bytes: body.quota_gb ? body.quota_gb * GB : null,
        used_bytes: 0, bandwidth_kbps: body.bandwidth_mbps ? body.bandwidth_mbps * 1000 : null,
        disabled: false, online: false,
      };
      state.clients.push(c);
      return jsonResponse(clientView(c), 201);
    }

    let m;
    if ((m = path.match(/^\/api\/clients\/([^/]+)\/limits$/)) && method === "PUT") {
      const c = state.clients.find((x) => x.name === decodeURIComponent(m[1]));
      if (!c) return jsonResponse({ detail: "Client not found." }, 404);
      c.max_connections = body.max_connections || 1;
      c.quota_bytes = body.quota_gb ? body.quota_gb * GB : null;
      c.bandwidth_kbps = body.bandwidth_mbps ? body.bandwidth_mbps * 1000 : null;
      return jsonResponse(clientView(c));
    }
    if ((m = path.match(/^\/api\/clients\/([^/]+)\/reset-usage$/)) && method === "POST") {
      const c = state.clients.find((x) => x.name === decodeURIComponent(m[1]));
      if (!c) return jsonResponse({ detail: "Client not found." }, 404);
      c.used_bytes = 0; c.disabled = false;
      return jsonResponse(clientView(c));
    }
    if ((m = path.match(/^\/api\/clients\/([^/]+)\/renew$/)) && method === "POST") {
      const c = state.clients.find((x) => x.name === decodeURIComponent(m[1]));
      if (!c) return jsonResponse({ detail: "Client not found." }, 404);
      c.expires = iso(daysFromNow(body.validity_days || 365));
      return jsonResponse(clientView(c));
    }
    if ((m = path.match(/^\/api\/clients\/([^/]+)$/)) && method === "DELETE") {
      const name = decodeURIComponent(m[1]);
      state.clients = state.clients.filter((x) => x.name !== name);
      return jsonResponse({ ok: true });
    }

    if (method === "POST" && path === "/api/endpoints") {
      const e = { name: body.name, port: body.port, protocol: body.protocol, cipher: body.cipher, dns1: body.dns1 || "", dns2: body.dns2 || "", created: iso(now()) };
      state.endpoints.push(e);
      return jsonResponse(endpointView(e), 201);
    }
    if ((m = path.match(/^\/api\/endpoints\/([^/]+)$/)) && method === "DELETE") {
      state.endpoints = state.endpoints.filter((x) => x.name !== decodeURIComponent(m[1]));
      return jsonResponse({ ok: true });
    }
    if (path.match(/^\/api\/endpoints\/[^/]+\/(restart|stop|start)$/) && method === "POST") {
      return jsonResponse({ ok: true });
    }
    if (path.match(/^\/api\/endpoints\/[^/]+\/logs$/)) {
      return jsonResponse({ logs: demoLogLines() });
    }
    if (path.match(/^\/api\/endpoints\/[^/]+\/config$/)) {
      return jsonResponse({ config: "port 1194\nproto udp\ndev tun\n... (demo — see the README for the real file)" });
    }

    if (method === "PUT" && path.startsWith("/api/settings/")) {
      const key = path.split("/").pop();
      const map = {
        security: ["session_timeout_minutes", "lockout_max_attempts", "lockout_seconds"],
        notifications: ["notify_webhook_enabled", "notify_webhook_url", "notify_telegram_enabled", "notify_telegram_bot_token", "notify_telegram_chat_id"],
        "client-defaults": ["client_default_max_connections", "client_default_quota_gb", "client_default_bandwidth_mbps", "client_default_validity_days"],
        retention: [],
      };
      if (key === "retention") { state.settings.retention_points = body.points; return jsonResponse({ ok: true }); }
      if (key === "password") return jsonResponse({ ok: true });
      (map[key] || []).forEach((f) => { if (f in body) state.settings[f] = body[f]; });
      return jsonResponse({ ok: true });
    }
    if (method === "POST" && path === "/api/settings/logout-others") return jsonResponse({ ok: true });
    if (method === "POST" && path === "/api/settings/notifications/test") return jsonResponse({ ok: true });

    if (path === "/api/backup" || path === "/api/backup/restore") {
      return jsonResponse({ detail: "Backup/restore isn't available in the demo — clone the repo to try it for real." }, 400);
    }

    return jsonResponse({ ok: true });
  };

  function demoLogLines() {
    const lines = [];
    for (let i = 0; i < 12; i++) {
      const t = new Date((now() - i * 45) * 1000).toISOString();
      lines.push(`${t} demo-host openvpn[1]: MANAGEMENT: Client ${i % 3 === 0 ? "connected from" : "disconnected"}`);
    }
    return lines.reverse().join("\n");
  }

  // ---------- guard the two real-navigation download actions ----------
  // app.js downloads a .ovpn via `window.location.href = "/api/.../download"`
  // for these two flows — a real navigation, not a fetch, so it can't be
  // mocked the same way. Block just the file-producing endpoints and say so.

  document.addEventListener(
    "click",
    (e) => {
      const el = e.target.closest("#backup-download, .download-item");
      if (!el) return;
      e.preventDefault();
      e.stopImmediatePropagation();
      flashToast("Downloads aren't available in the demo — clone the repo to try this for real.");
    },
    true
  );

  function flashToast(message) {
    const toast = document.getElementById("toast");
    if (!toast) return;
    toast.textContent = message;
    toast.dataset.variant = "error";
    toast.hidden = false;
    clearTimeout(flashToast._t);
    flashToast._t = setTimeout(() => { toast.hidden = true; }, 3200);
  }
})();
