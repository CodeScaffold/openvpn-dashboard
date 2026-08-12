(() => {
  "use strict";

  const loginView = document.getElementById("login-view");
  const dashView = document.getElementById("dashboard-view");
  const loginForm = document.getElementById("login-form");
  const loginPassword = document.getElementById("login-password");
  const loginError = document.getElementById("login-error");
  const loginSubmit = document.getElementById("login-submit");
  const logoutBtn = document.getElementById("logout-btn");
  const toastEl = document.getElementById("toast");

  let activePage = "dashboard";
  let pollTimer = null;
  let toastTimer = null;

  // ---------- server context ----------
  // Every server-scoped API call is prefixed with /api/servers/{currentServerId}.
  // currentServerId persists across reloads via localStorage; allServers is the
  // cached list backing both the sidebar switcher and the Servers page.

  const SERVER_STORAGE_KEY = "ovpn_current_server_id";
  let allServers = [];
  let currentServerId = parseInt(localStorage.getItem(SERVER_STORAGE_KEY), 10) || null;
  let currentServer = null;

  function serverPath(suffix) {
    return `/api/servers/${currentServerId}${suffix}`;
  }

  function setCurrentServer(id) {
    currentServerId = id;
    if (id) localStorage.setItem(SERVER_STORAGE_KEY, String(id));
    else localStorage.removeItem(SERVER_STORAGE_KEY);
  }

  // ---------- helpers ----------

  function showToast(message, variant = "ok") {
    toastEl.textContent = message;
    toastEl.dataset.variant = variant;
    toastEl.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toastEl.hidden = true; }, 3200);
  }

  function setBusy(button, busy) {
    button.disabled = busy;
    button.querySelector(".btn-label").style.visibility = busy ? "hidden" : "visible";
    const spinner = button.querySelector(".spinner");
    if (spinner) spinner.hidden = !busy;
  }

  function formatClock(unixSeconds) {
    return new Date(unixSeconds * 1000).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }

  // ---------- validity period picker (preset months/years, or an explicit date) ----------

  const VALIDITY_PRESETS = ["30", "90", "180", "365", "730", "1825"];

  function wirePresetValidity(selectId, untilWrapId, untilId) {
    const select = document.getElementById(selectId);
    const wrap = document.getElementById(untilWrapId);
    const until = document.getElementById(untilId);
    const tomorrow = new Date(Date.now() + 86400000);
    until.min = tomorrow.toISOString().slice(0, 10);
    select.addEventListener("change", () => {
      wrap.hidden = select.value !== "custom";
      if (select.value === "custom" && !until.value) {
        const d = new Date();
        d.setFullYear(d.getFullYear() + 1);
        until.value = d.toISOString().slice(0, 10);
      }
    });
  }

  function presetToDays(selectId, untilId) {
    const select = document.getElementById(selectId);
    if (select.value !== "custom") return parseInt(select.value, 10);
    const until = document.getElementById(untilId).value;
    if (!until) return 365;
    const days = Math.ceil((new Date(`${until}T00:00:00`) - Date.now()) / 86400000);
    return Math.min(3650, Math.max(30, days));
  }

  function daysToPresetSelection(selectId, untilWrapId, untilId, days) {
    const select = document.getElementById(selectId);
    const wrap = document.getElementById(untilWrapId);
    const until = document.getElementById(untilId);
    if (VALIDITY_PRESETS.includes(String(days))) {
      select.value = String(days);
      wrap.hidden = true;
    } else {
      select.value = "custom";
      wrap.hidden = false;
      until.value = new Date(Date.now() + days * 86400000).toISOString().slice(0, 10);
    }
  }

  wirePresetValidity("new-client-validity-preset", "new-client-validity-until-wrap", "new-client-validity-until");
  wirePresetValidity("def-validity-preset", "def-validity-until-wrap", "def-validity-until");
  wirePresetValidity("limits-validity-preset", "limits-validity-until-wrap", "limits-validity-until");

  function formatDate(iso) {
    try {
      return new Date(iso).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
    } catch { return iso; }
  }

  function formatBytes(n) {
    if (!n || n <= 0) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let i = 0, v = n;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
  }

  function formatBps(bytesPerSec) {
    const bits = bytesPerSec * 8;
    if (bits < 1000) return `${bits.toFixed(0)} bps`;
    if (bits < 1_000_000) return `${(bits / 1000).toFixed(1)} Kbps`;
    return `${(bits / 1_000_000).toFixed(2)} Mbps`;
  }

  function formatUptime(seconds) {
    const d = Math.floor(seconds / 86400);
    const h = Math.floor((seconds % 86400) / 3600);
    if (d > 0) return `${d}d ${h}h`;
    const m = Math.floor((seconds % 3600) / 60);
    return `${h}h ${m}m`;
  }

  async function api(path, options = {}) {
    const res = await fetch(path, {
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
      ...options,
    });
    if (res.status === 401) {
      showLogin();
      throw new Error("Session expired");
    }
    return res;
  }

  // ---------- view switching ----------

  function showLogin() {
    stopPolling();
    dashView.hidden = true;
    loginView.hidden = false;
    loginPassword.value = "";
    loginPassword.focus();
  }

  function showDashboard() {
    loginView.hidden = true;
    dashView.hidden = false;
    switchPage(pageFromHash());
    startPolling();
  }

  function startPolling() {
    stopPolling();
    refreshActivePage();
    pollTimer = setInterval(() => { if (!document.hidden) refreshActivePage(); }, 8000);
  }
  function stopPolling() { clearInterval(pollTimer); pollTimer = null; }

  // ---------- sidebar nav ----------
  // nav-items are real <a href="#page"> links, so browsers handle right-click
  // "open in new tab", middle-click, and cmd/ctrl-click natively. A left-click
  // just changes location.hash, which this hashchange listener picks up.

  const PAGES = ["dashboard", "endpoints", "clients", "servers", "settings"];
  const PAGES_NEED_SERVER = new Set(["dashboard", "endpoints", "clients"]);

  function pageFromHash() {
    const h = location.hash.slice(1);
    return PAGES.includes(h) ? h : "dashboard";
  }

  window.addEventListener("hashchange", () => {
    if (!dashView.hidden) switchPage(pageFromHash());
  });

  function switchPage(name) {
    activePage = name;
    if (location.hash.slice(1) !== name) location.hash = name;
    document.querySelectorAll(".nav-item").forEach((a) => {
      if (a.dataset.page === name) a.setAttribute("aria-current", "page");
      else a.removeAttribute("aria-current");
    });
    document.querySelectorAll(".page").forEach((p) => { p.hidden = p.id !== `page-${name}`; });
    refreshActivePage();
  }

  async function refreshActivePage() {
    await refreshServers();

    if (activePage === "servers") { renderServersPage(); return; }
    if (activePage === "settings") { loadSettingsPage(); return; }

    const body = document.getElementById(`${activePage}-body`);
    const empty = document.getElementById(`${activePage}-no-server`);
    if (PAGES_NEED_SERVER.has(activePage) && !currentServerId) {
      if (body) body.hidden = true;
      if (empty) empty.hidden = false;
      return;
    }
    if (body) body.hidden = false;
    if (empty) empty.hidden = true;

    if (activePage === "dashboard") loadDashboard();
    else if (activePage === "endpoints") loadEndpoints();
    else if (activePage === "clients") loadClients();
  }

  // ---------- login ----------

  loginForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    loginError.hidden = true;
    setBusy(loginSubmit, true);
    try {
      const res = await fetch("/api/login", {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password: loginPassword.value }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        loginError.textContent = body.detail || "Incorrect password.";
        loginError.hidden = false;
        return;
      }
      showDashboard();
    } catch {
      loginError.textContent = "Couldn't reach the server.";
      loginError.hidden = false;
    } finally {
      setBusy(loginSubmit, false);
    }
  });

  logoutBtn.addEventListener("click", async () => {
    await fetch("/api/logout", { method: "POST", credentials: "same-origin" }).catch(() => {});
    showLogin();
  });

  // ==================== SERVER SWITCHER + SERVERS PAGE ====================

  const switcherEl = document.getElementById("server-switcher");
  const switcherBtn = document.getElementById("server-switcher-btn");
  const switcherMenu = document.getElementById("server-switcher-menu");
  const switcherList = document.getElementById("server-switcher-list");

  function openSwitcher() {
    switcherMenu.hidden = false;
    switcherEl.dataset.open = "true";
    switcherBtn.setAttribute("aria-expanded", "true");
  }
  function closeSwitcher() {
    switcherMenu.hidden = true;
    switcherEl.dataset.open = "false";
    switcherBtn.setAttribute("aria-expanded", "false");
  }
  switcherBtn.addEventListener("click", () => { switcherMenu.hidden ? openSwitcher() : closeSwitcher(); });
  document.addEventListener("click", (e) => { if (!switcherEl.contains(e.target)) closeSwitcher(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeSwitcher(); });
  document.querySelector(".server-switcher-manage").addEventListener("click", closeSwitcher);

  async function refreshServers() {
    try {
      const res = await api("/api/servers");
      if (!res.ok) return;
      allServers = await res.json();
    } catch {
      return; // handled by api() on 401; keep last-known list otherwise
    }
    if (currentServerId && !allServers.some((s) => s.id === currentServerId)) {
      setCurrentServer(null);
    }
    if (!currentServerId && allServers.length) {
      setCurrentServer(allServers[0].id);
    }
    currentServer = allServers.find((s) => s.id === currentServerId) || null;
    renderSwitcher();
  }

  function renderSwitcher() {
    const dot = document.getElementById("switcher-status-dot");
    const nameEl = document.getElementById("switcher-server-name");
    const metaEl = document.getElementById("switcher-server-meta");
    if (currentServer) {
      dot.classList.toggle("status-dot--off", !currentServer.online);
      nameEl.textContent = currentServer.vpn_endpoint || currentServer.label || currentServer.name;
      metaEl.textContent = currentServer.online ? "Online" : "Offline";
    } else {
      dot.classList.add("status-dot--off");
      nameEl.textContent = allServers.length ? "Choose a server" : "No servers yet";
      metaEl.textContent = " ";
    }

    switcherList.innerHTML = "";
    for (const s of allServers) {
      const item = document.createElement("button");
      item.type = "button";
      item.className = "server-switcher-item";
      item.setAttribute("role", "menuitem");
      if (s.id === currentServerId) item.setAttribute("aria-current", "true");
      item.innerHTML = `
        <span class="status-dot ${s.online ? "" : "status-dot--off"}"></span>
        <span class="server-switcher-info">
          <span class="server-switcher-name"></span>
          <span class="server-switcher-meta">${s.online ? "Online" : "Offline"}</span>
        </span>`;
      item.querySelector(".server-switcher-name").textContent = s.vpn_endpoint || s.label || s.name;
      item.addEventListener("click", () => {
        closeSwitcher();
        if (s.id !== currentServerId) {
          setCurrentServer(s.id);
          currentServer = s;
          renderSwitcher();
          refreshActivePage();
        }
      });
      switcherList.appendChild(item);
    }
  }

  const serversGrid = document.getElementById("servers-grid");
  const serversEmpty = document.getElementById("servers-empty");

  function renderServersPage() {
    serversEmpty.hidden = allServers.length !== 0;
    serversGrid.innerHTML = "";
    for (const s of allServers) {
      const card = document.createElement("div");
      card.className = "server-card";
      card.innerHTML = `
        <div class="server-card-head">
          <div>
            <div class="server-card-name">
              <span class="status-dot ${s.online ? "" : "status-dot--off"}"></span>
              <span class="server-card-name-text"></span>
            </div>
            <div class="server-card-label"></div>
          </div>
          <button class="btn btn--ghost btn--icon" data-action="delete">Remove</button>
        </div>
        <div class="server-card-meta">
          <div class="server-card-meta-row"><span>Status</span><b>${s.online ? "Online" : "Offline"}</b></div>
          <div class="server-card-meta-row"><span>Agent</span><b>${s.agent_version || "—"}</b></div>
          <div class="server-card-meta-row"><span>Hostname</span><b>${s.hostname || "—"}</b></div>
          <div class="server-card-meta-row"><span>VPN endpoint</span><b>${s.vpn_endpoint || "—"}</b></div>
          <div class="server-card-meta-row"><span>Last seen</span><b>${s.last_seen ? formatDate(s.last_seen) : "never"}</b></div>
        </div>
        <div class="server-card-actions">
          <button class="btn btn--ghost btn--icon" data-action="edit">Edit server</button>
          <button class="btn btn--ghost btn--icon" data-action="use">${s.id === currentServerId ? "Active" : "Switch to"}</button>
        </div>
      `;
      card.querySelector(".server-card-name-text").textContent = s.name;
      card.querySelector(".server-card-label").textContent = s.label || " ";
      const useBtn = card.querySelector('[data-action="use"]');
      if (s.id === currentServerId) useBtn.disabled = true;
      useBtn.addEventListener("click", () => {
        setCurrentServer(s.id);
        currentServer = s;
        renderSwitcher();
        showToast(`Switched to "${s.label || s.name}".`);
        renderServersPage();
      });
      card.querySelector('[data-action="edit"]').addEventListener("click", () => openEditServer(s));
      card.querySelector('[data-action="delete"]').addEventListener("click", () => openDeleteServer(s));
      serversGrid.appendChild(card);
    }
  }

  // ---------- add server ----------

  const serverDialog = document.getElementById("server-dialog");
  const serverForm = document.getElementById("server-form");
  const serverError = document.getElementById("server-error");
  const serverSubmit = document.getElementById("server-submit");

  document.getElementById("open-create-server").addEventListener("click", () => {
    serverForm.reset();
    serverError.hidden = true;
    serverDialog.showModal();
  });
  document.getElementById("server-cancel").addEventListener("click", () => serverDialog.close());

  serverForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    serverError.hidden = true;
    const body = {
      name: document.getElementById("new-server-name").value.trim(),
      label: document.getElementById("new-server-label").value.trim(),
      vpn_endpoint: document.getElementById("new-server-vpn-endpoint").value.trim(),
    };
    setBusy(serverSubmit, true);
    try {
      const res = await api("/api/servers", { method: "POST", body: JSON.stringify(body) });
      const resBody = await res.json().catch(() => ({}));
      if (!res.ok) {
        serverError.textContent = resBody.detail || "Couldn't add that server.";
        serverError.hidden = false;
        return;
      }
      serverDialog.close();
      await refreshServers();
      openInstallCommand(resBody.name, resBody.install_token);
      renderServersPage();
    } catch (err) {
      if (err.message !== "Session expired") {
        serverError.textContent = "Couldn't reach the server.";
        serverError.hidden = false;
      }
    } finally {
      setBusy(serverSubmit, false);
    }
  });

  // ---------- one-time install command ----------

  const installDialog = document.getElementById("install-dialog");

  function openInstallCommand(name, token) {
    document.getElementById("install-server-name").textContent = `"${name}"`;
    const hubUrl = `wss://${location.host}/agent/ws`;
    const command = `curl -fsSL https://raw.githubusercontent.com/CodeScaffold/openvpn-dashboard/main/agent/install.sh | sudo HUB_URL=${hubUrl} AGENT_TOKEN=${token} bash`;
    document.getElementById("install-command").textContent = command;
    installDialog.showModal();
  }
  document.getElementById("install-command-copy").addEventListener("click", async () => {
    const text = document.getElementById("install-command").textContent;
    try {
      await navigator.clipboard.writeText(text);
      showToast("Copied to clipboard.");
    } catch {
      showToast("Couldn't copy — select and copy manually.", "error");
    }
  });
  document.getElementById("install-done").addEventListener("click", () => installDialog.close());

  // ---------- edit server VPN endpoint ----------

  const serverEditDialog = document.getElementById("server-edit-dialog");
  const serverEditForm = document.getElementById("server-edit-form");
  const serverEditError = document.getElementById("server-edit-error");
  const serverEditSubmit = document.getElementById("server-edit-submit");
  let pendingEditServerId = null;

  function openEditServer(s) {
    pendingEditServerId = s.id;
    document.getElementById("server-edit-title").textContent = `"${s.label || s.name}"`;
    document.getElementById("server-edit-name").value = s.name || "";
    document.getElementById("server-edit-label").value = s.label || "";
    document.getElementById("server-edit-vpn-endpoint").value = s.vpn_endpoint || "";
    serverEditError.hidden = true;
    serverEditDialog.showModal();
  }
  document.getElementById("server-edit-cancel").addEventListener("click", () => serverEditDialog.close());

  serverEditForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!pendingEditServerId) return;
    serverEditError.hidden = true;
    const name = document.getElementById("server-edit-name").value.trim();
    const label = document.getElementById("server-edit-label").value.trim();
    const vpnEndpoint = document.getElementById("server-edit-vpn-endpoint").value.trim();
    setBusy(serverEditSubmit, true);
    try {
      const res = await api(`/api/servers/${pendingEditServerId}`, {
        method: "PUT", body: JSON.stringify({ name, label, vpn_endpoint: vpnEndpoint }),
      });
      const resBody = await res.json().catch(() => ({}));
      if (!res.ok) {
        serverEditError.textContent = resBody.detail || "Couldn't save.";
        serverEditError.hidden = false;
        return;
      }
      serverEditDialog.close();
      showToast("Server updated.");
      await refreshServers();
      renderServersPage();
    } catch (err) {
      if (err.message !== "Session expired") {
        serverEditError.textContent = "Couldn't reach the server.";
        serverEditError.hidden = false;
      }
    } finally {
      setBusy(serverEditSubmit, false);
    }
  });

  // ---------- delete server ----------

  const serverDeleteDialog = document.getElementById("server-delete-dialog");
  const serverDeleteTarget = document.getElementById("server-delete-target");
  const serverDeleteConfirm = document.getElementById("server-delete-confirm");
  let pendingDeleteServerId = null;

  function openDeleteServer(s) {
    pendingDeleteServerId = s.id;
    serverDeleteTarget.textContent = `"${s.label || s.name}"`;
    serverDeleteDialog.showModal();
  }
  document.getElementById("server-delete-cancel").addEventListener("click", () => serverDeleteDialog.close());
  serverDeleteDialog.addEventListener("close", () => { pendingDeleteServerId = null; });

  serverDeleteConfirm.addEventListener("click", async () => {
    if (!pendingDeleteServerId) return;
    const id = pendingDeleteServerId;
    setBusy(serverDeleteConfirm, true);
    try {
      const res = await api(`/api/servers/${id}`, { method: "DELETE" });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        showToast(body.detail || "Couldn't remove that server.", "error");
      } else {
        showToast("Server removed.");
        if (id === currentServerId) setCurrentServer(null);
        await refreshServers();
        renderServersPage();
      }
    } catch { /* handled by api() */ }
    finally {
      setBusy(serverDeleteConfirm, false);
      serverDeleteDialog.close();
    }
  });

  // ==================== DASHBOARD PAGE ====================

  const WARN_PCT = 75;
  const CRIT_PCT = 90;
  let ipVisible = false;
  let lastSys = null;

  // The /system route returns {online, latest, history, vpn_endpoint} — latest and each
  // history point carry the agent's raw telemetry fields. This reshapes that into the
  // flat per-metric-history shape the chart renderers below were already written for.
  function adaptSystemPayload(payload) {
    const history = payload.history || [];
    const latest = payload.latest || (history.length ? history[history.length - 1] : null) || {};
    // "latest" has two possible shapes: the agent's raw live snapshot (memory/swap/disk
    // nested as {used, total}) when online, or a persisted metrics-table row (flat
    // mem_used/mem_total/... columns) when falling back to history while offline.
    // cpu_percent is flat in both, which is why only CPU rendered correctly before this fix.
    const mem = latest.memory || { used: latest.mem_used || 0, total: latest.mem_total || 0 };
    const swap = latest.swap || { used: latest.swap_used || 0, total: latest.swap_total || 0 };
    const disk = latest.disk || { used: latest.disk_used || 0, total: latest.disk_total || 0 };
    return {
      online: payload.online,
      vpn_endpoint: payload.vpn_endpoint,
      cpu_percent: latest.cpu_percent || 0,
      memory: mem,
      swap,
      disk,
      net_totals: latest.net_totals || { rx: 0, tx: 0 },
      uptime_seconds: latest.uptime_seconds || 0,
      process_mem_bytes: latest.process_mem_bytes || 0,
      network_history: history.map((h) => ({ t: h.t, rx_bps: h.net_rx_bps || 0, tx_bps: h.net_tx_bps || 0 })),
      cpu_history: history.map((h) => ({ t: h.t, v: h.cpu_percent || 0 })),
      mem_history: history.map((h) => ({ t: h.t, v: h.mem_total ? (h.mem_used / h.mem_total) * 100 : 0 })),
      swap_history: history.map((h) => ({ t: h.t, v: h.swap_total ? (h.swap_used / h.swap_total) * 100 : 0 })),
      disk_history: history.map((h) => ({ t: h.t, v: h.disk_total ? (h.disk_used / h.disk_total) * 100 : 0 })),
      conn_history: history.map((h) => ({ t: h.t, tcp: h.tcp_count || 0, udp: h.udp_count || 0 })),
    };
  }

  async function loadDashboard() {
    try {
      const [sysRes, clientsRes, statusRes, endpointsRes] = await Promise.all([
        api(serverPath("/system")), api(serverPath("/clients")), api(serverPath("/status")), api(serverPath("/endpoints")),
      ]);
      if (!sysRes.ok || !clientsRes.ok || !statusRes.ok || !endpointsRes.ok) return;
      const [sysRaw, clients, sessions, eps] = await Promise.all([
        sysRes.json(), clientsRes.json(), statusRes.json(), endpointsRes.json(),
      ]);
      const sys = adaptSystemPayload(sysRaw);
      lastSys = sys;
      renderMetrics(sys);
      renderHealthBanner(sys);
      renderThroughputChart(sys.network_history);
      renderConnectionsChart(sys.conn_history);
      renderInfoStrip(sys);
      renderChartFooters(sys, sessions);
      renderAgentStatus(sys);

      document.getElementById("stat-endpoints").textContent = eps.length;
      document.getElementById("stat-clients").textContent = clients.length;
      document.getElementById("stat-online").textContent = sessions.length;
      const totalBytes = clients.reduce((sum, c) => sum + (c.used_bytes || 0), 0);
      document.getElementById("stat-data").textContent = formatBytes(totalBytes);
    } catch {
      /* transient; next poll will retry */
    }
  }

  function renderChartFooters(sys, sessions) {
    const nt = sys.net_totals || { rx: 0, tx: 0 };
    document.getElementById("foot-sent").textContent = formatBytes(nt.tx);
    document.getElementById("foot-received").textContent = formatBytes(nt.rx);
    const hist = sys.network_history || [];
    const avgUp = seriesMean(hist, "tx_bps");
    const avgDown = seriesMean(hist, "rx_bps");
    document.getElementById("foot-avg-speed").textContent = hist.length
      ? `↑ ${formatBps(avgUp)}  ↓ ${formatBps(avgDown)}` : "—";
    document.getElementById("foot-open-sessions").textContent = String(sessions.length);
  }

  function renderAgentStatus(sys) {
    const dot = document.getElementById("agent-status-dot");
    const text = document.getElementById("agent-status-text");
    dot.classList.toggle("status-dot--off", !sys.online);
    text.textContent = sys.online ? "Agent · Connected" : "Agent · Offline";
    document.getElementById("action-bar-server-name").textContent = currentServer ? (currentServer.vpn_endpoint || currentServer.label || currentServer.name) : "";
  }

  function seriesMean(history, key) {
    if (!history.length) return 0;
    return history.reduce((s, h) => s + h[key], 0) / history.length;
  }
  function seriesPeak(history, key) {
    if (!history.length) return 0;
    return Math.max(...history.map((h) => h[key]));
  }

  function renderMetrics(sys) {
    const cpuPct = sys.cpu_percent;
    document.getElementById("m-cpu").textContent = `${cpuPct.toFixed(0)}%`;
    document.getElementById("m-cpu-detail").textContent = "";
    document.getElementById("m-cpu-avg").textContent = `avg ${seriesMean(sys.cpu_history, "v").toFixed(0)}%`;
    document.getElementById("m-cpu-peak").textContent = `peak ${seriesPeak(sys.cpu_history, "v").toFixed(0)}%`;
    renderSparkline("spark-cpu", sys.cpu_history, "v", 100, tileColor(cpuPct));

    const mem = sys.memory;
    const memPct = mem.total ? (mem.used / mem.total) * 100 : 0;
    document.getElementById("m-mem").textContent = `${memPct.toFixed(0)}%`;
    document.getElementById("m-mem-detail").textContent = `${formatBytes(mem.used)} / ${formatBytes(mem.total)}`;
    document.getElementById("m-mem-avg").textContent = `avg ${seriesMean(sys.mem_history, "v").toFixed(0)}%`;
    document.getElementById("m-mem-peak").textContent = `peak ${seriesPeak(sys.mem_history, "v").toFixed(0)}%`;
    renderSparkline("spark-mem", sys.mem_history, "v", 100, tileColor(memPct));

    const swap = sys.swap;
    const swapPct = swap.total ? (swap.used / swap.total) * 100 : 0;
    document.getElementById("m-swap").textContent = swap.total ? `${swapPct.toFixed(0)}%` : "n/a";
    document.getElementById("m-swap-detail").textContent = swap.total
      ? `${formatBytes(swap.used)} / ${formatBytes(swap.total)}` : "no swap configured";
    document.getElementById("m-swap-avg").textContent = `avg ${seriesMean(sys.swap_history, "v").toFixed(1)}%`;
    document.getElementById("m-swap-peak").textContent = `peak ${seriesPeak(sys.swap_history, "v").toFixed(0)}%`;
    renderSparkline("spark-swap", sys.swap_history, "v", 100, tileColor(swapPct));

    const disk = sys.disk;
    const diskPct = disk.total ? (disk.used / disk.total) * 100 : 0;
    document.getElementById("m-disk").textContent = `${diskPct.toFixed(0)}%`;
    document.getElementById("m-disk-detail").textContent = `${formatBytes(disk.used)} / ${formatBytes(disk.total)}`;
    document.getElementById("m-disk-free").textContent = `free ${formatBytes(disk.total - disk.used)}`;
    document.getElementById("m-disk-avg").textContent = `avg ${seriesMean(sys.disk_history, "v").toFixed(1)}%`;
    renderSparkline("spark-disk", sys.disk_history, "v", 100, tileColor(diskPct));
  }

  function tileColor(pct) {
    if (pct >= CRIT_PCT) return "var(--danger)";
    if (pct >= WARN_PCT) return "#d69e2e";
    return "var(--ov-orange)";
  }

  // Catmull-Rom -> cubic Bezier smoothing, so charts read as continuous curves
  // instead of a jagged polyline between raw samples.
  function smoothLineD(points) {
    if (points.length < 2) return "";
    if (points.length === 2) {
      return `M${points[0][0]},${points[0][1]} L${points[1][0]},${points[1][1]}`;
    }
    let d = `M${points[0][0]},${points[0][1]}`;
    for (let i = 0; i < points.length - 1; i++) {
      const p0 = points[i === 0 ? 0 : i - 1];
      const p1 = points[i];
      const p2 = points[i + 1];
      const p3 = points[i + 2] || p2;
      const c1x = p1[0] + (p2[0] - p0[0]) / 6;
      const c1y = p1[1] + (p2[1] - p0[1]) / 6;
      const c2x = p2[0] - (p3[0] - p1[0]) / 6;
      const c2y = p2[1] - (p3[1] - p1[1]) / 6;
      d += ` C${c1x.toFixed(1)},${c1y.toFixed(1)} ${c2x.toFixed(1)},${c2y.toFixed(1)} ${p2[0]},${p2[1]}`;
    }
    return d;
  }

  function seriesPoints(history, key, W, H, PAD, maxVal) {
    const n = history.length;
    const xStep = (W - PAD * 2) / (n - 1);
    return history.map((h, i) => {
      const x = PAD + i * xStep;
      const y = H - PAD - (Math.min(h[key], maxVal) / maxVal) * (H - PAD * 2);
      return [Number(x.toFixed(1)), Number(y.toFixed(1))];
    });
  }

  function renderSparkline(containerId, history, key, maxScale, color) {
    const el = document.getElementById(containerId);
    if (!history || history.length < 2) { el.innerHTML = ""; return; }
    const W = 200, H = 34, PAD = 2;
    const points = seriesPoints(history, key, W, H, PAD, maxScale);
    const linePath = smoothLineD(points);
    const last = points[points.length - 1];
    const first = points[0];
    const area = `${linePath} L${last[0]},${(H - PAD).toFixed(1)} L${first[0]},${(H - PAD).toFixed(1)} Z`;
    el.innerHTML = `
      <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
        <path d="${area}" fill="${color}" fill-opacity="0.12" stroke="none"></path>
        <path d="${linePath}" fill="none" stroke="${color}" stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round"></path>
      </svg>`;
  }

  function renderHealthBanner(sys) {
    const banner = document.getElementById("health-banner");
    const items = [
      { name: "CPU", value: sys.cpu_percent },
      { name: "Memory", value: sys.memory.total ? (sys.memory.used / sys.memory.total) * 100 : 0 },
      { name: "Swap", value: sys.swap.total ? (sys.swap.used / sys.swap.total) * 100 : 0 },
      { name: "Storage", value: sys.disk.total ? (sys.disk.used / sys.disk.total) * 100 : 0 },
    ];
    const fmt = (xs) => xs.map((i) => `${i.name} ${i.value.toFixed(0)}%`).join(", ");
    const crit = items.filter((i) => i.value >= CRIT_PCT);
    const warm = items.filter((i) => i.value >= WARN_PCT && i.value < CRIT_PCT);

    if (crit.length) {
      banner.dataset.level = "crit";
      document.getElementById("health-text").textContent = `Critical: ${fmt(crit)}`;
      banner.hidden = false;
    } else if (warm.length) {
      banner.dataset.level = "warn";
      document.getElementById("health-text").textContent = `Elevated: ${fmt(warm)}`;
      banner.hidden = false;
    } else {
      banner.hidden = true;
    }
  }

  function renderDualChart(containerId, history, keyA, keyB, nowIdA, nowIdB, formatFn, labelA, labelB) {
    const container = document.getElementById(containerId);
    if (!history || history.length < 2) {
      container.innerHTML = '<p class="chart-empty">Collecting data&hellip;</p>';
      if (nowIdA) document.getElementById(nowIdA).textContent = "—";
      if (nowIdB) document.getElementById(nowIdB).textContent = "—";
      return;
    }
    const W = 600, H = 180, PAD = 4;
    const maxVal = Math.max(1, ...history.flatMap((h) => [h[keyA], h[keyB]])) * 1.15;

    const pointsA = seriesPoints(history, keyA, W, H, PAD, maxVal);
    const pointsB = seriesPoints(history, keyB, W, H, PAD, maxVal);
    const pathA = smoothLineD(pointsA);
    const pathB = smoothLineD(pointsB);
    const lastA = pointsA[pointsA.length - 1];
    const firstA = pointsA[0];
    const areaA = `${pathA} L${lastA[0]},${(H - PAD).toFixed(1)} L${firstA[0]},${(H - PAD).toFixed(1)} Z`;

    container.innerHTML = `
      <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
        <path d="${areaA}" fill="var(--ov-orange-tint)" stroke="none"></path>
        <path d="${pathA}" fill="none" stroke="var(--ov-orange)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"></path>
        <path d="${pathB}" fill="none" stroke="#4a90c4" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"></path>
        <line class="chart-crosshair" x1="0" x2="0" y1="${PAD}" y2="${H - PAD}"></line>
        <circle class="chart-dot" cx="0" cy="0" fill="var(--ov-orange)"></circle>
        <circle class="chart-dot" cx="0" cy="0" fill="#4a90c4"></circle>
        <rect class="chart-hit" x="0" y="0" width="${W}" height="${H}"></rect>
      </svg>
      <div class="chart-tooltip"></div>`;

    const last = history[history.length - 1];
    if (nowIdA) document.getElementById(nowIdA).textContent = formatFn(last[keyA]);
    if (nowIdB) document.getElementById(nowIdB).textContent = formatFn(last[keyB]);

    // ---- hover: crosshair + tooltip, per the dataviz interaction spec ----
    const svg = container.querySelector("svg");
    const hitRect = container.querySelector(".chart-hit");
    const crosshair = container.querySelector(".chart-crosshair");
    const dots = container.querySelectorAll(".chart-dot");
    const tooltip = container.querySelector(".chart-tooltip");

    function pickIndex(clientX) {
      const rect = svg.getBoundingClientRect();
      const xInView = ((clientX - rect.left) / rect.width) * W;
      const xStep = (W - PAD * 2) / (history.length - 1);
      const idx = Math.round((xInView - PAD) / xStep);
      return Math.min(history.length - 1, Math.max(0, idx));
    }

    function showAt(idx) {
      const point = history[idx];
      crosshair.setAttribute("x1", pointsA[idx][0]);
      crosshair.setAttribute("x2", pointsA[idx][0]);
      crosshair.style.opacity = 1;
      dots[0].setAttribute("cx", pointsA[idx][0]);
      dots[0].setAttribute("cy", pointsA[idx][1]);
      dots[0].style.opacity = 1;
      dots[1].setAttribute("cx", pointsB[idx][0]);
      dots[1].setAttribute("cy", pointsB[idx][1]);
      dots[1].style.opacity = 1;

      tooltip.innerHTML = `
        <div class="chart-tooltip-time">${formatClock(point.t)}</div>
        <div class="chart-tooltip-row"><span class="chart-tooltip-swatch" style="background:var(--ov-orange)"></span>${labelA} ${formatFn(point[keyA])}</div>
        <div class="chart-tooltip-row"><span class="chart-tooltip-swatch" style="background:#4a90c4"></span>${labelB} ${formatFn(point[keyB])}</div>`;
      const rect = svg.getBoundingClientRect();
      tooltip.style.left = `${(pointsA[idx][0] / W) * rect.width}px`;
      tooltip.style.top = `${(Math.min(pointsA[idx][1], pointsB[idx][1]) / H) * rect.height}px`;
      tooltip.classList.add("visible");

      if (nowIdA) document.getElementById(nowIdA).textContent = formatFn(point[keyA]);
      if (nowIdB) document.getElementById(nowIdB).textContent = formatFn(point[keyB]);
    }

    function hide() {
      crosshair.style.opacity = 0;
      dots.forEach((d) => (d.style.opacity = 0));
      tooltip.classList.remove("visible");
      if (nowIdA) document.getElementById(nowIdA).textContent = formatFn(last[keyA]);
      if (nowIdB) document.getElementById(nowIdB).textContent = formatFn(last[keyB]);
    }

    hitRect.addEventListener("mousemove", (e) => showAt(pickIndex(e.clientX)));
    hitRect.addEventListener("mouseleave", hide);
  }

  function renderThroughputChart(history) {
    renderDualChart("throughput-chart", history, "rx_bps", "tx_bps", "chart-rx-now", "chart-tx-now", formatBps, "Down", "Up");
  }

  function renderConnectionsChart(history) {
    renderDualChart("connections-chart", history, "tcp", "udp", "chart-tcp-now", "chart-udp-now", (v) => String(v), "TCP", "UDP");
  }

  function renderInfoStrip(sys) {
    document.getElementById("info-os-uptime").textContent = formatUptime(sys.uptime_seconds);
    document.getElementById("info-agent-ram").textContent = formatBytes(sys.process_mem_bytes);
    document.getElementById("info-ip").textContent = ipVisible
      ? (sys.vpn_endpoint || "—")
      : "•••.•••.•••.•••";
  }

  document.getElementById("ip-toggle").addEventListener("click", () => {
    ipVisible = !ipVisible;
    const el = document.getElementById("info-ip");
    el.classList.toggle("ip-visible", ipVisible);
    if (lastSys) el.textContent = ipVisible ? (lastSys.vpn_endpoint || "—") : "•••.•••.•••.•••";
  });

  // ==================== ENDPOINTS PAGE ====================

  const endpointsGrid = document.getElementById("endpoints-grid");
  const endpointDialog = document.getElementById("endpoint-dialog");
  const endpointForm = document.getElementById("endpoint-form");
  const endpointError = document.getElementById("endpoint-error");
  const endpointSubmit = document.getElementById("endpoint-submit");
  const endpointDeleteDialog = document.getElementById("endpoint-delete-dialog");
  const endpointDeleteTarget = document.getElementById("endpoint-delete-target");
  const endpointDeleteConfirm = document.getElementById("endpoint-delete-confirm");
  let pendingDeleteEndpoint = null;

  async function loadEndpoints() {
    try {
      const res = await api(serverPath("/endpoints"));
      if (!res.ok) return;
      renderEndpoints(await res.json());
    } catch { /* handled by api() on 401 */ }
  }

  function renderEndpoints(eps) {
    endpointsGrid.innerHTML = "";
    for (const ep of eps) {
      const card = document.createElement("div");
      card.className = "ep-card";
      card.innerHTML = `
        <div class="ep-card-head">
          <div>
            <div class="ep-card-name">
              <span class="status-dot ${ep.active ? "" : "status-dot--off"}"></span>
              ${ep.name}
            </div>
            <div class="ep-card-meta">${ep.protocol.toUpperCase()} &middot; ${ep.port} &middot; ${ep.cipher}</div>
          </div>
          <button class="btn btn--ghost btn--icon" data-action="delete">Delete</button>
        </div>
        <div class="ep-card-stats">
          <div><span class="ep-card-stat-label">Online</span><span class="ep-card-stat-value">${ep.online}</span></div>
          <div><span class="ep-card-stat-label">Status</span><span class="ep-card-stat-value">${ep.active ? "Active" : "Stopped"}</span></div>
        </div>
        <div class="ep-card-actions" style="justify-content:flex-start;flex-wrap:wrap">
          <button class="btn btn--ghost btn--icon" data-action="restart">Restart</button>
          <button class="btn btn--ghost btn--icon" data-action="toggle">${ep.active ? "Stop" : "Start"}</button>
          <button class="btn btn--ghost btn--icon" data-action="logs">Logs</button>
          <button class="btn btn--ghost btn--icon" data-action="config">Config</button>
        </div>
      `;
      card.querySelector('[data-action="delete"]').addEventListener("click", () => openDeleteEndpoint(ep.name));
      card.querySelector('[data-action="restart"]').addEventListener("click", (e) => restartEndpoint(ep.name, e.currentTarget));
      card.querySelector('[data-action="toggle"]').addEventListener("click", (e) => toggleEndpoint(ep, e.currentTarget));
      card.querySelector('[data-action="logs"]').addEventListener("click", () => openEndpointLogs(ep.name));
      card.querySelector('[data-action="config"]').addEventListener("click", () => openEndpointConfig(ep.name));
      endpointsGrid.appendChild(card);
    }
  }

  async function restartEndpoint(name, btn) {
    btn.disabled = true;
    try {
      const res = await api(serverPath(`/endpoints/${encodeURIComponent(name)}/restart`), { method: "POST" });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) { showToast(body.detail || "Restart failed.", "error"); return; }
      showToast(`"${name}" restarted.`);
      loadEndpoints();
    } catch { /* handled by api() */ }
    finally { btn.disabled = false; }
  }

  async function toggleEndpoint(ep, btn) {
    const action = ep.active ? "stop" : "start";
    btn.disabled = true;
    try {
      const res = await api(serverPath(`/endpoints/${encodeURIComponent(ep.name)}/${action}`), { method: "POST" });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) { showToast(body.detail || `${action} failed.`, "error"); return; }
      showToast(action === "stop" ? `"${ep.name}" stopped.` : `"${ep.name}" started.`);
      loadEndpoints();
    } catch { /* handled by api() */ }
    finally { btn.disabled = false; }
  }

  const logsDialog = document.getElementById("logs-dialog");
  const logsContent = document.getElementById("logs-content");
  let pendingLogsEndpoint = null;

  async function openEndpointLogs(name) {
    pendingLogsEndpoint = name;
    document.getElementById("logs-dialog-sub").textContent = `${name} · last 200 lines`;
    logsDialog.showModal();
    logsContent.textContent = "Loading…";
    try {
      const res = await api(serverPath(`/endpoints/${encodeURIComponent(name)}/logs`));
      const body = await res.json().catch(() => ({}));
      logsContent.textContent = res.ok ? (body.logs || "(empty)") : (body.detail || "Couldn't load logs.");
      logsContent.scrollTop = logsContent.scrollHeight;
    } catch {
      logsContent.textContent = "Couldn't reach the server.";
    }
  }
  document.getElementById("logs-refresh").addEventListener("click", () => { if (pendingLogsEndpoint) openEndpointLogs(pendingLogsEndpoint); });
  document.getElementById("logs-close").addEventListener("click", () => logsDialog.close());

  const configDialog = document.getElementById("config-dialog");
  const configContent = document.getElementById("config-content");

  async function openEndpointConfig(name) {
    document.getElementById("config-dialog-title").innerHTML = `${name}.conf <span class="dialog-sub">read-only</span>`;
    configDialog.showModal();
    configContent.textContent = "Loading…";
    try {
      const res = await api(serverPath(`/endpoints/${encodeURIComponent(name)}/config`));
      const body = await res.json().catch(() => ({}));
      configContent.textContent = res.ok ? body.config : (body.detail || "Couldn't load config.");
    } catch {
      configContent.textContent = "Couldn't reach the server.";
    }
  }
  document.getElementById("config-close").addEventListener("click", () => configDialog.close());

  document.getElementById("open-create-endpoint").addEventListener("click", () => {
    endpointForm.reset();
    document.getElementById("ep-port").value = 1194;
    document.getElementById("ep-dns1").value = "1.1.1.1";
    document.getElementById("ep-dns2").value = "1.0.0.1";
    endpointError.hidden = true;
    endpointDialog.showModal();
  });
  document.getElementById("endpoint-cancel").addEventListener("click", () => endpointDialog.close());

  endpointForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    endpointError.hidden = true;
    const body = {
      name: document.getElementById("ep-name").value.trim(),
      port: parseInt(document.getElementById("ep-port").value, 10),
      protocol: document.getElementById("ep-protocol").value,
      cipher: document.getElementById("ep-cipher").value,
      dns1: document.getElementById("ep-dns1").value.trim(),
      dns2: document.getElementById("ep-dns2").value.trim(),
    };
    setBusy(endpointSubmit, true);
    try {
      const res = await api(serverPath("/endpoints"), { method: "POST", body: JSON.stringify(body) });
      const resBody = await res.json().catch(() => ({}));
      if (!res.ok) {
        endpointError.textContent = resBody.detail || "Couldn't create endpoint.";
        endpointError.hidden = false;
        return;
      }
      showToast(`Endpoint "${body.name}" is live.`);
      endpointDialog.close();
      loadEndpoints();
    } catch (err) {
      if (err.message !== "Session expired") {
        endpointError.textContent = "Couldn't reach the server.";
        endpointError.hidden = false;
      }
    } finally {
      setBusy(endpointSubmit, false);
    }
  });

  function openDeleteEndpoint(name) {
    pendingDeleteEndpoint = name;
    endpointDeleteTarget.textContent = `"${name}"`;
    endpointDeleteDialog.showModal();
  }
  document.getElementById("endpoint-delete-cancel").addEventListener("click", () => endpointDeleteDialog.close());
  endpointDeleteDialog.addEventListener("close", () => { pendingDeleteEndpoint = null; });

  endpointDeleteConfirm.addEventListener("click", async () => {
    if (!pendingDeleteEndpoint) return;
    const name = pendingDeleteEndpoint;
    try {
      const res = await api(serverPath(`/endpoints/${encodeURIComponent(name)}`), { method: "DELETE" });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        showToast(body.detail || `Couldn't delete "${name}".`, "error");
      } else {
        showToast(`Deleted "${name}".`);
        loadEndpoints();
      }
    } catch { /* handled by api() */ }
    finally { endpointDeleteDialog.close(); }
  });

  // ==================== CLIENTS PAGE ====================

  const clientsTbody = document.getElementById("clients-tbody");
  const clientsEmpty = document.getElementById("clients-empty");

  const clientDialog = document.getElementById("client-dialog");
  const clientForm = document.getElementById("client-form");
  const clientError = document.getElementById("client-error");
  const clientSubmit = document.getElementById("client-submit");

  const revokeDialog = document.getElementById("revoke-dialog");
  const revokeTarget = document.getElementById("revoke-target");
  const revokeCancel = document.getElementById("revoke-cancel");
  const revokeConfirm = document.getElementById("revoke-confirm");
  let pendingRevokeName = null;

  const limitsDialog = document.getElementById("limits-dialog");
  const limitsForm = document.getElementById("limits-form");
  const limitsTarget = document.getElementById("limits-target");
  const limitsMaxConn = document.getElementById("limits-max-connections");
  const limitsQuota = document.getElementById("limits-quota");
  const limitsBandwidth = document.getElementById("limits-bandwidth");
  const limitsTeam = document.getElementById("limits-team");
  const limitsUsage = document.getElementById("limits-usage");
  const limitsError = document.getElementById("limits-error");
  const limitsCancel = document.getElementById("limits-cancel");
  const limitsSubmit = document.getElementById("limits-submit");
  const limitsResetUsage = document.getElementById("limits-reset-usage");
  const limitsExpiry = document.getElementById("limits-expiry");
  const limitsRenew = document.getElementById("limits-renew");
  const limitsRenewError = document.getElementById("limits-renew-error");
  const limitsIpEndpoint = document.getElementById("limits-ip-endpoint");
  const limitsIpAddress = document.getElementById("limits-ip-address");
  const limitsIpPinBadge = document.getElementById("limits-ip-pin-badge");
  const limitsIpError = document.getElementById("limits-ip-error");
  const limitsIpSave = document.getElementById("limits-ip-save");
  const limitsIpClear = document.getElementById("limits-ip-clear");
  let pendingLimitsClient = null;
  let limitsEndpoints = [];

  const downloadDialog = document.getElementById("download-dialog");
  const downloadTarget = document.getElementById("download-target");
  const downloadList = document.getElementById("download-endpoint-list");
  document.getElementById("download-cancel").addEventListener("click", () => downloadDialog.close());

  let onlineNames = new Set();

  async function loadClients() {
    try {
      const [clientsRes, statusRes] = await Promise.all([api(serverPath("/clients")), api(serverPath("/status"))]);
      if (!clientsRes.ok) return;
      const clients = await clientsRes.json();
      onlineNames = new Set(statusRes.ok ? (await statusRes.json()).map((s) => s.name) : []);
      renderClients(clients);
    } catch { /* handled by api() */ }
  }

  function limitsSummary(c) {
    const parts = [];
    parts.push(`${c.max_connections} device${c.max_connections === 1 ? "" : "s"}`);
    parts.push(c.quota_bytes ? `${formatBytes(c.used_bytes)} / ${formatBytes(c.quota_bytes)}` : `${formatBytes(c.used_bytes)} used`);
    parts.push(c.bandwidth_kbps ? `${(c.bandwidth_kbps / 1000).toFixed(1)} Mbps cap` : "no speed cap");
    return parts.join(" · ");
  }

  function renderClients(clients) {
    clientsTbody.innerHTML = "";
    clientsEmpty.hidden = clients.length !== 0;

    for (const c of clients) {
      const online = onlineNames.has(c.name);
      const tr = document.createElement("tr");

      const nameTd = document.createElement("td");
      nameTd.innerHTML = `<span class="client-name"></span>${c.disabled ? '<span class="badge-disabled">quota exceeded</span>' : ""}`;
      nameTd.querySelector(".client-name").textContent = c.name;
      tr.appendChild(nameTd);

      const teamTd = document.createElement("td");
      if (c.team) {
        teamTd.innerHTML = `<span class="team-badge"></span>`;
        teamTd.querySelector(".team-badge").textContent = c.team;
      } else {
        teamTd.textContent = "—";
      }
      tr.appendChild(teamTd);

      const statusTd = document.createElement("td");
      statusTd.innerHTML = `<span class="status-pill"><span class="status-dot ${online ? "" : "status-dot--off"}"></span>${online ? "Online" : "Offline"}</span>`;
      tr.appendChild(statusTd);

      const createdTd = document.createElement("td");
      createdTd.className = "created-cell";
      createdTd.textContent = formatDate(c.created);
      tr.appendChild(createdTd);

      const expiresTd = document.createElement("td");
      if (c.expires) {
        const daysLeft = (new Date(c.expires) - Date.now()) / 86400000;
        expiresTd.className = daysLeft < 30 ? "expires-cell expires-cell--soon" : "expires-cell";
        expiresTd.textContent = daysLeft < 0 ? "Expired" : formatDate(c.expires);
        expiresTd.title = daysLeft >= 0 && daysLeft < 30 ? `Expires in ${Math.floor(daysLeft)} day${Math.floor(daysLeft) === 1 ? "" : "s"}` : "";
      } else {
        expiresTd.className = "expires-cell";
        expiresTd.textContent = "—";
      }
      tr.appendChild(expiresTd);

      const limitsTd = document.createElement("td");
      limitsTd.className = "limits-summary";
      limitsTd.textContent = limitsSummary(c);
      tr.appendChild(limitsTd);

      const actionsTd = document.createElement("td");
      actionsTd.className = "col-actions";
      actionsTd.innerHTML = `
        <div class="row-actions">
          <button class="btn btn--ghost btn--icon" data-action="limits">Limits</button>
          <button class="btn btn--ghost btn--icon" data-action="download">Download</button>
          <button class="btn btn--danger btn--icon" data-action="revoke">Revoke</button>
        </div>`;
      actionsTd.querySelector('[data-action="limits"]').addEventListener("click", () => openLimits(c));
      actionsTd.querySelector('[data-action="download"]').addEventListener("click", () => openDownload(c.name));
      actionsTd.querySelector('[data-action="revoke"]').addEventListener("click", () => openRevoke(c.name));
      tr.appendChild(actionsTd);

      clientsTbody.appendChild(tr);
    }
  }

  async function openDownload(name) {
    downloadTarget.textContent = `"${name}"`;
    downloadList.innerHTML = '<p class="chart-empty">Loading endpoints&hellip;</p>';
    downloadDialog.showModal();
    try {
      const res = await api(serverPath("/endpoints"));
      const eps = res.ok ? await res.json() : [];
      if (eps.length <= 1) {
        downloadDialog.close();
        window.location.href = serverPath(`/clients/${encodeURIComponent(name)}/download?endpoint=${encodeURIComponent(eps[0]?.name || "server")}`);
        return;
      }
      downloadList.innerHTML = "";
      for (const ep of eps) {
        const item = document.createElement("div");
        item.className = "download-item";
        item.innerHTML = `<span>${ep.name}</span><span class="download-item-meta">${ep.protocol.toUpperCase()} &middot; ${ep.port}</span>`;
        item.addEventListener("click", () => {
          downloadDialog.close();
          window.location.href = serverPath(`/clients/${encodeURIComponent(name)}/download?endpoint=${encodeURIComponent(ep.name)}`);
        });
        downloadList.appendChild(item);
      }
    } catch {
      downloadList.innerHTML = '<p class="chart-empty">Couldn’t load endpoints.</p>';
    }
  }

  // ---------- create client ----------

  document.getElementById("open-create-client").addEventListener("click", async () => {
    clientForm.reset();
    document.getElementById("new-client-max-conn").value = 1;
    clientError.hidden = true;
    try {
      const res = await api("/api/settings");
      if (res.ok) {
        const cfg = await res.json();
        document.getElementById("new-client-max-conn").value = cfg.client_default_max_connections || 1;
        document.getElementById("new-client-quota").value = cfg.client_default_quota_gb ?? "";
        document.getElementById("new-client-bandwidth").value = cfg.client_default_bandwidth_mbps ?? "";
        daysToPresetSelection("new-client-validity-preset", "new-client-validity-until-wrap", "new-client-validity-until", cfg.client_default_validity_days || 365);
      }
    } catch { /* fall back to blank defaults */ }
    clientDialog.showModal();
  });
  document.getElementById("client-cancel").addEventListener("click", () => clientDialog.close());

  clientForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    clientError.hidden = true;
    const name = document.getElementById("new-client-name").value.trim();
    const body = {
      name,
      team: document.getElementById("new-client-team").value.trim() || null,
      max_connections: parseInt(document.getElementById("new-client-max-conn").value, 10) || 1,
      quota_gb: document.getElementById("new-client-quota").value ? parseFloat(document.getElementById("new-client-quota").value) : null,
      bandwidth_mbps: document.getElementById("new-client-bandwidth").value ? parseFloat(document.getElementById("new-client-bandwidth").value) : null,
      validity_days: presetToDays("new-client-validity-preset", "new-client-validity-until"),
    };
    setBusy(clientSubmit, true);
    try {
      const res = await api(serverPath("/clients"), { method: "POST", body: JSON.stringify(body) });
      const resBody = await res.json().catch(() => ({}));
      if (!res.ok) {
        clientError.textContent = resBody.detail || "Couldn't create that client.";
        clientError.hidden = false;
        return;
      }
      clientDialog.close();
      showToast(`Created "${name}".`);
      loadClients();
      openDownload(name);
    } catch (err) {
      if (err.message !== "Session expired") {
        clientError.textContent = "Couldn't reach the server.";
        clientError.hidden = false;
      }
    } finally {
      setBusy(clientSubmit, false);
    }
  });

  // ---------- revoke ----------

  function openRevoke(name) {
    pendingRevokeName = name;
    revokeTarget.textContent = `"${name}"`;
    revokeDialog.showModal();
  }
  revokeCancel.addEventListener("click", () => revokeDialog.close());
  revokeDialog.addEventListener("close", () => { pendingRevokeName = null; });

  revokeConfirm.addEventListener("click", async () => {
    if (!pendingRevokeName) return;
    const name = pendingRevokeName;
    setBusy(revokeConfirm, true);
    try {
      const res = await api(serverPath(`/clients/${encodeURIComponent(name)}`), { method: "DELETE" });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        showToast(body.detail || `Couldn't revoke "${name}".`, "error");
      } else {
        showToast(`Revoked "${name}".`);
        loadClients();
      }
    } catch { /* handled by api() */ }
    finally {
      setBusy(revokeConfirm, false);
      revokeDialog.close();
    }
  });

  // ---------- limits ----------

  async function openLimits(client) {
    pendingLimitsClient = client;
    limitsTarget.textContent = `"${client.name}"`;
    limitsMaxConn.value = client.max_connections;
    limitsQuota.value = client.quota_bytes ? (client.quota_bytes / 1_000_000_000).toString() : "";
    limitsBandwidth.value = client.bandwidth_kbps ? (client.bandwidth_kbps / 1000).toString() : "";
    limitsTeam.value = client.team || "";
    limitsUsage.textContent = client.quota_bytes
      ? `Used so far: ${formatBytes(client.used_bytes)} of ${formatBytes(client.quota_bytes)}${client.disabled ? " — currently blocked (quota exceeded)" : ""}`
      : `Used so far: ${formatBytes(client.used_bytes)} (no cap set)`;
    limitsError.hidden = true;
    limitsExpiry.textContent = client.expires ? `Certificate expires: ${formatDate(client.expires)}` : "Certificate expiry unknown.";
    daysToPresetSelection("limits-validity-preset", "limits-validity-until-wrap", "limits-validity-until", 365);
    limitsRenewError.hidden = true;
    limitsIpError.hidden = true;

    limitsIpEndpoint.innerHTML = '<option value="">Loading&hellip;</option>';
    limitsDialog.showModal();
    try {
      const res = await api(serverPath("/endpoints"));
      limitsEndpoints = res.ok ? await res.json() : [];
    } catch { limitsEndpoints = []; }
    limitsIpEndpoint.innerHTML = "";
    for (const ep of limitsEndpoints) {
      const opt = document.createElement("option");
      opt.value = ep.name;
      opt.textContent = ep.name;
      limitsIpEndpoint.appendChild(opt);
    }
    renderLimitsIp();
  }

  function renderLimitsIp() {
    const ep = limitsIpEndpoint.value;
    const entry = pendingLimitsClient && pendingLimitsClient.static_ips ? pendingLimitsClient.static_ips[ep] : null;
    limitsIpAddress.value = entry ? entry.static_ip : "";
    limitsIpPinBadge.hidden = !(entry && entry.pinned);
  }
  limitsIpEndpoint.addEventListener("change", renderLimitsIp);

  limitsCancel.addEventListener("click", () => limitsDialog.close());
  limitsDialog.addEventListener("close", () => { pendingLimitsClient = null; });

  limitsForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!pendingLimitsClient) return;
    limitsError.hidden = true;
    const name = pendingLimitsClient.name;
    const body = {
      max_connections: parseInt(limitsMaxConn.value, 10) || 1,
      quota_gb: limitsQuota.value ? parseFloat(limitsQuota.value) : null,
      bandwidth_mbps: limitsBandwidth.value ? parseFloat(limitsBandwidth.value) : null,
      team: limitsTeam.value.trim() || null,
    };
    setBusy(limitsSubmit, true);
    try {
      const res = await api(serverPath(`/clients/${encodeURIComponent(name)}/limits`), { method: "PUT", body: JSON.stringify(body) });
      const resBody = await res.json().catch(() => ({}));
      if (!res.ok) {
        limitsError.textContent = resBody.detail || "Couldn't save limits.";
        limitsError.hidden = false;
        return;
      }
      showToast(`Updated limits for "${name}".`);
      limitsDialog.close();
      loadClients();
    } catch (err) {
      if (err.message !== "Session expired") {
        limitsError.textContent = "Couldn't reach the server.";
        limitsError.hidden = false;
      }
    } finally {
      setBusy(limitsSubmit, false);
    }
  });

  limitsResetUsage.addEventListener("click", async () => {
    if (!pendingLimitsClient) return;
    const name = pendingLimitsClient.name;
    try {
      const res = await api(serverPath(`/clients/${encodeURIComponent(name)}/reset-usage`), { method: "POST" });
      if (!res.ok) { showToast("Couldn't reset usage.", "error"); return; }
      showToast(`Usage reset for "${name}".`);
      limitsDialog.close();
      loadClients();
    } catch { /* handled by api() */ }
  });

  limitsRenew.addEventListener("click", async () => {
    if (!pendingLimitsClient) return;
    const name = pendingLimitsClient.name;
    limitsRenewError.hidden = true;
    const body = { validity_days: presetToDays("limits-validity-preset", "limits-validity-until") };
    setBusy(limitsRenew, true);
    try {
      const res = await api(serverPath(`/clients/${encodeURIComponent(name)}/renew`), { method: "POST", body: JSON.stringify(body) });
      const resBody = await res.json().catch(() => ({}));
      if (!res.ok) {
        limitsRenewError.textContent = resBody.detail || "Couldn't renew certificate.";
        limitsRenewError.hidden = false;
        return;
      }
      limitsExpiry.textContent = resBody.expires ? `Certificate expires: ${formatDate(resBody.expires)}` : "";
      showToast(`Renewed "${name}" — the old profile stopped working.`);
      loadClients();
      limitsDialog.close();
      openDownload(name);
    } catch (err) {
      if (err.message !== "Session expired") {
        limitsRenewError.textContent = "Couldn't reach the server.";
        limitsRenewError.hidden = false;
      }
    } finally {
      setBusy(limitsRenew, false);
    }
  });

  // ---------- static IP (admin-only pin — never a user choice) ----------

  limitsIpSave.addEventListener("click", async () => {
    if (!pendingLimitsClient || !limitsIpEndpoint.value) return;
    const name = pendingLimitsClient.name;
    const endpoint = limitsIpEndpoint.value;
    const ip = limitsIpAddress.value.trim();
    if (!ip) { limitsIpError.textContent = "Enter an IP address, or use Clear to remove one."; limitsIpError.hidden = false; return; }
    limitsIpError.hidden = true;
    setBusy(limitsIpSave, true);
    try {
      const res = await api(serverPath(`/clients/${encodeURIComponent(name)}/static-ip`), {
        method: "PUT", body: JSON.stringify({ endpoint, static_ip: ip }),
      });
      const resBody = await res.json().catch(() => ({}));
      if (!res.ok) {
        limitsIpError.textContent = resBody.detail || "Couldn't save that IP.";
        limitsIpError.hidden = false;
        return;
      }
      pendingLimitsClient = resBody;
      renderLimitsIp();
      showToast(`Pinned "${name}" to ${ip} on ${endpoint}.`);
      loadClients();
    } catch (err) {
      if (err.message !== "Session expired") {
        limitsIpError.textContent = "Couldn't reach the server.";
        limitsIpError.hidden = false;
      }
    } finally {
      setBusy(limitsIpSave, false);
    }
  });

  limitsIpClear.addEventListener("click", async () => {
    if (!pendingLimitsClient || !limitsIpEndpoint.value) return;
    const name = pendingLimitsClient.name;
    const endpoint = limitsIpEndpoint.value;
    limitsIpError.hidden = true;
    try {
      const res = await api(serverPath(`/clients/${encodeURIComponent(name)}/static-ip`), {
        method: "PUT", body: JSON.stringify({ endpoint, static_ip: null }),
      });
      const resBody = await res.json().catch(() => ({}));
      if (!res.ok) {
        limitsIpError.textContent = resBody.detail || "Couldn't clear that IP.";
        limitsIpError.hidden = false;
        return;
      }
      pendingLimitsClient = resBody;
      renderLimitsIp();
      showToast(`Cleared the static IP for "${name}" on ${endpoint}.`);
      loadClients();
    } catch { /* handled by api() */ }
  });

  // ==================== SETTINGS PAGE ====================

  const passwordForm = document.getElementById("password-form");
  const passwordError = document.getElementById("password-error");
  const passwordSubmit = document.getElementById("password-submit");

  passwordForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    passwordError.hidden = true;
    const body = {
      current_password: document.getElementById("pw-current").value,
      new_password: document.getElementById("pw-new").value,
    };
    setBusy(passwordSubmit, true);
    try {
      const res = await api("/api/settings/password", { method: "PUT", body: JSON.stringify(body) });
      const resBody = await res.json().catch(() => ({}));
      if (!res.ok) {
        passwordError.textContent = resBody.detail || "Couldn't update password.";
        passwordError.hidden = false;
        return;
      }
      passwordForm.reset();
      showToast("Password updated.");
    } catch (err) {
      if (err.message !== "Session expired") {
        passwordError.textContent = "Couldn't reach the server.";
        passwordError.hidden = false;
      }
    } finally {
      setBusy(passwordSubmit, false);
    }
  });

  // ---------- security & lockout ----------

  const securityForm = document.getElementById("security-form");
  const securityError = document.getElementById("security-error");
  const securitySubmit = document.getElementById("security-submit");

  securityForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    securityError.hidden = true;
    const body = {
      session_timeout_minutes: parseInt(document.getElementById("sec-timeout").value, 10) || 0,
      lockout_max_attempts: parseInt(document.getElementById("sec-lockout-attempts").value, 10) || 5,
      lockout_seconds: parseInt(document.getElementById("sec-lockout-seconds").value, 10) || 300,
    };
    setBusy(securitySubmit, true);
    try {
      const res = await api("/api/settings/security", { method: "PUT", body: JSON.stringify(body) });
      const resBody = await res.json().catch(() => ({}));
      if (!res.ok) {
        securityError.textContent = resBody.detail || "Couldn't save.";
        securityError.hidden = false;
        return;
      }
      showToast("Session & lockout settings saved.");
    } catch (err) {
      if (err.message !== "Session expired") {
        securityError.textContent = "Couldn't reach the server.";
        securityError.hidden = false;
      }
    } finally {
      setBusy(securitySubmit, false);
    }
  });

  document.getElementById("logout-others-btn").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    setBusy(btn, true);
    try {
      const res = await api("/api/settings/logout-others", { method: "POST" });
      if (res.ok) showToast("Every other session has been signed out.");
      else showToast("Couldn't sign out other sessions.", "error");
    } catch { /* handled by api() */ }
    finally { setBusy(btn, false); }
  });

  // ---------- notifications ----------

  const notifyForm = document.getElementById("notify-form");
  const notifyError = document.getElementById("notify-error");
  const notifySubmit = document.getElementById("notify-submit");
  const notifyTest = document.getElementById("notify-test");

  notifyForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    notifyError.hidden = true;
    const body = {
      notify_webhook_enabled: document.getElementById("notify-webhook-enabled").checked,
      notify_webhook_url: document.getElementById("notify-webhook-url").value.trim(),
      notify_telegram_enabled: document.getElementById("notify-telegram-enabled").checked,
      notify_telegram_bot_token: document.getElementById("notify-telegram-token").value.trim(),
      notify_telegram_chat_id: document.getElementById("notify-telegram-chat").value.trim(),
    };
    setBusy(notifySubmit, true);
    try {
      const res = await api("/api/settings/notifications", { method: "PUT", body: JSON.stringify(body) });
      const resBody = await res.json().catch(() => ({}));
      if (!res.ok) {
        notifyError.textContent = resBody.detail || "Couldn't save.";
        notifyError.hidden = false;
        return;
      }
      showToast("Notification settings saved.");
    } catch (err) {
      if (err.message !== "Session expired") {
        notifyError.textContent = "Couldn't reach the server.";
        notifyError.hidden = false;
      }
    } finally {
      setBusy(notifySubmit, false);
    }
  });

  notifyTest.addEventListener("click", async () => {
    notifyError.hidden = true;
    setBusy(notifyTest, true);
    try {
      const res = await api("/api/settings/notifications/test", { method: "POST" });
      const resBody = await res.json().catch(() => ({}));
      if (!res.ok) {
        notifyError.textContent = resBody.detail || "Test failed.";
        notifyError.hidden = false;
        return;
      }
      showToast("Test notification sent.");
    } catch (err) {
      if (err.message !== "Session expired") {
        notifyError.textContent = "Couldn't reach the server.";
        notifyError.hidden = false;
      }
    } finally {
      setBusy(notifyTest, false);
    }
  });

  // ---------- client defaults ----------

  const defaultsForm = document.getElementById("defaults-form");
  const defaultsError = document.getElementById("defaults-error");
  const defaultsSubmit = document.getElementById("defaults-submit");

  defaultsForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    defaultsError.hidden = true;
    const body = {
      max_connections: parseInt(document.getElementById("def-max-conn").value, 10) || 1,
      quota_gb: document.getElementById("def-quota").value ? parseFloat(document.getElementById("def-quota").value) : null,
      bandwidth_mbps: document.getElementById("def-bandwidth").value ? parseFloat(document.getElementById("def-bandwidth").value) : null,
      validity_days: presetToDays("def-validity-preset", "def-validity-until"),
    };
    setBusy(defaultsSubmit, true);
    try {
      const res = await api("/api/settings/client-defaults", { method: "PUT", body: JSON.stringify(body) });
      const resBody = await res.json().catch(() => ({}));
      if (!res.ok) {
        defaultsError.textContent = resBody.detail || "Couldn't save.";
        defaultsError.hidden = false;
        return;
      }
      showToast("Client defaults saved.");
    } catch (err) {
      if (err.message !== "Session expired") {
        defaultsError.textContent = "Couldn't reach the server.";
        defaultsError.hidden = false;
      }
    } finally {
      setBusy(defaultsSubmit, false);
    }
  });

  // ---------- retention ----------

  const retentionForm = document.getElementById("retention-form");
  const retentionError = document.getElementById("retention-error");
  const retentionSubmit = document.getElementById("retention-submit");

  retentionForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    retentionError.hidden = true;
    const body = { points: parseInt(document.getElementById("retention-points").value, 10) || 60 };
    setBusy(retentionSubmit, true);
    try {
      const res = await api("/api/settings/retention", { method: "PUT", body: JSON.stringify(body) });
      const resBody = await res.json().catch(() => ({}));
      if (!res.ok) {
        retentionError.textContent = resBody.detail || "Couldn't save.";
        retentionError.hidden = false;
        return;
      }
      showToast("Retention saved.");
    } catch (err) {
      if (err.message !== "Session expired") {
        retentionError.textContent = "Couldn't reach the server.";
        retentionError.hidden = false;
      }
    } finally {
      setBusy(retentionSubmit, false);
    }
  });

  // ---------- backup & restore ----------

  const restoreDialog = document.getElementById("restore-dialog");
  const restoreForm = document.getElementById("restore-form");
  const restoreFile = document.getElementById("restore-file");
  const restoreConfirmText = document.getElementById("restore-confirm-text");
  const restoreError = document.getElementById("restore-error");
  const restoreSubmit = document.getElementById("restore-submit");

  document.getElementById("restore-open").addEventListener("click", () => {
    if (!restoreFile.files || !restoreFile.files.length) {
      showToast("Choose a backup file first.", "error");
      return;
    }
    restoreConfirmText.value = "";
    restoreError.hidden = true;
    restoreDialog.showModal();
  });
  document.getElementById("restore-cancel").addEventListener("click", () => restoreDialog.close());

  restoreForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    restoreError.hidden = true;
    if (restoreConfirmText.value.trim() !== "RESTORE") {
      restoreError.textContent = 'Type "RESTORE" (all caps) to confirm.';
      restoreError.hidden = false;
      return;
    }
    if (!restoreFile.files || !restoreFile.files.length) {
      restoreError.textContent = "Choose a backup file first.";
      restoreError.hidden = false;
      return;
    }
    setBusy(restoreSubmit, true);
    try {
      const fd = new FormData();
      fd.append("file", restoreFile.files[0]);
      const res = await fetch("/api/backup/restore", { method: "POST", credentials: "same-origin", body: fd });
      const resBody = await res.json().catch(() => ({}));
      if (!res.ok) {
        restoreError.textContent = resBody.detail || "Restore failed.";
        restoreError.hidden = false;
        return;
      }
      restoreDialog.close();
      showToast("Dashboard settings restored.");
    } catch {
      restoreError.textContent = "Couldn't reach the server.";
      restoreError.hidden = false;
    } finally {
      setBusy(restoreSubmit, false);
    }
  });

  // ---------- settings tabs ----------

  document.querySelectorAll(".settings-tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".settings-tab").forEach((t) => t.removeAttribute("aria-current"));
      tab.setAttribute("aria-current", "page");
      document.querySelectorAll(".settings-panel").forEach((p) => {
        p.hidden = p.id !== `settings-panel-${tab.dataset.tab}`;
      });
    });
  });

  // ---------- load settings page ----------

  async function loadSettingsPage() {
    try {
      const res = await api("/api/settings");
      if (!res.ok) return;
      const cfg = await res.json();
      document.getElementById("sec-timeout").value = cfg.session_timeout_minutes;
      document.getElementById("sec-lockout-attempts").value = cfg.lockout_max_attempts;
      document.getElementById("sec-lockout-seconds").value = cfg.lockout_seconds;
      document.getElementById("notify-webhook-enabled").checked = cfg.notify_webhook_enabled;
      document.getElementById("notify-webhook-url").value = cfg.notify_webhook_url || "";
      document.getElementById("notify-telegram-enabled").checked = cfg.notify_telegram_enabled;
      document.getElementById("notify-telegram-token").value = cfg.notify_telegram_bot_token || "";
      document.getElementById("notify-telegram-chat").value = cfg.notify_telegram_chat_id || "";
      document.getElementById("def-max-conn").value = cfg.client_default_max_connections;
      document.getElementById("def-quota").value = cfg.client_default_quota_gb ?? "";
      document.getElementById("def-bandwidth").value = cfg.client_default_bandwidth_mbps ?? "";
      daysToPresetSelection("def-validity-preset", "def-validity-until-wrap", "def-validity-until", cfg.client_default_validity_days);
      document.getElementById("retention-points").value = cfg.retention_points;
      document.getElementById("panel-version").textContent = `v${cfg.panel_version}`;
      const repoLink = document.getElementById("panel-repo-link");
      repoLink.href = cfg.panel_repo;
      repoLink.textContent = cfg.panel_repo.replace(/^https?:\/\//, "");
    } catch { /* handled by api() */ }
  }

  // ---------- boot ----------

  (async function init() {
    try {
      const res = await fetch("/api/servers", { credentials: "same-origin" });
      if (res.ok) showDashboard(); else showLogin();
    } catch { showLogin(); }
  })();
})();
