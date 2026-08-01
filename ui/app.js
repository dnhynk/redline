/* REDLINE — 두 화면의 렌더러.
 *
 * 렌더 원칙: 행은 살아 있는 요소다. 이벤트마다 목록 전체를 다시 짓지 않는다.
 * 골격은 문장 목록이 실제로 바뀔 때만 짓고, 이후에는 바뀐 조각만 갱신한다.
 * 진행 중인 애니메이션 위에서는 DOM 을 건드리지 않고 끝난 뒤로 미룬다 —
 * 시작한 애니메이션이 전부 끝나야 화면이 거짓말을 하지 않는다.
 */
(function () {
  "use strict";

  // ---------------------------------------------------------------- 사전

  var VERDICT_LABEL = {
    pending: "확인 중",
    supported: "뒷받침됨",
    unsupported: "뒷받침 안 됨",
    overstated: "지나친 단정",
    no_source: "출처 못 찾음",
    undecidable: "확인 불가",
    non_auditable: "의견·권고"
  };
  var VERDICT_ORDER = [
    "no_source",
    "non_auditable",
    "unsupported",
    "undecidable",
    "overstated",
    "supported",
    "pending"
  ];
  var MARKED = { supported: 1, unsupported: 1, overstated: 1, no_source: 1, undecidable: 1 };
  var STRUCTURAL_KINDS = { heading: 1, table_header: 1, code_fence: 1, divider: 1 };
  var NETWORK_TOOLS = { search_web: 1, search_scholar: 1, fetch_source: 1 };
  var AXIS_NAME = ["감사 준비", "논문·웹 출처 확인", "내용 확인", "반박 찾기"];
  var TERMINAL = {
    complete: { title: "감사 완료", reason: "설정된 감사 범위를 모두 확인했습니다.", outcome: "complete" },
    incomplete: { title: "부분 감사", reason: "감사를 끝내지 못한 항목이 있어 확인한 범위까지만 표시합니다.", outcome: "partial" },
    timebox: { title: "부분 감사", reason: "시간 제한에 도달해 확인한 범위까지만 표시합니다.", outcome: "partial" },
    max_turns: { title: "부분 감사", reason: "내부 반복 한도에 도달해 확인한 범위까지만 표시합니다.", outcome: "partial" },
    non_auditable: { title: "감사 대상 아님", reason: "", outcome: "non_auditable" },
    error: { title: "중단됨", reason: "", outcome: "error" }
  };
  var STAGES = [
    null,
    { target: "#galley", peek: 0 },
    { target: "#galley", peek: 0 },
    { target: "#galley", peek: 0 },
    { target: "#omissions-section", peek: 132 },
    { target: "#terminal-summary", peek: 0 }
  ];

  // ---------------------------------------------------------------- 잡동사니

  function $(sel) {
    return document.querySelector(sel);
  }
  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }
  function pad(n, width) {
    var s = String(n);
    while (s.length < width) s = "0" + s;
    return s;
  }
  function clip(s, n) {
    s = String(s == null ? "" : s).replace(/\s+/g, " ").trim();
    return s.length > n ? s.slice(0, n - 1) + "…" : s;
  }
  function domainOf(url) {
    try {
      return new URL(url).hostname.replace(/^www\./, "");
    } catch (err) {
      return String(url || "").replace(/^https?:\/\//, "").split("/")[0].replace(/^www\./, "");
    }
  }
  function safeHref(url) {
    return /^https?:\/\//i.test(String(url || "")) ? url : null;
  }
  function markColor(verdict) {
    return MARKED[verdict] ? "var(--v-" + verdict + "-line)" : "var(--rule)";
  }
  function pct(x) {
    return Math.round(Math.max(0, Math.min(1, Number(x) || 0)) * 100);
  }

  // ---------------------------------------------------------------- 계측 훅

  var motion = { start: 0, end: 0, cancel: 0, names: {} };
  window.__redlineMotion = motion;
  window.__redlineStageLog = [];

  document.addEventListener(
    "animationstart",
    function (e) {
      motion.start++;
      motion.names[e.animationName] = motion.names[e.animationName] || { start: 0, end: 0 };
      motion.names[e.animationName].start++;
    },
    true
  );
  document.addEventListener(
    "animationcancel",
    function (e) {
      motion.cancel++;
    },
    true
  );

  var forceReduced = false;
  Object.defineProperty(window, "__redlineForceReduced", {
    get: function () {
      return forceReduced;
    },
    set: function (v) {
      forceReduced = !!v;
      document.documentElement.classList.toggle("force-reduced", forceReduced);
    }
  });
  function reduced() {
    return forceReduced || window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  }

  // ---------------------------------------------------------------- 모션 조율
  //
  // 한 요소 위에서 애니메이션이 도는 동안에는 그 요소를 건드리지 않는다.
  // 미뤄 둔 일은 animationend 에서 처리한다 — 그래서 시작한 것은 전부 끝난다.

  var deferred = [];

  function busy(node) {
    if (!node || typeof node.getAnimations !== "function") return false;
    var running = node.getAnimations({ subtree: true });
    for (var i = 0; i < running.length; i++) {
      var a = running[i];
      var isCss = typeof CSSAnimation !== "undefined" ? a instanceof CSSAnimation : !!a.animationName;
      if (isCss && (a.playState === "running" || a.playState === "pending")) return true;
    }
    return false;
  }

  function whenIdle(node, key, fn) {
    if (!busy(node)) {
      fn();
      return;
    }
    for (var i = 0; i < deferred.length; i++) {
      if (deferred[i].node === node && deferred[i].key === key) {
        deferred[i].fn = fn;
        return;
      }
    }
    deferred.push({ node: node, key: key, fn: fn });
  }

  function flushDeferred() {
    if (!deferred.length) return;
    var keep = [];
    var ready = [];
    for (var i = 0; i < deferred.length; i++) {
      if (deferred[i].node.isConnected && busy(deferred[i].node)) keep.push(deferred[i]);
      else if (deferred[i].node.isConnected) ready.push(deferred[i]);
    }
    deferred = keep;
    for (var j = 0; j < ready.length; j++) ready[j].fn();
  }

  function fireOnce(node, cls) {
    if (!node) return;
    if (node.dataset.anim) {
      node.dataset.animQueue = cls;
      return;
    }
    node.classList.add(cls);
    var live = typeof node.getAnimations === "function" && node.getAnimations().length > 0;
    if (!live) {
      node.classList.remove(cls);
      return;
    }
    node.dataset.anim = cls;
  }

  document.addEventListener(
    "animationend",
    function (e) {
      motion.end++;
      var rec = motion.names[e.animationName];
      if (rec) rec.end++;
      var node = e.target;
      if (node && node.nodeType === 1 && node.dataset && node.dataset.anim) {
        var cls = node.dataset.anim;
        node.classList.remove(cls);
        delete node.dataset.anim;
        var queued = node.dataset.animQueue;
        if (queued) {
          delete node.dataset.animQueue;
          requestAnimationFrame(function () {
            fireOnce(node, queued);
          });
        }
      }
      requestAnimationFrame(flushDeferred);
    },
    true
  );

  setInterval(flushDeferred, 400);

  // ---------------------------------------------------------------- 상태

  var state = {
    run: null,
    lastSeq: 0,
    seen: {},
    audit: null,
    status: null,
    timebox: 90,
    mock: false,
    stage: 0,
    following: false,
    burstUntil: 0,
    rowSig: "",
    rows: [],
    pctSeen: {},
    verdictSeen: {},
    foldOpen: false,
    outputItems: {},
    args: {},
    netCalls: 0,
    elapsed: 0,
    elapsedWall: 0,
    done: false,
    paused: false
  };
  window.__redlineStage = function () {
    return { stage: state.stage, following: state.following, run: state.run };
  };

  var isMain = !!$("#galley");
  var isRaw = !!$("#raw-events");

  // ---------------------------------------------------------------- 소켓

  var ws = null;
  var retry = null;

  function setConn(cls, text) {
    var label = $("#connection-label");
    if (!label) return;
    label.textContent = text;
    label.classList.toggle("is-live", cls === "live");
  }

  function connect() {
    var proto = location.protocol === "https:" ? "wss" : "ws";
    var qs = state.run ? "?last_seq=" + state.lastSeq + "&run=" + encodeURIComponent(state.run) : "";
    ws = new WebSocket(proto + "://" + location.host + "/ws/events" + qs);
    window.__redlineWs = ws;
    state.burstUntil = Date.now() + 800;
    ws.onopen = function () {
      setConn("live", "연결됨");
    };
    ws.onclose = function () {
      setConn("", "재연결 중");
      clearTimeout(retry);
      retry = setTimeout(connect, 800);
    };
    ws.onerror = function () {
      setConn("", "연결 오류");
    };
    ws.onmessage = function (e) {
      var event;
      try {
        event = JSON.parse(e.data);
      } catch (err) {
        return;
      }
      handle(event);
    };
  }

  window.__redlineValidationEvent = handle;

  function handle(event) {
    if (!event || !event.kind) return;
    if (event.kind === "config") {
      var cfg = event.payload || {};
      if (cfg.timebox_s) state.timebox = cfg.timebox_s;
      setMock(!!cfg.mock);
      paintBudget();
      return;
    }
    if (event.run && event.run !== state.run) resetRun(event.run);
    var key = String(event.run) + ":" + String(event.seq);
    if (state.seen[key]) return;
    state.seen[key] = 1;
    if (typeof event.seq === "number" && event.seq > state.lastSeq) state.lastSeq = event.seq;

    if (event.kind === "audit") {
      state.audit = event.payload || {};
      if (state.audit.source_mode === "mock") setMock(true);
    } else if (event.kind === "status") {
      state.status = event.payload || {};
      if (state.status.source_mode === "mock") setMock(true);
      if (typeof state.status.elapsed_s === "number") {
        state.elapsed = state.status.elapsed_s;
        state.elapsedWall = Date.now();
      }
      if (state.status.done) state.done = true;
    }

    if (isRaw) appendRaw(event);
    if (isMain) paintMain(event);
    else paintRawState(event);
  }

  function setMock(on) {
    if (state.mock === on) return;
    state.mock = on;
    var banner = $("#source-banner");
    if (banner) banner.hidden = !on;
    document.body.classList.toggle("has-banner", on);
  }

  function resetRun(run) {
    state.run = run;
    state.lastSeq = 0;
    state.seen = {};
    state.audit = null;
    state.status = null;
    state.stage = 0;
    state.rowSig = "";
    state.rows = [];
    state.pctSeen = {};
    state.verdictSeen = {};
    state.foldOpen = false;
    state.outputItems = {};
    state.args = {};
    state.netCalls = 0;
    state.elapsed = 0;
    state.done = false;
    deferred = [];
    if (isMain) {
      $("#sentences").textContent = "";
      $("#omissions").textContent = "";
      $("#final-report").textContent = "";
      $("#missing-actions").textContent = "";
      $("#terminal-summary").hidden = true;
      $("#omissions-empty").hidden = false;
      $("#galley-empty").hidden = false;
      paintStageRail();
    }
    if (isRaw) {
      $("#raw-events").textContent = "";
      $("#state-claims").textContent = "";
      $("#state-empty").hidden = false;
    }
  }

  // ================================================================ 메인 화면

  function claimsOf(audit) {
    return (audit && Array.isArray(audit.claims) ? audit.claims : []).slice();
  }
  function sentencesOf(audit) {
    return audit && Array.isArray(audit.sentences) ? audit.sentences : [];
  }
  function kindsOf(audit) {
    var s = sentencesOf(audit);
    var k = audit && audit.sentence_kinds;
    if (!Array.isArray(k) || k.length !== s.length) return s.map(function () { return "prose"; });
    return k.map(function (x) { return typeof x === "string" ? x : "prose"; });
  }

  function ledger(audit) {
    var map = {};
    var recs = audit && Array.isArray(audit.evidence) ? audit.evidence : [];
    for (var i = 0; i < recs.length; i++) if (recs[i] && recs[i].id) map[recs[i].id] = recs[i];
    return map;
  }

  function paintMain(event) {
    var audit = state.audit;
    if (audit && sentencesOf(audit).length) {
      $("#galley-empty").hidden = true;
      ensureRows(audit);
      updateRows(audit);
    }
    paintTally(audit);
    paintOmissions(audit);
    if (state.status) paintStatus(state.status);
    driveStage(event);
  }

  function paintPreview(text) {
    var host = $("#sentences");
    host.textContent = "";
    state.rowSig = "";
    $("#galley-empty").hidden = true;
    var row = el("div", "s-row is-preview");
    row.appendChild(el("span", "s-num", "··"));
    row.appendChild(el("div", "s-text", text));
    var margin = el("div", "s-margin");
    var bare = el("div", "bare");
    bare.appendChild(el("i", "swatch", ""));
    bare.lastChild.setAttribute("data-mark", "pending");
    bare.appendChild(el("span", null, "원고 접수 — 문장 단위로 나누는 중"));
    margin.appendChild(bare);
    row.appendChild(margin);
    host.appendChild(row);
  }

  // 행 골격은 문장 목록·종류가 실제로 바뀔 때만 짓는다
  function ensureRows(audit) {
    var sentences = sentencesOf(audit);
    var kinds = kindsOf(audit);
    var sig = JSON.stringify([sentences, kinds]);
    if (sig === state.rowSig) return;
    state.rowSig = sig;
    var host = $("#sentences");
    host.textContent = "";
    state.rows = [];
    var number = 0;
    for (var i = 0; i < sentences.length; i++) {
      var kind = kinds[i];
      var structural = !!STRUCTURAL_KINDS[kind];
      var row = el("div", "s-row kind-" + kind + (structural ? " is-structural" : ""));
      row.dataset.i = String(i);
      if (!structural) number++;
      row.appendChild(el("span", "s-num", structural ? "" : pad(number, 2)));
      var text = el("div", "s-text");
      text.textContent = sentences[i];
      text.dataset.sig = "";
      row.appendChild(text);
      var margin = el("div", "s-margin");
      row.appendChild(margin);
      host.appendChild(row);
      state.rows.push({
        row: row,
        text: text,
        margin: margin,
        kind: kind,
        structural: structural,
        sentence: sentences[i]
      });
    }
    var note = el("p", "fold-note");
    note.hidden = true;
    note.appendChild(el("button", null, ""));
    note.firstChild.type = "button";
    note.firstChild.setAttribute("aria-expanded", "false");
    note.firstChild.addEventListener("click", function () {
      state.foldOpen = !state.foldOpen;
      applyFold();
    });
    host.appendChild(note);
    state.foldNote = note;
  }

  function updateRows(audit) {
    var claims = claimsOf(audit);
    var byIndex = {};
    for (var i = 0; i < claims.length; i++) {
      var idx = claims[i].index;
      (byIndex[idx] = byIndex[idx] || []).push(claims[i]);
    }
    var evidence = ledger(audit);
    for (var r = 0; r < state.rows.length; r++) {
      var slot = state.rows[r];
      var mine = byIndex[r] || [];
      updateManuscript(slot, mine);
      updateMargin(slot, mine, evidence);
    }
    applyFold();
  }

  // 원고 열: 클레임 구절에만 밑줄. 앵커 집합이 바뀔 때만 다시 짓는다.
  function updateManuscript(slot, claims) {
    var anchors = anchorsFor(slot.sentence, claims);
    var sig = JSON.stringify(
      anchors.map(function (a) {
        return [a.id, a.start, a.end];
      })
    );
    if (slot.text.dataset.sig !== sig) {
      whenIdle(slot.text, "markup", function () {
        slot.text.dataset.sig = sig;
        slot.text.innerHTML = buildMarkup(slot.sentence, anchors);
        for (var k = 0; k < claims.length; k++) {
          var born = slot.text.querySelector('[data-cid="' + claims[k].id + '"]');
          if (born) applyMark(born, claims[k].id, claims[k].verdict || "pending", slot.row);
        }
      });
    }
    // 판정색은 살아 있는 span 위에서 바뀐다 — 노드를 갈아 끼우지 않는다
    for (var i = 0; i < claims.length; i++) {
      var claim = claims[i];
      var span = slot.text.querySelector('[data-cid="' + claim.id + '"]');
      if (!span) continue;
      applyMark(span, claim.id, claim.verdict || "pending", slot.row);
    }
  }

  function applyMark(span, cid, verdict, row) {
    whenIdle(span, "mark", function () {
      if (span.dataset.mark === verdict) return;
      span.dataset.mark = verdict;
      if (MARKED[verdict]) {
        fireOnce(span, "draw");
        fireOnce(row, "land");
      }
    });
  }

  function anchorsFor(sentence, claims) {
    var taken = [];
    var out = [];
    for (var i = 0; i < claims.length; i++) {
      var needle = String(claims[i].text || "").trim().replace(/[.。!?！？,·:;]+$/u, "");
      if (needle.length < 2) continue;
      var at = sentence.indexOf(needle);
      if (at < 0) {
        var loose = needle.replace(/\s+/g, " ");
        at = sentence.replace(/\s+/g, " ").indexOf(loose);
        if (at < 0) {
          out.push({ id: claims[i].id, start: -1, end: -1, quote: needle });
          continue;
        }
      }
      var end = at + needle.length;
      var clash = false;
      for (var t = 0; t < taken.length; t++) {
        if (at < taken[t][1] && end > taken[t][0]) clash = true;
      }
      if (clash) {
        out.push({ id: claims[i].id, start: -1, end: -1, quote: needle });
        continue;
      }
      taken.push([at, end]);
      out.push({ id: claims[i].id, start: at, end: end, quote: needle });
    }
    return out.sort(function (a, b) {
      return a.start - b.start;
    });
  }

  function buildMarkup(sentence, anchors) {
    var placed = anchors.filter(function (a) {
      return a.start >= 0;
    });
    if (!placed.length) return esc(sentence);
    var html = "";
    var cursor = 0;
    for (var i = 0; i < placed.length; i++) {
      var a = placed[i];
      html += esc(sentence.slice(cursor, a.start));
      html += '<span class="claim-mark" data-cid="' + esc(a.id) + '">' + esc(sentence.slice(a.start, a.end)) + "</span>";
      cursor = a.end;
    }
    return html + esc(sentence.slice(cursor));
  }

  // 여백 열: 클레임 id 로 재사용하고 조각만 갱신한다
  function updateMargin(slot, claims, evidence) {
    if (slot.structural) return;
    var margin = slot.margin;
    if (!claims.length) {
      var bare = margin.querySelector(".bare");
      if (margin.querySelector(".mark")) {
        margin.textContent = "";
        bare = null;
      }
      if (!bare) {
        bare = el("div", "bare");
        var ring = el("i", "swatch");
        ring.setAttribute("data-mark", "pending");
        bare.appendChild(ring);
        bare.appendChild(el("span"));
        margin.appendChild(bare);
      }
      var label = bareLabel();
      if (bare.lastChild.textContent !== label) bare.lastChild.textContent = label;
      slot.row.dataset.verdict = "unaudited";
      return;
    }
    if (margin.querySelector(".bare")) margin.textContent = "";
    var order = [];
    for (var i = 0; i < claims.length; i++) {
      var claim = claims[i];
      var node = margin.querySelector('.mark[data-cid="' + claim.id + '"]');
      if (!node) {
        node = buildMark(claim);
        margin.appendChild(node);
        fireOnce(node, "settle");
      }
      order.push(node);
      updateMark(node, claim, evidence, slot);
    }
    slot.row.dataset.verdict = claims[0].verdict || "pending";
  }

  function bareLabel() {
    if (!state.done) return "확인 중";
    var reason = state.status && state.status.reason;
    if (reason === "complete" || reason === "non_auditable") return VERDICT_LABEL.non_auditable;
    if (reason === "incomplete") return "확인 못 함 · 미완료";
    return "확인 못 함 · 중단됨";
  }

  function buildMark(claim) {
    var node = el("div", "mark");
    node.dataset.cid = claim.id;
    var swatch = el("i", "swatch mark-swatch");
    swatch.setAttribute("data-mark", "pending");
    node.appendChild(swatch);
    var head = el("div", "mark-head");
    head.appendChild(el("span", "mark-label", VERDICT_LABEL.pending));
    head.appendChild(el("span", "mark-id", claim.id));
    node.appendChild(head);
    if (claim.auditable !== false) {
      var conf = el("div", "mark-conf");
      var track = el("span", "bar-track");
      track.appendChild(el("i", "bar-fill"));
      track.title =
        "확보한 지지 근거의 양(진실 확률 아님) — 접근 실패·반증 미발견은 점수를 움직이지 않습니다";
      conf.appendChild(track);
      var read = el("span", "mark-pct");
      read.title = track.title;
      conf.appendChild(read);
      node.appendChild(conf);
    }
    node.appendChild(el("div", "mark-chips"));
    var quote = el("p", "mark-quote");
    quote.hidden = true;
    node.appendChild(quote);
    return node;
  }

  function updateMark(node, claim, evidence, slot) {
    var verdict = claim.verdict || "pending";
    var swatch = node.querySelector(".mark-swatch");
    if (swatch.getAttribute("data-mark") !== verdict) {
      swatch.setAttribute("data-mark", verdict);
      var label = node.querySelector(".mark-label");
      label.textContent = VERDICT_LABEL[verdict] || VERDICT_LABEL.pending;
      label.style.color = "var(--v-" + verdict + "-label)";
      fireOnce(node, "settle");
    }

    var conf = node.querySelector(".mark-conf");
    if (conf) {
      var now = pct(claim.confidence);
      var seen = state.pctSeen[claim.id];
      if (!seen) state.pctSeen[claim.id] = seen = { prev: null, now: now };
      else if (seen.now !== now) {
        seen.prev = seen.now;
        seen.now = now;
      }
      var fill = conf.querySelector(".bar-fill");
      fill.style.background = markColor(verdict);
      var target = "scaleX(" + now / 100 + ")";
      if (fill.dataset.born !== "1") {
        fill.dataset.born = "1";
        requestAnimationFrame(function () {
          fill.style.transform = target;
        });
      } else if (fill.style.transform !== target) {
        fill.style.transform = target;
      }
      var read = conf.querySelector(".mark-pct");
      var html = seen.prev == null ? '<span class="now">' + now + "%</span>"
        : seen.prev + "% → " + '<span class="now">' + now + "%</span>";
      if (read.innerHTML !== html) read.innerHTML = html;
    }

    var chips = chipsFor(claim, evidence);
    var host = node.querySelector(".mark-chips");
    var chipSig = JSON.stringify(chips);
    if (host.dataset.sig !== chipSig) {
      host.dataset.sig = chipSig;
      host.textContent = "";
      var show = chips.slice(0, 2);
      for (var i = 0; i < show.length; i++) {
        var chip = show[i];
        var link = safeHref(chip.url);
        var node2 = el(link ? "a" : "span", "chip", chip.label);
        if (link) {
          node2.href = link;
          node2.target = "_blank";
          node2.rel = "noopener";
        }
        node2.title = chip.title;
        host.appendChild(node2);
      }
      if (chips.length > show.length) {
        var more = el("span", "chip more", "+" + (chips.length - show.length));
        more.title = chips
          .slice(show.length)
          .map(function (c) {
            return c.title;
          })
          .join("\n");
        host.appendChild(more);
      }
    }

    var anchor = slot.text.querySelector('[data-cid="' + claim.id + '"]');
    var quote = node.querySelector(".mark-quote");
    if (!anchor && claim.text) {
      var line = "「" + clip(claim.text, 60) + "」";
      if (quote.textContent !== line) quote.textContent = line;
      quote.hidden = false;
    } else if (!quote.hidden) {
      quote.hidden = true;
    }
  }

  function chipsFor(claim, evidence) {
    var seenId = {};
    var domains = [];
    var byDomain = {};
    var axes = Array.isArray(claim.axis_results) ? claim.axis_results : [];
    for (var a = 0; a < axes.length; a++) {
      var ids = axes[a].evidence_ids || [];
      var urls = axes[a].source_urls || [];
      for (var i = 0; i < Math.max(ids.length, urls.length); i++) {
        var id = ids[i];
        var url = urls[i] || (evidence[id] && evidence[id].url);
        if (!url || (id && seenId[id])) continue;
        if (id) seenId[id] = 1;
        var dom = domainOf(url);
        if (!byDomain[dom]) {
          byDomain[dom] = { domain: dom, url: url, count: 0, cites: null, titles: [] };
          domains.push(byDomain[dom]);
        }
        byDomain[dom].count++;
        var rec = evidence[id];
        if (rec) {
          byDomain[dom].titles.push((id || "") + " · " + (rec.tool || "") + " · " + (rec.title || ""));
          var cc = rec.extra && rec.extra.citation_count;
          if (typeof cc === "number" && byDomain[dom].cites == null) byDomain[dom].cites = cc;
        } else if (id) {
          byDomain[dom].titles.push(id);
        }
      }
    }
    return domains.map(function (d) {
      var label = d.domain;
      if (d.count > 1) label += " ·" + d.count;
      if (d.cites != null) label += " · 인용 " + d.cites + "회";
      return { label: label, url: d.url, title: d.titles.join("\n") || d.domain };
    });
  }

  // 감사하지 않은 문장 접기 — 완주 종결 뒤에만, 지우지 않고 접는다
  function applyFold() {
    if (!state.foldNote) return;
    var reason = state.status && state.status.reason;
    var eligible = state.done && (reason === "complete" || reason === "non_auditable");
    var count = 0;
    for (var i = 0; i < state.rows.length; i++) {
      var slot = state.rows[i];
      var fold = eligible && !slot.structural && slot.row.dataset.verdict === "unaudited";
      if (fold) count++;
      slot.row.classList.toggle("is-folded", fold && !state.foldOpen);
    }
    state.foldNote.hidden = !count;
    if (count) {
      var button = state.foldNote.firstChild;
      button.textContent = state.foldOpen ? count + "개 접기" : "감사하지 않은 문장 " + count + "개 — 모두 보기";
      button.setAttribute("aria-expanded", state.foldOpen ? "true" : "false");
    }
  }

  // ---------------------------------------------------------------- 집계행

  function paintTally(audit) {
    if (!audit) return;
    var claims = claimsOf(audit);
    var audited = claims.filter(function (c) {
      return (c.axis_results || []).length > 0;
    });
    var rate = Array.isArray(audit.unsupported_rate)
      ? audit.unsupported_rate
      : [
          claims.filter(function (c) {
            return c.verdict === "unsupported" || c.verdict === "overstated";
          }).length,
          audited.length
        ];
    var noSource =
      typeof audit.no_source_count === "number"
        ? audit.no_source_count
        : claims.filter(function (c) {
            return c.verdict === "no_source";
          }).length;
    var kinds = kindsOf(audit);
    var claimable = kinds.filter(function (k) {
      return !STRUCTURAL_KINDS[k];
    }).length;
    var cov = Array.isArray(audit.coverage)
      ? audit.coverage
      : [
          Object.keys(
            audited.reduce(function (acc, c) {
              acc[c.index] = 1;
              return acc;
            }, {})
          ).length,
          claimable
        ];

    setMetric($("#unsupported-rate"), rate[0], rate[1]);
    setMetric($("#coverage"), cov[0], cov[1]);
    setText($("#no-source-count"), "출처 못 찾음 " + noSource + "건");
    setText($("#claim-count"), "등록 클레임 " + claims.length + "건");

    var total = typeof audit.evidence_total === "number" ? audit.evidence_total : (audit.evidence || []).length;
    var cited = typeof audit.evidence_cited === "number" ? audit.evidence_cited : (audit.evidence || []).length;
    setText($("#evidence-summary"), "실제 받은 검색 결과 " + total + "건 · 판정에 인용 " + cited + "건");

    var axis = (state.status && state.status.axis) || 0;
    var cells = $("#axis").children;
    for (var i = 0; i < cells.length; i++) cells[i].classList.toggle("on", i < axis);
  }

  function setMetric(node, a, b) {
    var html = a + ' <span class="sep">/</span> ' + b;
    if (node.innerHTML === html) return;
    node.innerHTML = html;
    fireOnce(node, "settle");
  }
  function setText(node, text) {
    if (node && node.textContent !== text) node.textContent = text;
  }

  // ---------------------------------------------------------------- 반박

  function paintOmissions(audit) {
    var host = $("#omissions");
    var list = (audit && Array.isArray(audit.omissions) ? audit.omissions : []).slice();
    if (!list.length) {
      $("#omissions-empty").hidden = false;
      if (state.done) {
        $("#omissions-empty").lastElementChild.textContent =
          "탐색 완료 · 이 글에 대한 반박 0건. 반박까지 찾아봤지만 나오지 않았습니다 — 0건도 결과입니다.";
        $("#omissions-empty").firstElementChild.textContent = "0";
      }
      return;
    }
    $("#omissions-empty").hidden = true;
    for (var i = 0; i < list.length; i++) {
      var om = list[i];
      var key = om.claim_id + "/" + om.evidence_id;
      if (host.querySelector('[data-key="' + CSS.escape(key) + '"]')) continue;
      host.appendChild(buildOmission(om, key));
    }
  }

  function buildOmission(om, key) {
    var href = safeHref(om.url);
    var card = el(href ? "a" : "div", "rebut-card");
    card.dataset.key = key;
    if (href) {
      card.href = href;
      card.target = "_blank";
      card.rel = "noopener";
    }
    var brow = el("div", "rebut-brow");
    brow.appendChild(el("span", null, "반박 근거 · " + om.claim_id));
    if (om.url) brow.appendChild(el("span", "chip", domainOf(om.url)));
    card.appendChild(brow);
    card.appendChild(el("h3", null, om.title || om.url || "제목 없음"));
    if (om.summary) card.appendChild(el("p", null, om.summary));
    var meta = [];
    if (om.date) meta.push(om.date);
    if (typeof om.citation_count === "number") meta.push("인용 " + om.citation_count + "회");
    if (href) meta.push("↗");
    if (meta.length) card.appendChild(el("p", "rebut-meta", meta.join(" · ")));
    fireOnce(card, "arrive");
    return card;
  }

  // ---------------------------------------------------------------- 종결

  function paintStatus(status) {
    var badge = $("#phase-badge");
    if (!status.done) {
      var axis = status.axis || 0;
      badge.textContent = axis ? AXIS_NAME[axis] + " 진행 중" : "감사 준비 중";
      badge.removeAttribute("data-outcome");
    } else {
      var map = TERMINAL[status.reason] || TERMINAL.error;
      badge.textContent =
        map.outcome === "complete" ? "감사 완료"
          : map.outcome === "partial" ? "부분 감사"
          : map.outcome === "non_auditable" ? "감사 대상 아님"
          : "중단됨";
      badge.setAttribute("data-outcome", map.outcome);
      paintTerminal(status, map);
    }
  }

  function paintTerminal(status, map) {
    var section = $("#terminal-summary");
    section.hidden = false;
    section.setAttribute("data-outcome", map.outcome);
    setText($("#terminal-title"), map.title);
    var reason = map.reason;
    if (status.reason === "non_auditable")
      reason = status.reason_detail || "검증 가능한 사실 주장을 찾지 못했습니다.";
    if (status.reason === "error") reason = status.error || "감사가 중단됐습니다.";
    setText($("#terminal-reason"), reason);

    var missing = (status.completion && status.completion.missing_actions) || [];
    var list = $("#missing-actions");
    list.hidden = !missing.length;
    if (missing.length && list.dataset.sig !== JSON.stringify(missing)) {
      list.dataset.sig = JSON.stringify(missing);
      list.textContent = "";
      for (var i = 0; i < missing.length; i++) list.appendChild(el("li", null, missing[i]));
    }

    var audit = status.audit || state.audit || {};
    var box = $("#terminal-coverage");
    if (map.outcome === "partial" && Array.isArray(audit.coverage)) {
      box.hidden = false;
      box.querySelector(".metric").innerHTML =
        audit.coverage[0] + ' <span class="sep">/</span> ' + audit.coverage[1];
    } else {
      box.hidden = true;
    }

    var report = $("#final-report");
    var md = status.final_report || "";
    if (report.dataset.sig !== md) {
      report.dataset.sig = md;
      report.innerHTML = md ? renderReport(md) : "";
      $(".report-label").hidden = !md;
    }
  }

  // ---------------------------------------------------------------- 보고 본문
  //
  // 전부 이스케이프한 뒤 제한 서식만 되살린다.

  var OPEN = "\u0001";
  var CLOSE = "\u0002";

  function inline(text) {
    var s = esc(text);
    s = s.replace(/\*\*([^*]+)\*\*/g, OPEN + "B" + "$1" + CLOSE + "B");
    s = s.replace(
      /\b(no_source|non_auditable|unsupported|undecidable|overstated|supported|pending)\b/g,
      function (m, name) {
        return OPEN + "V" + name + "|" + VERDICT_LABEL[name] + CLOSE + "V";
      }
    );
    s = s.replace(/\bC(\d{1,2})\b/g, OPEN + "C" + "C$1" + CLOSE + "C");
    s = s.replace(
      /(\d+(?:\.\d+)?\s*\/\s*\d+|\d+(?:\.\d+)?(?:%|건|회|개|배|초|s))/g,
      OPEN + "N" + "$1" + CLOSE + "N"
    );
    return s
      .replace(new RegExp(OPEN + "B", "g"), "<b>")
      .replace(new RegExp(CLOSE + "B", "g"), "</b>")
      .replace(new RegExp(OPEN + "V(\\w+)\\|", "g"), '<span class="v" data-v="$1">')
      .replace(new RegExp(CLOSE + "V", "g"), "</span>")
      .replace(new RegExp(OPEN + "C", "g"), '<span class="cid">')
      .replace(new RegExp(CLOSE + "C", "g"), "</span>")
      .replace(new RegExp(OPEN + "N", "g"), '<span class="num">')
      .replace(new RegExp(CLOSE + "N", "g"), "</span>");
  }

  function verdictIn(text) {
    var found = null;
    for (var i = 0; i < VERDICT_ORDER.length; i++) {
      if (new RegExp("\\b" + VERDICT_ORDER[i] + "\\b").test(text)) {
        found = VERDICT_ORDER[i];
        break;
      }
    }
    return found;
  }

  function renderReport(md) {
    var lines = String(md).replace(/\r\n/g, "\n").split("\n");
    var blocks = [];
    var para = [];
    var items = null;
    function flushPara() {
      if (para.length) blocks.push({ type: "p", text: para.join(" ") });
      para = [];
    }
    function flushItems() {
      if (items) blocks.push({ type: "ul", items: items });
      items = null;
    }
    for (var i = 0; i < lines.length; i++) {
      var line = lines[i].replace(/\s+$/, "");
      var head = /^(#{1,4})\s+(.*)$/.exec(line);
      var item = /^\s*[-*]\s+(.*)$/.exec(line);
      if (head) {
        flushPara();
        flushItems();
        blocks.push({ type: "h", level: head[1].length, text: head[2] });
      } else if (item) {
        flushPara();
        if (!items) items = [];
        items.push(item[1]);
      } else if (!line.trim()) {
        flushPara();
        flushItems();
      } else {
        flushItems();
        para.push(line.trim());
      }
    }
    flushPara();
    flushItems();

    var html = "";
    var fix = -1;
    for (var b = 0; b < blocks.length; b++) {
      if (blocks[b].type === "h" && blocks[b].text.trim() === "추천 수정안") {
        fix = b;
        break;
      }
    }
    var fixEnd = blocks.length;
    if (fix >= 0) {
      for (var e = fix + 1; e < blocks.length; e++) {
        if (blocks[e].type === "h" && blocks[e].level <= blocks[fix].level) {
          fixEnd = e;
          break;
        }
      }
    }
    for (var n = 0; n < blocks.length; n++) {
      if (n === fix) html += '<section class="fixbox">';
      html += renderBlock(blocks[n], fix >= 0 && n >= fix && n < fixEnd);
      if (fix >= 0 && n === fixEnd - 1) html += "</section>";
    }
    return html;
  }

  function renderBlock(block, inFix) {
    if (block.type === "h") {
      var level = Math.min(4, Math.max(1, block.level));
      return "<h" + level + ">" + inline(block.text) + "</h" + level + ">";
    }
    if (block.type === "p") return inFix ? "<p>" + fixLine(block.text) + "</p>" : "<p>" + inline(block.text) + "</p>";
    var out = "<ul>";
    for (var i = 0; i < block.items.length; i++) {
      var text = block.items[i];
      var mark = verdictIn(text);
      out +=
        "<li" + (mark ? ' data-mark="' + mark + '"' : "") + ">" +
        (inFix ? fixLine(text) : inline(text)) +
        "</li>";
    }
    return out + "</ul>";
  }

  // 추천 절 안에서는 화살표에서 줄을 끊는다 — 대안은 그대로 옮겨 적을 문장이다
  function fixLine(text) {
    var at = text.indexOf("→");
    if (at < 0) return inline(text);
    return (
      '<span class="fix-from">' + inline(text.slice(0, at).trim()) + "</span>" +
      '<span class="fix-to">→ ' + inline(text.slice(at + 1).trim()) + "</span>"
    );
  }

  // ---------------------------------------------------------------- 단계 엔진

  function stageFor(event) {
    var stage = state.stage;
    var audit = state.audit;
    if (state.status && state.status.done) return 5;
    if (audit && (audit.omissions || []).length) return 4;
    if (audit) {
      var claims = claimsOf(audit);
      for (var i = 0; i < claims.length; i++) {
        if (claims[i].verdict && claims[i].verdict !== "pending") return 3;
      }
      if (claims.length) return 2;
      if (sentencesOf(audit).length) return 1;
    }
    if (state.status) return Math.max(stage, 1);
    return stage;
  }

  function driveStage(event) {
    var next = stageFor(event);
    if (next <= state.stage) {
      paintFocus();
      return;
    }
    state.stage = next;
    paintStageRail();
    paintFocus();
    if (state.following && Date.now() > state.burstUntil) scrollToStage(next);
  }

  function paintStageRail() {
    var rail = $("#stage-rail");
    if (!rail) return;
    var items = rail.children;
    for (var i = 0; i < items.length; i++) {
      var n = Number(items[i].dataset.stage);
      items[i].classList.toggle("reached", n <= state.stage);
      items[i].classList.toggle("now", n === state.stage);
    }
  }

  function paintFocus() {
    var galley = $("#galley");
    var rebut = $("#omissions-section");
    if (!galley || !rebut) return;
    var live = state.following && state.stage >= 1 && state.stage <= 4;
    galley.classList.toggle("is-focus", live && state.stage < 4);
    galley.classList.toggle("is-receded", live && state.stage === 4);
    rebut.classList.toggle("is-focus", live && state.stage === 4);
  }

  var scrollTimer = null;
  var scrollFrame = null;

  function scrollToStage(stage) {
    var spec = STAGES[stage];
    if (!spec) return;
    var target = $(spec.target);
    if (!target || target.hidden) return;
    var top = target.getBoundingClientRect().top + window.scrollY - 78 - spec.peek;
    smoothTo(Math.max(0, top), stage);
  }

  function smoothTo(top, stage) {
    cancelScroll();
    var from = window.scrollY;
    var delta = top - from;
    if (reduced() || Math.abs(delta) < 2) {
      window.scrollTo(0, top);
      window.__redlineStageLog.push({ stage: stage, ms: 0, reduced: true });
      return;
    }
    var started = performance.now();
    var settle = function () {
      window.scrollTo(0, top);
      cancelScroll();
      window.__redlineStageLog.push({ stage: stage, ms: Math.round(performance.now() - started), reduced: false });
    };
    scrollTimer = setTimeout(settle, 360);
    var step = function (now) {
      var k = Math.min(1, (now - started) / 360);
      window.scrollTo(0, from + delta * (1 - Math.pow(1 - k, 3)));
      if (k < 1) scrollFrame = requestAnimationFrame(step);
      else settle();
    };
    scrollFrame = requestAnimationFrame(step);
  }

  function cancelScroll() {
    if (scrollFrame) cancelAnimationFrame(scrollFrame);
    if (scrollTimer) clearTimeout(scrollTimer);
    scrollFrame = null;
    scrollTimer = null;
  }

  function detach() {
    if (!state.following) return;
    state.following = false;
    cancelScroll();
    paintFocus();
    var button = $("#follow-run");
    if (button) button.hidden = false;
  }

  // ================================================================ 프로젝터

  var TYPE_SHORT = [
    [/^response\./, ""],
    [/^function_call_arguments/, "args"],
    [/^output_text/, "text"]
  ];

  function shortType(type) {
    var s = String(type || "");
    for (var i = 0; i < TYPE_SHORT.length; i++) s = s.replace(TYPE_SHORT[i][0], TYPE_SHORT[i][1]);
    return s;
  }

  function toolClass(name) {
    if (!name) return "";
    if (/^search_/.test(name)) return "search";
    if (name === "fetch_source") return "fetch";
    if (/^record_/.test(name) || name === "update_verdict") return "record";
    return "";
  }

  function appendRaw(event) {
    var host = $("#raw-events");
    var info = summarize(event);
    var row = el("details", "ev");
    row.dataset.kind = event.kind;
    if (info.tool) row.dataset.tool = info.tool;
    var summary = el("summary");
    summary.appendChild(el("span", "ev-seq", "#" + pad(event.seq || 0, 3)));
    if (info.type) summary.appendChild(el("span", "ev-type", info.type));
    if (info.name) summary.appendChild(el("span", "ev-name", info.name));
    if (info.hint) summary.appendChild(el("span", "ev-hint", info.hint));
    row.appendChild(summary);
    row.appendChild(el("pre", null, JSON.stringify(event, null, 2)));
    host.appendChild(row);
    if (!state.paused) host.scrollTop = host.scrollHeight;
  }

  function summarize(event) {
    var p = event.payload || {};
    if (event.kind === "raw") return summarizeRaw(p);
    if (event.kind === "run_item") {
      var item = p.item || {};
      var name = (item.raw_item && item.raw_item.name) || item.name || "";
      return { type: "run_item", name: p.name || "", hint: "", tool: toolClass(name) || "" };
    }
    if (event.kind === "audit") return { type: "audit", name: "", hint: auditHint(p), tool: "audit" };
    if (event.kind === "status") {
      var tail = p.done ? "종결 · " + ((TERMINAL[p.reason] || {}).title || p.reason || "") : "진행 중";
      return {
        type: "status",
        name: "",
        hint: "축 " + (p.axis || 0) + " · 툴 " + (p.tool_calls || 0) + " · " + tail,
        tool: ""
      };
    }
    return { type: event.kind, name: "", hint: "", tool: "" };
  }

  function summarizeRaw(p) {
    var type = shortType(p.type);
    var index = p.output_index;
    var known = state.outputItems[index];
    var out = { type: type, name: "", hint: "", tool: "" };

    if (p.type === "response.created" || p.type === "response.completed") {
      out.name = (p.response && p.response.model) || "";
      var size = JSON.stringify(p).length;
      if (p.type === "response.created" && size > 2048)
        out.hint = "페이로드 " + (size / 1024).toFixed(1) + "KB — 펼치면 전문";
      return out;
    }
    if (p.type === "response.output_item.added" || p.type === "response.output_item.done") {
      var item = p.item || {};
      if (item.name) state.outputItems[index] = item.name;
      if (item.name && p.type === "response.output_item.added" && NETWORK_TOOLS[item.name]) state.netCalls++;
      out.name = item.name || "";
      out.hint = p.type === "response.output_item.added" ? item.call_id || "" : "";
      out.tool = toolClass(item.name);
      return out;
    }
    if (/function_call_arguments/.test(p.type)) {
      out.name = known || "";
      out.tool = toolClass(known);
      var text = p.delta != null ? p.delta : p.arguments;
      if (/\.delta$/.test(p.type)) {
        state.args[index] = (state.args[index] || "") + (p.delta || "");
        text = state.args[index];
      }
      out.hint = clip(text, 76);
      return out;
    }
    if (/output_text/.test(p.type)) {
      out.hint = clip(p.delta != null ? p.delta : p.text, 76);
      return out;
    }
    out.hint = clip(p.delta || p.text || "", 76);
    return out;
  }

  function auditHint(p) {
    var claims = Array.isArray(p.claims) ? p.claims : [];
    for (var i = 0; i < claims.length; i++) {
      var prev = state.verdictSeen[claims[i].id];
      var now = claims[i].verdict || "pending";
      if (prev !== now) {
        state.verdictSeen[claims[i].id] = now;
        if (now !== "pending") return claims[i].id + " → " + (VERDICT_LABEL[now] || now);
      }
    }
    return "클레임 " + claims.length + "건 · 원장 " + (p.evidence_total || (p.evidence || []).length) + "건";
  }

  function paintRawState(event) {
    var audit = state.audit;
    paintBudget();
    var axis = (state.status && state.status.axis) || 0;
    var nodes = $("#axis-track").querySelectorAll(".axis-node");
    for (var i = 0; i < nodes.length; i++) nodes[i].classList.toggle("on", i < axis);
    if (!audit) return;
    var claims = claimsOf(audit);
    if (!claims.length) return;
    $("#state-empty").hidden = true;
    var host = $("#state-claims");
    var evidence = ledger(audit);
    for (var c = 0; c < claims.length; c++) {
      var claim = claims[c];
      var card = host.querySelector('[data-cid="' + CSS.escape(claim.id) + '"]');
      if (!card) {
        card = buildStateCard(claim);
        host.appendChild(card);
      }
      updateStateCard(card, claim, evidence);
    }
  }

  function buildStateCard(claim) {
    var card = el("div", "state-card");
    card.dataset.cid = claim.id;
    var head = el("div", "state-head");
    head.appendChild(el("span", "state-id", claim.id));
    head.appendChild(el("span", "state-axis", "AXIS 0"));
    head.appendChild(el("span", "state-verdict", VERDICT_LABEL.pending));
    card.appendChild(head);
    card.appendChild(el("p", "state-text", claim.text || ""));
    var meter = el("div", "state-meter");
    meter.appendChild(el("span"));
    meter.appendChild(el("b", "state-pct", "0%"));
    card.appendChild(meter);
    card.appendChild(el("div", "state-chips"));
    return card;
  }

  function updateStateCard(card, claim, evidence) {
    var verdict = claim.verdict || "pending";
    if (card.getAttribute("data-mark") !== verdict) card.setAttribute("data-mark", verdict);
    var axes = claim.axis_results || [];
    var reached = axes.length ? axes[axes.length - 1].axis : 0;
    setText(card.querySelector(".state-axis"), "AXIS " + reached);
    setText(card.querySelector(".state-verdict"), VERDICT_LABEL[verdict] || verdict);
    setText(card.querySelector(".state-text"), claim.text || "");
    var fill = card.querySelector(".state-meter span");
    var target = "scaleX(" + pct(claim.confidence) / 100 + ")";
    if (fill.dataset.born !== "1") {
      fill.dataset.born = "1";
      requestAnimationFrame(function () {
        fill.style.transform = target;
      });
    } else if (fill.style.transform !== target) {
      fill.style.transform = target;
    }
    setText(card.querySelector(".state-pct"), pct(claim.confidence) + "%");
    var chips = chipsFor(claim, evidence);
    var host = card.querySelector(".state-chips");
    var sig = JSON.stringify(chips);
    if (host.dataset.sig === sig) return;
    host.dataset.sig = sig;
    host.textContent = "";
    for (var i = 0; i < chips.length; i++) {
      var href = safeHref(chips[i].url);
      var chip = el(href ? "a" : "span", null, chips[i].label);
      if (href) {
        chip.href = href;
        chip.target = "_blank";
        chip.rel = "noopener";
      }
      chip.title = chips[i].title;
      host.appendChild(chip);
    }
  }

  function paintBudget() {
    var count = $("#tool-count");
    if (!count) return;
    var reported = (state.status && state.status.tool_calls) || 0;
    setText(count, String(Math.max(reported, state.netCalls)));
    setText($("#timebox"), Math.round(state.timebox) + "s");
    var shown = state.elapsed;
    if (!state.done && state.elapsedWall) shown = state.elapsed + (Date.now() - state.elapsedWall) / 1000;
    setText($("#elapsed"), Math.min(Math.round(shown), Math.round(state.timebox)) + "s");
  }

  // ================================================================ 배선

  function boot() {
    if (isMain) {
      $("#run-form").addEventListener("submit", submitRun);
      $("#intake-open").addEventListener("click", function () {
        $("#intake").hidden = false;
        $("#intake-collapsed").hidden = true;
      });
      $("#follow-run").addEventListener("click", function () {
        state.following = true;
        $("#follow-run").hidden = true;
        paintFocus();
        scrollToStage(state.stage);
      });
      $("#stage-rail").addEventListener("click", function (e) {
        var item = e.target.closest("li");
        if (!item) return;
        var target = $(item.dataset.target);
        if (target && !target.hidden) smoothTo(Math.max(0, target.getBoundingClientRect().top + window.scrollY - 78), Number(item.dataset.stage));
      });
      ["wheel", "touchstart", "pointerdown", "keydown"].forEach(function (name) {
        window.addEventListener(name, detach, { capture: true, passive: true });
      });
    }
    if (isRaw) {
      var pause = $("#pause-scroll");
      pause.addEventListener("click", function () {
        state.paused = !state.paused;
        pause.classList.toggle("is-on", state.paused);
        pause.setAttribute("aria-pressed", state.paused ? "true" : "false");
        pause.textContent = state.paused ? "자동 스크롤 재개" : "자동 스크롤 일시정지";
      });
      setInterval(paintBudget, 500);
    }
    connect();
  }

  function submitRun(e) {
    e.preventDefault();
    var text = $("#input-text").value;
    var button = $("#run-button");
    var error = $("#form-error");
    error.hidden = true;
    button.disabled = true;
    button.textContent = "감사 중…";
    fetch("/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: text })
    })
      .then(function (res) {
        return res.json().then(function (body) {
          return { ok: res.ok, status: res.status, body: body };
        });
      })
      .then(function (res) {
        if (!res.ok) throw new Error(detailOf(res));
        state.following = true;
        state.burstUntil = 0;
        state.done = false;
        $("#follow-run").hidden = true;
        $("#intake").hidden = true;
        $("#intake-collapsed").hidden = false;
        $("#terminal-summary").hidden = true;
        paintPreview(text.trim());
      })
      .catch(function (err) {
        button.disabled = false;
        button.textContent = "다시 시도 →";
        error.hidden = false;
        error.textContent =
          err && err.message
            ? err.message
            : "서버에 연결할 수 없습니다. 연결 상태를 확인한 뒤 다시 시도해 주세요.";
      });
  }

  function detailOf(res) {
    var detail = res.body && res.body.detail;
    if (Array.isArray(detail)) detail = detail.map(function (d) { return d.msg; }).join(" · ");
    return (detail || "요청이 거부됐습니다.") + " (HTTP " + res.status + ")";
  }

  // 런이 끝나면 버튼을 되살리고, 완주 뒤에만 감사하지 않은 문장을 접는다
  var lastDone = false;
  setInterval(function () {
    if (!isMain) return;
    if (state.done && !lastDone) {
      lastDone = true;
      var button = $("#run-button");
      button.disabled = false;
      button.textContent = "감사 시작 →";
      setTimeout(function () {
        state.following = false;
        $("#follow-run").hidden = true;
        paintFocus();
        applyFold();
      }, 360);
    }
    if (!state.done) lastDone = false;
  }, 250);

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
