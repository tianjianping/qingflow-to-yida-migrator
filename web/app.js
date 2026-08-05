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
    dataPollFail: 0,
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
    opts = opts || {};
    // P0-3: 统一超时（默认 15s，长任务轮询可传更大值），避免请求无限挂起
    var timeout = opts.timeout || 15000;
    var ctrl = (typeof AbortController !== "undefined") ? new AbortController() : null;
    var timer = null;
    if (ctrl) {
      timer = setTimeout(function () { ctrl.abort(); }, timeout);
    }
    var init = Object.assign({ headers: { "Content-Type": "application/json" } }, opts);
    if (ctrl) init.signal = ctrl.signal;
    return fetch(path, init)
      .then(function (res) {
        if (timer) clearTimeout(timer);
        return res.json().then(function (data) {
          return { ok: res.ok, status: res.status, data: data };
        }).catch(function () {
          return { ok: res.ok, status: res.status, data: null };
        });
      }, function (err) {
        if (timer) clearTimeout(timer);
        var msg = (err && err.name === "AbortError") ? "请求超时，请稍后重试" : "网络请求失败";
        var e = new Error(msg);
        e.timeout = !!(err && err.name === "AbortError");
        throw e;
      });
  }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }
  function fmtDur(sec) {
    if (!sec || sec < 0) return "--";
    var s = Math.round(sec), m = Math.floor(s / 60);
    if (m >= 60) { var h = Math.floor(m / 60); return h + "h" + (m % 60) + "m"; }
    return m + "m" + (s % 60) + "s";
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
  function loadForms(opts) {
    opts = opts || {};
    return api("/api/forms").then(function (r) {
      if (!r.data) return;
      state.forms = r.data;
      renderAppFilter();
      renderList();
      if (state.selected) {
        var still = r.data.find(function (f) { return f.name === state.selected; });
      if (still) { state.selected = still.name; renderDetail(); loadFormDetail(still.name, opts); }
      }
    }).catch(function () { toast("加载表单列表失败，请检查服务状态", "err"); });
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
    }).catch(function () { toast("加载设置失败", "err"); });
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
    // 立即渲染列表（高亮选中项）
    renderList();
    // 先用列表中的轻量数据渲染，再按需加载完整详情
    renderDetail();
    setPrepareUi();
    // 异步加载完整详情（含计数、附件统计等）
    loadFormDetail(name);
  }

  function loadFormDetail(name, opts) {
    opts = opts || {};
    api("/api/forms/" + encodeURIComponent(name) + "/detail").then(function (r) {
      if (!r.ok || !r.data) return;
      var idx = state.forms.findIndex(function (f) { return f.name === name; });
      if (idx >= 0) {
        state.forms[idx] = Object.assign(state.forms[idx], r.data);
      }
      if (state.selected === name) {
        var f = state.forms.find(function (x) { return x.name === name; });
        if (f && f.diffExists && f.diffFresh) {
          state.prepare = { status: "done", jobId: null };
          // 写入数据后默认不自动刷新预检（预检是迁移前诊断，写入后立即重跑会覆盖警告展示）
          if (!opts.skipPreflight) loadPreflight(name);
        } else if (f && f.diffExists && !f.diffFresh) {
          state.prepare = { status: "stale", jobId: null };
        } else {
          state.prepare = { status: "idle", jobId: null };
        }
        renderDetail();
        setPrepareUi();
        loadAttachStats(name);
      }
    }).catch(function () { consoleData("加载表单详情失败"); });
  }

  // ---- 右侧详情 ----
  function renderDetail() {
    var f = state.forms.find(function (x) { return x.name === state.selected; });
    if (!f) { el("no-form").classList.remove("hidden"); el("content").classList.add("hidden"); return; }
    el("no-form").classList.add("hidden");
    el("content").classList.remove("hidden");
    var ftCls = f.formType === "process" ? "proc" : "norm";
    var ftLabel = f.formTypeLabel || "普通表单";
    var ftSrc = f.formTypeSource || "";
    el("fh-name").innerHTML = esc(f.name) +
      ' <span class="badge ' + ftCls + '" title="宜搭表单类型（探测来源: ' + esc(ftSrc) + '）">' +
      esc(ftLabel) + "</span>";
    var done = f.resultDone || 0, fail = f.resultFailed || 0;
    el("fh-meta").textContent =
      "appKey: " + (f.appKey || "--") + " | formUuid: " + (f.formUuid || "(无)") +
      " | 原始: " + (f.rawCount != null ? f.rawCount : "--") + " | 已写入: " + done + (fail ? " (失败 " + fail + ")" : "");
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

  // ---- 数据准备（拉取数据与格式化解耦） ----
  // mode: "all"(默认) 完整准备 | "fetch" 仅拉取 | "transform" 仅格式化
  function runPrepare(mode) {
    if (!state.selected) return;
    if (state.prepare.status === "running") return;
    mode = mode || "all";
    state.prepare = { status: "running", jobId: null, mode: mode };
    stopDataPoll();
    setPrepareUi();
    el("console").textContent = "";
    var label = mode === "fetch" ? "拉取数据" : mode === "transform" ? "格式化数据" : "数据准备";
    setConsoleStatus("running", label + "中...");
    var body = { form: state.selected, mode: mode };
    // 轻流拉取调试开关（可选，默认增量拉取）；跳过拉取与强制全量互斥（跳过优先）
    body.skipFetch = !!el("pf-skip-fetch").checked || undefined;
    body.forceFull = (!body.skipFetch && !!el("pf-force-full").checked) || undefined;
    // 刷新宜搭结构（修改宜搭表单后必须勾选，让 02 忽略缓存重新拉取）
    body.refreshYida = !!el("pf-refresh-yida").checked || undefined;
    api("/api/prepare", { method: "POST", body: JSON.stringify(body) }).then(function (r) {
      if (!r.ok || !r.data || !r.data.ok) {
        state.prepare.status = "failed";
        setConsoleStatus("failed", label + "启动失败");
        consoleData((r.data && r.data.msg) || label + "任务启动失败");
        setPrepareUi();
        return;
      }
      state.prepare.jobId = r.data.jobId;
      state.dataJob = r.data.jobId;
      consoleData(label + "任务 " + state.prepare.jobId + " 已启动: " +
        (r.data.steps || []).join(" → "));
      pollPrepare();
    }).catch(function (e) {
      state.prepare.status = "failed";
      setConsoleStatus("failed", "启动失败");
      consoleData((e && e.message) || label + "任务启动失败");
      setPrepareUi();
      toast(label + "启动失败", "err");
    });
  }

  function pollPrepare() {
    if (!state.prepare.jobId) return;
    api("/api/job/" + state.prepare.jobId, { timeout: 60000 }).then(function (r) {
      state.dataPollFail = 0;
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
        // 拉取数据(fetch)仅缓存源数据，不做字段对齐 -> 不自动预检（避免旧映射产生误导性工作清单）
        var hasAlign = state.prepare.mode !== "fetch";
        // 刷新最新表单状态与附件统计（绕过缓存）
        loadForms().then(function () {
          loadAttachStats(state.selected, true);
          setPrepareUi(); // 用最新 diff 数据渲染差异统计
          if (hasAlign) {
            loadPreflight(state.selected); // 准备/格式化完成后自动预检
          } else {
            consoleData("源数据已拉取到本地缓存。请点击「格式化数据」完成字段对齐与三方对账后，再查看迁移预检。");
          }
        });
      } else {
        toast("数据准备失败，请查看控制台日志", "err");
      }
    }).catch(function (e) {
      state.dataPollFail = (state.dataPollFail || 0) + 1;
      if (state.dataPollFail >= 8) {
        state.prepare.status = "failed";
        state.prepare.jobId = null;
        state.dataJob = null;
        setConsoleStatus("failed", "进度获取失败");
        setPrepareUi();
        toast("数据准备进度获取失败，请检查服务状态", "err");
        return;
      }
      setConsoleStatus("running", "连接中断，重试中...");
      state.dataPollTimer = setTimeout(pollPrepare, 1500);
    });
  }

  // ---- 迁移前预检 ----
  function loadPreflight(formName) {
    var box = el("preflight-box");
    if (!box) return;
    box.classList.remove("hidden");
    box.innerHTML = '<div class="pf-loading">正在预检...</div>';
    api("/api/preflight/" + encodeURIComponent(formName), { timeout: 40000 }).then(function (r) {
      if (!r.ok || !r.data) { box.classList.add("hidden"); return; }
      renderPreflight(r.data);
    }).catch(function () {
      box.innerHTML = '<div class="pf-loading">预检失败或超时，请稍后重试</div>';
    });
  }

  function renderPreflight(data) {
    var box = el("preflight-box");
    if (!box) return;
    var checks = data.checks || [];
    var worklist = data.worklist || [];
    if (!checks.length && !worklist.length) {
      box.classList.add("hidden");
      return;
    }
    var summary = data.summary || {};
    var icon = summary.errors ? "⛔" : summary.warnings ? "⚠️" : "✅";
    var title = icon + " 迁移预检：";
    if (summary.errors) title += summary.errors + " 个错误";
    if (summary.warnings) title += (summary.errors ? " / " : "") + summary.warnings + " 个警告";
    if (summary.infos) title += (summary.errors || summary.warnings ? " / " : "") + summary.infos + " 个提示";
    if (!summary.errors && !summary.warnings && !summary.infos && !worklist.length) title = "✅ 迁移预检通过，未发现异常";

    var cls = summary.errors ? "pf-has-error" : summary.warnings ? "pf-has-warn" : "pf-ok";
    var html = '<div class="pf-header ' + cls + '">' + esc(title) + "</div>";
    // 工作清单操作：保存为本地 MD（docs/worklist/）+ 一键复制
    if (worklist.length || checks.length) {
      html += '<div class="pf-actions">' +
        '<button type="button" class="btn tiny" id="btn-wl-save">保存 MD</button>' +
        '<button type="button" class="btn tiny" id="btn-wl-copy">复制清单</button>' +
        "</div>";
    }
    // 宜搭手动搭建工作清单：可勾选的待办列表（勾选后本地划掉）
    if (worklist.length) {
      html += '<div class="pf-group wl-group">' +
        '<span class="pf-cat">宜搭手动调整工作清单（' + worklist.length + ' 项）</span>' +
        '<div class="wl-list">';
      worklist.forEach(function (w, i) {
        html += '<label class="wl-item" data-idx="' + i + '">' +
          '<input type="checkbox" class="wl-chk">' +
          '<span class="wl-body">' +
            '<span class="wl-action wl-' + w.action + '">' + esc(w.action) + '</span>' +
            '<span class="wl-field">' + esc(w.field) + '</span>' +
            '<span class="wl-detail">' + esc(w.detail) + "</span>" +
          "</span></label>";
      });
      html += "</div></div>";
    }
    var grouped = {};
    checks.forEach(function (c) {
      if (!grouped[c.category]) grouped[c.category] = [];
      grouped[c.category].push(c);
    });
    var body = "";
    Object.keys(grouped).forEach(function (cat) {
      body += '<div class="pf-group"><span class="pf-cat">' + esc(cat) + "</span>";
      grouped[cat].forEach(function (c) {
        var lv = c.level === "error" ? "err" : c.level === "warn" ? "warn" : "info";
        body += '<div class="pf-item pf-' + lv + '">' +
          '<span class="pf-title">' + esc(c.title) + "</span>" +
          (c.detail ? '<span class="pf-detail">' + esc(c.detail) + "</span>" : "") +
          (c.suggestion ? '<span class="pf-suggest">→ ' + esc(c.suggestion) + "</span>" : "") +
          "</div>";
      });
      body += "</div>";
    });
    // 有错误时默认展开；仅有警告/提示时折叠为可展开详情
    if (summary.errors) {
      box.innerHTML = html + '<div class="pf-body">' + body + "</div>";
    } else {
      box.innerHTML = html + '<details class="pf-body"><summary>查看详情</summary>' + body + "</details>";
    }
    // 勾选交互：勾掉后整项划淡（本地状态，仅当前页面）
    box.querySelectorAll(".wl-chk").forEach(function (chk) {
      chk.addEventListener("change", function () {
        chk.closest(".wl-item").classList.toggle("done", chk.checked);
      });
    });
    // 保存 MD / 复制清单
    var btnSave = el("btn-wl-save"), btnCopy = el("btn-wl-copy");
    if (btnSave) btnSave.addEventListener("click", function () {
      api("/api/preflight/" + encodeURIComponent(state.selected) + "/md", { timeout: 60000 }).then(function (r) {
        if (r.ok && r.data && r.data.ok) {
          toast("工作清单已保存: " + r.data.path, "ok");
          consoleData("工作清单已保存: " + r.data.path);
        } else {
          toast((r.data && r.data.msg) || "保存失败", "err");
        }
      }).catch(function () { toast("保存失败", "err"); });
    });
    if (btnCopy) btnCopy.addEventListener("click", function () {
      api("/api/preflight/" + encodeURIComponent(state.selected) + "/md", { timeout: 60000 }).then(function (r) {
        if (!r.ok || !r.data || !r.data.ok) {
          toast((r.data && r.data.msg) || "生成失败", "err");
          return;
        }
        copyText(r.data.md);
      }).catch(function () { toast("生成失败", "err"); });
    });
  }

  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () {
        toast("已复制到剪贴板", "ok");
      }).catch(function () { fallbackCopy(text); });
    } else { fallbackCopy(text); }
  }

  function fallbackCopy(text) {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); toast("已复制到剪贴板", "ok"); }
    catch (e) { toast("复制失败，请手动复制", "err"); }
    document.body.removeChild(ta);
  }

  function setPrepareUi() {
    var st = state.prepare.status;
    var badge = el("prepare-status");
    // 预检结果区：仅在已就绪时显示
    var pfBox = el("preflight-box");
    if (pfBox && st !== "done") pfBox.classList.add("hidden");
    if (badge) {
      var map = {
        idle: ["todo", "未准备"], running: ["running", "数据准备中..."],
        done: ["done", "已就绪"], stale: ["failed", "已过期"],
        failed: ["failed", "准备失败"],
      };
      badge.className = "badge " + (map[st] ? map[st][0] : "todo");
      badge.textContent = map[st] ? map[st][1] : "未知";
    }
    // 准备按钮文案：① 卡片内固定「准备数据」；「重新准备数据」已移至写入侧与写入数据对齐
    var pb = el("btn-prepare");
    if (pb) pb.textContent = "准备数据";
    // 「重新准备数据」可用性：已准备过（done/stale/failed）才可重新执行
    var reprepareBtn = el("btn-reprepare");
    if (reprepareBtn) reprepareBtn.disabled = st === "idle" || st === "running";
    // 更新按钮可用性：仅「已就绪」可用（stale/failed/idle 禁用）
    var ready = st === "done";
    var updateBtn = el("btn-update");
    if (updateBtn) updateBtn.disabled = !ready;
    ["att-peek", "att-migrate"].forEach(function (id) {
      var b = el(id);
      if (b) b.disabled = !ready;
    });
    // 分阶段排查：仅异常状态（已过期/失败）显示
    var adv = el("adv-steps");
    if (adv) adv.classList.toggle("hidden", st !== "stale" && st !== "failed");
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

  // ---- 数据迁移运行 ----
  function runStage(stage) {
    if (!state.selected) return;
    var forceBox = document.querySelector('input[data-force="' + stage + '"]');
    var commitBox = document.querySelector('input[data-commit="' + stage + '"]');
    var ao = activeApp();
    var commit = (stage === "s4") ? !!(commitBox && commitBox.checked) : !!ao.commitDefault;
    var force = !!(forceBox && forceBox.checked) || !!ao.force;
    var body = {
      form: state.selected, stage: stage,
      commit: (stage === "s4" && commit) ? true : undefined,
      limit: (stage === "s4" && state.settings.limit > 0) ? state.settings.limit : undefined,
      force: force || undefined,
    };
    // 轻流拉取调试开关（阶段一重跑时生效）
    if (stage === "s1") {
      body.skipFetch = !!el("pf-skip-fetch").checked || undefined;
      body.forceFull = (!body.skipFetch && !!el("pf-force-full").checked) || undefined;
      body.refreshYida = !!el("pf-refresh-yida").checked || undefined;
    }
    startDataJob({
      url: "/api/run-stage",
      body: body,
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
      label: "写入数据 (04)" + (commit ? " [真实写入]" : " [预览]"),
      stages: ["s4"],
      onDone: function (ok) {
        // 写入完成后刷新表单状态与附件统计（宜搭已有数据可能变化）；
        // skipPreflight: 预检是迁移前诊断，写入后不自动重跑，避免立即刷新覆盖警告展示
        loadForms({ skipPreflight: true }).then(function () {
          loadAttachStats(state.selected, true);
        });
        // 成功反馈由界面状态承载：按钮短暂显示「已完成」
        if (ok && commit) {
          var ub = el("btn-update");
          if (ub) {
            ub.textContent = "已完成"; ub.disabled = true;
            setTimeout(function () {
              ub.textContent = "写入数据"; ub.disabled = state.prepare.status !== "done";
            }, 1800);
          }
        }
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
    }).catch(function (e) {
      setConsoleStatus("failed", "启动失败");
      consoleData((e && e.message) || "任务启动失败");
      toast((e && e.message) || "启动失败", "err");
    });
  }

  function pollDataJob(onDone) {
    if (!state.dataJob) return;
    api("/api/job/" + state.dataJob, { timeout: 60000 }).then(function (r) {
      state.dataPollFail = 0;
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
      else consoleData(ok ? "任务完成" : "任务失败，请查看上方输出");
    }).catch(function () {
      state.dataPollFail = (state.dataPollFail || 0) + 1;
      if (state.dataPollFail >= 8) {
        setConsoleStatus("failed", "进度获取失败");
        toast("数据任务进度获取失败，请检查服务状态", "err");
        return;
      }
      setConsoleStatus("running", "连接中断，重试中...");
      state.dataPollTimer = setTimeout(function () { pollDataJob(onDone); }, 1500);
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
      var section = el("att-section");
      var sbox = el("att-stats"), cbox = el("att-controls");
      if (!d.hasAttachment) {
        if (section) section.classList.add("hidden");
        return;
      }
      if (section) section.classList.remove("hidden");
      if (sbox) sbox.classList.remove("hidden");
      if (cbox) cbox.classList.remove("hidden");
      // URL 新鲜度用于「迁移附件」时的定向提示（runAttTask）
      state.attRawFresh = !!d.rawFresh;
      var pendingN = d.pendingRecords || 0;
      var shtml =
        statCard("待迁移文件", d.pendingFiles || 0, pendingN > 0 ? "#ea580c" : "#16a34a") +
        statCard("已迁移记录", d.migratedRecords || 0, "#16a34a") +
        statCard("即将过期链接", d.expiredUrls || 0, d.expiredUrls > 0 ? "#ef4444" : "");
      var more =
        statCard("宜搭已有数据", d.yidaRecords != null ? d.yidaRecords : "--") +
        statCard("已迁移文件", d.migratedFiles || 0, "#16a34a") +
        statCard("待迁移记录", pendingN, pendingN > 0 ? "#ea580c" : "#16a34a") +
        statCard("附件字段", d.attFields || 0);
      shtml += '<details class="att-more"><summary>更多统计</summary><div class="att-more-grid">' + more + "</div></details>";
      if (sbox) sbox.innerHTML = shtml;
    }).catch(function () { consoleAtt("附件统计加载失败"); });
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
    if (mode === "migrate" && state.attRawFresh === false) {
      consoleAtt("附件 URL 缓存已过期，任务将先自动增量拉取刷新");
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
    state.attStartTs = Date.now();   // C4: ETA 估算基准
    scheduleAttPoll();
  }

  function stopAttPoll() {
    if (state.attPollTimer) { clearTimeout(state.attPollTimer); state.attPollTimer = null; }
    state.attJob = null;
    setAttButtons(true);
  }

  function scheduleAttPoll() {
    if (!state.attJob) return;
    if (state.attPollTimer) clearTimeout(state.attPollTimer);
    state.attPollTimer = setTimeout(pollAttProgress, 500);
  }

  function pollAttProgress() {
    if (!state.attJob) { stopAttPoll(); return; }
    api("/api/attach/progress/" + state.attJob + "?since=" + state.attLastEventTime, { timeout: 60000 }).then(function (r) {
      var d = r.data;
      if (!d || d.err) { scheduleAttPoll(); return; }
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
        // C4: 进度条 + 百分比 + ETA（total/done 由后端在任务运行时实时更新）
        if (s.total > 0) {
          var done = s.done || 0;
          var pct = Math.min(100, Math.max(0, Math.round(done / s.total * 100)));
          html += '<div class="att-progress"><div class="att-bar" style="width:' + pct + '%"></div>' +
                  '<span class="att-pct">' + pct + '%</span></div>';
          html += '<span class="att-job-stat"><span class="val">' + done + '</span>/' + s.total + ' 条</span>';
          if (done > 0 && state.attStartTs) {
            var elapsed = (Date.now() - state.attStartTs) / 1000;
            var eta = Math.round(elapsed / done * (s.total - done));
            html += '<span class="att-job-stat">预计剩余 ' + fmtDur(eta) + '</span>';
          }
        }
        if (s.downloaded) html += '<span class="att-job-stat"><span class="val">' + s.downloaded + '</span> 已下载</span>';
        if (s.cached) html += '<span class="att-job-stat"><span class="val">' + s.cached + '</span> 缓存命中</span>';
        if (s.uploaded) html += '<span class="att-job-stat"><span class="val">' + s.uploaded + '</span> 已上传</span>';
        if (s.written) html += '<span class="att-job-stat"><span class="val">' + s.written + '</span> 已写入</span>';
        if (s.refreshed_urls) html += '<span class="att-job-stat"><span class="val">' + s.refreshed_urls + '</span> 定向刷新</span>';
        if (s.skipped_migrated) html += '<span class="att-job-stat"><span class="val">' + s.skipped_migrated + '</span> 跳过(已迁移)</span>';
        if (s.skipped_no_yida) html += '<span class="att-job-stat"><span class="val">' + s.skipped_no_yida + '</span> 跳过(无宜搭数据)</span>';
        if (s.errors) html += '<span class="att-job-stat err"><span class="val">' + s.errors + '</span> 错误</span>';
        el("att-job-stats").innerHTML = html;
      }
      if (["done", "error", "cancelled"].indexOf(d.status) !== -1) {
        stopAttPoll();
        if (d.status === "error") toast("附件迁移出错", "err");
        else if (d.status === "cancelled") toast("附件迁移已取消", "err");
        loadAttachStats(state.selected);
      } else {
        scheduleAttPoll();
      }
    }).catch(function () { scheduleAttPoll(); });
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
    // 打开弹窗时自动拉取表单/应用列表（凭证未配置时下拉保持可手动输入兜底）
    pullYidaForms(sel.value);
    pullQingFlowApps();
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
    pullYidaForms(sel.value);
    pullQingFlowApps();
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
      loadForms();
    }).catch(function () { toast("删除请求失败", "err"); });
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
      loadForms().then(function () {
        selectForm(body.name);
        if (r.data.jobId) {
          state.dataJob = r.data.jobId;
          setConsoleStatus("running", "运行中...");
          setStageState("s1", "running");
          pollDataJob();
        }
      });
    }).catch(function () { toast("保存表单请求失败", "err"); });
  }

  // ---- 设置弹窗 ----
  function openSettings() {
    el("s-userId").value = state.settings.userId || "";
    switchTab("credentials");
    el("settings-modal").classList.remove("hidden");
  }
  function closeSettings() { el("settings-modal").classList.add("hidden"); }

  // ---- 宜搭应用管理弹窗（增删改 / 设为当前） ----
  function openAppsModal() {
    resetAppForm();
    renderAppList();
    el("apps-modal").classList.remove("hidden");
  }
  function closeAppsModal() {
    el("apps-modal").classList.add("hidden");
    resetAppForm();
  }
  function saveApps() {
    var apps = (state.settings.yidaApps || []).map(function (a) {
      return { name: a.name, appType: a.appType, systemToken: a.systemToken,
               commitDefault: !!a.commitDefault, force: !!a.force };
    });
    api("/api/settings", {
      method: "POST",
      body: JSON.stringify({
        yidaApps: apps,
        activeApp: state.settings.activeApp || 0,
        userId: state.settings.userId || "",
        limit: state.settings.limit || 0,
        attLimit: state.settings.attLimit || 0,
      })
    }).then(function (r) {
      if (!r.ok || !r.data || !r.data.ok) { toast((r.data && r.data.msg) || "保存失败", "err"); return; }
      loadSettings().then(function () {
        renderAppFilter();
        renderList();
        closeAppsModal();
      });
    }).catch(function () { toast("保存应用请求失败", "err"); });
  }

  function switchTab(tab) {
    document.querySelectorAll(".modal-tabs .tab").forEach(function (t) {
      t.classList.toggle("active", t.dataset.tab === tab);
    });
    el("pane-system").classList.toggle("hidden", tab !== "system");
    el("pane-credentials").classList.toggle("hidden", tab !== "credentials");
    if (tab === "credentials") loadCredentialStatus();
  }

  function setupEye() {
    document.querySelectorAll(".eye").forEach(function (btn) {
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
    el("a-submit").textContent = "+ 添加应用";
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
  }

  function deleteApp(i) {
    var apps = state.settings.yidaApps || [];
    if (apps.length <= 1) { toast("至少保留一个应用", "err"); return; }
    apps.splice(i, 1);
    if (state.settings.activeApp >= i) state.settings.activeApp = Math.max(0, state.settings.activeApp - 1);
    state.settings.yidaApps = apps;
    renderAppList();
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
    }).catch(function () { toast("保存设置失败", "err"); });
  }

  // ---- 凭证配置（部署后网页配置，只写不回显） ----
  var CRED_KEYS = [
    "qingflow.accessToken", "qingflow.baseUrl", "qingflow.userId",
    "dingtalk.appKey", "dingtalk.appSecret",
    "yida.systemToken", "yida.appType", "yida.userId",
    "attachment_storage.endpoint", "attachment_storage.upload_url",
    "attachment_storage.upload_token", "attachment_storage.local_cache",
  ];

  function credInputId(key) { return "c-" + key.replace(".", "-"); }

  function loadCredentialStatus() {
    return api("/api/credentials").then(function (r) {
      var summary = r.data || {};
      // 重置清除按钮状态
      document.querySelectorAll("#pane-credentials .cred-clear").forEach(function (b) {
        b.textContent = "清除"; b.classList.remove("pending");
      });
      CRED_KEYS.forEach(function (key) {
        var parts = key.split(".");
        var st = (summary[parts[0]] || {})[parts[1]] || {};
        var input = el(credInputId(key));
        if (!input) return;
        var box = input.closest(".cred-field");
        var tag = box ? box.querySelector("[data-src]") : null;
        var val = box ? box.querySelector("[data-val]") : null;
        var clr = box ? box.querySelector("[data-clear]") : null;
        if (st.source === "env") {
          input.disabled = true;
          input.value = "";
          input.placeholder = "由环境变量 " + (st.envVar || "") + " 提供";
          if (tag) { tag.textContent = "env"; tag.className = "src-tag env"; }
          if (val) val.textContent = st.envVar ? "由 " + st.envVar + " 覆盖，网页不可修改" : "环境变量覆盖";
          if (clr) clr.classList.add("hidden");
        } else if (st.source === "file") {
          input.disabled = false;
          if (st.sensitive) {
            input.value = "";
            input.placeholder = "已配置 " + (st.value || "****") + "，留空保持不变";
            if (val) val.textContent = "脱敏模式下不回显明文";
          } else {
            input.value = st.value || "";
            input.placeholder = "";
            if (val) val.textContent = "已配置";
          }
          if (tag) { tag.textContent = "已配置"; tag.className = "src-tag file"; }
          if (clr) clr.classList.remove("hidden");
        } else {
          input.disabled = false;
          input.value = "";
          input.placeholder = "";
          if (tag) { tag.textContent = "未配置"; tag.className = "src-tag none"; }
          if (val) val.textContent = "";
          if (clr) clr.classList.add("hidden");
        }
      });
    });
  }

  function saveCredentials() {
    var body = {};
    CRED_KEYS.forEach(function (key) {
      var parts = key.split(".");
      var input = el(credInputId(key));
      if (!input || input.disabled) return;
      var v = input.value.trim();
      if (!v) return;
      if (!body[parts[0]]) body[parts[0]] = {};
      body[parts[0]][parts[1]] = v;
    });
    var cleared = [];
    document.querySelectorAll("#pane-credentials .cred-clear.pending").forEach(function (b) {
      cleared.push(b.getAttribute("data-clear"));
    });
    if (cleared.length) body.clear = cleared;
    api("/api/credentials", { method: "POST", body: JSON.stringify(body) }).then(function (r) {
      if (!r.ok || !r.data || !r.data.ok) { toast((r.data && r.data.msg) || "保存凭证失败", "err"); return; }
      loadCredentialStatus();
      var sb = el("btn-cred-save");
      if (sb) { sb.textContent = "已保存"; setTimeout(function () { sb.textContent = "保存凭证"; }, 1600); }
    }).catch(function () { toast("保存凭证请求失败", "err"); });
  }

  function testCredentials() {
    var btn = el("btn-cred-test");
    var box = el("cred-test-result");
    btn.disabled = true; btn.textContent = "测试中...";
    box.classList.remove("hidden");
    box.innerHTML = '<div class="cred-test-item"><span class="t-msg">正在测试连接...</span></div>';
    api("/api/credentials/test", { method: "POST", timeout: 240000 }).then(function (r) {
      var d = r.data || {};
      var rows = "";
      [["dingtalk", "钉钉"], ["yida", "宜搭"], ["qingflow", "轻流"]].forEach(function (t) {
        var s = d[t[0]] || {};
        rows += '<div class="cred-test-item ' + (s.ok ? "ok" : "fail") + '">' +
          '<span class="t-name">' + t[1] + '</span>' +
          '<span class="t-msg">' + esc(s.msg || "未测试") + '</span></div>';
      });
      box.innerHTML = rows || '<div class="cred-test-item fail"><span class="t-msg">无响应</span></div>';
      btn.disabled = false; btn.textContent = "测试连接";
    }).catch(function () {
      box.innerHTML = '<div class="cred-test-item fail"><span class="t-name">请求</span><span class="t-msg">测试请求失败</span></div>';
      btn.disabled = false; btn.textContent = "测试连接";
    });
  }

  // ---- 拉取表单/应用列表（新建/编辑表单弹窗，供手动关联） ----
  function pullYidaForms(appIdx) {
    var sel = el("yida-form-picker");
    var btn = el("btn-pull-yida");
    if (!sel) return;
    if (btn) { btn.disabled = true; btn.textContent = "拉取中..."; }
    var url = "/api/yida/forms?pageSize=100";
    if (appIdx != null && String(appIdx) !== "") url += "&appIdx=" + encodeURIComponent(appIdx);
    api(url).then(function (r) {
      if (btn) { btn.disabled = false; btn.textContent = "↻ 拉取"; }
      if (!r.ok || !r.data || !r.data.ok) {
        sel.innerHTML = '<option value="">拉取失败，可手动输入</option>';
        toast((r.data && r.data.msg) || "拉取宜搭表单失败", "err");
        return;
      }
      var forms = r.data.forms || [];
      var opts = ['<option value="">宜搭表单（' + forms.length + ' 个）</option>'];
      forms.forEach(function (f) {
        opts.push('<option value="' + esc(f.formUuid) + '" data-title="' + esc(f.title) + '">' +
          esc(f.title) + " · " + esc(f.formUuid) + "</option>");
      });
      sel.innerHTML = opts.join("");
    }).catch(function () {
      sel.innerHTML = '<option value="">拉取失败，可手动输入</option>';
      if (btn) { btn.disabled = false; btn.textContent = "↻ 拉取"; }
    });
  }

  function pullQingFlowApps() {
    var sel = el("qf-app-picker");
    var btn = el("btn-pull-qf");
    if (!sel) return;
    if (btn) { btn.disabled = true; btn.textContent = "拉取中..."; }
    api("/api/qingflow/apps").then(function (r) {
      if (btn) { btn.disabled = false; btn.textContent = "↻ 拉取"; }
      if (!r.ok || !r.data || !r.data.ok) {
        sel.innerHTML = '<option value="">拉取失败，可手动输入</option>';
        toast((r.data && r.data.msg) || "拉取轻流应用失败", "err");
        return;
      }
      var tags = r.data.tags || [];
      var opts = ['<option value="">轻流应用</option>'];
      tags.forEach(function (t) {
        (t.apps || []).forEach(function (a) {
          var label = (t.tagName ? "[" + t.tagName + "] " : "") + (a.appName || "");
          opts.push('<option value="' + esc(a.appKey) + '">' + esc(label) + " · " + esc(a.appKey) + "</option>");
        });
      });
      sel.innerHTML = opts.join("");
    }).catch(function () {
      sel.innerHTML = '<option value="">拉取失败，可手动输入</option>';
      if (btn) { btn.disabled = false; btn.textContent = "↻ 拉取"; }
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
  // 宜搭应用管理弹窗（左侧栏「管理」入口）
  el("btn-manage-apps").onclick = openAppsModal;
  el("apps-close").onclick = closeAppsModal;
  el("apps-cancel").onclick = closeAppsModal;
  el("apps-save").onclick = saveApps;
  // 系统页：userId 变更即自动保存
  var sUserId = el("s-userId");
  if (sUserId) sUserId.onchange = function () {
    state.settings.userId = sUserId.value.trim();
    saveSettingsPayload({ userId: state.settings.userId });
  };
  document.querySelectorAll(".modal-tabs .tab").forEach(function (t) {
    t.onclick = function () { switchTab(t.dataset.tab); };
  });
  setupEye();
  el("a-submit").onclick = submitApp;
  el("a-cancel").onclick = resetAppForm;
  // 凭证页：测试连接 / 保存 / 清除标记
  var credTestBtn = el("btn-cred-test");
  if (credTestBtn) credTestBtn.onclick = testCredentials;
  var credSaveBtn = el("btn-cred-save");
  if (credSaveBtn) credSaveBtn.onclick = saveCredentials;
  document.querySelectorAll("#pane-credentials .cred-clear").forEach(function (btn) {
    btn.onclick = function () {
      var key = btn.getAttribute("data-clear");
      var input = el(credInputId(key));
      if (input) input.value = "";
      btn.textContent = "待清除";
      btn.classList.add("pending");
    };
  });
  // 表单弹窗：拉取列表 + 选中回填
  var pullYidaBtn = el("btn-pull-yida");
  if (pullYidaBtn) pullYidaBtn.onclick = function () { pullYidaForms(el("f-appid").value); };
  var pullQfBtn = el("btn-pull-qf");
  if (pullQfBtn) pullQfBtn.onclick = pullQingFlowApps;
  var qfPicker = el("qf-app-picker");
  if (qfPicker) qfPicker.onchange = function () {
    if (this.value) el("f-appkey").value = this.value;
  };
  var yfPicker = el("yida-form-picker");
  if (yfPicker) yfPicker.onchange = function () {
    var opt = this.options[this.selectedIndex];
    if (!this.value || !opt) return;
    el("f-formuuid").value = this.value;
    if (!el("f-name").value.trim() && opt.getAttribute("data-title")) {
      el("f-name").value = opt.getAttribute("data-title");
    }
  };
  var fAppId = el("f-appid");
  if (fAppId) fAppId.onchange = function () { pullYidaForms(this.value); };
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
  if (prepareBtn) prepareBtn.onclick = function () { runPrepare("all"); };
  var fetchBtn = el("btn-fetch");
  if (fetchBtn) fetchBtn.onclick = function () { runPrepare("fetch"); };
  var transformBtn = el("btn-transform");
  if (transformBtn) transformBtn.onclick = function () { runPrepare("transform"); };
  // 轻流拉取开关互斥：勾选「跳过轻流拉取」时禁用「强制全量重拉」
  var skipFetchChk = el("pf-skip-fetch"), forceFullChk = el("pf-force-full");
  if (skipFetchChk && forceFullChk) {
    skipFetchChk.addEventListener("change", function () {
      forceFullChk.disabled = skipFetchChk.checked;
      if (skipFetchChk.checked) forceFullChk.checked = false;
    });
    forceFullChk.disabled = skipFetchChk.checked;
  }
  var updateBtn = el("btn-update");
  if (updateBtn) updateBtn.onclick = updateData;
  // 「重新准备数据」与「写入数据」对齐：修改宜搭表单后，重新拉取/对齐并生成最新差异
  var reprepareBtn = el("btn-reprepare");
  if (reprepareBtn) reprepareBtn.onclick = function () { runPrepare("all"); };
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

  setupCards();

  // ---- 启动 ----
  loadSettings().then(loadForms);
})();
