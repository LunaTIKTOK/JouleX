/* JouleX server-served UI (M1.5) — thin client over the live API.
 *
 * No build step, no dependencies beyond the two vendored crypto
 * files (sha256.js: JCS canonical JSON + SHA-256; ed25519.js:
 * SHA-512 + Ed25519 signing). A stranger can register an account,
 * load a demo agent, link it, and place a first paper trade — the
 * order is signed in this page with the demo agent's key and
 * verified by the server's CF-KYA verifier exactly like a Python
 * client's order. No admission bypass anywhere in this file.
 *
 * The demo agent seed is held in page memory only (never
 * localStorage, never sent anywhere except as a signature input
 * inside this page). Demo agents exist only on demo-clock servers
 * (GET /__demo/agents 404s elsewhere) and are labelled DEMO ONLY.
 */
(function () {
  "use strict";

  var H = window.JoulexHash;
  var Ed = window.JoulexEd;

  // -- small helpers ---------------------------------------------------
  function $(id) { return document.getElementById(id); }
  function notice(msg) { $("notice").textContent = msg; }
  function hexToBytes(hex) {
    var out = new Uint8Array(hex.length / 2);
    for (var i = 0; i < out.length; i++) out[i] = parseInt(hex.substr(i * 2, 2), 16);
    return out;
  }
  function utf8Encode(str) {
    // UTF-8 encode without TextEncoder (works in jsdom too).
    var bytes = [];
    for (var i = 0; i < str.length; i++) {
      var c = str.charCodeAt(i);
      if (c < 0x80) bytes.push(c);
      else if (c < 0x800) bytes.push(0xc0 | (c >> 6), 0x80 | (c & 0x3f));
      else if (c >= 0xd800 && c < 0xdc00) {
        var c2 = str.charCodeAt(++i);
        var cp = 0x10000 + ((c - 0xd800) << 10) + (c2 - 0xdc00);
        bytes.push(0xf0 | (cp >> 18), 0x80 | ((cp >> 12) & 0x3f),
                   0x80 | ((cp >> 6) & 0x3f), 0x80 | (cp & 0x3f));
      } else bytes.push(0xe0 | (c >> 12), 0x80 | ((c >> 6) & 0x3f), 0x80 | (c & 0x3f));
    }
    return new Uint8Array(bytes);
  }
  function base64urlEncode(bytes) {
    var alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    var out = "";
    for (var i = 0; i < bytes.length; i += 3) {
      var b0 = bytes[i], b1 = i + 1 < bytes.length ? bytes[i + 1] : 0;
      var b2 = i + 2 < bytes.length ? bytes[i + 2] : 0;
      out += alphabet[b0 >> 2];
      out += alphabet[((b0 & 0x03) << 4) | (b1 >> 4)];
      if (i + 1 < bytes.length) out += alphabet[((b1 & 0x0f) << 2) | (b2 >> 6)];
      if (i + 2 < bytes.length) out += alphabet[b2 & 0x3f];
    }
    return out;
  }
  async function api(path, opts) {
    var res = await fetch(path, opts || {});
    var text = await res.text();
    var json = null;
    try { json = JSON.parse(text); } catch (e) { /* non-JSON */ }
    if (!res.ok) {
      var reason = (json && (json.reason || json.error)) || text || ("HTTP " + res.status);
      throw new Error("HTTP " + res.status + ": " + reason);
    }
    return json;
  }

  // -- state (memory only; the demo seed is never persisted) -----------
  var sessionToken = null;
  var account = null;
  var agent = null; // {credential, agent_seed_hex, agent_id, principal_id}

  // -- CF-KYA signing ----------------------------------------------------
  async function signPresentationObject(payloadHash) {
    var ch = await api("/challenge");
    var signed = { challenge: ch.challenge, payload_hash: payloadHash, timestamp: ch.server_time };
    var seed = hexToBytes(agent.agent_seed_hex);
    var pub = hexToBytes(agent.credential.agent.public_key_hex);
    var sig = Ed.sign(utf8Encode(H.canonical(signed)), seed, pub);
    var sigHex = "";
    for (var i = 0; i < sig.length; i++) sigHex += ("0" + sig[i].toString(16)).slice(-2);
    return {
      credential: agent.credential,
      challenge: ch.challenge,
      payload_hash: payloadHash,
      timestamp: ch.server_time,
      presentation_signature_hex: sigHex,
    };
  }
  async function signPresentationHeader(payloadHash) {
    var presentation = await signPresentationObject(payloadHash);
    return base64urlEncode(utf8Encode(H.canonical(presentation)));
  }
  function orderPayloadHash(payload) {
    return H.sha256Hex(H.canonical(payload));
  }

  // -- rendering ---------------------------------------------------------
  async function refreshHealth() {
    try {
      var health = await api("/health");
      $("health-line").textContent =
        "Server ok — " + health.markets + " markets, " + health.hcs_messages +
        " HCS messages, mode " + health.hcs_mode +
        (health.hcs_topic ? " (topic " + health.hcs_topic + ")" : " (local log)") +
        ", time " + health.server_time;
    } catch (e) {
      $("health-line").textContent = "Server unreachable: " + e.message;
    }
  }
  async function refreshAeso() {
    try {
      var price = await api("/aeso/price");
      $("aeso-price").textContent =
        "AESO pool price: $" + price.pool_price + "/MWh (" + price.source + ", " +
        price.hour_start + ")";
    } catch (e) {
      $("aeso-price").textContent = "AESO pool price: unavailable (" + e.message + ")";
    }
  }
  async function refreshMarkets() {
    try {
      var markets = await api("/markets");
      var list = $("markets-list");
      list.innerHTML = "";
      var select = $("order-market");
      select.innerHTML = "";
      if (!markets.length) {
        list.textContent = "No markets yet.";
        return;
      }
      markets.forEach(function (m) {
        var card = document.createElement("div");
        card.className = "market-card";
        card.setAttribute("data-market-id", m.market_id);
        card.innerHTML =
          "<b>" + escapeHtml(m.question) + "</b><br>" +
          "status " + escapeHtml(m.status) + " · YES " + m.price_yes +
          " · NO " + m.price_no + " · " + escapeHtml(m.contract_type || "") +
          " · resolves " + escapeHtml(m.resolves_at || "");
        list.appendChild(card);
        var opt = document.createElement("option");
        opt.value = m.market_id;
        opt.textContent = m.market_id + " — " + m.question;
        select.appendChild(opt);
      });
    } catch (e) {
      $("markets-list").textContent = "Could not load markets: " + e.message;
    }
  }
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  async function refreshPositions() {
    if (!agent) return;
    try {
      var pos = await api("/positions?agent_id=" + encodeURIComponent(agent.agent_id));
      var html = "Balance: " + pos.balance_points + " points";
      if (pos.positions && pos.positions.length) {
        html += "<ul>" + pos.positions.map(function (p) {
          return "<li>" + escapeHtml(p.market_id) + " " + escapeHtml(p.side) +
            " " + p.shares + " shares</li>";
        }).join("") + "</ul>";
      } else html += " — no positions yet.";
      $("positions-list").innerHTML = html;
    } catch (e) {
      $("positions-list").textContent = "Could not load positions: " + e.message;
    }
  }
  async function refreshLedger() {
    try {
      var ledger = await api("/ledger");
      var ok = ledger.conservation && ledger.conservation.ok;
      $("ledger-view").textContent =
        "Ledger total " + (ledger.conservation ? ledger.conservation.total : "?") +
        " points — " + (ok ? "conserved" : "MISMATCH") +
        " (" + (ledger.entries ? ledger.entries.length : 0) + " entries)";
    } catch (e) {
      $("ledger-view").textContent = "Could not load ledger: " + e.message;
    }
  }
  async function refreshAudit() {
    try {
      var log = await api("/hcs-log?limit=20");
      var messages = (log && log.messages) || [];
      var tbody = document.querySelector("#audit-log tbody");
      tbody.innerHTML = "";
      messages.forEach(function (m) {
        var tr = document.createElement("tr");
        [m.sequence, m.action, (m.payload && m.payload.agent_id) || m.sender || "",
         m.consensus_timestamp || ""].forEach(function (v) {
          var td = document.createElement("td");
          td.textContent = String(v);
          tr.appendChild(td);
        });
        tbody.appendChild(tr);
      });
    } catch (e) {
      notice("Could not load audit log: " + e.message);
    }
  }
  async function refreshAll() {
    await Promise.all([refreshHealth(), refreshAeso(), refreshMarkets(),
                       refreshPositions(), refreshLedger(), refreshAudit()]);
  }

  // -- account -----------------------------------------------------------
  $("register-btn").addEventListener("click", async function () {
    try {
      account = await api("/accounts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email: $("account-email").value,
          password: $("account-password").value,
          display_name: $("account-name").value,
          role: $("account-role").value,
        }),
      });
      // Registration returns the public view only — log in for a token.
      var login = await api("/accounts/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email: $("account-email").value,
          password: $("account-password").value,
        }),
      });
      sessionToken = login.session_token;
      account = login.account;
      $("account-status").textContent =
        "Logged in as " + account.display_name + " (" + account.email + ", " + account.role + ").";
      notice("");
    } catch (e) {
      notice("Register failed: " + e.message);
    }
  });
  $("login-btn").addEventListener("click", async function () {
    try {
      var login = await api("/accounts/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email: $("account-email").value,
          password: $("account-password").value,
        }),
      });
      sessionToken = login.session_token;
      account = login.account;
      $("account-status").textContent =
        "Logged in as " + account.display_name + " (" + account.email + ", " + account.role + ").";
      notice("");
    } catch (e) {
      notice("Login failed: " + e.message);
    }
  });
  $("logout-btn").addEventListener("click", async function () {
    if (!sessionToken) { $("account-status").textContent = "Not logged in."; return; }
    try {
      await api("/accounts/logout", {
        method: "POST",
        headers: { "Content-Type": "application/json", "Authorization": "Bearer " + sessionToken },
        body: JSON.stringify({}),
      });
    } catch (e) { /* logout is idempotent; a dead token is already dead */ }
    sessionToken = null;
    account = null;
    $("account-status").textContent = "Not logged in.";
  });

  // -- demo agent --------------------------------------------------------
  $("demo-agent-btn").addEventListener("click", async function () {
    try {
      var agents = await api("/__demo/agents");
      agent = agents.alice;
      $("agent-status").textContent =
        "Demo agent loaded: " + agent.agent_id + " (DEMO ONLY — throwaway credential, seed in memory only).";
      notice("");
      await refreshPositions();
    } catch (e) {
      $("agent-status").textContent = "Demo agents unavailable on this server (" + e.message + ").";
    }
  });
  $("link-agent-btn").addEventListener("click", async function () {
    if (!sessionToken || !account) { notice("Log in first, then link an agent."); return; }
    if (!agent) { notice("Load a demo agent first."); return; }
    try {
      var payload = { type: "joulex.account_link", account_id: account.account_id, agent_id: agent.agent_id };
      var presentation = await signPresentationObject(H.sha256Hex(H.canonical(payload)));
      var linked = await api("/accounts/link-agent", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Authorization": "Bearer " + sessionToken,
        },
        body: JSON.stringify({ presentation: presentation, agent_id: agent.agent_id }),
      });
      account = linked;
      $("account-status").textContent =
        "Logged in as " + account.display_name + " — agent " + agent.agent_id + " linked.";
      notice("");
    } catch (e) {
      notice("Link failed: " + e.message);
    }
  });

  // -- order ---------------------------------------------------------------
  $("place-order-btn").addEventListener("click", async function () {
    if (!agent) { notice("Load a demo agent first."); return; }
    try {
      var payload = {
        agent_id: agent.agent_id,
        market_id: $("order-market").value,
        shares: parseFloat($("order-shares").value),
        side: $("order-side").value,
        action: $("order-action").value,
      };
      var header = await signPresentationHeader(orderPayloadHash(payload));
      var receipt = await api("/orders", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CF-KYA": header },
        body: JSON.stringify(payload),
      });
      $("order-result").textContent = JSON.stringify(receipt, null, 2);
      notice("");
      await Promise.all([refreshPositions(), refreshLedger(), refreshAudit(), refreshMarkets()]);
    } catch (e) {
      $("order-result").textContent = "Order failed: " + e.message;
    }
  });

  $("audit-verify-btn").addEventListener("click", async function () {
    try {
      var report = await api("/hcs-log/verify");
      $("audit-verify-result").textContent =
        report.ok ? "log verifies (" + report.messages + " messages)" : "LOG FAILED: " + JSON.stringify(report);
    } catch (e) {
      $("audit-verify-result").textContent = "Verify failed: " + e.message;
    }
  });

  refreshAll();
})();
