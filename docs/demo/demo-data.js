// Demo mode: intercepts window.fetch so the real, unmodified app.js runs
// against fake-but-evolving data instead of a real backend. No server, no
// build step — this is what makes a live demo possible as a static GitHub
// Pages site. Load this BEFORE app.js.
//
// Root-relative fetch() calls like fetch("/api/servers") are intercepted
// here regardless of what subpath this page itself is served from, because
// we replace window.fetch before app.js ever calls it — the browser never
// sees a real HTTP request. The two places app.js triggers a real page
// navigation instead of a fetch (downloading a .ovpn file) can't be mocked
// this way; see the click-guard at the bottom. Demo data always has 2+
// endpoints per server so app.js's "auto-navigate when there's only one
// endpoint" shortcut never fires — every download goes through the picker
// dialog, which IS click-guardable.
(() => {
  "use strict";

  const now = () => Date.now() / 1000;
  const GB = 1_000_000_000;

  function daysAgo(n) { return now() - n * 86400; }
  function daysFromNow(n) { return now() + n * 86400; }
  function iso(unixSeconds) { return new Date(unixSeconds * 1000).toISOString(); }

  // ---------- mutable demo state — two servers, one online, one not ----------
  // Independent PKI per server is the whole point of the real product, so
  // each server gets its own client/endpoint lists here too.

  const state = {
    servers: [
      {
        id: 1, name: "de-frankfurt-1", label: "Frankfurt", online: true,
        agent_version: "1.0.0", hostname: "vpn-fra-1", public_addr: "203.0.113.5",
        vpn_endpoint: "vpn.example.com", last_seen: iso(now()), created: iso(daysAgo(120)),
      },
      {
        id: 2, name: "us-newyork-1", label: "New York", online: false,
        agent_version: "1.0.0", hostname: "vpn-nyc-1", public_addr: "198.51.100.9",
        vpn_endpoint: "vpn2.example.com", last_seen: iso(daysAgo(2)), created: iso(daysAgo(60)),
      },
    ],
    clientsByServer: {
      1: [
        {
          name: "alice-phone", team: "sales", created: iso(daysAgo(41)), expires: iso(daysFromNow(324)),
          max_connections: 2, quota_bytes: 5 * GB, used_bytes: 3.1 * GB,
          bandwidth_kbps: null, disabled: false, online: true,
          static_ips: { server: { static_ip: "10.9.0.50", pinned: true } },
        },
        {
          name: "bob-laptop", team: "sales", created: iso(daysAgo(19)), expires: iso(daysFromNow(346)),
          max_connections: 1, quota_bytes: null, used_bytes: 41.7 * GB,
          bandwidth_kbps: 20000, disabled: false, online: false, static_ips: {},
        },
        {
          name: "work-desktop", team: null, created: iso(daysAgo(6)), expires: iso(daysFromNow(359)),
          max_connections: 1, quota_bytes: null, used_bytes: 2.4 * GB,
          bandwidth_kbps: null, disabled: false, online: true, static_ips: {},
        },
        {
          name: "guest-tablet", team: null, created: iso(daysAgo(88)), expires: iso(daysFromNow(2)),
          max_connections: 1, quota_bytes: 2 * GB, used_bytes: 2 * GB,
          bandwidth_kbps: null, disabled: true, online: false, static_ips: {},
        },
      ],
      2: [
        {
          name: "ny-office-router", team: "ops", created: iso(daysAgo(55)), expires: iso(daysFromNow(310)),
          max_connections: 3, quota_bytes: null, used_bytes: 88 * GB,
          bandwidth_kbps: null, disabled: false, online: false,
          static_ips: { server: { static_ip: "10.9.0.10", pinned: true } },
        },
      ],
    },
    endpointsByServer: {
      1: [
        { name: "server", port: 1194, protocol: "udp", cipher: "AES-128-GCM", dns1: "1.1.1.1", dns2: "1.0.0.1", created: iso(daysAgo(120)) },
        { name: "stealth-443", port: 443, protocol: "tcp", cipher: "CHACHA20-POLY1305", dns1: "1.1.1.1", dns2: "1.0.0.1", created: iso(daysAgo(30)) },
      ],
      2: [
        { name: "server", port: 1194, protocol: "udp", cipher: "AES-256-GCM", dns1: "1.1.1.1", dns2: "1.0.0.1", created: iso(daysAgo(60)) },
        { name: "tcp-8443", port: 8443, protocol: "tcp", cipher: "AES-256-GCM", dns1: "1.1.1.1", dns2: "1.0.0.1", created: iso(daysAgo(45)) },
      ],
    },
    settings: {
      session_timeout_minutes: 0, lockout_max_attempts: 5, lockout_seconds: 300,
      notify_webhook_enabled: false, notify_webhook_url: "",
      notify_telegram_enabled: false, notify_telegram_bot_token: "", notify_telegram_chat_id: "",
      client_default_max_connections: 1, client_default_quota_gb: null,
      client_default_bandwidth_mbps: null, client_default_validity_days: 365,
      retention_points: 60, panel_version: "2.0.0",
      panel_repo: "https://github.com/CodeScaffold/openvpn-dashboard",
    },
  };

  let nextServerId = 3;

  // ---------- live-looking system metrics (server 1 only — server 2 is offline) ----------

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
    const tcp = state.clientsByServer[1].filter((c) => c.online).length;
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

  const MEM_TOTAL = 2_000_000_000, SWAP_TOTAL = 2_147_000_000, DISK_TOTAL = 39_000_000_000;

  // Matches the real /api/servers/{id}/system shape: {online, latest, history, vpn_endpoint}.
  // "latest" carries the full raw telemetry snapshot (only available while online, same as
  // the real hub); "history" is the persisted per-point mirror (flat metric columns).
  function systemPayload(server) {
    if (!server.online) {
      return { online: false, latest: null, history: [], vpn_endpoint: server.vpn_endpoint };
    }
    const history = sys.cpu.map((_, i) => ({
      t: sys.cpu[i].t, cpu_percent: sys.cpu[i].v,
      mem_used: Math.round((sys.mem[i].v / 100) * MEM_TOTAL), mem_total: MEM_TOTAL,
      swap_used: Math.round((sys.swap[i].v / 100) * SWAP_TOTAL), swap_total: SWAP_TOTAL,
      disk_used: Math.round((sys.disk[i].v / 100) * DISK_TOTAL), disk_total: DISK_TOTAL,
      net_rx_bps: sys.net[i].rx_bps, net_tx_bps: sys.net[i].tx_bps,
      tcp_count: sys.conn[i].tcp, udp_count: sys.conn[i].udp,
    }));
    const last = history[history.length - 1];
    const latest = {
      ...last,
      memory: { used: last.mem_used, total: last.mem_total },
      swap: { used: last.swap_used, total: last.swap_total },
      disk: { used: last.disk_used, total: last.disk_total },
      net_totals: { rx: 7_100_000_000, tx: 6_800_000_000 },
      uptime_seconds: 14 * 86400 + 3600 * 6,
      process_mem_bytes: 54_000_000,
    };
    return { online: true, latest, history, vpn_endpoint: server.vpn_endpoint };
  }

  function clientView(c) {
    return {
      name: c.name, team: c.team, created: c.created, expires: c.expires,
      max_connections: c.max_connections, quota_bytes: c.quota_bytes,
      used_bytes: c.used_bytes, bandwidth_kbps: c.bandwidth_kbps, disabled: c.disabled,
      static_ips: c.static_ips || {},
    };
  }

  function endpointView(e, clients) {
    return {
      name: e.name, port: e.port, protocol: e.protocol, cipher: e.cipher,
      dns1: e.dns1, dns2: e.dns2, active: true,
      online: clients.filter((c) => c.online).length, created: e.created,
    };
  }

  function sessionsView(clients) {
    return clients.filter((c) => c.online).map((c, i) => ({
      name: c.name, real_address: `203.0.113.${10 + i}`, virtual_address: `10.9.0.${10 + i}`,
      bytes_received: Math.round(c.used_bytes * 0.4), bytes_sent: Math.round(c.used_bytes * 0.6),
      connected_since: iso(now() - (1800 + i * 900)), client_id: String(i + 1), endpoint: "server",
    }));
  }

  // ---------- fetch mock ----------

  function jsonResponse(body, status = 200) {
    return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
  }

  function csvResponse(rows) {
    const text = rows.map((r) => r.map((v) => `"${String(v).replace(/"/g, '""')}"`).join(",")).join("\n");
    return new Response(text, { status: 200, headers: { "Content-Type": "text/csv" } });
  }

  window.fetch = async (url, opts = {}) => {
    const path = new URL(url, location.href).pathname.replace(/^.*\/(api\/.*)$/, "/$1");
    const method = (opts.method || "GET").toUpperCase();
    let body = {};
    try { if (opts.body) body = JSON.parse(opts.body); } catch { /* ignore */ }

    await new Promise((r) => setTimeout(r, 120 + Math.random() * 180)); // feels like a real network

    if (method === "POST" && path === "/api/login") return jsonResponse({ ok: true });
    if (method === "POST" && path === "/api/logout") return jsonResponse({ ok: true });
    if (method === "GET" && path === "/api/settings") return jsonResponse(state.settings);

    // ---- servers (hub-level) ----
    if (method === "GET" && path === "/api/servers") {
      return jsonResponse(state.servers.map((s) => ({ ...s })));
    }
    if (method === "POST" && path === "/api/servers") {
      const s = {
        id: nextServerId++, name: body.name, label: body.label || "", online: false,
        agent_version: null, hostname: null, public_addr: null,
        vpn_endpoint: body.vpn_endpoint, last_seen: null, created: iso(now()),
      };
      state.servers.push(s);
      state.clientsByServer[s.id] = [];
      state.endpointsByServer[s.id] = [
        { name: "server", port: 1194, protocol: "udp", cipher: "AES-128-GCM", dns1: "1.1.1.1", dns2: "1.0.0.1", created: iso(now()) },
        { name: "backup-443", port: 443, protocol: "tcp", cipher: "AES-256-GCM", dns1: "1.1.1.1", dns2: "1.0.0.1", created: iso(now()) },
      ];
      return jsonResponse({ id: s.id, name: s.name, install_token: "demo-token-not-real-" + Math.random().toString(36).slice(2, 10) }, 201);
    }

    let m;
    if ((m = path.match(/^\/api\/servers\/(\d+)$/)) && method === "PUT") {
      const s = state.servers.find((x) => x.id === Number(m[1]));
      if (!s) return jsonResponse({ detail: "Server not found." }, 404);
      if (state.servers.some((x) => x.id !== s.id && x.name === body.name)) {
        return jsonResponse({ detail: `A server named "${body.name}" already exists.` }, 400);
      }
      s.name = body.name;
      s.label = body.label || "";
      s.vpn_endpoint = body.vpn_endpoint;
      return jsonResponse({ ok: true });
    }
    if ((m = path.match(/^\/api\/servers\/(\d+)$/)) && method === "DELETE") {
      const id = Number(m[1]);
      state.servers = state.servers.filter((x) => x.id !== id);
      delete state.clientsByServer[id];
      delete state.endpointsByServer[id];
      return jsonResponse({ ok: true });
    }

    // ---- everything else is server-scoped: /api/servers/{id}/... ----
    const scoped = path.match(/^\/api\/servers\/(\d+)(\/.*)$/);
    if (!scoped) return jsonResponse({ ok: true });
    const serverId = Number(scoped[1]);
    const rest = scoped[2];
    const server = state.servers.find((x) => x.id === serverId);
    if (!server) return jsonResponse({ detail: "Server not found." }, 404);
    const clients = state.clientsByServer[serverId] || [];
    const endpoints = state.endpointsByServer[serverId] || [];
    const offline = () => jsonResponse({ detail: "Server is not currently connected." }, 503);

    if (method === "GET" && rest === "/system") return jsonResponse(systemPayload(server));
    if (method === "GET" && rest === "/status") return jsonResponse(server.online ? sessionsView(clients) : []);
    if (method === "GET" && rest === "/clients") {
      return jsonResponse(server.online ? clients.map(clientView) : clients.map((c) => ({ ...clientView(c), stale: true })));
    }
    if (method === "GET" && rest === "/endpoints") {
      const views = endpoints.map((e) => endpointView(e, clients));
      return jsonResponse(server.online ? views : views.map((v) => ({ ...v, stale: true })));
    }
    if (method === "GET" && rest === "/clients/export") {
      const days = (c) => Math.round((new Date(c.expires) - new Date(c.created)) / 86400000);
      return csvResponse([
        ["name", "label", "team", "max_connections", "quota_gb", "bandwidth_mbps", "validity_days"],
        ...clients.map((c) => [
          c.name, c.label || "", c.team || "", c.max_connections,
          c.quota_bytes ? c.quota_bytes / GB : "", c.bandwidth_kbps ? c.bandwidth_kbps / 1000 : "", days(c),
        ]),
      ]);
    }
    if (method === "GET" && rest === "/endpoints/export") {
      return csvResponse([
        ["name", "port", "protocol", "cipher", "dns1", "dns2"],
        ...endpoints.map((e) => [e.name, e.port, e.protocol, e.cipher, e.dns1 || "", e.dns2 || ""]),
      ]);
    }
    if (method === "POST" && (rest === "/clients/import" || rest === "/endpoints/import")) {
      return jsonResponse({ created: [], failed: [{ name: "import", error: "Import isn't available in the demo — clone the repo to try this for real." }] });
    }

    if (method === "POST" && rest === "/clients") {
      if (!server.online) return offline();
      if (clients.some((c) => c.name === body.name)) {
        return jsonResponse({ detail: "A client with that name already exists." }, 409);
      }
      if (body.name === "server") return jsonResponse({ detail: "That name is reserved." }, 400);
      const days = body.validity_days || 365;
      const c = {
        name: body.name, team: body.team || null, created: iso(now()), expires: iso(daysFromNow(days)),
        max_connections: body.max_connections || 1,
        quota_bytes: body.quota_gb ? body.quota_gb * GB : null,
        used_bytes: 0, bandwidth_kbps: body.bandwidth_mbps ? body.bandwidth_mbps * 1000 : null,
        disabled: false, online: false, static_ips: {},
      };
      clients.push(c);
      return jsonResponse(clientView(c), 201);
    }

    if ((m = rest.match(/^\/clients\/([^/]+)\/download$/)) && method === "GET") {
      if (!server.online) return offline();
      return jsonResponse({ detail: "Downloads aren't available in the demo — clone the repo to try this for real." }, 400);
    }
    if ((m = rest.match(/^\/clients\/([^/]+)\/limits$/)) && method === "PUT") {
      if (!server.online) return offline();
      const c = clients.find((x) => x.name === decodeURIComponent(m[1]));
      if (!c) return jsonResponse({ detail: "Client not found." }, 404);
      c.max_connections = body.max_connections || 1;
      c.quota_bytes = body.quota_gb ? body.quota_gb * GB : null;
      c.bandwidth_kbps = body.bandwidth_mbps ? body.bandwidth_mbps * 1000 : null;
      if ("team" in body) c.team = body.team;
      return jsonResponse(clientView(c));
    }
    if ((m = rest.match(/^\/clients\/([^/]+)\/static-ip$/)) && method === "PUT") {
      if (!server.online) return offline();
      const c = clients.find((x) => x.name === decodeURIComponent(m[1]));
      if (!c) return jsonResponse({ detail: "Client not found." }, 404);
      const ep = endpoints.find((e) => e.name === body.endpoint);
      if (!ep) return jsonResponse({ detail: "Endpoint not found." }, 404);
      c.static_ips = c.static_ips || {};
      if (body.static_ip) {
        const owner = clients.find((o) => o !== c && o.static_ips && o.static_ips[body.endpoint] && o.static_ips[body.endpoint].static_ip === body.static_ip);
        if (owner) return jsonResponse({ detail: `${body.static_ip} is already assigned to client "${owner.name}" on this endpoint.` }, 409);
        c.static_ips[body.endpoint] = { static_ip: body.static_ip, pinned: true };
      } else {
        delete c.static_ips[body.endpoint];
      }
      return jsonResponse(clientView(c));
    }
    if ((m = rest.match(/^\/clients\/([^/]+)\/reset-usage$/)) && method === "POST") {
      if (!server.online) return offline();
      const c = clients.find((x) => x.name === decodeURIComponent(m[1]));
      if (!c) return jsonResponse({ detail: "Client not found." }, 404);
      c.used_bytes = 0; c.disabled = false;
      return jsonResponse(clientView(c));
    }
    if ((m = rest.match(/^\/clients\/([^/]+)\/renew$/)) && method === "POST") {
      if (!server.online) return offline();
      const c = clients.find((x) => x.name === decodeURIComponent(m[1]));
      if (!c) return jsonResponse({ detail: "Client not found." }, 404);
      c.expires = iso(daysFromNow(body.validity_days || 365));
      return jsonResponse(clientView(c));
    }
    if ((m = rest.match(/^\/clients\/([^/]+)$/)) && method === "DELETE") {
      if (!server.online) return offline();
      const name = decodeURIComponent(m[1]);
      state.clientsByServer[serverId] = clients.filter((x) => x.name !== name);
      return jsonResponse({ ok: true });
    }

    if (method === "POST" && rest === "/endpoints") {
      if (!server.online) return offline();
      const e = { name: body.name, port: body.port, protocol: body.protocol, cipher: body.cipher, dns1: body.dns1 || "", dns2: body.dns2 || "", created: iso(now()) };
      endpoints.push(e);
      return jsonResponse(endpointView(e, clients), 201);
    }
    if ((m = rest.match(/^\/endpoints\/([^/]+)$/)) && method === "DELETE") {
      if (!server.online) return offline();
      state.endpointsByServer[serverId] = endpoints.filter((x) => x.name !== decodeURIComponent(m[1]));
      return jsonResponse({ ok: true });
    }
    if (rest.match(/^\/endpoints\/[^/]+\/(restart|stop|start)$/) && method === "POST") {
      if (!server.online) return offline();
      return jsonResponse({ ok: true });
    }
    if (rest.match(/^\/endpoints\/[^/]+\/logs$/)) {
      if (!server.online) return offline();
      return jsonResponse({ logs: demoLogLines() });
    }
    if (rest.match(/^\/endpoints\/[^/]+\/config$/)) {
      if (!server.online) return offline();
      return jsonResponse({ config: "port 1194\nproto udp\ndev tun\n... (demo — see the README for the real file)" });
    }
    if (rest === "/backup" || rest === "/backup/restore") {
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
