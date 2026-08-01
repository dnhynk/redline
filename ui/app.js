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
  // 검색 방향. 메인 화면의 어휘(뒷받침 / 반박)를 그대로 쓴다.
  var STANCE_SEARCH = { support: "뒷받침 검색", challenge: "반박 검색" };
  // 서버가 config 로 알려 주기 전까지 쓰는 값. 진실은 서버에 있다.
  var textMaxChars = 2000;
  // 한꺼번에 도착한 반박 카드를 차례로 드러내는 간격과, 그 전체 창의 상한.
  // 창을 360ms 로 묶은 것은 카드 자체가 240ms 라, 첫 장이 앉을 무렵 마지막 장이
  // 출발해 물결 하나로 읽히는 지점이기 때문이다. 어떤 장수에서도 마지막 카드가
  // 0.6초 안에 다 뜬다.
  var CARD_STEP_MS = 120;
  var CARD_WINDOW_MS = 360;
  // 문장 행은 카드보다 잦고 수가 많다 — 간격을 좁게, 창은 카드와 같게 묶는다
  var ROW_STEP_MS = 26;
  var ROW_WINDOW_MS = 360;
  var MARKED = { supported: 1, unsupported: 1, overstated: 1, no_source: 1, undecidable: 1 };
  var STRUCTURAL_KINDS = { heading: 1, table_header: 1, code_fence: 1, divider: 1 };
  var AXIS_NAME = ["감사 준비", "논문·웹 출처 확인", "내용 확인", "반박 찾기"];
  var TERMINAL = {
    complete: { title: "감사 완료", reason: "설정된 감사 범위를 모두 확인했습니다.", outcome: "complete", note: "" },
    incomplete: { title: "부분 감사", reason: "감사를 끝내지 못한 항목이 있어 확인한 범위까지만 표시합니다.", outcome: "partial", note: "미완 항목" },
    timebox: { title: "부분 감사", reason: "시간 제한에 도달해 확인한 범위까지만 표시합니다.", outcome: "partial", note: "시간 제한" },
    max_turns: { title: "부분 감사", reason: "내부 반복 한도에 도달해 확인한 범위까지만 표시합니다.", outcome: "partial", note: "반복 한도" },
    non_auditable: { title: "감사 대상 아님", reason: "", outcome: "non_auditable", note: "" },
    error: { title: "중단됨", reason: "", outcome: "error", note: "" }
  };
  // 코어가 준 미완 사유에는 내부 어휘가 섞여 온다. 화면 어휘로 옮긴다.
  var ACTION_TERMS = [
    [/축\s*1\s*\([^)]*\)/g, "논문·웹 출처 확인"],
    [/축\s*2\s*\([^)]*\)/g, "내용 확인"],
    [/축\s*3\s*\([^)]*\)/g, "반박 찾기"],
    [/축\s*1(?![0-9])/g, "논문·웹 출처 확인"],
    [/축\s*2(?![0-9])/g, "내용 확인"],
    [/축\s*3(?![0-9])/g, "반박 찾기"],
    [/미확정 클레임/g, "판정이 남은 클레임"],
    // 바꿔 넣은 말이 모음으로 끝나므로 뒤따르던 조사를 함께 고친다
    [/반박 찾기을/g, "반박 찾기를"],
    [/반박 찾기은/g, "반박 찾기는"],
    [/반박 찾기이/g, "반박 찾기가"]
  ];
  var STAGES = [
    null,
    { target: "#galley", peek: 0 },
    { target: "#galley", peek: 0 },
    { target: "#galley", peek: 0 },
    { target: "#omissions-section", peek: 0 },
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
      settleAnim(e.target);
      requestAnimationFrame(flushDeferred);
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

  // 끝났든 도중에 끊겼든 뒷정리는 같다. 패널을 숨기면 그 안에서 돌던
  // 애니메이션은 취소되는데, 그때 일회성 클래스를 안 떼면 그 요소는 영원히
  // "도는 중"으로 남아 다음 갱신이 밀린다.
  function settleAnim(node) {
    if (!node || node.nodeType !== 1 || !node.dataset || !node.dataset.anim) return;
    var cls = node.dataset.anim;
    node.classList.remove(cls);
    delete node.dataset.anim;
    // 차례로 드러내려고 걸어 둔 지연은 그 한 번에만 쓴다 — 다시 뜰 때
    // 지난번 지연을 물려받으면 이유 없이 늦게 나타난다.
    if (node.style.animationDelay) node.style.animationDelay = "";
    var queued = node.dataset.animQueue;
    if (queued) {
      delete node.dataset.animQueue;
      requestAnimationFrame(function () {
        fireOnce(node, queued);
      });
    }
  }

  document.addEventListener(
    "animationend",
    function (e) {
      motion.end++;
      var rec = motion.names[e.animationName];
      if (rec) rec.end++;
      settleAnim(e.target);
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
    replay: false,
    fixtureSearch: false,
    sourceMode: undefined,
    stage: 0,
    tab: 0,
    fixCount: 0,
    reportParts: null,
    following: false,
    burstUntil: 0,
    rowSig: "",
    rows: [],
    pctSeen: {},
    foldOpen: false,
    historyGap: 0,
    historyFrom: 1,
    historyRun: null,
    t0: null,
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
      if (cfg.text_max_chars) {
        textMaxChars = cfg.text_max_chars;
        paintInputCount();
      }
      state.replay = !!cfg.mock;
      paintSource();
      noteHistoryGap(cfg);
      return;
    }
    if (event.run && event.run !== state.run) resetRun(event.run);
    var key = String(event.run) + ":" + String(event.seq);
    if (state.seen[key]) return;
    state.seen[key] = 1;
    if (typeof event.seq === "number" && event.seq > state.lastSeq) state.lastSeq = event.seq;
    // 런의 첫 봉투가 시계의 0 이다 — 프로젝터의 경과 시간은 여기서 잰다
    if (state.t0 == null && typeof event.t === "number") state.t0 = event.t;

    if (event.kind === "audit") {
      state.audit = event.payload || {};
      if (state.audit.source_mode === "mock") noteFixtureSearch();
    } else if (event.kind === "status") {
      state.status = event.payload || {};
      if (state.status.source_mode === "mock") noteFixtureSearch();
      if (typeof state.status.elapsed_s === "number") {
        state.elapsed = state.status.elapsed_s;
        state.elapsedWall = Date.now();
      }
      if (state.status.done) {
        state.done = true;
        // 종결 봉투의 audit 이 정본이다. 스트림이 뒤처졌더라도 여기서 수렴한다.
        if (state.status.audit) state.audit = state.status.audit;
      }
    }

    if (isRaw) appendRaw(event);
    if (isMain) paintMain(event);
  }

  // 서버 보관 상한에 걸려 앞부분이 재생되지 않았으면 화면이 그렇게 말한다.
  // 런 경계에서 피드를 비운 뒤에도 다시 세워야 한다 — 조용히 사라지면 안 본 것을 본 줄 안다.
  function noteHistoryGap(cfg) {
    if (cfg) {
      state.historyGap = Number(cfg.history_dropped) || 0;
      state.historyFrom = Number(cfg.history_from) || 1;
      state.historyRun = cfg.run || null;
    }
    var host = $("#raw-events");
    if (!host || !state.historyGap) return;
    var text =
      "앞선 이벤트 " + state.historyGap + "건은 서버 보관 상한을 넘어 재생되지 않았습니다 — 이 목록은 #" +
      state.historyFrom + " 부터입니다";
    var existing = host.querySelector(".ev-gap");
    if (existing) {
      existing.textContent = text;
      return;
    }
    host.insertBefore(el("p", "ev-gap", text), host.firstChild);
  }

  // 실패 모드가 둘이고 서로 다른 말이다.
  //   replay  — 서버가 저장된 이벤트를 튼다. 입력한 글은 아예 읽히지 않는다.
  //   fixture — 감사는 입력한 글을 실제로 하고, 검색 결과만 고정 픽스처다.
  // 둘을 한 문구로 묶으면 뒤쪽 경우에 "감사도 가짜"라고 잘못 말하게 된다.
  var SOURCE_NOTE = {
    replay: "저장된 이벤트를 재생하는 중입니다 — 입력한 글과 무관한 고정 시나리오입니다",
    fixture: "검색 결과가 고정 픽스처입니다 — 입력한 글은 실제로 감사하지만 근거는 실제 검색이 아닙니다"
  };

  function noteFixtureSearch() {
    state.fixtureSearch = true;
    paintSource();
  }

  function paintSource() {
    var mode = state.replay ? "replay" : state.fixtureSearch ? "fixture" : null;
    if (state.sourceMode === mode) return;
    state.sourceMode = mode;
    var banner = $("#source-banner");
    if (banner) {
      banner.hidden = !mode;
      if (mode) banner.textContent = SOURCE_NOTE[mode];
    }
    document.body.classList.toggle("has-banner", !!mode);
  }

  function resetRun(run) {
    state.run = run;
    state.lastSeq = 0;
    state.seen = {};
    state.audit = null;
    state.status = null;
    state.stage = 0;
    state.tab = 0;
    state.fixCount = 0;
    state.reportParts = null;
    state.rowSig = "";
    state.rows = [];
    state.pctSeen = {};
    state.foldOpen = false;
    state.t0 = null;
    state.elapsed = 0;
    state.elapsedWall = 0;
    state.done = false;
    // 검색이 픽스처였다는 것은 그 런의 사실이다. 다음 런은 실검색일 수 있다.
    // 서버가 재생 중이라는 것은 접속의 사실이라 런 경계에서 지우지 않는다.
    state.fixtureSearch = false;
    paintSource();
    deferred = [];
    if (isMain) {
      $("#sentences").textContent = "";
      $("#omissions").textContent = "";
      $("#final-report").textContent = "";
      delete $("#final-report").dataset.sig;
      $("#missing-actions").textContent = "";
      for (var s = 0; s < 2; s++) {
        var summary = $(s ? "#rebut-summary" : "#sentence-summary");
        if (!summary) continue;
        summary.hidden = true;
        summary.open = false;
        var summaryReport = $(s ? "#rebut-summary-report" : "#sentence-summary-report");
        if (summaryReport) {
          summaryReport.textContent = "";
          delete summaryReport.dataset.sig;
        }
      }

      $("#omissions-empty").hidden = false;
      $("#galley-empty").hidden = false;
      paintStageRail();
    }
    if (isRaw) {
      $("#raw-events").textContent = "";
      // 이 런의 재생이 잘렸다는 사실은 피드를 비워도 남아야 한다
      if (state.historyRun !== run) state.historyGap = 0;
      noteHistoryGap(null);
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
    if (audit || state.status) revealStage();
    if (audit && sentencesOf(audit).length) {
      $("#galley-empty").hidden = true;
      ensureRows(audit);
      updateRows(audit);
    }
    paintTally(audit);
    paintOmissions(audit);
    paintTabs();
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
    var fresh = [];
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
      fresh.push(row);
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

    // 행이 많아도 마지막 행이 창 안에서 출발한다
    var step = fresh.length > 1 ? Math.min(ROW_STEP_MS, ROW_WINDOW_MS / (fresh.length - 1)) : 0;
    for (var n = 0; n < fresh.length; n++) {
      if (step) fresh[n].style.animationDelay = Math.round(n * step) + "ms";
      fireOnce(fresh[n], "arrive");
    }
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
    // 구조 마크다운은 주장 없는 줄에서만 걷어 낸다. 클레임이 하나라도 붙으면
    // record_claim 과 같은 원문 좌표계로 즉시 돌아가야 밑줄 구간이 밀리지 않는다.
    var view = claims.length ? null : manuscriptView(slot.sentence, slot.kind);
    // 앵커는 등록 시점에 확정된다. 마크업 적용이 애니메이션 뒤로 밀려도
    // 여백은 이 표를 보고 「구절」 인용 여부를 정한다 — 행 높이가 나중에 안 변한다.
    slot.anchors = {};
    for (var a = 0; a < anchors.length; a++) slot.anchors[anchors[a].id] = anchors[a].start >= 0;
    var sig = JSON.stringify([
      anchors.map(function (a) {
        return [a.id, a.start, a.end];
      }),
      view
    ]);
    if (slot.text.dataset.sig !== sig) {
      whenIdle(slot.text, "markup", function () {
        slot.text.dataset.sig = sig;
        resetManuscriptView(slot.text);
        if (view) applyManuscriptView(slot.text, view);
        else slot.text.innerHTML = buildMarkup(slot.sentence, anchors);
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

  // 문장 전체가 구조 표기일 때만 내용과 표기를 갈라낸다. 인라인 강조·코드는
  // 일부 글자만 없애 오프셋을 바꾸므로 여기서 다루지 않는다.
  function manuscriptView(sentence, kind) {
    var line = String(sentence == null ? "" : sentence);
    var heading = /^\s{0,3}(#{1,6})[ \t]+(.+?)(?:\s+#+\s*)?$/.exec(line);
    if (heading) return { type: "heading", text: heading[2], level: heading[1].length };
    if (/^\s{0,3}(?:(?:\*\s*){3,}|(?:-\s*){3,}|(?:_\s*){3,})$/.test(line))
      return { type: "divider", text: "" };
    var quote = /^\s{0,3}>\s?(.*)$/.exec(line);
    if (quote) return { type: "quote", text: quote[1] };
    var item = /^\s{0,3}([-+*]|\d{1,9}[.)])[ \t]+(.+)$/.exec(line);
    if (item) return { type: "list", text: item[2], marker: /^\d/.test(item[1]) ? item[1] : "•" };
    // 분할기는 목록·인용 표기를 좌표계 밖으로 이미 떼어 낸다. 주장 없는 줄에만
    // 그 층위를 되살리고, 주장 줄은 이 경로에 들어오지 않는다.
    if (kind === "list_item") return { type: "list", text: line, marker: "•" };
    if (kind === "quote") return { type: "quote", text: line };
    return null;
  }

  function resetManuscriptView(node) {
    node.classList.remove("is-md-heading", "is-md-list", "is-md-quote", "is-md-divider");
    delete node.dataset.mdLevel;
    delete node.dataset.mdMarker;
    node.removeAttribute("role");
    node.removeAttribute("aria-level");
    node.removeAttribute("aria-label");
  }

  function applyManuscriptView(node, view) {
    node.textContent = view.text;
    node.classList.add("is-md-" + view.type);
    if (view.type === "heading") {
      node.dataset.mdLevel = String(view.level);
      node.setAttribute("role", "heading");
      node.setAttribute("aria-level", String(view.level));
    } else if (view.type === "list") {
      node.dataset.mdMarker = view.marker;
    } else if (view.type === "divider") {
      node.setAttribute("role", "separator");
      node.setAttribute("aria-label", "구분선");
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
        // 정규화한 문자열의 인덱스를 원문에 쓰면 공백 하나만 달라도 다른 글자를
        // 밑줄 친다. 정확한 원문 슬라이스가 아니면 인용 여백으로 물러난다.
        out.push({ id: claims[i].id, start: -1, end: -1, quote: needle });
        continue;
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

  // 클레임이 안 붙은 문장. 모델이 "의견·권고"라고 판정한 것이 아니므로 그 라벨을 붙이면 안 된다 —
  // 하지 않은 판정을 한 것처럼 말하는 것이다. 왜 안 골랐는지에 따라 갈라 쓴다.
  function bareLabel() {
    if (!state.done) return "확인 중";
    var reason = state.status && state.status.reason;
    if (reason === "non_auditable") return "감사 대상 아님";
    if (reason === "complete") {
      // 상한을 다 쓴 런에서는 이 문장을 보지 못했을 수 있다 — 그때는 단정하지 않는다.
      // 상한에 안 닿았더라도 본문을 얼마나 짚었는지 모르면 마찬가지다.
      return capReached() || !sweptEnough() ? "감사 안 함" : "논증 문장 아님";
    }
    return cutLabel();
  }

  function capReached() {
    var audit = state.audit || {};
    var cap = audit.max_claims;
    if (!cap) return true; // 상한을 모르면 덜 단정적인 쪽으로 둔다
    return (audit.claims || []).length >= cap;
  }

  // 상한에 안 닿았다는 것만으로는 모델이 이 문장을 보고 넘어갔다고 말할 수 없다.
  // 등록을 덜 했을 뿐 본문 뒤쪽에 닿지도 못한 런이 있기 때문이다. 실제로 훑었는지는
  // 커버리지로 어림잡는 수밖에 없고, 절반도 못 짚은 런에서는 단정하지 않는다.
  // 이 어림은 한쪽으로만 틀린다 — 사실 판정 문장이 적은 글에서는 덜 단정하게 되고,
  // 그 방향은 안 한 것을 한 것처럼 말하지 않는다는 규칙과 같은 쪽이다.
  var SWEPT_FRACTION = 0.5;

  function sweptEnough() {
    var audit = state.audit || {};
    var coverage = audit.coverage;
    if (!Array.isArray(coverage) || !coverage[1]) return false;
    if (!(audit.audited_claim_count > 0)) return false;
    return coverage[0] / coverage[1] >= SWEPT_FRACTION;
  }

  function cutLabel() {
    return (state.status && state.status.reason) === "incomplete" ? "확인 못 함 · 미완료" : "확인 못 함 · 중단됨";
  }

  function markLabel(verdict) {
    if (verdict !== "pending") return VERDICT_LABEL[verdict] || VERDICT_LABEL.pending;
    var reason = state.status && state.status.reason;
    if (!state.done || reason === "complete" || reason === "non_auditable") return VERDICT_LABEL.pending;
    return cutLabel();
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
    var label = node.querySelector(".mark-label");
    if (swatch.getAttribute("data-mark") !== verdict) {
      swatch.setAttribute("data-mark", verdict);
      label.style.color = "var(--v-" + verdict + "-label)";
      fireOnce(node, "settle");
    }
    setText(label, markLabel(verdict));

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
      // 첫 행 1개 + 둘째 행 [1개 + 넘침 +N]
      for (var r = 0; r < 2 && r < chips.length; r++) {
        var row = el("span", "chip-row");
        row.appendChild(chipNode(chips[r]));
        if (r === 1 && chips.length > 2) {
          var more = el("span", "chip more", "+" + (chips.length - 2));
          more.title = chips
            .slice(2)
            .map(function (c) {
              return c.title;
            })
            .join("\n");
          row.appendChild(more);
        }
        host.appendChild(row);
      }
    }

    var anchor = slot.anchors && slot.anchors[claim.id];
    var quote = node.querySelector(".mark-quote");
    if (!anchor && claim.text) {
      var line = "「" + clip(claim.text, 60) + "」";
      if (quote.textContent !== line) quote.textContent = line;
      quote.hidden = false;
    } else if (!quote.hidden) {
      quote.hidden = true;
    }
  }

  function chipNode(chip) {
    var link = safeHref(chip.url);
    var node = el(link ? "a" : "span", "chip", chip.label);
    if (link) {
      node.href = link;
      node.target = "_blank";
      node.rel = "noopener";
    }
    if (chip.stance) node.dataset.stance = chip.stance;
    node.title = chip.title;
    return node;
  }

  // 칩 한 줄이 여백 열을 넘으면 줄바꿈으로 잘려 도메인을 못 읽는다.
  // 인용 횟수는 자리가 남을 때만 붙이고, 없으면 툴팁이 들고 있는다.
  function chipWidth(text) {
    var width = 0;
    for (var i = 0; i < text.length; i++) width += text.charCodeAt(i) > 0x2000 ? 13 : 7.8;
    return width;
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
          var stance = STANCE_SEARCH[rec.stance];
          byDomain[dom].titles.push(
            (id || "") + " · " + (rec.tool || "") + (stance ? " · " + stance : "") + " · " + (rec.title || "")
          );
          if (stance && !byDomain[dom].stance) byDomain[dom].stance = rec.stance;
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
      var title = d.titles.join("\n") || d.domain;
      if (d.cites != null) {
        var full = label + " · 인용 " + d.cites + "회";
        if (chipWidth(full) <= 224) label = full;
        else title = "인용 " + d.cites + "회\n" + title;
      }
      return { label: label, url: d.url, title: title, stance: d.stance || "" };
    });
  }

  // 감사하지 않은 문장 접기 — 완주 종결 뒤에만, 지우지 않고 접는다
  function applyFold() {
    if (!state.foldNote) return;
    var reason = state.status && state.status.reason;
    var eligible = state.done && reason === "complete";
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

    var total = typeof audit.evidence_total === "number" ? audit.evidence_total : (audit.evidence || []).length;
    var cited = typeof audit.evidence_cited === "number" ? audit.evidence_cited : (audit.evidence || []).length;

    if (crashed()) {
      // 중단된 런의 0/0 은 정보가 아니라 오해다 — 집계 자체를 내지 않는다.
      setDash($("#unsupported-rate"));
      setDash($("#coverage"));
      setText($("#no-source-count"), "집계 없음");
      setText($("#claim-count"), "등록 클레임 " + claims.length + "건");
      setText($("#evidence-summary"), "감사가 중단돼 집계를 내지 않았습니다");
    } else {
      setMetric($("#unsupported-rate"), rate[0], rate[1]);
      setMetric($("#coverage"), cov[0], cov[1]);
      setText($("#no-source-count"), "출처 못 찾음 " + noSource + "건");
      setText($("#claim-count"), "등록 클레임 " + claims.length + "건");
      setText($("#evidence-summary"), "실제 받은 검색 결과 " + total + "건 · 판정에 인용 " + cited + "건");
    }

    var axis = (state.status && state.status.axis) || 0;
    var cells = $("#axis").children;
    for (var i = 0; i < cells.length; i++) cells[i].classList.toggle("on", i < axis);
  }

  function crashed() {
    return !!(state.done && state.status && state.status.reason === "error");
  }

  function setDash(node) {
    var html = '&mdash; <span class="sep">/</span> &mdash;';
    if (node.innerHTML !== html) node.innerHTML = html;
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

  // 반박 없음 기록은 가리킬 자료가 없다 — 그 자리를 "none" 이 채운다.
  function omissionKey(om) {
    return om.claim_id + "/" + (om.evidence_id || "none");
  }

  function rebuttalsFound(audit) {
    var list = audit && Array.isArray(audit.omissions) ? audit.omissions : [];
    return list.filter(function (om) {
      return !foundNothing(om);
    });
  }

  function paintOmissions(audit) {
    var host = $("#omissions");
    var evidence = ledger(audit);
    var list = (audit && Array.isArray(audit.omissions) ? audit.omissions : []).slice();
    // "반박 없음"을 적어 둔 자리에 나중에 진짜 반박이 오면 코어가 앞의 기록을 버린다.
    // 붙이기만 하면 두 장이 남아 같은 클레임에 "없었다"와 "있었다"가 나란히 뜬다.
    var live = {};
    for (var k = 0; k < list.length; k++) live[omissionKey(list[k])] = 1;
    for (var d = host.children.length - 1; d >= 0; d--) {
      if (!live[host.children[d].dataset.key]) host.removeChild(host.children[d]);
    }
    if (!list.length) {
      $("#omissions-empty").hidden = false;
      if (state.done) {
        $("#omissions-empty").lastElementChild.textContent = emptyRebuttalCopy(audit);
        $("#omissions-empty").firstElementChild.textContent = "0";
      }
      return;
    }
    $("#omissions-empty").hidden = true;
    var fresh = [];
    for (var i = 0; i < list.length; i++) {
      var om = list[i];
      var key = omissionKey(om);
      if (host.querySelector('[data-key="' + CSS.escape(key) + '"]')) continue;
      fresh.push(buildOmission(om, key, evidence));
    }
    // 한꺼번에 온 카드만 차례로 드러낸다. 하나씩 도착하는 런에서는 지연이 0 이라
    // 아무것도 늦어지지 않는다. 장수가 많으면 간격을 좁혀 창을 넘기지 않는다 —
    // 아홉 장에 120ms 씩이면 마지막이 1초 뒤고, 그때는 이미 볼 사람이 읽고 있다.
    var step = fresh.length > 1 ? Math.min(CARD_STEP_MS, CARD_WINDOW_MS / (fresh.length - 1)) : 0;
    for (var n = 0; n < fresh.length; n++) {
      var card = fresh[n];
      host.appendChild(card); // 붙인 뒤에 정착 — 문서 밖에서는 애니메이션이 시작되지 않는다
      if (step) card.style.animationDelay = Math.round(n * step) + "ms";
      fireOnce(card, "arrive");
    }
  }

  // "찾아봤지만 나오지 않았다"는 반증 검색이 실제로 결과를 받아온 런에서만 쓸 수 있는 말이다.
  // 근거가 없으면 단정하지 않고 무슨 일이 있었는지만 말한다.
  function emptyRebuttalCopy(audit) {
    var status = state.status || {};
    var reason = status.reason;
    if (reason === "non_auditable") return "감사 대상이 아니어서 반박을 찾지 않았습니다.";
    if (reason === "error") return "감사가 중단돼 반박을 찾지 못했습니다.";
    var fired = challengeSearches(status);
    var received = evidenceReceived(audit);
    if (reason !== "complete")
      return fired
        ? "반박 찾기를 끝내지 못했습니다 — 확인한 범위에서 나온 반박은 0건입니다."
        : "반박을 찾는 검색에 도달하기 전에 감사가 끝났습니다.";
    if (!fired) return "반박을 찾는 검색이 실행되지 않았습니다 — 0건은 탐색 결과가 아닙니다.";
    if (!received)
      return "반박을 찾는 검색을 " + fired + "회 실행했지만 결과를 한 건도 받지 못했습니다 — 0건은 탐색 결과가 아닙니다.";
    return "탐색 완료 · 이 글에 대한 반박 0건. 반박까지 찾아봤지만 나오지 않았습니다 — 0건도 결과입니다.";
  }

  function challengeSearches(status) {
    var completion = status.completion || {};
    var counts = completion.search_counts || status.search_counts || {};
    var candidates = [counts.challenge, completion.challenge_queries, status.challenge_queries];
    for (var i = 0; i < candidates.length; i++) {
      if (typeof candidates[i] === "number") return candidates[i];
    }
    return 0;
  }

  function evidenceReceived(audit) {
    if (!audit) return 0;
    if (typeof audit.evidence_total === "number") return audit.evidence_total;
    return (audit.evidence || []).length;
  }

  // 반박을 성실히 찾았지만 없었던 기록. 코어가 `found:false` 로 낸다 — 키가 없는 옛
  // 기록은 호스트 보고와 같게 "찾았다"로 읽는다.
  function foundNothing(om) {
    return om.found === false;
  }

  function searchedQueries(om) {
    var raw = Array.isArray(om.searched_queries) ? om.searched_queries : [];
    var out = [];
    for (var i = 0; i < raw.length; i++) {
      var q = typeof raw[i] === "string" ? raw[i].trim() : "";
      if (q && out.indexOf(q) < 0) out.push(q);
    }
    return out;
  }

  function buildOmission(om, key, evidence) {
    // 찾았으나 없었던 결과와 찾아낸 반박은 같은 카드의 두 결과다 — 자리도 리듬도 같다.
    var none = foundNothing(om);
    var href = none ? null : safeHref(om.url);
    var card = el(href ? "a" : "div", "rebut-card");
    card.dataset.key = key;
    if (none) card.dataset.result = "none";
    if (href) {
      card.href = href;
      card.target = "_blank";
      card.rel = "noopener";
    }
    var record = (evidence || {})[om.evidence_id] || {};
    var stance = om.stance || record.stance || "";
    var queries = searchedQueries(om);
    var brow = el("div", "rebut-brow");
    brow.appendChild(el("span", null, (none ? "반박 찾기 · " : "반박 근거 · ") + om.claim_id));
    if (none) {
      // 뱃지 자리에 무엇을 찾았는지가 아니라 얼마나 찾았는지가 온다. 이 카드에서는
      // 그것이 증거다 — 빈 자리로 두면 반박이 있는 카드보다 가벼워 보인다.
      if (queries.length) brow.appendChild(el("span", "rebut-tag", "반증 질의 " + queries.length + "건"));
    } else if (STANCE_SEARCH[stance]) {
      // 확증 검색에서 나온 반박 자료는 약한 증거다 — 어느 방향에서 왔는지 밝힌다
      var tag = el("span", "stance-tag", STANCE_SEARCH[stance]);
      tag.dataset.stance = stance;
      brow.appendChild(tag);
    }
    if (!none && om.url) brow.appendChild(el("span", "chip", domainOf(om.url)));
    card.appendChild(brow);
    card.appendChild(el("h3", null, none ? "반박을 찾았으나 없었다" : om.title || om.url || "제목 없음"));
    if (om.summary) {
      var copy = el("div", "rebut-copy");
      copy.innerHTML = renderMarkdown(om.summary);
      card.appendChild(copy);
    }
    if (none && queries.length) {
      // 무엇을 찾아봤는가가 이 카드의 증거다. 질의문이 없으면 "없었다"는 주장뿐이다.
      var probe = el("div", "rebut-queries");
      probe.appendChild(el("p", "rebut-query-label", "찾아본 반증 질의"));
      var items = el("ul", "rebut-query-list");
      for (var q = 0; q < queries.length; q++) items.appendChild(el("li", null, queries[q]));
      probe.appendChild(items);
      card.appendChild(probe);
    }
    if (none) return card;
    var meta = [];
    if (om.date) meta.push(om.date);
    if (typeof om.citation_count === "number") meta.push("인용 " + om.citation_count + "회");
    var line = meta.join(" · ") + (href ? (meta.length ? " ↗" : "↗") : "");
    if (line) card.appendChild(el("p", "rebut-meta", line));
    return card;
  }

  // ---------------------------------------------------------------- 종결

  // 지금 무슨 일을 하는지 사람 말로. 내부 어휘를 그대로 쓰지 않는다.
  function workingOn(status) {
    var audit = state.audit;
    var axis = status.axis || 0;
    if (axis >= 3) return "반박을 찾는 중";
    if (axis === 2) return "내용을 읽는 중";
    if (axis === 1) return "출처를 찾는 중";
    if (audit && claimsOf(audit).length) return "확인할 문장을 고르는 중";
    if (audit && sentencesOf(audit).length) return "확인할 문장을 고르는 중";
    return "문장을 나누는 중";
  }

  function paintStatus(status) {
    var badge = $("#phase-badge");
    if (!status.done) {
      var axis = status.axis || 0;
      if (badge) {
        badge.textContent = axis ? AXIS_NAME[axis] + " 진행 중" : "감사 준비 중";
        badge.removeAttribute("data-outcome");
      }
      setText($("#terminal-title"), workingOn(status));
      var note = $("#terminal-note");
      if (note) note.hidden = true;
      var band = $("#status-band");
      if (band) band.removeAttribute("data-outcome");
    } else {
      var map = TERMINAL[status.reason] || TERMINAL.error;
      if (badge) {
        badge.textContent =
          (map.outcome === "complete" ? "감사 완료"
            : map.outcome === "partial" ? "부분 감사"
            : map.outcome === "non_auditable" ? "감사 대상 아님"
            : "중단됨") + (map.note ? " · " + map.note : "");
        badge.setAttribute("data-outcome", map.outcome);
      }
      paintTerminal(status, map);
    }
    paintBand(status);
  }

  // 상태 띠의 게이지와 결과 한 줄. 남은 시간은 예측하지 않는다 —
  // 경과와 상한만 보여주고 얼마나 왔는지는 보는 사람이 읽는다.
  function paintBand(status) {
    var band = $("#status-band");
    if (!band || band.hidden) return;
    var shown = state.elapsed;
    if (!state.done && state.elapsedWall) shown = state.elapsed + (Date.now() - state.elapsedWall) / 1000;
    shown = Math.max(0, Math.min(shown, state.timebox));
    var frac = state.timebox > 0 ? shown / state.timebox : 0;
    if (state.done) frac = 1;
    // 상태 이벤트와 타이머가 같은 구간에서 겹쳐 들어온다. 눈에 보이는 만큼
    // 값이 움직였을 때만 쓴다 — 같은 값을 다시 써서 전환을 또 돌릴 이유가 없다.
    var tick = String(Math.round(frac * 1000));
    if (band.dataset.tick !== tick) {
      band.dataset.tick = tick;
      var fill = $("#sb-fill");
      if (fill) fill.style.transform = "scaleX(" + frac.toFixed(4) + ")";
      var gauge = $("#sb-gauge");
      if (gauge) gauge.setAttribute("aria-valuenow", String(Math.round(frac * 100)));
    }
  }

  function paintTerminal(status, map) {
    var section = $("#terminal-summary");
    // 보이고 숨기는 것은 탭이 맡는다 — 여기서는 내용만 채운다
    section.setAttribute("data-outcome", map.outcome);
    var band = $("#status-band");
    if (band) band.setAttribute("data-outcome", map.outcome);
    setText($("#terminal-title"), map.title);
    var note = $("#terminal-note");
    note.hidden = !map.note;
    setText(note, map.note);
    var reason = map.reason;
    if (status.reason === "non_auditable")
      reason = status.reason_detail || "검증 가능한 사실 주장을 찾지 못했습니다.";
    // 중단은 사용자 문구를 고정한다. 제공자가 던진 원문은 접힌 상세 안에만 둔다 —
    // 화면에 영문 자격증명 안내문이 뜨면 그건 우리 제품의 말이 아니다.
    if (status.reason === "error") reason = "감사를 시작하지 못했습니다.";
    setText($("#terminal-reason"), reason);
    paintErrorDetail(status);

    var missing = missingActions(status);
    var list = $("#missing-actions");
    list.hidden = !missing.length;
    if (missing.length && list.dataset.sig !== JSON.stringify(missing)) {
      list.dataset.sig = JSON.stringify(missing);
      list.textContent = "";
      for (var i = 0; i < missing.length; i++) list.appendChild(el("li", null, missing[i]));
    }


    var report = $("#final-report");
    // 중단된 런의 최종 보고는 시작도 못 한 감사의 수치다 — 싣지 않는다.
    var md = status.reason === "error" ? "" : status.final_report || "";
    if (report.dataset.sig !== md) {
      report.dataset.sig = md;
      state.reportParts = md
        ? renderReport(md)
        : { report: "", fixes: [], sentenceSummary: "", rebutSummary: "" };
      report.innerHTML = state.reportParts.report;
      $("#report-empty").hidden = !!md;
      paintFixes(state.reportParts.fixes);
    }
    paintReportFacts(status, !!md);
    paintPanelSummaries(status, state.reportParts);
  }

  // 아래 보고문은 모델이 쓴 글이라 수치를 잘못 적을 수 있다. 호스트가 센 값을
  // 그 위에 나란히 두고, 어긋나면 이쪽이 정본이라고 화면이 말하게 한다.
  function paintReportFacts(status, hasReport) {
    var line = $("#report-facts");
    paintReportFactsAt(line, status, hasReport);
  }

  function paintReportFactsAt(line, status, hasReport) {
    if (!line) return;
    var audit = (status && status.audit) || state.audit;
    if (!hasReport || !audit) {
      line.hidden = true;
      return;
    }
    var bits = [];
    var rate = audit.unsupported_rate;
    if (Array.isArray(rate) && rate[1]) bits.push("뒷받침 안 됨 " + rate[0] + " / " + rate[1]);
    if (typeof audit.no_source_count === "number")
      bits.push("출처 못 찾음 " + audit.no_source_count + "건");
    if (Array.isArray(audit.coverage) && audit.coverage[1])
      bits.push("커버리지 " + audit.coverage[0] + " / " + audit.coverage[1]);
    line.hidden = !bits.length;
    if (!bits.length) return;
    line.textContent = "";
    line.appendChild(el("b", null, "감사가 센 값"));
    line.appendChild(el("span", "rf-nums", bits.join("  ·  ")));
    line.appendChild(el("span", "rf-note", "아래 보고문과 수치가 다르면 이 줄이 맞습니다"));
  }

  function paintPanelSummaries(status, parts) {
    parts = parts || { sentenceSummary: "", rebutSummary: "" };
    paintPanelSummary(
      $("#sentence-summary"),
      $("#sentence-summary-report"),
      $("#sentence-summary-facts"),
      status,
      parts.sentenceSummary
    );
    paintPanelSummary(
      $("#rebut-summary"),
      $("#rebut-summary-report"),
      $("#rebut-summary-facts"),
      status,
      parts.rebutSummary
    );
  }

  function paintPanelSummary(details, host, facts, status, html) {
    if (!details || !host) return;
    details.hidden = !html;
    if (!html) {
      details.open = false;
      host.textContent = "";
      delete host.dataset.sig;
      paintReportFactsAt(facts, status, false);
      return;
    }
    if (host.dataset.sig !== html) {
      host.dataset.sig = html;
      host.innerHTML = html;
    }
    paintReportFactsAt(facts, status, true);
  }

  // 추천 수정안 — 목록 전체를 다시 짓지 않고 있는 줄만 갈아 끼운다
  function paintFixes(fixes) {
    var host = $("#fix-list");
    if (!host) return;
    var sig = fixes.join("\u0000");
    if (host.dataset.sig === sig) return;
    host.dataset.sig = sig;
    while (host.children.length > fixes.length) host.removeChild(host.lastChild);
    for (var i = 0; i < fixes.length; i++) {
      var li = host.children[i];
      if (!li) {
        li = el("li");
        host.appendChild(li);
      }
      var next = fixLine(fixes[i]);
      if (li.dataset.sig !== next) {
        li.dataset.sig = next;
        li.innerHTML = next;
      }
    }
    $("#fix-empty").hidden = fixes.length > 0;
    state.fixCount = fixes.length;
    paintTabs();
  }

  function humanizeAction(text) {
    var out = String(text == null ? "" : text);
    for (var i = 0; i < ACTION_TERMS.length; i++) out = out.replace(ACTION_TERMS[i][0], ACTION_TERMS[i][1]);
    return out;
  }

  function missingActions(status) {
    var completion = status.completion || {};
    var raw = Array.isArray(completion.missing_actions) ? completion.missing_actions.slice() : [];
    var done = pickNumber(status.axis3_done, completion.axis3_done);
    var expected = pickNumber(status.axis3_expected, completion.axis3_expected);
    if (!raw.length && expected > 0 && done < expected)
      raw.push("반박 찾기를 " + done + "/" + expected + " 클레임에 실행했다");
    return raw.map(humanizeAction);
  }

  function pickNumber() {
    for (var i = 0; i < arguments.length; i++) {
      if (typeof arguments[i] === "number") return arguments[i];
    }
    return 0;
  }

  function paintErrorDetail(status) {
    var box = $("#error-detail");
    if (!box) return;
    var detail = status.reason === "error" ? String(status.error || "") : "";
    box.hidden = !detail;
    if (!detail || box.dataset.sig === detail) return;
    box.dataset.sig = detail;
    box.textContent = "";
    box.appendChild(el("summary", null, "기술 상세"));
    box.appendChild(el("pre", null, detail));
  }

  // ---------------------------------------------------------------- 보고 본문
  //
  // 전부 이스케이프한 뒤 제한 서식만 되살린다.

  // 화면 라벨 → 판정 이름. 보고 본문의 [뒷받침 안 됨] 같은 표기를 판정색으로 잇는다.
  var LABEL_TO_VERDICT = (function () {
    var map = {};
    for (var key in VERDICT_LABEL) if (VERDICT_LABEL.hasOwnProperty(key)) map[VERDICT_LABEL[key]] = key;
    map["감사 안 함"] = "pending";
    return map;
  })();
  var LABEL_IN_BRACKETS = new RegExp(
    "\\[(" +
      Object.keys(LABEL_TO_VERDICT)
        .sort(function (a, b) {
          return b.length - a.length; // 긴 라벨 먼저 — 짧은 라벨이 앞을 먹지 않게
        })
        .map(function (s) {
          return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
        })
        .join("|") +
      ")\\]",
    "g"
  );

  var OPEN = "\u0001";
  var CLOSE = "\u0002";
  var MD_OPEN = "\u0003";
  var MD_CLOSE = "\u0004";

  function inline(text) {
    var tokens = [];
    var s = String(text == null ? "" : text);
    function keep(html) {
      var id = tokens.length;
      tokens.push(html);
      return MD_OPEN + "M" + id + MD_CLOSE;
    }
    // 코드와 링크를 먼저 빼 두면 그 안의 별표·판정 enum·수치가 다시 꾸며지지 않는다.
    s = s.replace(/`([^`\n]+)`/g, function (m, code) {
      return keep("<code>" + esc(code) + "</code>");
    });
    s = s.replace(/\[([^\]\n]+)\]\(([^)\s]+)\)/g, function (m, label, href) {
      var safe = safeHref(href);
      if (!safe) return keep(esc(label));
      return keep(
        '<a href="' + esc(safe) + '" target="_blank" rel="noopener">' + esc(label) + "</a>"
      );
    });
    s = esc(s);
    s = s.replace(/\*\*([^*]+)\*\*/g, OPEN + "B" + "$1" + CLOSE + "B");
    s = s.replace(
      /\b(no_source|non_auditable|unsupported|undecidable|overstated|supported|pending)\b/g,
      function (m, name) {
        return OPEN + "V" + name + "|" + VERDICT_LABEL[name] + CLOSE + "V";
      }
    );
    // 모델은 판정을 영어 enum 이 아니라 대괄호 안의 화면 라벨로 쓴다 — 그쪽도 색을 입힌다.
    s = s.replace(LABEL_IN_BRACKETS, function (m, label) {
      return OPEN + "V" + LABEL_TO_VERDICT[label] + "|[" + label + "]" + CLOSE + "V";
    });
    s = s.replace(/\bC(\d{1,2})\b/g, OPEN + "C" + "C$1" + CLOSE + "C");
    s = s.replace(
      /(\d+(?:\.\d+)?\s*\/\s*\d+|\d+(?:\.\d+)?(?:%|건|회|개|배|초|s))/g,
      OPEN + "N" + "$1" + CLOSE + "N"
    );
    s = s
      .replace(new RegExp(OPEN + "B", "g"), "<b>")
      .replace(new RegExp(CLOSE + "B", "g"), "</b>")
      .replace(new RegExp(OPEN + "V(\\w+)\\|", "g"), '<span class="v" data-v="$1">')
      .replace(new RegExp(CLOSE + "V", "g"), "</span>")
      .replace(new RegExp(OPEN + "C", "g"), '<span class="cid">')
      .replace(new RegExp(CLOSE + "C", "g"), "</span>")
      .replace(new RegExp(OPEN + "N", "g"), '<span class="num">')
      .replace(new RegExp(CLOSE + "N", "g"), "</span>");
    return s.replace(new RegExp(MD_OPEN + "M(\\d+)" + MD_CLOSE, "g"), function (m, id) {
      return tokens[Number(id)];
    });
  }

  // 판정 줄은 두 부분이다 — 무엇을 주장했나(머리)와 왜 그렇게 봤나(설명).
  // 머리는 굵게 세우고 설명은 한 단 들여써서, 훑는 사람이 주장만 먼저 읽을 수 있게 한다.
  function claimLine(text) {
    var at = text.indexOf(" — ");
    if (at < 0) return inline(text);
    return (
      '<span class="claim-head">' + inline(text.slice(0, at).trim()) + "</span>" +
      '<span class="claim-why">' + inline(text.slice(at + 3).trim()) + "</span>"
    );
  }

  function labelVerdictIn(text) {
    LABEL_IN_BRACKETS.lastIndex = 0;
    var hit = LABEL_IN_BRACKETS.exec(text);
    return hit ? LABEL_TO_VERDICT[hit[1]] : null;
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
      } else if (items && items.length) {
        // 목록이 열려 있는데 제목도 항목도 아닌 줄이면 앞 항목의 이어짐이다.
        // 여기서 목록을 닫으면 항목의 뒷부분이 별도 문단으로 떨어져 나간다 —
        // 추천 절에서는 그 뒷부분이 바로 추천 문장이라 통째로 사라졌다.
        items[items.length - 1] += " " + line.trim();
      } else {
        para.push(line.trim());
      }
    }
    flushPara();
    flushItems();

    // 패널 안 요약은 보고문을 다시 해석하지 않고, 이 파서가 만든 같은 블록에서
    // 해당 절만 고른다. 절 제목을 못 찾으면 빈 문자열이라 토글 자체가 숨는다.
    var sentenceBlocks = reportSection(blocks, "감사 결과", ["이 글에 대한 반박", "추천 수정안"]);
    var rebutBlocks = reportSection(blocks, "이 글에 대한 반박", ["추천 수정안"]);

    // 추천 절은 최종 보고에서 떼어 자기 탭으로 보낸다 — 같은 글이 두 곳에 있으면 안 된다
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

    var html = "";
    var fixes = [];
    for (var n = 0; n < blocks.length; n++) {
      if (fix >= 0 && n >= fix && n < fixEnd) {
        if (blocks[n].type === "ul") fixes = fixes.concat(blocks[n].items);
        // 목록이 아닌 모양으로 와도 고쳐 쓸 문장이면 버리지 않는다 —
        // 산출물을 조용히 버리는 경로를 남기지 않는다.
        else if (blocks[n].type === "p" && blocks[n].text.indexOf("→") >= 0)
          fixes.push(blocks[n].text);
        continue; // 제목과 안내문은 탭 라벨과 패널 설명이 대신한다
      }
      html += renderBlock(blocks[n], false);
    }
    return {
      report: html,
      fixes: fixes,
      sentenceSummary: renderBlocks(sentenceBlocks),
      rebutSummary: renderBlocks(rebutBlocks)
    };
  }

  function reportSection(blocks, title, stopTitles) {
    var start = -1;
    var level = 4;
    for (var i = 0; i < blocks.length; i++) {
      if (blocks[i].type === "h" && blocks[i].text.trim() === title) {
        start = i;
        level = blocks[i].level;
        break;
      }
    }
    if (start < 0) return [];
    var end = blocks.length;
    for (var n = start + 1; n < blocks.length; n++) {
      if (blocks[n].type !== "h") continue;
      var namedStop = stopTitles.indexOf(blocks[n].text.trim()) >= 0;
      if (namedStop || blocks[n].level < level) {
        end = n;
        break;
      }
    }
    return blocks.slice(start + 1, end);
  }

  function renderBlocks(blocks) {
    var html = "";
    for (var i = 0; i < blocks.length; i++) html += renderBlock(blocks[i], false);
    return html;
  }

  // 판정 사유·근거 설명도 최종 보고와 똑같이 이스케이프·제한 서식을 거친다.
  function renderMarkdown(md) {
    return renderReport(md).report;
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
      var mark = verdictIn(text) || labelVerdictIn(text);
      out +=
        "<li" + (mark ? ' data-mark="' + mark + '"' : "") + ">" +
        (inFix ? fixLine(text) : claimLine(text)) +
        "</li>";
    }
    return out + "</ul>";
  }

  // 추천 절 안에서는 화살표에서 줄을 끊는다 — 대안은 그대로 옮겨 적을 문장이다
  function fixLine(text) {
    var at = text.indexOf("→");
    if (at < 0) return '<p class="fix-now">' + inline(text) + "</p>";
    var was = text.slice(0, at).trim();
    var now = text.slice(at + 1).trim();
    // "C5 원문: …" 에서 클레임 번호와 안내말을 떼어 낸다 — 화면이 이름표를 따로 단다.
    // 굵게 표시해 오는 경우와 아닌 경우가 둘 다 있어 별표는 있어도 없어도 받는다.
    var id = /^\*{0,2}([A-Z]\d+)\*{0,2}(?=\s|$)/.exec(was);
    was = was.replace(/^\*{0,2}[A-Z]\d+\*{0,2}\s*/, "").replace(/^원문\s*[:：]\s*/, "");
    now = now.replace(/^추천\s*[:：]\s*/, "");
    return (
      '<p class="fix-was"><span class="fix-tag">원문' +
      (id ? " " + inline(id[1]) : "") +
      '</span><span class="fix-said">' + inline(was) + "</span></p>" +
      '<p class="fix-now"><span class="fix-tag is-now">고쳐 쓰기</span>' +
      '<span class="fix-said">' + inline(now) + "</span></p>"
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

  // ---------------------------------------------------------------- 탭
  //
  // 세 패널은 서로 대등한 형제라 방향이 없다. 그래서 옆으로 밀지 않고 불투명도만
  // 쓴다. 종결 수치는 탭에 들어가지 않는다 — 어느 탭을 보든 보여야 하는 값이다.

  var TAB_SPEC = [
    { tab: "tab-sentences", panel: "galley", count: "count-sentences" },
    { tab: "tab-rebut", panel: "omissions-section", count: "count-rebut" },
    { tab: "tab-fix", panel: "fix-panel", count: "count-fix" },
    // 최종 보고는 셀 것이 없다 — 하나뿐인 글이라 건수를 붙이지 않는다
    { tab: "tab-report", panel: "terminal-summary", count: null }
  ];

  function tabIndexOf(node) {
    for (var i = 0; i < TAB_SPEC.length; i++) if (TAB_SPEC[i].tab === node.id) return i;
    return 0;
  }

  function paintTabs() {
    var audit = state.audit;
    var counts = [
      audit ? sentencesOf(audit).length : 0,
      // 찾았으나 없었다는 기록은 반박이 아니다 — 탭 수는 실제로 찾아낸 반박만 센다.
      // 그 카드가 왜 거기 있는지는 카드 자신이 말한다.
      audit ? rebuttalsFound(audit).length : 0,
      state.fixCount || 0,
      state.done ? 1 : 0
    ];
    for (var i = 0; i < TAB_SPEC.length; i++) {
      var tab = $("#" + TAB_SPEC[i].tab);
      if (!tab) continue;
      var slot = TAB_SPEC[i].count ? $("#" + TAB_SPEC[i].count) : null;
      if (slot) {
        var text = String(counts[i]);
        if (slot.textContent !== text) slot.textContent = text;
      }
      // 0 이어도 누를 수 있게 둔다. 왜 비었는지는 그 패널만 말할 수 있고,
      // 찾아봤지만 없었다와 아예 안 찾았다를 가르는 문장이 거기 있다.
      tab.dataset.empty = counts[i] ? "0" : "1";
    }
  }

  function selectTab(index, opts) {
    opts = opts || {};
    index = Math.max(0, Math.min(TAB_SPEC.length - 1, index));
    var moved = state.tab !== index;
    state.tab = index;
    for (var i = 0; i < TAB_SPEC.length; i++) {
      var tab = $("#" + TAB_SPEC[i].tab);
      var panel = $("#" + TAB_SPEC[i].panel);
      var on = i === index;
      if (tab) {
        tab.setAttribute("aria-selected", on ? "true" : "false");
        tab.tabIndex = on ? 0 : -1;
      }
      if (!panel) continue;
      if (!on) hidePanel(panel);
      else if (moved || opts.force || panel.hidden) showPanel(panel);
    }
    if (opts.focus) {
      var active = $("#" + TAB_SPEC[index].tab);
      if (active) active.focus();
    }
  }

  // 숨기면 그 안에서 돌던 애니메이션이 사라진다. 브라우저가 취소를 알려 주기를
  // 기다리지 않고 여기서 직접 매듭짓는다 — 안 그러면 그 여백 부호는 영원히
  // "도는 중"으로 남아 다음 갱신이 밀린다.
  function hidePanel(panel) {
    if (panel.hidden) return;
    var pending = panel.querySelectorAll("[data-anim]");
    for (var i = 0; i < pending.length; i++) settleAnim(pending[i]);
    panel.hidden = true;
  }

  // 들어오는 패널만 뜬다. 나가는 패널을 겹쳐 두면 두 패널의 높이가 달라 화면이
  // 튄다 — 읽는 중에 글이 움직이지 않는 것이 먼저다.
  function showPanel(panel) {
    panel.hidden = false;
    panel.classList.add("is-entering");
    void panel.offsetWidth; // 여기서 레이아웃을 확정시켜야 전환이 실제로 돈다
    panel.classList.remove("is-entering");
  }

  // 단계 1~3 은 문장, 4 는 반박. 종결에서는 탭을 옮기지 않는다 —
  // 결과는 위 띠가 이미 말하고 있고, 읽던 자리를 그 순간에 뺏지 않는다.
  function tabForStage(stage) {
    return stage >= 4 ? 1 : 0;
  }

  // 대기 화면은 검사 과정 밴드로 마감된다 — 무대는 런이 시작될 때 올라온다
  function revealStage() {
    var band = $("#status-band");
    if (band) band.hidden = false;
    document.documentElement.classList.add("is-running");
    var tabs = $("#result-tabs");
    if (tabs && tabs.hidden) {
      tabs.hidden = false;
      selectTab(state.tab, { force: true });
    }
    // 무대가 올라오는 그 프레임에 배지 폭을 확정한다 — 검사 이름이 바뀌어도 안 흔들린다
    $("#phase-badge").dataset.live = "1";
  }

  function driveStage(event) {
    var next = stageFor(event);
    if (next >= 1) revealStage();
    if (next <= state.stage) {
      return;
    }
    state.stage = next;
    paintStageRail();
    if (state.following && Date.now() > state.burstUntil) goToStage(next);
  }

  // 자동 따라가기가 탭을 옮긴다. 이탈한 사용자의 탭은 뺏지 않는다 —
  // 여기 오는 것은 state.following 이 참일 때뿐이다.
  function goToStage(stage) {
    // 종결은 탭을 건드리지 않는다. 여기서 패널을 숨기면 돌던 여백 부호가 끊긴다.
    if (stage < 1 || stage >= 5) return;
    selectTab(tabForStage(stage));
    scrollToTabs(stage);
  }

  // 상단 바와 상태 띠가 둘 다 sticky 다. 목적지가 그 아래로 오지 않으면 탭 줄이
  // 띠에 가린다. 토큰을 믿지 않고 실제 높이를 재서 쓴다 — 띠 내용이 바뀌어도 안 어긋난다.
  function stickyTop() {
    var bar = document.querySelector(".topbar");
    var band = $("#status-band");
    var height = bar ? bar.getBoundingClientRect().height : 0;
    if (band && !band.hidden) height += band.getBoundingClientRect().height;
    return Math.round(height) + 14;
  }

  function scrollToTabs(stage) {
    var tabs = $("#result-tabs");
    if (!tabs || tabs.hidden) return;
    smoothTo(Math.max(0, tabs.getBoundingClientRect().top + window.scrollY - stickyTop()), stage);
  }

  function paintInputCount() {
    var area = $("#input-text");
    var count = $("#input-count");
    if (!area || !count) return;
    var n = area.value.length;
    count.textContent = n.toLocaleString("en-US") + " / " + textMaxChars.toLocaleString("en-US");
    count.dataset.over = n > textMaxChars ? "1" : "0";
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

  var scrollTimer = null;
  var scrollFrame = null;

  function scrollToStage(stage) {
    var spec = STAGES[stage];
    if (!spec) return;
    var target = $(spec.target);
    if (!target || target.hidden) return;
    var top = target.getBoundingClientRect().top + window.scrollY - stickyTop() - spec.peek;
    smoothTo(Math.max(0, top), stage);
  }

  function smoothTo(top, stage) {
    cancelScroll();
    var from = window.scrollY;
    var delta = top - from;
    if (reduced() || Math.abs(delta) < 2) {
      window.scrollTo(0, top);
      window.__redlineStageLog.push({ stage: stage, ms: 0, reduced: reduced() });
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

  // 사용자가 직접 고르면 자동 전환을 멈춘다. 되돌리는 버튼은 두지 않는다 —
  // 보고 싶은 탭을 그냥 누르면 된다.
  function detach() {
    if (!state.following) return;
    state.following = false;
    cancelScroll();
  }

  // ================================================================ 프로젝터
  //
  // 대회 규칙: tool_call / tool_result 이벤트를 가공 없이 세컨드 화면에 실시간
  // 출력한다. 그래서 이 화면에 오르는 것은 아래 셋뿐이다.
  //
  //   raw      / response.function_call_arguments.done   API 가 직접 보낸 호출
  //   run_item / tool_called                             SDK 가 본 같은 호출
  //   run_item / tool_output                             툴이 돌려준 결과
  //
  // tool_result 는 API 스트림에 없다 — 결과는 우리가 API 로 보내는 것이라
  // run_item/tool_output 에만 있다. 첫 줄이 있어야 「실제 API 호출」이 API 자신의
  // 이벤트로 뒷받침된다. 나머지는 규칙이 지목한 것이 아니고, 정작 봐야 할 것을 덮는다.
  var SHOWN = {
    "raw/response.function_call_arguments.done": "call",
    "run_item/tool_called": "sdk",
    "run_item/tool_output": "result"
  };

  // 봉투가 문자열이 아니라 구조체로 온 경우에만 붙는 말. 한 줄 JSON 으로 옮겼다는
  // 사실을 행이 직접 말한다 — 옮긴 것을 받은 그대로인 척하지 않는다.
  var OBJECT_NOTE = "구조체로 도착 · JSON 한 줄";

  function roleOf(event) {
    var p = event.payload || {};
    return SHOWN[event.kind + "/" + (event.kind === "raw" ? p.type : p.name)] || "";
  }

  // 페이로드 문자열이 봉투에 앉아 있는 자리. 꺼내기만 한다 — 여기서 자르거나 다시
  // 짜면 심사위원이 보는 것은 API 가 보낸 것이 아니라 우리가 만든 것이 된다.
  function payloadOf(event) {
    var p = event.payload || {};
    if (event.kind === "raw") return p.arguments;
    var item = p.item || {};
    var rawItem = item.raw_item || {};
    if (p.name !== "tool_output") return rawItem.arguments;
    // 결과 문자열은 raw_item 에 앉는다. 합성 픽스처는 그 자리 대신 파싱된 봉투만
    // 들고 오므로 그때는 그것을 쓰고, 문자열이 아니라는 사실을 행이 말한다.
    return rawItem.output != null ? rawItem.output : item.output;
  }

  // 문자열이면 그대로 돌려준다. 그것이 이 화면의 전부다.
  function payloadText(value) {
    if (typeof value === "string") return value;
    return value == null ? "" : JSON.stringify(value);
  }

  // 행 머리에는 봉투가 스스로 들고 있는 이름표만 쓴다. 이름이 없는 이벤트는
  // 이름 자리를 비운 채로 나간다 — 우리가 추론해 채우지 않는다.
  function headOf(event) {
    var p = event.payload || {};
    if (event.kind === "raw") return { type: p.type || "", name: "", id: p.item_id || "" };
    var rawItem = (p.item || {}).raw_item || {};
    return { type: p.name || "", name: rawItem.name || "", id: rawItem.call_id || "" };
  }

  // 런의 첫 봉투를 0 으로 잡은 경과. t 는 적재 시각이라 재생에서도 그 런이 실제로
  // 걸린 시간을 말한다 — 재생 속도가 아니라.
  function sinceStart(event) {
    if (typeof event.t !== "number" || state.t0 == null) return "";
    return Math.max(0, event.t - state.t0).toFixed(1) + "s";
  }

  function appendRaw(event) {
    var role = roleOf(event);
    if (!role) return;
    var head = headOf(event);
    var value = payloadOf(event);
    var row = el("article", "ev");
    row.dataset.role = role;
    var line = el("div", "ev-head");
    line.appendChild(el("span", "ev-seq", "#" + (event.seq || 0)));
    line.appendChild(el("span", "ev-at", sinceStart(event)));
    line.appendChild(el("span", "ev-kind", event.kind));
    line.appendChild(el("span", "ev-type", head.type));
    if (head.name) line.appendChild(el("span", "ev-name", head.name));
    if (head.id) line.appendChild(el("span", "ev-id", head.id));
    if (typeof value !== "string") {
      row.dataset.form = "object";
      line.appendChild(el("span", "ev-note", OBJECT_NOTE));
    }
    row.appendChild(line);
    // 접지 않는다. 처음부터 전문이 보이고, 줄바꿈 말고는 아무것도 하지 않는다.
    row.appendChild(el("pre", "ev-payload", payloadText(value)));
    var host = $("#raw-events");
    host.appendChild(row);
    if (!state.paused) host.scrollTop = host.scrollHeight;
  }

  // ================================================================ 배선

  function boot() {
    if (isMain) {
      $("#run-form").addEventListener("submit", submitRun);
      var area = $("#input-text");
      if (area) {
        area.addEventListener("input", paintInputCount);
        // 칸을 누른 순간이 곧 쓰겠다는 뜻이다 — 예시를 치우고 자리를 비워 준다.
        // 비운 채로 떠나면 다시 비쳐 보인다.
        var sample = area.getAttribute("placeholder") || "";
        area.addEventListener("focus", function () {
          area.setAttribute("placeholder", "");
        });
        area.addEventListener("blur", function () {
          if (!area.value) area.setAttribute("placeholder", sample);
        });
        paintInputCount();
      }
      $("#intake-open").addEventListener("click", function () {
        $("#intake").hidden = false;
        // 다시 입력하러 왔다는 것은 이 런이 끝났다는 뜻이다 —
        // 단계 표시도 방금 감사한 글도 지난 런의 것이므로 비운다.
        state.stage = 0;
        paintStageRail();
        var area = $("#input-text");
        if (area) {
          area.value = "";
          paintInputCount();
          area.focus();
        }
      });
      // 레일은 스크롤이 아니라 탭을 옮긴다 — 숨은 패널로는 스크롤할 수 없다
      $("#stage-rail").addEventListener("click", function (e) {
        var item = e.target.closest("li");
        if (!item) return;
        var tabs = $("#result-tabs");
        if (!tabs || tabs.hidden) return;
        goToStage(Number(item.dataset.stage));
      });

      var tablist = $("#tablist");
      if (tablist) {
        // 누르는 순간 반응한다 — 떼는 순간에 표시가 오면 죽은 느낌이 난다
        tablist.addEventListener("pointerdown", function (e) {
          var tab = e.target.closest('[role="tab"]');
          if (!tab || e.button) return;
          e.preventDefault(); // 기본 포커스 이동을 막고 우리가 옮긴다
          selectTab(tabIndexOf(tab), { focus: true });
        });
        // 포인터가 없는 길(Enter·Space)도 같은 자리로 온다
        tablist.addEventListener("click", function (e) {
          var tab = e.target.closest('[role="tab"]');
          if (tab) selectTab(tabIndexOf(tab), { focus: true });
        });
        tablist.addEventListener("keydown", function (e) {
          var tab = e.target.closest('[role="tab"]');
          if (!tab || e.ctrlKey || e.altKey || e.metaKey) return;
          var at = tabIndexOf(tab);
          var last = TAB_SPEC.length - 1;
          var to = -1;
          if (e.key === "ArrowRight" || e.key === "ArrowDown") to = at === last ? 0 : at + 1;
          else if (e.key === "ArrowLeft" || e.key === "ArrowUp") to = at === 0 ? last : at - 1;
          else if (e.key === "Home") to = 0;
          else if (e.key === "End") to = last;
          if (to < 0) return;
          e.preventDefault();
          selectTab(to, { focus: true });
        });
      }

      ["wheel", "touchstart", "pointerdown", "keydown"].forEach(function (name) {
        window.addEventListener(name, detach, { capture: true, passive: true });
      });
      // 게이지는 이벤트가 없는 동안에도 흐른다 — 시간은 계속 가고 있다
      setInterval(function () {
        if (state.status) paintBand(state.status);
      }, 500);
    }
    if (isRaw) {
      var pause = $("#pause-scroll");
      pause.addEventListener("click", function () {
        state.paused = !state.paused;
        pause.classList.toggle("is-on", state.paused);
        pause.setAttribute("aria-pressed", state.paused ? "true" : "false");
        pause.textContent = state.paused ? "자동 스크롤 재개" : "자동 스크롤 일시정지";
      });
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
        $("#intake").hidden = true;
        revealStage();
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

  // 서버는 거부 사유를 한국어 한 문장으로 준다. 그 밖의 모양이면 원문을 화면에
  // 올리지 않고 갈음한다 — 화면에 영문 오류 원문이 오르는 일은 없어야 한다.
  function detailOf(res) {
    var detail = res.body && res.body.detail;
    if (typeof detail !== "string" || !detail) detail = "요청이 거부됐습니다.";
    return detail;
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
        applyFold();
      }, 360);
    }
    if (!state.done) lastDone = false;
  }, 250);

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
