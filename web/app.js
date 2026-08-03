// 轻流 -> 宜搭 统一迁移控制台 -- 前端逻辑
(function () {
  "use strict";

  const STAGE_ORDER = ["s1", "s2", "s3", "s4"];
  const STAGE_META = {
    s1: { no: "→", title: "拉取", steps: ["00", "01", "02", "02b", "02c"],
          desc: "拉取轻流数据 + 宜搭表单元数据 + 字段对齐 + 宜搭存量" },
    s2: { no: "→", title: "对比", steps: ["02d"],
          desc: "三方对比，生成新增/更新/跳过清单", force: true },
    s3: { no: "→", title: "转换", steps: ["03"],
          desc: "将差异集转换为宜搭原始值格式" },
    s4: { no: "→", title: "写入", steps: ["04"],
          desc: "批量新增 / 更新存量" },
  };
  const STEP_TITLE = {
    "00": "配置", "01": "轻流", "02": "元数据",
    "02b": "自动映射", "02c": "宜搭存量", "02d": "差异",
    "03": "转换", "04": "写入",
  };

  let state = {
    forms: [],
    selected: null,
    editingForm: null,
    stageStatus: {},
    // data migration
    dataJob: null,
    dataPollTimer: null,
    // 数据准备（选表单后自动执行）
    prepare: { status: "idle", jobId: null },
    // attachment migration
    attJob: null,
    attPollTimer: null,
    attLastEventTime: 0,
    // settings
    settings: { limit: 0, attLimit: 0, yidaApps: [], activeApp: 0, userId: "" },
    appFilter: 0,
    editingIdx: null,
  };

  // ---- 工具 ----
  function el(id) { return document.getElementById(id); }
  function toast(msg, type) {
    const t = el("toast");
    t.textContent = msg;
    t.className = "toast " + (type || "");
    setTimeout(function () { t.classList.add("hidden"); }, 2600);
  }
  function api(path, opts) {
    return fetch(path, Object.assign({ headers: { "Content-Type": "application/json" } }, opts || {}))
      .then(function (res) {
        return res.json().then(function (data) {
          return { ok: res.ok, status: res.status, data: data };
        }).catch(function () {
          return { ok: res.ok, status: res.status, data: null };
        });
      });
  }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }
  function activeApp() {
    var apps = state.settings.yidaApps || [];
    var i = state.settings.activeApp || 0;
    if (i < 0 || i >= apps.length) i = 0;
    return apps[i] || {};
  }

  function stageDone(f, sk) {
    if (sk === "s1") return !!(f.configExists && f.rawExists && f.schemaExists && f.mappingExists && f.yidaExists);
    if (sk === "s2") return !!f.diffExists;
    if (sk === "s3") return !!f.transformedExists;
    if (sk === "s4") return !!f.resultExists;
    return false;
  }
  function stepDone(f, s) {
    var map = {
      "00": f.configExists, "01": f.rawExists, "02": f.schemaExists,
      "02b": f.mappingExists, "02c": f.yidaExists, "02d": f.diffExists,
      "03": f.transformedExists, "04": f.resultExists,
    };
    return !!map[s];
  }
  function stepToStage(step) {
    for (var k = 0; k < STAGE_ORDER.length; k++) {
      var sk = STAGE_ORDER[k];
      if (STAGE_META[sk].steps.indexOf(step) !== -1) return sk;
    }
    return null;
  }

  // ---- 控制台 ----
  function consoleLog(label, text) {
    var con = el("console");
    var now = new Date().toLocaleTimeString();
    con.textContent = (con.textContent || "") + "[" + now + " " + label + "] " + text + "\n";
    con.scrollTop = con.scrollHeight;
  }
  function consoleData(text) { consoleLog("DATA", text); }
  function consoleAtt(text) { consoleLog("ATTACH", text); }

  function setConsoleStatus(cls, txt) {
    var b = el("console-status");
    b.className = "badge " + cls;
    b.textContent = txt;
  }

  // ---- 加载数据 ----
  function loadForms() {
    return api("/api/forms").then(function (r) {
      if (!r.data) return;
      state.forms = r.data;
      renderAppFilter();
      renderList();
      if (state.selected) {
        var still = r.data.find(function (f) { return f.name === state.selected; });
        if (still) { state.selected = still.name; renderDetail(); }
      }
    });
  }

  function loadSettings() {
    return api("/api/settings").then(function (r) {
      if (r.data) {
        state.settings = Object.assign(
          { limit: 0, attLimit: 0, yidaApps: [], activeApp: 0, userId: "" }, r.data);
        var i = state.settings.activeApp || 0;
        var apps = state.settings.yidaApps || [];
        if (i < 0 || i >= apps.length) i = 0;
        state.appFilter = i;
        var dl = el("dm-limit"); if (dl) dl.value = state.settings.limit || 0;
        var al = el("att-limit"); if (al) al.value = state.settings.attLimit || 0;
      }
    });
  }

  // ---- 左侧栏 ----
  function renderAppFilter() {
    var sel = el("app-filter");
    var apps = state.settings.yidaApps || [];
    var opts = ['<option value="all">全部应用</option>'];
    apps.forEach(function (a, i) {
      opts.push('<option value="' + i + '">' + esc(a.name || ("应用" + (i + 1))) + '</option>');
    });
    sel.innerHTML = opts.join("");
    sel.value = (state.appFilter === "all") ? "all" : String(state.appFilter);
  }

  function stageBadges(f) {
    var run = state.stageStatus;
    var html = "";
    STAGE_ORDER.forEach(function (sk) {
      var m = STAGE_META[sk];
      var cls = "todo", txt = m.no + m.title;
      if (f.name === state.selected && run[sk] === "running") { cls = "running"; txt = m.no + m.title + "..."; }
      else if (f.name === state.selected && run[sk] === "failed") { cls = "failed"; txt = m.no + m.title + "X"; }
      else if (stageDone(f, sk)) { cls = "done"; txt = m.no + m.title + "->"; }
      html += '<span class="badge ' + cls + '">' + txt + "</span>";
    });
    return html;
  }

  // 表单列表项显示轻流 appKey 与宜搭 formUuid（标识信息，比阶段流程更有意义）
  function formKeyBadges(f) {
    var ak = f.appKey || "--";
    var fu = f.formUuid || "(未填)";
    return '<span class="badge key" title="轻流 appKey">appKey: ' + esc(ak) + '</span>' +
           '<span class="badge key" title="宜搭 formUuid">formUuid: ' + esc(fu) + '</span>';
  }

  function renderList() {
    var box = el("form-list");
    var all = state.forms;
    var list = (state.appFilter === "all") ? all : all.filter(function (f) { return f.appId === state.appFilter; });
    if (!list.length) {
      box.innerHTML = '<div class="empty small">此应用暂无表单</div>';
      return;
    }
    box.innerHTML = "";
    list.forEach(function (f) {
      var item = document.createElement("div");
      item.className = "form-item" + (f.name === state.selected ? " active" : "");
      item.innerHTML =
        '<div class="fi-top">' +
          '<span class="fi-name">' + esc(f.name) + '</span>' +
          '<span class="fi-acts">' +
            '<button class="icon-btn" data-edit title="编辑">✎</button>' +
            '<button class="icon-btn danger" data-del title="删除">✕</button>' +
          '</span>' +
        '</div>' +
        '<div class="fi-badges">' + formKeyBadges(f) + '</div>';
      item.onclick = function (e) {
        if (e.target.closest(".icon-btn")) return;
        selectForm(f.name);
      };
      var editBtn = item.querySelector('[data-edit]');
      if (editBtn) editBtn.onclick = function (e) { e.stopPropagation(); openEditModal(f); };
      var delBtn = item.querySelector('[data-del]');
      if (delBtn) delBtn.onclick = function (e) { e.stopPropagation(); deleteForm(f.name); };
      box.appendChild(item);
    });
  }

  function selectForm(name) {
    state.selected = name;
    state.stageStatus = {};
    stopDataPoll(); stopAttPoll();
    // 不再自动拉取数据：根据现有产物初始化「数据准备」状态
    //  diff 存在且新鲜(diffFresh) -> 已就绪可直接操作；存在但不新鲜 -> 已过期需重新准备
    var f = state.forms.find(function (x) { return x.name === name; });
    if (f && f.diffExists && f.diffFresh) state.prepare = { status: "done", jobId: null };
    else if (f && f.diffExists && !f.diffFresh) state.prepare = { status: "stale", jobId: null };
    else state.prepare = { status: "idle", jobId: null };
    el("console").textContent = "";
    setConsoleStatus("idle", "空闲");
    renderList();
    renderDetail();
    setPrepareUi();
    loadAttachStats(name);
  }

  // ---- 右侧详情 ----
  function renderDetail() {
    var f = state.forms.find(function (x) { return x.name === state.selected; });
    if (!f) { el("no-form").classList.remove("hidden"); el("content").classList.add("hidden"); return; }
    el("no-form").classList.add("hidden");
    el("content").classList.remove("hidden");
    el("fh-name").textContent = f.name;
    var done = f.resultDone || 0, fail = f.resultFailed || 0;
    el("fh-meta").textContent =
      "appKey: " + (f.appKey || "--") + " | formUuid: " + (f.formUuid || "(无)") +
      " | 原始: " + (f.rawCount != null ? f.rawCount : "--") + " | 已写入: " + done + (fail ? " (失败 " + fail + ")" : "");
    el("fh-badges").innerHTML = stageBadges(f);
    renderStages(f);
  }

  function renderStages(f) {
    var box = el("steps");
    box.innerHTML = "";
    STAGE_ORDER.forEach(function (sk) {
      var m = STAGE_META[sk];
      var card = document.createElement("div");
      card.className = "step-card";
      var chips = "";
      m.steps.forEach(function (s) {
        chips += '<span class="badge ' + (stepDone(f, s) ? "done" : "todo") + '" title="' + s + '">' +
          s + " " + STEP_TITLE[s] + "</span> ";
      });
      var extra = "";
      if (sk === "s2" && f.diffExists) {
        extra = '<div class="sc-desc">最新：新增 ' + (f.diffCreate || 0) + " / 更新 " + (f.diffUpdate || 0) +
          " / 跳过 " + (f.diffSkip || 0) + "</div>";
      }
      var opts = "";
      if (m.force) opts = '<label class="chk"><input type="checkbox" data-force="' + sk + '">强制全部</label>';
      if (sk === "s4") {
        opts = '<label class="chk"><input type="checkbox" data-commit="s4"' + (activeApp().commitDefault ? " checked" : "") + '>提交</label>';
      }
      card.innerHTML =
        '<div class="sc-head"><span class="sc-no">' + m.no + '</span><span class="sc-title">' + m.title + '</span></div>' +
        '<div class="sc-desc">' + m.desc + '</div>' +
        '<div class="fi-badges">' + chips + '</div>' + extra +
        '<div class="sc-foot"><span class="sc-status badge todo" id="st-' + sk + '">' +
        (stageDone(f, sk) ? "完成" : "待执行") + '</span>' + opts + '</div>' +
        '<button class="btn primary sc-btn" data-stage="' + sk + '">运行 ' + m.no + '</button>';
      var stEl = card.querySelector("#st-" + sk);
      if (stageDone(f, sk)) stEl.className = "sc-status badge done";
      box.appendChild(card);
    });
    box.querySelectorAll("button[data-stage]").forEach(function (btn) {
      btn.onclick = function () { runStage(btn.getAttribute("data-stage")); };
    });
  }

  // ---- 数据准备（选表单后自动执行，一次拉取最新数据） ----
  function runPrepare() {
    if (!state.selected) return;
    if (state.prepare.status === "running") return;
    state.prepare = { status: "running", jobId: null };
    stopDataPoll();
    setPrepareUi();
    el("console").textContent = "";
    setConsoleStatus("running", "数据准备中...");
    api("/api/prepare", { method: "POST", body: JSON.stringify({ form: state.selected }) }).then(function (r) {
      if (!r.ok || !r.data || !r.data.ok) {
        state.prepare.status = "failed";
        setConsoleStatus("failed", "准备启动失败");
        consoleData((r.data && r.data.msg) || "数据准备任务启动失败");
        setPrepareUi();
        return;
      }
      state.prepare.jobId = r.data.jobId;
      state.dataJob = r.data.jobId;
      consoleData("数据准备任务 " + state.prepare.jobId + " 已启动: " +
        (r.data.steps || []).join(" → "));
      pollPrepare();
    });
  }

  function pollPrepare() {
    if (!state.prepare.jobId) return;
    api("/api/job/" + state.prepare.jobId).then(function (r) {
      var j = r.data;
      if (!j) return;
      if (j.output) {
        el("console").textContent = j.output;
        el("console").scrollTop = el("console").scrollHeight;
      }
      if (j.status === "running") {
        state.dataPollTimer = setTimeout(pollPrepare, 800);
        return;
      }
      var ok = j.status === "success";
      state.prepare.status = ok ? "done" : "failed";
      state.prepare.jobId = null;
      state.dataJob = null;
      setConsoleStatus(ok ? "done" : "failed", ok ? "数据准备完成" : "数据准备失败 (退出码 " + (j.returncode || "?") + ")");
      setPrepareUi();
      if (ok) {
        // 刷新最新表单状态与附件统计（绕过缓存）
        loadForms().then(function () {
          loadAttachStats(state.selected, true);
          setPrepareUi(); // 用最新 diff 数据渲染差异统计
          toast("数据准备完成，可执行更新数据 / 迁移附件", "ok");
        });
      } else {
        toast("数据准备失败，请查看控制台日志", "err");
      }
    });
  }

  function setPrepareUi() {
    var st = state.prepare.status;
    var badge = el("prepare-status");
    var detail = el("prepare-detail");
    if (badge) {
      var map = {
        idle: ["todo", "未准备"], running: ["running", "数据准备中..."],
        done: ["done", "已就绪"], stale: ["failed", "已过期"],
        failed: ["failed", "准备失败"],
      };
      badge.className = "badge " + (map[st] ? map[st][0] : "todo");
      badge.textContent = map[st] ? map[st][1] : "未知";
    }
    if (detail) {
      if (st === "running") detail.textContent = "正在拉取轻流数据 / 宜搭存量并生成差异清单...";
      else if (st === "done") detail.textContent = "数据已就绪，差异统计如下";
      else if (st === "stale") detail.textContent = "上次更新后未重新准备数据，再次更新会重复插入，请先重新准备";
      else if (st === "failed") detail.textContent = "请查看下方运行输出，修复后重新准备";
      else detail.textContent = "点击「准备数据」拉取最新数据，完成后可执行更新 / 迁移附件";
    }
    // 准备按钮文案
    var pb = el("btn-prepare");
    if (pb) pb.textContent = (st === "idle") ? "准备数据" : "重新准备数据";
    // 更新按钮可用性：仅「已就绪」可用（stale/failed/idle 禁用）
    var ready = st === "done";
    var updateBtn = el("btn-update");
    if (updateBtn) updateBtn.disabled = !ready;
    ["att-peek", "att-migrate"].forEach(function (id) {
      var b = el(id);
      if (b) b.disabled = !ready;
    });
    // 禁用原因提示 + 同步流程引导
    var why = ready ? "" : "请先完成「准备数据」";
    if (updateBtn) updateBtn.title = why;
    ["att-peek", "att-migrate"].forEach(function (id) {
      var b = el(id);
      if (b) b.title = why;
    });
    renderFlowSteps();
    // 差异统计
    var ds = el("diff-stats");
    if (ds) {
      if (st === "done") {
        var f = state.forms.find(function (x) { return x.name === state.selected; });
        if (f && f.diffExists) {
          ds.classList.remove("hidden");
          ds.innerHTML =
            '<span class="diff-chip">待新建 <b>' + (f.diffCreate || 0) + "</b></span>" +
            '<span class="diff-chip">待更新 <b>' + (f.diffUpdate || 0) + "</b></span>" +
            '<span class="diff-chip">无变化跳过 <b>' + (f.diffSkip || 0) + "</b></span>" +
            '<span class="diff-note">轻流源 ' + (f.rawCount != null ? f.rawCount : "--") +
            " 条 | 宜搭存量 " + (f.yidaCount != null ? f.yidaCount : "--") + " 条</span>";
          return;
        }
      }
      ds.classList.add("hidden");
      ds.innerHTML = "";
    }
  }

  // ---- 操作流程引导（准备数据 → 更新数据 → 迁移附件） ----
  function renderFlowSteps() {
    var st = state.prepare.status || "idle";
    var m1 = { idle: ["未开始", "1"], running: ["进行中", "…"],
               done: ["已完成", "✓"], stale: ["需重新准备", "!"],
               failed: ["准备失败", "!"] }[st] || ["未开始", "1"];
    var d1 = el("fs-dot-1"), s1 = el("fs-state-1");
    if (d1) {
      d1.textContent = m1[1];
      d1.className = "fs-dot" +
        (st === "done" ? " done" : st === "running" ? " running" :
         (st === "stale" || st === "failed") ? " err" : "");
    }
    if (s1) { s1.textContent = m1[0]; s1.className = "fs-state" + (st === "done" ? " ok" : ""); }
    var ready = st === "done";
    [[2, "update"], [3, "attach"]].forEach(function (t) {
      var stepEl = document.querySelector('.flow-step[data-action="' + t[1] + '"]');
      var dot = el("fs-dot-" + t[0]), stt = el("fs-state-" + t[0]);
      if (stepEl) stepEl.classList.toggle("blocked", !ready);
      if (dot) { dot.textContent = ready ? "✓" : String(t[0]); dot.className = "fs-dot" + (ready ? " done" : " blocked"); }
      if (stt) { stt.textContent = ready ? "可执行" : "需先准备数据"; stt.className = "fs-state" + (ready ? " ok" : " blocked"); }
    });
  }

  // ---- 数据迁移运行 ----
  function runStage(stage) {
    if (!state.selected) return;
    var forceBox = document.querySelector('input[data-force="' + stage + '"]');
    var commitBox = document.querySelector('input[data-commit="' + stage + '"]');
    var ao = activeApp();
    var commit = (stage === "s4") ? !!(commitBox && commitBox.checked) : !!ao.commitDefault;
    var force = !!(forceBox && forceBox.checked) || !!ao.force;
    startDataJob({
      url: "/api/run-stage",
      body: {
        form: state.selected, stage: stage,
        commit: (stage === "s4" && commit) ? true : undefined,
        limit: (stage === "s4" && state.settings.limit > 0) ? state.settings.limit : undefined,
        force: force || undefined,
      },
      label: "阶段 " + STAGE_META[stage].no + " " + STAGE_META[stage].title,
      stages: [stage],
    });
  }

  // 「更新数据」主按钮：按差异清单执行 04 写入（create batchSave / update insertOrUpdate）
  function updateData() {
    if (!state.selected || state.prepare.status !== "done") return;
    var commit = !!el("dm-commit").checked;
    var limit = parseInt(el("dm-limit").value, 10) || 0;
    startDataJob({
      url: "/api/run",
      body: {
        form: state.selected, step: "04",
        commit: commit,
        limit: limit > 0 ? limit : undefined,
      },
      label: "更新数据 (04 写入)" + (commit ? " [真实写入]" : " [预览]"),
      stages: ["s4"],
      onDone: function (ok) {
        // 写入完成后刷新表单状态与附件统计（宜搭已有数据可能变化）
        loadForms().then(function () {
          loadAttachStats(state.selected, true);
        });
        if (ok && commit) toast("数据已写入宜搭", "ok");
      },
    });
  }

  function startDataJob(opts) {
    stopDataPoll();
    el("console").textContent = "";
    setConsoleStatus("running", "运行中...");
    api(opts.url, { method: "POST", body: JSON.stringify(opts.body) }).then(function (r) {
      if (!r.ok || !r.data || !r.data.ok) {
        setConsoleStatus("failed", "启动失败");
        consoleData((r.data && r.data.msg) || "任务启动失败");
        toast((r.data && r.data.msg) || "启动失败", "err");
        // 409 = 差异清单过期（防重复写入拦截），把准备状态置为「已过期」
        if (r.status === 409 && state.prepare.status === "done") {
          state.prepare.status = "stale";
          setPrepareUi();
        }
        return;
      }
      state.dataJob = r.data.jobId;
      (opts.stages || []).forEach(function (sk) { setStageState(sk, "running"); });
      consoleData("任务 " + state.dataJob + " 已启动: " + (opts.label || ""));
      pollDataJob(opts.onDone);
    });
  }

  function pollDataJob(onDone) {
    if (!state.dataJob) return;
    api("/api/job/" + state.dataJob).then(function (r) {
      var j = r.data;
      if (!j) return;
      if (j.output) {
        el("console").textContent = j.output;
        el("console").scrollTop = el("console").scrollHeight;
      }
      if (j.status === "running") {
        var sk = stepToStage(j.step);
        if (sk) setStageState(sk, "running");
        state.dataPollTimer = setTimeout(function () { pollDataJob(onDone); }, 800);
        return;
      }
      var ok = j.status === "success";
      setConsoleStatus(ok ? "done" : "failed", ok ? "完成" : "失败 (退出码 " + (j.returncode || "?") + ")");
      STAGE_ORDER.forEach(function (sk) {
        if (state.stageStatus[sk] === "running") setStageState(sk, ok ? "done" : "failed");
      });
      state.dataJob = null;
      loadForms();
      if (onDone) onDone(ok);
      if (!onDone) toast(ok ? "数据迁移完成" : "数据迁移失败", ok ? "ok" : "err");
    });
  }

  function setStageState(stage, st) {
    state.stageStatus[stage] = st;
    var badge = el("st-" + stage);
    if (badge) {
      badge.className = "sc-status badge " + (st === "running" ? "running" : st === "failed" ? "failed" : st === "done" ? "done" : "todo");
      badge.textContent = st === "running" ? "运行中" : st === "failed" ? "失败" : st === "done" ? "完成" : "待执行";
    }
    renderList();
    if (state.selected) {
      var f = state.forms.find(function (x) { return x.name === state.selected; });
      if (f) el("fh-badges").innerHTML = stageBadges(f);
    }
  }

  function stopDataPoll() {
    if (state.dataPollTimer) { clearTimeout(state.dataPollTimer); state.dataPollTimer = null; }
    state.dataJob = null;
  }

  // ---- 附件迁移 ----
  function loadAttachStats(formName, refresh) {
    var url = "/api/attach/stats/" + encodeURIComponent(formName);
    if (refresh) url += "?refresh=1";
    api(url).then(function (r) {
      if (!r.data) return;
      var d = r.data;
      if (!d.hasAttachment) {
        el("att-no-data").classList.remove("hidden");
        el("att-stats").classList.add("hidden");
        el("att-controls").classList.add("hidden");
        return;
      }
      el("att-no-data").classList.add("hidden");
      el("att-stats").classList.remove("hidden");
      el("att-controls").classList.remove("hidden");
      var shtml = "";
      if (d.yidaRecords != null) shtml += statCard("宜搭已有数据", d.yidaRecords);
      var pendingN = d.pendingRecords || 0;
      shtml +=
        statCard("已迁移记录", d.migratedRecords || 0, "#16a34a") +
        statCard("已迁移文件", d.migratedFiles || 0, "#16a34a") +
        statCard("待迁移记录", pendingN, pendingN > 0 ? "#ea580c" : "#16a34a") +
        statCard("待迁移文件", d.pendingFiles || 0, pendingN > 0 ? "#ea580c" : "#16a34a") +
        statCard("即将过期链接", d.expiredUrls || 0, d.expiredUrls > 0 ? "#ef4444" : "") +
        statCard("附件字段", d.attFields || 0);
      el("att-stats").innerHTML = shtml;
    });
  }

  function statCard(label, value, color) {
    return '<div class="att-stat"><div class="val" style="color:' + (color || "#2f6fed") + '">' +
      value + '</div><div class="lbl">' + label + '</div></div>';
  }

  function runAttTask(mode) {
    if (!state.selected) return;
    if (mode !== "peek" && state.prepare.status !== "done") {
      toast("请先完成数据准备", "err"); return;
    }
    var commit = el("att-commit").checked;
    var limit = parseInt(el("att-limit").value, 10) || 0;
    if (mode === "migrate" && !commit) {
      // 未勾选「写入宜搭」= 预取验证（下载+上传VPS，不改宜搭）
      consoleAtt("未勾选「写入宜搭」，将以预取模式执行（下载+上传VPS，不写宜搭）");
    }
    setAttButtons(false);
    state.attJob = null;
    state.attLastEventTime = 0;
    el("att-job-stats").innerHTML = "";
    api("/api/attach/run", {
      method: "POST",
      body: JSON.stringify({ form: state.selected, mode: mode, commit: commit, limit: limit })
    }).then(function (r) {
      if (r.data && r.data.err) { toast(r.data.err, "err"); setAttButtons(true); return; }
      if (!r.data || !r.data.job_id) { toast("启动失败", "err"); setAttButtons(true); return; }
      state.attJob = r.data.job_id;
      consoleAtt("附件任务 " + state.attJob + " 已启动 (模式=" + mode + " 提交=" + commit + ")");
      startAttPolling();
    }).catch(function (e) {
      consoleAtt("启动失败: " + e.message);
      setAttButtons(true);
    });
  }

  function startAttPolling() {
    if (state.attPollTimer) clearInterval(state.attPollTimer);
    state.attPollTimer = setInterval(pollAttProgress, 500);
  }

  function stopAttPoll() {
    if (state.attPollTimer) { clearInterval(state.attPollTimer); state.attPollTimer = null; }
    state.attJob = null;
    setAttButtons(true);
  }

  function pollAttProgress() {
    if (!state.attJob) { stopAttPoll(); return; }
    api("/api/attach/progress/" + state.attJob + "?since=" + state.attLastEventTime).then(function (r) {
      var d = r.data;
      if (!d || d.err) return;
      if (d.events && d.events.length > 0) {
        d.events.forEach(function (e) {
          var prefix = e.type === "error" ? "[ERR]" : e.type === "warn" ? "[WARN]" : "[OK]";
          consoleAtt(prefix + " " + e.text);
          if (e.time > state.attLastEventTime) state.attLastEventTime = e.time;
        });
      }
      if (d.stats && Object.keys(d.stats).length > 0) {
        var s = d.stats;
        var html = "";
        if (s.downloaded) html += '<span class="att-job-stat"><span class="val">' + s.downloaded + '</span> 已下载</span>';
        if (s.cached) html += '<span class="att-job-stat"><span class="val">' + s.cached + '</span> 缓存命中</span>';
        if (s.uploaded) html += '<span class="att-job-stat"><span class="val">' + s.uploaded + '</span> 已上传</span>';
        if (s.written) html += '<span class="att-job-stat"><span class="val">' + s.written + '</span> 已写入</span>';
        if (s.skipped_migrated) html += '<span class="att-job-stat"><span class="val">' + s.skipped_migrated + '</span> 跳过(已迁移)</span>';
        if (s.skipped_no_yida) html += '<span class="att-job-stat"><span class="val">' + s.skipped_no_yida + '</span> 跳过(无宜搭数据)</span>';
        if (s.errors) html += '<span class="att-job-stat err"><span class="val">' + s.errors + '</span> 错误</span>';
        el("att-job-stats").innerHTML = html;
      }
      if (["done", "error", "cancelled"].indexOf(d.status) !== -1) {
        stopAttPoll();
        if (d.status === "done") toast("附件迁移完成", "ok");
        else if (d.status === "error") toast("附件迁移出错", "err");
        else toast("附件迁移已取消", "err");
        loadAttachStats(state.selected);
      }
    }).catch(function () {});
  }

  function setAttButtons(enabled) {
    // peek/migrate 的可用性由「数据准备完成」状态决定（setPrepareUi 控制），
    // 此处只控制运行中/空闲的禁用态
    ["att-peek", "att-migrate"].forEach(function (id) {
      var b = el(id);
      if (b) b.disabled = !enabled || state.prepare.status !== "done";
    });
    el("att-cancel").disabled = enabled;
  }

  // ---- 弹窗：新增/编辑表单 ----
  function openModal() {
    state.editingForm = null;
    el("modal-title").textContent = "新建表单";
    el("modal-save").textContent = "保存并生成配置";
    ["f-name", "f-appkey", "f-formuuid", "f-note"].forEach(function (i) { el(i).value = ""; });
    var apps = state.settings.yidaApps || [];
    var sel = el("f-appid");
    sel.innerHTML = apps.map(function (a, i) {
      return '<option value="' + i + '">' + esc(a.name || ("应用" + (i + 1))) + "</option>";
    }).join("");
    sel.value = String(state.settings.activeApp || 0);
    el("modal").classList.remove("hidden");
    el("f-name").focus();
  }

  function openEditModal(f) {
    state.editingForm = f.name;
    el("modal-title").textContent = "编辑表单";
    el("modal-save").textContent = "保存修改";
    el("f-name").value = f.name || "";
    el("f-appkey").value = f.appKey || "";
    el("f-formuuid").value = f.formUuid || "";
    el("f-note").value = f.note || "";
    var apps = state.settings.yidaApps || [];
    var sel = el("f-appid");
    sel.innerHTML = apps.map(function (a, i) {
      return '<option value="' + i + '">' + esc(a.name || ("应用" + (i + 1))) + "</option>";
    }).join("");
    sel.value = String(f.appId || 0);
    el("modal").classList.remove("hidden");
    el("f-name").focus();
  }

  function closeModal() {
    el("modal").classList.add("hidden");
    state.editingForm = null;
    ["f-name", "f-appkey", "f-formuuid", "f-note"].forEach(function (i) { el(i).value = ""; });
  }

  function deleteForm(name) {
    if (!window.confirm("删除表单「" + name + "」?\n将从注册表移除并删除本地配置（不可恢复）。\n原始数据文件将保留。")) return;
    api("/api/forms", { method: "DELETE", body: JSON.stringify({ name: name }) }).then(function (r) {
      if (!r.ok || !r.data || !r.data.ok) { toast((r.data && r.data.msg) || "删除失败", "err"); return; }
      if (state.selected === name) {
        state.selected = null;
        el("no-form").classList.remove("hidden");
        el("content").classList.add("hidden");
      }
      toast("已删除", "ok");
      loadForms();
    });
  }

  function saveForm() {
    var body = {
      name: el("f-name").value.trim(),
      appKey: el("f-appkey").value.trim(),
      formUuid: el("f-formuuid").value.trim(),
      note: el("f-note").value.trim(),
      appId: parseInt(el("f-appid").value, 10) || 0,
    };
    if (!body.name || !body.appKey) { toast("名称和 AppKey 为必填项", "err"); return; }
    var editing = state.editingForm;
    if (editing) body.oldName = editing;
    api("/api/forms", { method: editing ? "PUT" : "POST", body: JSON.stringify(body) }).then(function (r) {
      if (!r.ok || !r.data || !r.data.ok) { toast((r.data && r.data.msg) || "保存失败", "err"); return; }
      closeModal();
      toast(editing ? "已保存，正在同步配置..." : "已添加，正在生成配置...", "ok");
      loadForms().then(function () {
        selectForm(body.name);
        if (r.data.jobId) {
          state.dataJob = r.data.jobId;
          setConsoleStatus("running", "运行中...");
          setStageState("s1", "running");
          pollDataJob();
        }
      });
    });
  }

  // ---- 设置弹窗 ----
  function openSettings() {
    resetAppForm();
    renderAppList();
    el("s-userId").value = state.settings.userId || "";
    switchTab("apps");
    el("settings-modal").classList.remove("hidden");
  }
  function closeSettings() { el("settings-modal").classList.add("hidden"); }

  function switchTab(tab) {
    document.querySelectorAll(".modal-tabs .tab").forEach(function (t) {
      t.classList.toggle("active", t.dataset.tab === tab);
    });
    el("pane-apps").classList.toggle("hidden", tab !== "apps");
    el("pane-system").classList.toggle("hidden", tab !== "system");
  }

  function setupEye() {
    document.querySelectorAll("#settings-modal .eye").forEach(function (btn) {
      btn.onclick = function () {
        var input = el(btn.dataset.eye);
        if (!input) return;
        var showing = input.type === "text";
        input.type = showing ? "password" : "text";
        btn.textContent = showing ? "显" : "隐";
      };
    });
  }

  function renderAppList() {
    var box = el("app-list");
    var apps = state.settings.yidaApps || [];
    var active = state.settings.activeApp || 0;
    box.innerHTML = "";
    if (!apps.length) { box.innerHTML = '<div class="empty small">暂无已保存的宜搭应用</div>'; return; }
    apps.forEach(function (a, i) {
      var card = document.createElement("div");
      card.className = "app-card" + (i === active ? " active" : "");
      var tok = a.systemToken || "";
      var masked = tok.length > 6 ? (tok.slice(0, 4) + "..." + tok.slice(-4)) : (tok || "(空)");
      card.innerHTML =
        '<div class="ac-top"><span class="ac-name">' + esc(a.name || ("应用" + (i + 1))) + '</span>' +
        (i === active ? '<span class="badge done">当前</span>' : '<button class="btn tiny primary ac-use">使用</button>') +
        '</div>' +
        '<div class="ac-meta">appType: ' + esc(a.appType || "--") + '<br>systemToken: ' + esc(masked) + '</div>' +
        (apps.length > 1 ? '<button class="btn tiny ghost ac-del">删除</button>' : '');
      var useBtn = card.querySelector(".ac-use");
      if (useBtn) useBtn.onclick = function (e) { e.stopPropagation(); setActiveApp(i, true); };
      var delBtn = card.querySelector(".ac-del");
      if (delBtn) delBtn.onclick = function (e) { e.stopPropagation(); deleteApp(i); };
      card.onclick = function () { loadAppIntoForm(i); };
      box.appendChild(card);
    });
  }

  function setActiveApp(idx, persist) {
    state.settings.activeApp = idx;
    state.appFilter = idx;
    renderAppFilter();
    renderList();
    renderAppList();
    if (persist) saveSettingsPayload({ activeApp: idx });
  }

  function loadAppIntoForm(i) {
    var a = (state.settings.yidaApps || [])[i];
    if (!a) return;
    el("a-name").value = a.name || "";
    el("a-appType").value = a.appType || "";
    el("a-systemToken").value = a.systemToken || "";
    el("a-commit").checked = !!a.commitDefault;
    el("a-force").checked = !!a.force;
    el("a-submit").textContent = "保存修改";
    el("a-cancel").classList.remove("hidden");
    state.editingIdx = i;
  }

  function resetAppForm() {
    el("a-name").value = ""; el("a-appType").value = ""; el("a-systemToken").value = "";
    el("a-commit").checked = false; el("a-force").checked = false;
    el("a-submit").textContent = "+ Add App";
    el("a-cancel").classList.add("hidden");
    state.editingIdx = null;
  }

  function submitApp() {
    var name = el("a-name").value.trim();
    var appType = el("a-appType").value.trim();
    var systemToken = el("a-systemToken").value.trim();
    if (!appType || !systemToken) { toast("应用类型和系统令牌为必填项", "err"); return; }
    var apps = state.settings.yidaApps || [];
    var entry = {
      name: name || ("应用" + (apps.length + 1)),
      appType: appType, systemToken: systemToken,
      commitDefault: el("a-commit").checked,
      force: el("a-force").checked,
    };
    if (state.editingIdx != null && apps[state.editingIdx]) {
      apps[state.editingIdx] = entry;
    } else {
      apps.push(entry);
    }
    state.settings.yidaApps = apps;
    resetAppForm();
    renderAppList();
    toast("应用列表已更新（保存后生效）", "ok");
  }

  function deleteApp(i) {
    var apps = state.settings.yidaApps || [];
    if (apps.length <= 1) { toast("至少保留一个应用", "err"); return; }
    apps.splice(i, 1);
    if (state.settings.activeApp >= i) state.settings.activeApp = Math.max(0, state.settings.activeApp - 1);
    state.settings.yidaApps = apps;
    renderAppList();
    toast("已删除（保存后生效）", "ok");
  }

  function saveSettingsPayload(extra) {
    return api("/api/settings", {
      method: "POST",
      body: JSON.stringify(Object.assign({
        yidaApps: state.settings.yidaApps || [],
        activeApp: state.settings.activeApp || 0,
        userId: state.settings.userId || "",
        limit: state.settings.limit || 0,
        attLimit: state.settings.attLimit || 0,
      }, extra || {}))
    });
  }

  function saveSettings() {
    var apps = (state.settings.yidaApps || []).map(function (a) {
      return { name: a.name, appType: a.appType, systemToken: a.systemToken,
               commitDefault: !!a.commitDefault, force: !!a.force };
    });
    var body = {
      yidaApps: apps,
      activeApp: state.settings.activeApp || 0,
      userId: el("s-userId").value.trim(),
      limit: state.settings.limit || 0,
      attLimit: state.settings.attLimit || 0,
    };
    api("/api/settings", { method: "POST", body: JSON.stringify(body) }).then(function (r) {
      if (!r.ok || !r.data || !r.data.ok) { toast((r.data && r.data.msg) || "保存失败", "err"); return; }
      loadSettings().then(function () {
        renderAppFilter();
        renderList();
        closeSettings();
        toast("设置已保存", "ok");
      });
    });
  }

  // ---- 卡片折叠 ----
  function setupCards() {
    document.querySelectorAll("[data-toggle]").forEach(function (head) {
      head.onclick = function () {
        var card = this.parentElement;
        card.classList.toggle("collapsed");
      };
    });
  }

  // ---- 事件绑定 ----
  el("btn-refresh").onclick = function () { loadForms(); };
  el("btn-add").onclick = openModal;
  el("modal-close").onclick = closeModal;
  el("modal-cancel").onclick = closeModal;
  el("modal-save").onclick = saveForm;
  el("btn-clear").onclick = function () { el("console").textContent = ""; };
  el("btn-settings").onclick = openSettings;
  el("settings-close").onclick = closeSettings;
  el("settings-cancel").onclick = closeSettings;
  el("settings-save").onclick = saveSettings;
  document.querySelectorAll(".modal-tabs .tab").forEach(function (t) {
    t.onclick = function () { switchTab(t.dataset.tab); };
  });
  setupEye();
  el("a-submit").onclick = submitApp;
  el("a-cancel").onclick = resetAppForm;
  el("app-filter").onchange = function () {
    var v = el("app-filter").value;
    state.appFilter = v;
    if (v !== "all") setActiveApp(parseInt(v, 10), true);
    else renderList();
  };
  // 附件按钮
  el("att-peek").onclick = function () { runAttTask("peek"); };
  el("att-migrate").onclick = function () { runAttTask("migrate"); };
  el("att-cancel").onclick = function () {
    if (!state.attJob) return;
    api("/api/attach/cancel/" + state.attJob, { method: "POST" });
    consoleAtt("正在取消附件任务...");
  };
  // 数据迁移面板：重新准备数据 + 更新数据主按钮 + 写入数量限制
  var prepareBtn = el("btn-prepare");
  if (prepareBtn) prepareBtn.onclick = runPrepare;
  var updateBtn = el("btn-update");
  if (updateBtn) updateBtn.onclick = updateData;
  var dmLimit = el("dm-limit");
  if (dmLimit) {
    dmLimit.value = state.settings.limit || 0;
    dmLimit.onchange = function () {
      state.settings.limit = parseInt(dmLimit.value, 10) || 0;
      saveSettingsPayload({ limit: state.settings.limit });
    };
  }
  var attLimit = el("att-limit");
  if (attLimit) {
    attLimit.value = state.settings.attLimit || 0;
    attLimit.onchange = function () {
      state.settings.attLimit = parseInt(attLimit.value, 10) || 0;
      saveSettingsPayload({ attLimit: state.settings.attLimit });
    };
  }

  // 流程引导：点击步骤跳转到对应面板（已就绪的步骤可执行）
  document.querySelectorAll(".flow-step").forEach(function (step) {
    step.onclick = function () {
      var card = el(step.getAttribute("data-scroll"));
      if (!card) return;
      card.classList.remove("collapsed");
      card.scrollIntoView({ behavior: "smooth", block: "start" });
      if (step.getAttribute("data-action") === "update" && state.prepare.status === "done") {
        var ub = el("btn-update");
        if (ub) { ub.focus(); ub.classList.add("pulse"); setTimeout(function () { ub.classList.remove("pulse"); }, 1800); }
      }
    };
  });

  setupCards();

  // ---- 启动 ----
  loadSettings().then(loadForms);
})();
