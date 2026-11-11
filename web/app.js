(function () {
  "use strict";

  var STATUS = {
    scheduled_draft: { label: "已存草稿", tone: "pass" },
    scheduled_pending_confirmation: { label: "待确认", tone: "warn" },
    needs_info: { label: "信息不足", tone: "warn" },
    blocked: { label: "规则阻断", tone: "block" },
    save_failed: { label: "保存失败", tone: "block" },
    confirmation_rejected: { label: "改期被否决", tone: "block" },
    material_not_found: { label: "素材不存在", tone: "block" },
    unresolved: { label: "未完成", tone: "muted" },
    not_processed: { label: "未处理", tone: "muted" }
  };

  var STATUS_LABEL = {
    completed: "推理正常收尾",
    step_limit_reached: "达到步数上限",
    model_error: "模型调用失败",
    tool_error: "工具异常中断",
    aborted: "运行中断"
  };

  var OUTCOME = {
    scheduled: "已排期 · 仅草稿",
    pending_confirmation: "待用户确认",
    partial: "部分完成",
    failed: "存在失败项",
    incomplete: "步数上限中断"
  };

  var CONFIRM_STATUS = {
    pending: "待拍板",
    approved: "已同意",
    rejected: "已否决"
  };
  var DRAFT_STATUS = {
    draft_ready: "已存草稿",
    draft_pending_confirmation: "待确认草稿",
    draft_rejected: "已否决 · 草稿作废"
  };

  var REVIEW = {
    pass: "审核通过",
    pass_with_warnings: "通过 · 有警告",
    risk: "规则风险",
    missing_info: "信息缺失",
    missing: "信息缺失"
  };

  var REVIEW_TONE = {
    pass: "pass",
    pass_with_warnings: "warn",
    risk: "block",
    missing_info: "block",
    missing: "block"
  };

  var selected = "02-conflict";
  var state = null;
  var lastReport = null;
  var activeDate = null;
  var activeChannel = null;
  var running = false;
  var stream = null;
  var finished = false;
  var startedAt = 0;

  function $(id) { return document.getElementById(id); }

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) { node.className = cls; }
    if (text !== undefined && text !== null) { node.textContent = String(text); }
    return node;
  }

  function clear(node) { while (node.firstChild) { node.removeChild(node.firstChild); } }

  function text(value, fallback) {
    if (value === null || value === undefined || value === "") { return fallback === undefined ? "—" : fallback; }
    return String(value);
  }

  function when(day, time) {
    if (!day) { return text(time); }
    return text(day) + " " + text(time);
  }

  function chip(label, tone) {
    return el("span", "chip tone-" + (tone || "muted"), label);
  }

  /* 演示口令（可选）：服务端用 --token 开启后，所有 /api/* 都要带 token。
     EventSource 不能自定义请求头，所以统一走 ?token= 查询参数，口令存在 sessionStorage 里。 */
  var TOKEN_KEY = "scheduling.desk.token";

  function token() {
    try { return window.sessionStorage.getItem(TOKEN_KEY) || ""; } catch (e) { return ""; }
  }

  function saveToken(value) {
    try { window.sessionStorage.setItem(TOKEN_KEY, value); } catch (e) { /* 隐私模式下写不了，只影响本次会话 */ }
  }

  function withToken(url) {
    var value = token();
    if (!value) { return url; }
    return url + (url.indexOf("?") >= 0 ? "&" : "?") + "token=" + encodeURIComponent(value);
  }

  function askForToken() {
    $("token-row").hidden = false;
    $("token").focus();
  }

  function fetchJSON(url, options) {
    return fetch(withToken(url), options).then(function (resp) {
      return resp.json().then(function (body) {
        if (resp.status === 401 || body.auth_required) { askForToken(); }
        if (!resp.ok || body.status === "error") {
          throw new Error(body.error || ("HTTP " + resp.status));
        }
        return body;
      });
    });
  }

  /* ---------- static chrome ---------- */
  var DRAFT_STATE = {
    draft_ready: "已存草稿",
    draft_pending_confirmation: "待确认草稿",
    draft_rejected: "已否决 · 作废"
  };

  function ledgerRows(root, items, render) {
    var list = el("ul", "ledger-list");
    items.forEach(function (item) {
      var li = el("li", "ledger-row");
      render(li, item);
      list.appendChild(li);
    });
    root.appendChild(list);
  }

  function renderLedger(data) {
    var body = $("ledger-body");
    if (!body) { return; }
    clear(body);
    var drafts = [];
    var confirmations = [];
    var key;
    for (key in (data.drafts || {})) {
      if (Object.prototype.hasOwnProperty.call(data.drafts, key)) { drafts.push(data.drafts[key]); }
    }
    for (key in (data.confirmations || {})) {
      if (Object.prototype.hasOwnProperty.call(data.confirmations, key)) { confirmations.push(data.confirmations[key]); }
    }
    if (!drafts.length && !confirmations.length) {
      body.appendChild(el("p", "fine", "账本还是空的：跑一个场景就会写进草稿，需要拍板时再多一条确认项。"));
      return;
    }
    if (drafts.length) {
      body.appendChild(el("p", "ledger-head", "草稿 " + drafts.length));
      ledgerRows(body, drafts, function (li, item) {
        li.appendChild(el("code", null, text(item.draft_id)));
        li.appendChild(el("span", null, text(item.material_id) + " · " + when(item.date || item.scheduled_date, item.scheduled_time)));
        li.appendChild(el("em", null, (DRAFT_STATE[item.status] || text(item.status)) + (item.submit_count > 1 ? " · 提交 " + item.submit_count + " 次" : "")));
      });
    }
    if (confirmations.length) {
      body.appendChild(el("p", "ledger-head", "确认项 " + confirmations.length));
      ledgerRows(body, confirmations, function (li, item) {
        li.appendChild(el("code", null, text(item.confirmation_id)));
        li.appendChild(el("span", null, text(item.material_id) + " · " + when(item.proposed_date || item.date, item.proposed_time)));
        var decided = item.approved_by || item.rejected_by || item.decided_by;
        li.appendChild(el("em", null, (CONFIRM_STATUS[item.status] || text(item.status)) + (decided ? " · " + text(decided) : "")));
      });
    }
  }
  function renderMeta(data) {
    var dates = (data.dates && data.dates.length) ? data.dates : [data.date];
    $("meta-date").textContent = dates.length > 1
      ? text(dates[0]) + " 起 " + dates.length + " 天"
      : text(data.date);
    $("meta-channel").textContent = data.channel === "main" ? "主频道" : text(data.channel);
    $("meta-ledger").textContent = text(data.ledger);
    renderLedger(data);
  }

  function renderEnv(offline) {
    var node = $("env-chip");
    node.dataset.mode = offline ? "offline" : "deepseek";
    node.textContent = offline ? "离线 Mock" : "真实模型";
  }

  function renderCases(list) {
    var root = $("cases");
    clear(root);
    list.forEach(function (item, index) {
      var li = el("li");
      var btn = el("button", "case");
      btn.type = "button";
      btn.dataset.case = item.file;
      btn.setAttribute("aria-current", item.file === selected ? "true" : "false");
      btn.appendChild(el("span", "case-index", String(index + 1).padStart(2, "0")));
      btn.appendChild(el("span", "case-title", item.title));
      btn.appendChild(el("span", "case-tag", item.tag));
      btn.appendChild(el("span", "case-note", item.note));
      btn.addEventListener("click", function () { select(item.file); });
      btn.addEventListener("keydown", onCaseKey);
      li.appendChild(btn);
      root.appendChild(li);
    });
    syncCaseTabindex();
  }

  /* 列表是单选组：只有当前项进 Tab 序列，箭头键在项之间移动。 */
  function syncCaseTabindex() {
    Array.prototype.forEach.call(document.querySelectorAll(".case"), function (node) {
      node.tabIndex = node.dataset.case === selected ? 0 : -1;
    });
  }

  function onCaseKey(event) {
    var keys = { ArrowDown: 1, ArrowRight: 1, ArrowUp: -1, ArrowLeft: -1 };
    var nodes = Array.prototype.slice.call(document.querySelectorAll(".case"));
    var here = nodes.indexOf(event.currentTarget);
    var next = null;
    if (event.key in keys) {
      next = nodes[(here + keys[event.key] + nodes.length) % nodes.length];
    } else if (event.key === "Home") {
      next = nodes[0];
    } else if (event.key === "End") {
      next = nodes[nodes.length - 1];
    }
    if (!next) { return; }
    event.preventDefault();
    select(next.dataset.case);
    next.focus();
  }

  function select(name) {
    selected = name;
    Array.prototype.forEach.call(document.querySelectorAll(".case"), function (node) {
      node.setAttribute("aria-current", node.dataset.case === name ? "true" : "false");
    });
    syncCaseTabindex();
  }

  /* ---------- timeline ---------- */
  function toMinutes(hhmm) {
    var parts = String(hhmm || "00:00").split(":");
    return parseInt(parts[0], 10) * 60 + parseInt(parts[1], 10);
  }

  function pad(n) { return (n < 10 ? "0" : "") + n; }

  function buildSlots(data) {
    var hours = data.hours || {};
    var start = toMinutes(hours.start || "09:00");
    var end = toMinutes(hours.end || "21:00");
    var step = hours.granularity_min || 30;
    var slots = [];
    for (var m = start; m < end; m += step) {
      slots.push({ minutes: m, time: pad(Math.floor(m / 60)) + ":" + pad(m % 60), hour: m % 60 === 0 });
    }
    return slots;
  }

  function channelList(data) {
    return (data.channels && data.channels.length) ? data.channels : [{ name: data.channel || "main", label: data.channel || "main", dates: data.dates || [] }];
  }

  function resolveChannel(data, report) {
    var list = channelList(data);
    var names = list.map(function (c) { return c.name; });
    if (names.indexOf(activeChannel) >= 0) { return activeChannel; }
    var wanted = null;
    (report && report.materials || []).forEach(function (m) {
      if (!wanted && m.channel && m.channel !== (data.channel || "main") && names.indexOf(m.channel) >= 0) { wanted = m.channel; }
    });
    activeChannel = wanted || data.channel || names[0];
    return activeChannel;
  }

  function channelLabel(data, channel) {
    var list = channelList(data);
    for (var i = 0; i < list.length; i += 1) {
      if (list[i].name === channel) { return list[i].label || list[i].name; }
    }
    return channel;
  }

  function channelDates(data, channel) {
    var list = channelList(data);
    for (var i = 0; i < list.length; i += 1) {
      if (list[i].name === channel && list[i].dates && list[i].dates.length) { return list[i].dates; }
    }
    return (data.dates && data.dates.length) ? data.dates : [data.date];
  }

  function renderChannelTabs(data) {
    var box = $("channel-tabs");
    if (!box) { return; }
    clear(box);
    var list = channelList(data);
    if (list.length < 2) { box.hidden = true; return; }
    box.hidden = false;
    list.forEach(function (item) {
      var btn = el("button", "channel-tab", item.label || item.name);
      btn.type = "button";
      btn.dataset.channel = item.name;
      btn.setAttribute("aria-pressed", item.name === activeChannel ? "true" : "false");
      btn.addEventListener("click", function () {
        activeChannel = item.name;
        activeDate = null;
        if (state) { renderTimeline(state, lastReport); }
      });
      box.appendChild(btn);
    });
  }

  function resolveDay(data, report, dates) {
    dates = (dates && dates.length) ? dates : ((data.dates && data.dates.length) ? data.dates : [data.date]);
    var target = activeDate;
    if (dates.indexOf(target) < 0) {
      var moved = null;
      (report && report.materials || []).forEach(function (m) {
        var mday = m.scheduled_date || m.requested_date;
        if (!moved && mday && mday !== data.date) { moved = mday; }
      });
      // 用户要求的日期优先：先看这一场排期到底在跟哪一天较劲，改期结果写在素材行里
      var wanted = null;
      (report && report.materials || []).forEach(function (m) {
        if (!wanted && m.requested_date && m.requested_date !== data.date) { wanted = m.requested_date; }
      });
      target = wanted || moved || data.date || dates[0];
    }
    if (dates.indexOf(target) < 0) { target = data.date || dates[0]; }
    activeDate = target;
    return { dates: dates, day: target };
  }

  function renderDayTabs(dates) {
    var box = $("day-tabs");
    if (!box) { return; }
    clear(box);
    if (dates.length < 2) { box.hidden = true; return; }
    box.hidden = false;
    dates.forEach(function (day) {
      var btn = el("button", "day-tab", day);
      btn.type = "button";
      btn.setAttribute("aria-pressed", day === activeDate ? "true" : "false");
      btn.addEventListener("click", function () {
        activeDate = day;
        if (state) { renderTimeline(state, lastReport); }
      });
      box.appendChild(btn);
    });
  }

  function renderTimeline(data, report) {
    var root = $("timeline");
    var scale = $("timeline-scale");
    var ledger = $("slot-ledger");
    clear(root);
    clear(scale);
    clear(ledger);
    var hours = data.hours || {};
    var channel = resolveChannel(data, report);
    renderChannelTabs(data);
    var picked = resolveDay(data, report, channelDates(data, channel));
    var day = picked.day;
    renderDayTabs(picked.dates);
    var label = channelLabel(data, channel);
    $("timeline-hint").textContent = label + " · " + day + " · " + text(hours.start) + "–" + text(hours.end) + " · " + (hours.granularity_min || 30) + " 分钟一格";

    var slots = buildSlots(data);
    var cells = {};
    slots.forEach(function (slot) { cells[slot.time] = { slot: slot, booked: null, suggested: null, drafted: null }; });

    var byChannel = (data.channel_days && data.channel_days[channel]) || {};
    var daySlots = byChannel[day] || (day === data.date && channel === (data.channel || "main") ? data.slots : []) || [];
    daySlots.forEach(function (item) {
      if (cells[item.time]) { cells[item.time].booked = item.material_id; }
    });
    var drafts = data.drafts || {};
    Object.keys(drafts).forEach(function (key) {
      var rec = drafts[key] || {};
      if ((rec.channel || (data.channel || "main")) !== channel) { return; }
      if (rec.date === day && rec.scheduled_time && cells[rec.scheduled_time]) {
        cells[rec.scheduled_time].drafted = rec.draft_id || rec.material_id;
      }
    });
    var materials = (report && report.materials) || [];
    materials.forEach(function (m) {
      var mday = m.scheduled_date || m.requested_date;
      var mchannel = m.channel || (data.channel || "main");
      if (m.suggested_time && mchannel === channel && mday === day && cells[m.suggested_time]) { cells[m.suggested_time].suggested = m.material_id; }
    });

    slots.forEach(function (slot) {
      var cell = cells[slot.time];
      var node = el("div", "slot");
      if (slot.hour) { node.dataset.hour = "true"; } else { node.dataset.half = "true"; }
      var labels = [slot.time];
      if (cell.booked) { node.classList.add("is-occupied"); labels.push("已占用 " + cell.booked); }
      if (cell.drafted) { node.classList.add("is-drafted"); labels.push("草稿 " + cell.drafted); }
      if (cell.suggested) { node.classList.add("is-suggested"); labels.push("建议 " + cell.suggested); }
      if (!cell.booked && !cell.drafted && !cell.suggested) { labels.push("空闲"); }
      node.title = labels.join(" · ");
      root.appendChild(node);
    });

    slots.forEach(function (slot) {
      if (!slot.hour) { return; }
      scale.appendChild(el("span", null, slot.time));
    });

    var rows = slots.filter(function (slot) {
      var cell = cells[slot.time];
      return cell.booked || cell.drafted || cell.suggested;
    });
    if (!rows.length) {
      ledger.appendChild(el("li", "hint", "当前账本没有占用，也没有建议时段"));
      return;
    }
    rows.forEach(function (slot) {
      var cell = cells[slot.time];
      var li = el("li");
      li.appendChild(el("span", "t", slot.time));
      var tags = [];
      if (cell.booked) { tags.push("已有排期"); }
      if (cell.drafted) { tags.push("草稿占位"); }
      if (cell.suggested) { tags.push("建议"); }
      li.appendChild(el("span", "hint", tags.join(" · ")));
      var ids = [];
      if (cell.booked) { ids.push(cell.booked); }
      if (cell.drafted) { ids.push(cell.drafted); }
      if (cell.suggested) { ids.push(cell.suggested); }
      li.appendChild(el("span", "id", ids.join(" → ")));
      ledger.appendChild(li);
    });
  }

  /* ---------- report ---------- */
  function counter(label, value, tone) {
    var li = el("li");
    li.appendChild(el("span", "k", label));
    var v = el("span", "v" + (tone ? " " + tone : ""), value);
    li.appendChild(v);
    return li;
  }

  function renderVerdict(report) {
    var outcome = report.outcome || "failed";
    $("outcome").textContent = OUTCOME[outcome] || outcome;
    var terminal = STATUS_LABEL[report.status] || ("未正常收尾：" + text(report.status));
    $("verdict-sub").textContent = "运行结论 · " + terminal;

    var counters = $("counters");
    clear(counters);
    var limits = report.limits || {};
    counters.appendChild(counter("步数", text(limits.steps_used) + " / " + text(limits.max_steps)));
    counters.appendChild(counter("工具调用", text(limits.tool_calls)));
    var failed = report.unfinished_materials && report.unfinished_materials.length;
    counters.appendChild(counter("已发布", report.published ? "true" : "false", report.published ? "is-fail" : "is-ok"));
    counters.appendChild(counter("未完成素材", failed ? String(failed) : "0", failed ? "is-fail" : "is-ok"));
  }

  function kv(term, value) {
    var dl = document.createElement("dl");
    dl.className = "kv";
    dl.appendChild(el("dt", null, term));
    var dd = el("dd");
    if (value instanceof Node) { dd.appendChild(value); } else { dd.textContent = value; }
    dl.appendChild(dd);
    return dl;
  }

  function idText(value) {
    return el("span", "mono", text(value));
  }

  function renderMaterial(m) {
    var li = el("li", "material");
    var status = STATUS[m.result_status] || { label: text(m.result_status), tone: "muted" };

    var left = el("div", "m-body");
    var head = el("div", "m-head");
    head.appendChild(el("span", "m-id", text(m.material_id)));
    if (m.channel && m.channel !== (state && state.channel || "main")) {
      head.appendChild(el("span", "m-channel", text(m.channel_label || m.channel)));
    }
    head.appendChild(chip(status.label, status.tone));
    left.appendChild(head);
    left.appendChild(el("h3", "m-title", text(m.title)));

    var facts = el("div", "m-facts");
    facts.appendChild(el("span", null, "要求 " + when(m.requested_date, m.requested_time) + (m.time_hard ? " · 硬性" : " · 可调整")));
    if (m.suggested_time) {
      facts.appendChild(el("span", null, "建议 " + when(m.scheduled_date || m.requested_date, m.suggested_time)));
    } else {
      facts.appendChild(el("span", null, "建议 · 暂无可用时段"));
    }
    if (m.time_changed) { facts.appendChild(el("span", null, "已改时段")); }
    if (m.date_changed) { facts.appendChild(el("span", null, "已改日期")); }
    if (m.time_hard && m.hard_time_preserved === false) { facts.appendChild(el("span", "flag", "硬性要求未满足")); }
    left.appendChild(facts);

    var right = el("div", "m-decision");

    var reviewTone = REVIEW_TONE[m.review_status] || "muted";
    var reviewLine = el("span");
    reviewLine.appendChild(chip(REVIEW[m.review_status] || text(m.review_status), reviewTone));
    if (m.requires_manual_review) { reviewLine.appendChild(el("span", "note-inline", " 需人工复核")); }
    right.appendChild(kv("审核", reviewLine));

    if (m.risk_reasons && m.risk_reasons.length) {
      var risks = el("ul", "risk-list");
      m.risk_reasons.forEach(function (item) {
        if (item && typeof item === "object") {
          var li = el("li");
          if (item.rule_id) { li.appendChild(el("span", "mono", item.rule_id + " ")); }
          li.appendChild(el("span", null, text(item.reason)));
          if (item.severity) { li.classList.add("sev-" + item.severity); }
          risks.appendChild(li);
        } else {
          risks.appendChild(el("li", null, text(item)));
        }
      });
      right.appendChild(kv("风险", risks));
    }
    if (m.missing_fields && m.missing_fields.length) {
      right.appendChild(kv("缺失字段", el("span", "mono", m.missing_fields.join("、"))));
    }

    if (m.pending_confirmations && m.pending_confirmations.length) {
      var list = el("ul", "confirm-list");
      m.pending_confirmations.forEach(function (item) {
        var conf = el("li");
        conf.appendChild(el("span", "mono", text(item.confirmation_id) + " · " + text(CONFIRM_STATUS[item.status] || item.status)));
        conf.appendChild(el("div", null, text(item.question)));
        if (item.replanned_from) {
          conf.appendChild(el("div", "confirm-note", "改选自 " + text(item.replanned_from)));
        }
        if (item.decided_by) {
          conf.appendChild(el("div", "confirm-note", "由 " + text(item.decided_by) + " 于 " + text(item.decided_at || "—") + " 决定"));
        }
        if (item.status === "pending") {
          var actions = el("div", "confirm-actions");
          var approve = el("button", "confirm-btn", "记为用户已确认");
          approve.type = "button";
          approve.addEventListener("click", function () { decide(item.confirmation_id, approve, "approve"); });
          var reject = el("button", "confirm-btn reject", "不同意这个改期");
          reject.type = "button";
          reject.addEventListener("click", function () { decide(item.confirmation_id, reject, "reject"); });
          actions.appendChild(approve);
          actions.appendChild(reject);
          var options = (m.slot_options || []).filter(function (o) { return o.time !== item.proposed_time || o.date !== item.proposed_date; });
          if (options.length) {
            var pick = el("select", "confirm-pick");
            pick.setAttribute("aria-label", "改到工具给过的其它时段");
            options.forEach(function (o) {
              var opt = el("option", null, o.date + " " + o.time);
              opt.value = o.date + "|" + o.time;
              pick.appendChild(opt);
            });
            var replan = el("button", "confirm-btn replan", "改到所选时段");
            replan.type = "button";
            replan.addEventListener("click", function () {
              var parts = (pick.value || "").split("|");
              decide(item.confirmation_id, replan, "replan", { date: parts[0], time: parts[1] });
            });
            actions.appendChild(pick);
            actions.appendChild(replan);
          }
          conf.appendChild(actions);
        }
        list.appendChild(conf);
      });
      right.appendChild(kv("确认项", list));
    }

    if (m.draft) {
      var draftLine = el("span");
      draftLine.appendChild(idText(m.draft.draft_id));
      draftLine.appendChild(el("span", "note-inline", " · " + text(DRAFT_STATUS[m.draft.status] || m.draft.status) + " · 提交 " + text(m.draft.submit_count) + " 次 · 发布 " + text(m.draft.published)));
      right.appendChild(kv("草稿", draftLine));
    } else {
      right.appendChild(kv("草稿", el("span", "note-inline", m.failed_save_attempts ? "未保存（失败 " + m.failed_save_attempts + " 次）" : "未落库")));
    }

    if (m.note) { right.appendChild(kv("说明", el("span", "note-inline", m.note))); }

    li.appendChild(left);
    li.appendChild(right);
    return li;
  }
  function renderReport(report, trace) {
    $("empty").hidden = true;
    var results = $("results");
    results.hidden = false;
    results.classList.remove("flash");
    void results.offsetWidth;
    results.classList.add("flash");

    renderVerdict(report);

    var list = $("materials");
    clear(list);
    (report.materials || []).forEach(function (m) { list.appendChild(renderMaterial(m)); });
    if (!(report.materials || []).length) { list.appendChild(el("li", "note-inline", "本次请求没有素材结论")); }

    lastReport = report;
    renderTimeline(state, report);

    var summaryFallback = report.status === "step_limit_reached"
      ? "（步数上限生效，模型未收尾）"
      : "（模型没有留下总结）";
    $("agent-summary").textContent = text(report.agent_summary, summaryFallback);
    var model = (report.model || {}).name || "—";
    $("footer-note").textContent = "运行 " + text(report.run_id) + " · 模型 " + model + " · " + text(report.note);
    $("report-md").hidden = false;

    renderTrace(trace, report);
  }

  /* ---------- trace ---------- */
  function renderTrace(trace, report) {
    var root = $("trace");
    clear(root);
    $("trace-limits").classList.remove("live");
    if (!trace) {
      $("trace-limits").textContent = "未运行";
      root.appendChild(el("li", "trace-empty", "还没有决策记录"));
      return;
    }
    var limits = (report && report.limits) || trace.limits || {};
    $("trace-limits").textContent = "步数 " + text(limits.steps_used) + "/" + text(limits.max_steps) + " · 工具 " + text(limits.tool_calls);
    (trace.steps || []).forEach(function (step) { root.appendChild(stepItem(step)); });
  }

  /* 耗时按浏览器时钟算：它只说明"这一轮跑了多久"，结论仍只信 report。 */
  function renderDuration() {
    if (!startedAt) { $("meta-duration").textContent = "—"; return; }
    var elapsed = Date.now() - startedAt;
    $("meta-duration").textContent = elapsed < 1000
      ? Math.max(1, Math.round(elapsed)) + " 毫秒"
      : (elapsed / 1000).toFixed(1) + " 秒";
  }

  function stepItem(step) {
    var li = el("li", "step");
    var head = el("div", "step-head");
    head.appendChild(el("span", "step-no", String(step.step)));
    head.appendChild(el("span", "step-tool", text(step.action && step.action.tool)));
    var ok = step.observation && step.observation.ok;
    head.appendChild(el("span", "step-ok tone-" + (ok ? "pass" : "block"), ok ? "OK" : "FAIL"));
    li.appendChild(head);
    if (step.rationale) { li.appendChild(el("p", "step-why", step.rationale)); }
    li.appendChild(el("p", "step-obs", text(step.observation && step.observation.summary)));
    if (step.next_action) { li.appendChild(el("p", "step-next", step.next_action)); }
    var args = el("details", "args");
    args.appendChild(el("summary", null, "工具参数"));
    args.appendChild(el("pre", null, JSON.stringify((step.action && step.action.args) || {}, null, 2)));
    li.appendChild(args);
    return li;
  }

  /* SSE 中途推来的快照：逐步追加，让演示能看到“先查了什么、再改了什么”。 */
  function renderLiveTrace(steps, terminal) {
    var root = $("trace");
    clear(root);
    $("trace-limits").classList.add("live");
    $("trace-limits").textContent = "执行中 · 已产生 " + steps.length + " 条决策记录";
    steps.forEach(function (step) { root.appendChild(stepItem(step)); });
    if (terminal && terminal !== "running") {
      root.appendChild(el("li", "trace-empty", "已收尾：" + text(terminal)));
    }
  }

  /* ---------- actions ---------- */
  function setRunning(value) {
    running = value;
    var btn = $("run");
    btn.disabled = value;
    btn.firstChild.textContent = value ? "执行中…" : "运行这个场景";
  }

  function showError(message) {
    $("empty").hidden = true;
    var results = $("results");
    results.hidden = true;
    var box = document.getElementById("error-box");
    if (!box) {
      box = el("div", "error");
      box.id = "error-box";
      results.parentNode.insertBefore(box, results);
    }
    box.textContent = "运行失败：" + message;
    box.hidden = false;
  }

  function hideError() {
    var box = document.getElementById("error-box");
    if (box) { box.hidden = true; }
  }

  function closeStream() {
    if (stream) { stream.close(); stream = null; }
  }

  function run() {
    if (running) { return; }
    hideError();
    closeStream();
    finished = false;
    activeDate = null;
    activeChannel = null;
    setRunning(true);
    var offline = $("offline").checked;
    renderEnv(offline);
    var params = new URLSearchParams({
      case: selected,
      offline: offline ? "1" : "0",
      reset: $("reset-ledger").checked ? "1" : "0"
    });
    var maxSteps = $("max-steps").value;
    if (maxSteps) { params.set("max_steps", maxSteps); }

    startedAt = Date.now();
    $("meta-duration").textContent = "计中…";
    $("empty").hidden = true;
    $("results").hidden = false;
    $("results").classList.add("stale");  // 上一轮的结论先压暗，别让人误以为是新的
    clear($("trace"));
    $("trace-limits").classList.add("live");
    $("trace-limits").textContent = "等待模型第一步…";

    var source = new EventSource(withToken("/api/stream?" + params.toString()));
    stream = source;
    source.addEventListener("trace", function (event) {
      var payload = JSON.parse(event.data);
      renderLiveTrace(payload.steps || [], payload.terminal);
    });
    source.addEventListener("report", function (event) {
      var payload = JSON.parse(event.data);
      finished = true;
      state = payload.state;
      renderMeta(state);
      renderCustom(state);
      renderReport(payload.report, payload.trace);
      renderDuration();
      $("results").classList.remove("stale");
      closeStream();
      setRunning(false);
    });
    source.addEventListener("failed", function (event) {
      var payload = JSON.parse(event.data);
      finished = true;
      closeStream();
      setRunning(false);
      showError(payload.error || "运行失败");
    });
    source.onerror = function () {
      if (finished) { return; }
      finished = true;
      closeStream();
      setRunning(false);
      showError("与服务器的连接中断");
    };
  }

  /* 人工决策走 /api/confirm 与 /api/reject：模型工具集里没有这两个动作，只有人点这一下才算拍板。 */
  function decide(confirmationId, btn, action, extra) {
    if (running) { return; }
    var idle = btn.textContent;
    btn.disabled = true;
    btn.textContent = "记录中…";
    var payload = { confirmation_id: confirmationId };
    if (extra && extra.time) { payload.time = extra.time; payload.date = extra.date; }
    fetchJSON(action === "approve" ? "/api/confirm" : "/api/reject", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    }).then(function (payload) {
      state = payload.state || state;
      renderMeta(state);
      $("reset-ledger").checked = false;  // 别把刚写下的决策清掉
      return run();
    }).catch(function (err) {
      btn.disabled = false;
      btn.textContent = idle;
      showError(err.message || String(err));
    });
  }

  /* ---------- 自定义请求 ---------- */
  function fillSelect(node, items, keep) {
    var current = keep && node.value;
    clear(node);
    items.forEach(function (item) {
      var opt = el("option", null, item.label);
      opt.value = item.value;
      node.appendChild(opt);
    });
    if (current) {
      var hit = items.filter(function (item) { return item.value === current; })[0];
      if (hit) { node.value = current; }
    }
  }

  function renderCustom(data) {
    var materials = (data.materials || []).map(function (item) {
      return { value: item.material_id, label: item.material_id + " · " + text(item.title) };
    });
    fillSelect($("custom-material"), materials, true);
    fillSelect($("custom-date"), (data.dates || []).map(function (day) { return { value: day, label: day }; }), true);
    fillSelect($("custom-channel"), channelList(data).map(function (item) {
      return { value: item.name, label: item.label };
    }), true);
  }

  function runCustom(btn) {
    if (running) { return; }
    var body = {
      requests: [{
        material_id: $("custom-material").value,
        requested_date: $("custom-date").value,
        requested_time: $("custom-time").value.trim(),
        requested_channel: $("custom-channel").value,
        time_hard: $("custom-hard").checked
      }],
      max_steps: $("max-steps").value || undefined
    };
    btn.disabled = true;
    fetchJSON("/api/custom", { method: "POST", body: JSON.stringify(body) }).then(function (payload) {
      btn.disabled = false;
      selected = payload.case;
      select(selected);  // 自定义请求不在场景列表里，选中态全部清掉
      $("custom-hint").textContent = "本轮请求：" + body.requests[0].material_id + " · "
        + body.requests[0].requested_date + " " + body.requests[0].requested_time + "（" + payload.case + "）";
      run();
    }).catch(function (err) {
      btn.disabled = false;
      showError(err.message || String(err));
    });
  }

  var bound = false;

  function bindChrome() {
    if (bound) { return; }
    bound = true;
    $("run").addEventListener("click", run);
    $("custom-run").addEventListener("click", function () { runCustom(this); });
    $("offline").addEventListener("change", function () { renderEnv(this.checked); });
    $("token-save").addEventListener("click", function () {
      saveToken($("token").value.trim());
      $("token-row").hidden = true;
      hideError();
      boot();
    });
    $("token").addEventListener("keydown", function (event) {
      if (event.key === "Enter") { event.preventDefault(); $("token-save").click(); }
    });
    document.addEventListener("keydown", function (event) {
      if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
        event.preventDefault();
        run();
      }
    });
  }

  function boot() {
    bindChrome();
    fetchJSON("/api/state").then(function (payload) {
      state = payload.state;
      renderMeta(state);
      renderCases(state.cases || []);
      renderCustom(state);
      renderTimeline(state, null);
      select(selected);
      return run();
    }).catch(function (err) {
      showError(err.message || String(err));
    });
  }

  document.addEventListener("DOMContentLoaded", boot);
})();
