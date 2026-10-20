/**
 * 微信助手控制台 — Hermes dashboard plugin (plain IIFE, no build step).
 *
 * Uses window.__HERMES_PLUGIN_SDK__ for React/hooks and the host's fetchJSON
 * (which handles base path, session token and ?profile=). All writes go to
 * /api/plugins/wechatpadpro-console/; the gateway picks them up within ~3s.
 */
(function () {
  "use strict";

  var SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK) return;

  var React = SDK.React;
  var h = React.createElement;
  var useState = SDK.hooks.useState;
  var useEffect = SDK.hooks.useEffect;

  var Button = (SDK.components && SDK.components.Button) || "button";
  var Badge = (SDK.components && SDK.components.Badge) || "span";

  var API = "/api/plugins/wechatpadpro-console";

  // ---------------------------------------------------------------- helpers

  function parseApiError(err) {
    var raw = err && err.message ? String(err.message) : String(err || "");
    var m = raw.match(/^(\d{3}):\s*([\s\S]*)$/);
    var status = m ? m[1] : "";
    var body = m ? m[2] : raw;
    var detail = body;
    try {
      var parsed = JSON.parse(body);
      if (parsed && typeof parsed.detail === "string") detail = parsed.detail;
      else if (parsed && parsed.detail && typeof parsed.detail.message === "string") {
        detail = parsed.detail.message;
      }
    } catch (_e) { /* not JSON */ }
    if (status === "404") {
      return "接口 404：当前 profile 未启用 wechatpadpro-console。" +
        "请把它加入该 profile 的 plugins.enabled（root profile 见 $HERMES_HOME/config.yaml，" +
        "次级 profile 见 $HERMES_HOME/profiles/<name>/config.yaml）后重启 hermes-dashboard。";
    }
    return (status ? "[" + status + "] " : "") + (detail || raw);
  }

  function getJSON(path) {
    return SDK.fetchJSON(API + path);
  }

  function putJSON(path, body) {
    return SDK.fetchJSON(API + path, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
  }

  function splitLines(value) {
    if (Array.isArray(value)) return value;
    if (!value) return [];
    return String(value).split(/[\n,]+/).map(function (x) { return x.trim(); })
      .filter(function (x) { return x.length > 0; });
  }

  function joinItems(list) {
    return Array.isArray(list) ? list.join("\n") : "";
  }

  function fmtTime(epochSec) {
    if (!epochSec) return "—";
    var d = new Date(epochSec * 1000);
    return d.toLocaleTimeString();
  }

  function expandQuietHours(text) {
    var parts = String(text || "").split(/[\s,]+/).filter(Boolean);
    var out = [];
    for (var i = 0; i < parts.length; i++) {
      var part = parts[i];
      var range = part.match(/^(\d{1,2})\s*-\s*(\d{1,2})$/);
      if (range) {
        var a = parseInt(range[1], 10);
        var b = parseInt(range[2], 10);
        if (a < 0 || a > 23 || b < 0 || b > 23) {
          throw new Error("静默时段只能填 0–23 的小时：" + part);
        }
        for (var hh = 0; hh < 24; hh++) {
          if (a <= b ? (hh >= a && hh <= b) : (hh >= a || hh <= b)) out.push(hh);
        }
      } else {
        var n = Number(part);
        if (!Number.isInteger(n) || n < 0 || n > 23) {
          throw new Error("静默时段只能填 0–23 的整数（或 23-7 这样的区间）：" + part);
        }
        out.push(n);
      }
    }
    out.sort(function (x, y) { return x - y; });
    return out.filter(function (v, idx) { return out.indexOf(v) === idx; });
  }

  function buildForm(account) {
    var jw = (account.joinWelcome && account.joinWelcome.raw) || {};
    var policy = {};
    ["dmPolicy", "groupPolicy"].forEach(function (k) {
      policy[k] = account[k] || "open";
    });
    ["allowFrom", "blacklistUsers", "groupAllowFrom", "blacklistGroups",
      "adminUsers", "employeeDenyList"].forEach(function (k) {
      policy[k] = joinItems(account[k]);
    });
    var features = {};
    Object.keys(account.features || {}).forEach(function (k) {
      features[k] = !!account.features[k];
    });
    return {
      policy: policy,
      welcome: {
        enabled: !!jw.enabled,
        mode: jw.mode || "allowlist",
        groups: joinItems(jw.groups),
        template: jw.template || "",
        llmPolish: !!jw.llmPolish,
        mention: !!jw.mention,
        quietHours: Array.isArray(jw.quietHours) ? jw.quietHours.join(",") : "",
        maxBatch: (typeof jw.maxBatch === "number") ? jw.maxBatch : 5,
      },
      features: features,
    };
  }

  // ---------------------------------------------------------------- pieces

  function StatusBadge(props) {
    var st = props.status || {};
    var state = st.state || "unknown";
    var label = { live: "在线", delayed: "延迟", stale: "已过期", unknown: "未知" }[state] || state;
    var age = (typeof st.age_sec === "number") ? " · " + st.age_sec + "s 前" : "";
    return h("span", { className: "wppc-badge wppc-badge--" + state, title: "快照状态" },
      label + age);
  }

  function Section(props) {
    return h("section", { className: "wppc-card" },
      h("div", { className: "wppc-card-head" }, h("h3", null, props.title),
        props.right || null),
      props.children);
  }

  function TextField(props) {
    return h("label", { className: "wppc-field" },
      h("span", { className: "wppc-label" }, props.label),
      h("textarea", {
        className: "wppc-input wppc-textarea",
        value: props.value,
        rows: props.rows || 3,
        spellCheck: false,
        placeholder: props.placeholder || "",
        onChange: function (e) { props.onChange(e.target.value); },
      }),
      props.hint ? h("span", { className: "wppc-hint" }, props.hint) : null);
  }

  function CheckField(props) {
    return h("label", { className: "wppc-check" },
      h("input", {
        type: "checkbox",
        checked: !!props.checked,
        disabled: !!props.disabled,
        onChange: function (e) { props.onChange(e.target.checked); },
      }),
      h("span", null, props.label),
      props.hint ? h("span", { className: "wppc-hint" }, props.hint) : null);
  }

  // ---------------------------------------------------------------- page

  function ConsolePage() {
    var [overview, setOverview] = useState(null);
    var [pageError, setPageError] = useState("");
    var [selected, setSelected] = useState("");
    var [account, setAccount] = useState(null);
    var [form, setForm] = useState(null);
    var [groups, setGroups] = useState(null);
    var [groupsHours, setGroupsHours] = useState(24);
    var [tab, setTab] = useState("policy");
    var [saving, setSaving] = useState("");
    var [notice, setNotice] = useState(null);
    var [pendingMtime, setPendingMtime] = useState(0);

    function refreshOverview(id) {
      var q = (id || selected) ? "?account=" + encodeURIComponent(id || selected) : "";
      return getJSON("/overview" + q).then(function (data) {
        setOverview(data);
        setPageError("");
        if (!selected && data.selected) setSelected(data.selected);
        var st = data.status || {};
        if (pendingMtime && st.account && st.account.config_mtime_ns >= pendingMtime) {
          setNotice({ type: "ok", text: "已热加载（" + fmtTime(st.written_at) + "）" });
          setPendingMtime(0);
        }
        return data;
      }).catch(function (err) {
        setPageError(parseApiError(err));
      });
    }

    function loadDetail(id) {
      if (!id) return;
      getJSON("/accounts/" + encodeURIComponent(id)).then(function (data) {
        setAccount(data.account);
        setForm(buildForm(data.account));
      }).catch(function (err) {
        setNotice({ type: "error", text: parseApiError(err) });
      });
    }

    function loadGroups(id, hours) {
      if (!id) return;
      setGroups(null);
      getJSON("/accounts/" + encodeURIComponent(id) + "/groups?since_hours=" + hours)
        .then(function (data) { setGroups(data); })
        .catch(function (err) {
          setGroups({ db_available: false, groups: [], error: parseApiError(err) });
        });
    }

    useEffect(function () {
      var alive = true;
      function tick() { if (alive) refreshOverview(); }
      tick();
      var timer = setInterval(tick, 5000);
      return function () { alive = false; clearInterval(timer); };
    }, []);

    useEffect(function () {
      if (selected) {
        loadDetail(selected);
        loadGroups(selected, groupsHours);
      }
    }, [selected]);

    useEffect(function () {
      if (selected) loadGroups(selected, groupsHours);
    }, [groupsHours]);

    function save(kind) {
      if (!selected || !form || saving) return;
      var path;
      var body;
      try {
        if (kind === "policy") {
          path = "/accounts/" + encodeURIComponent(selected) + "/policy";
          body = {
            dmPolicy: form.policy.dmPolicy,
            groupPolicy: form.policy.groupPolicy,
            allowFrom: splitLines(form.policy.allowFrom),
            blacklistUsers: splitLines(form.policy.blacklistUsers),
            groupAllowFrom: splitLines(form.policy.groupAllowFrom),
            blacklistGroups: splitLines(form.policy.blacklistGroups),
            adminUsers: splitLines(form.policy.adminUsers),
            employeeDenyList: splitLines(form.policy.employeeDenyList),
          };
        } else if (kind === "welcome") {
          path = "/accounts/" + encodeURIComponent(selected) + "/join-welcome";
          body = {
            enabled: !!form.welcome.enabled,
            mode: form.welcome.mode,
            groups: splitLines(form.welcome.groups),
            template: form.welcome.template,
            llmPolish: !!form.welcome.llmPolish,
            mention: !!form.welcome.mention,
            quietHours: expandQuietHours(form.welcome.quietHours),
            maxBatch: Number(form.welcome.maxBatch),
          };
        } else {
          path = "/accounts/" + encodeURIComponent(selected) + "/features";
          body = {};
          Object.keys(form.features).forEach(function (k) {
            body[k === "xiaowei" ? "xiaoweiEnabled" : k] = !!form.features[k];
          });
        }
      } catch (err) {
        setNotice({ type: "error", text: String(err.message || err) });
        return;
      }
      setSaving(kind);
      setNotice(null);
      putJSON(path, body).then(function (res) {
        setNotice({ type: "ok", text: "已保存，等待网关热加载（约 3 秒）…" });
        setPendingMtime(Number(res.mtime_ns) || Date.now());
        setTimeout(function () { loadDetail(selected); refreshOverview(selected); }, 4000);
      }).catch(function (err) {
        setNotice({ type: "error", text: parseApiError(err) });
      }).then(function () {
        setSaving("");
      });
    }

    function setPolicy(key, value) {
      setForm(function (f) { return Object.assign({}, f, { policy: Object.assign({}, f.policy, { [key]: value }) }); });
    }
    function setWelcome(key, value) {
      setForm(function (f) { return Object.assign({}, f, { welcome: Object.assign({}, f.welcome, { [key]: value }) }); });
    }
    function setFeature(key, value) {
      setForm(function (f) { return Object.assign({}, f, { features: Object.assign({}, f.features, { [key]: value }) }); });
    }

    if (!overview && !pageError) {
      return h("div", { className: "wppc-page" }, h("div", { className: "wppc-empty" }, "加载中…"));
    }
    if (pageError && !overview) {
      return h("div", { className: "wppc-page" }, h("div", { className: "wppc-error" }, pageError));
    }

    var accounts = (overview && overview.accounts) || [];
    var status = (overview && overview.status) || {};
    var stats = (overview && overview.stats) || {};
    var warnings = (overview && overview.warnings) || [];
    var acctStatus = status.account || {};
    var pending = pendingMtime > 0;

    return h("div", { className: "wppc-page" },
      h("header", { className: "wppc-top" },
        h("div", null,
          h("h2", { className: "wppc-title" }, "微信助手控制台",
            h("span", { className: "wppc-version" }, "v" + (overview.version || ""))),
          h("div", { className: "wppc-sub" },
            accounts.length ? null : "没有账号文件；请先配置 accounts/*.json")),
        h("div", { className: "wppc-top-right" },
          h("label", { className: "wppc-inline" }, "账号 ",
            h("select", {
              className: "wppc-input wppc-select",
              value: selected,
              onChange: function (e) { setSelected(e.target.value); setAccount(null); setForm(null); },
            }, accounts.map(function (a) {
              return h("option", { key: a.id, value: a.id },
                (a.nickname || a.id) + "（" + a.id + "）");
            }))),
          h(StatusBadge, { status: status }),
          h("span", { className: "wppc-inline" }, "WS ",
            h("span", { className: acctStatus.ws_connected ? "wppc-dot wppc-dot--ok" : "wppc-dot" }),
            acctStatus.ws_connected ? "已连" : "未连"),
          h(Button, {
            size: "sm",
            onClick: function () { refreshOverview(selected); if (selected) loadDetail(selected); },
          }, "刷新"))),

      notice ? h("div", { className: "wppc-notice wppc-notice--" + notice.type }, notice.text) : null,
      pending ? h("div", { className: "wppc-notice wppc-notice--wait" },
        "已写盘，等待网关热加载（网关每 3 秒检查账号文件）…") : null,
      pageError ? h("div", { className: "wppc-error" }, pageError) : null,

      h("div", { className: "wppc-stats" },
        h("div", { className: "wppc-stat" }, h("b", null, stats.totals ? stats.totals.inbound + stats.totals.outbound : "—"), h("span", null, "近 24h 收发（群）")),
        h("div", { className: "wppc-stat" }, h("b", null, stats.totals ? stats.totals.inbound : "—"), h("span", null, "入站")),
        h("div", { className: "wppc-stat" }, h("b", null, stats.totals ? stats.totals.outbound : "—"), h("span", null, "出站")),
        h("div", { className: "wppc-stat" }, h("b", null, stats.group_count || 0), h("span", null, "有记录的群")),
        h("div", { className: "wppc-stat" }, h("b", null, acctStatus.last_reload_at ? fmtTime(acctStatus.last_reload_at) : "—"), h("span", null, "最近热加载"))),

      warnings.length ? h("div", { className: "wppc-warn" },
        h("b", null, "注意"), h("ul", null, warnings.map(function (w, i) { return h("li", { key: i }, w); }))) : null,

      h("div", { className: "wppc-tabs" },
        [["policy", "准入策略"], ["welcome", "进群欢迎"], ["features", "能力开关"], ["groups", "群活跃"]].map(function (t) {
          return h("button", {
            key: t[0],
            className: "wppc-tab" + (tab === t[0] ? " wppc-tab--active" : ""),
            onClick: function () { setTab(t[0]); },
          }, t[1]);
        })),

      !form ? h("div", { className: "wppc-empty" }, account ? "加载表单…" : "请选择一个账号") : null,

      form && tab === "policy" && h(Section, { title: "准入策略（保存后约 3 秒热加载）" },
        h("div", { className: "wppc-danger" },
          account && account.allowAllUsers
            ? "⚠️ allowAllUsers=true：运行时绕过 dmPolicy，私聊仍全放行。"
            : "allowAllUsers=false（正常）。网关层 default-deny 仍需 WECHATPRO_ALLOW_ALL_USERS=1 才会放行。",
          account ? h("div", { className: "wppc-hint" }, "账号 enabled=" + String(account.enabled) +
            "；凭据与 enabled 的变更要重启网关（reload 不重连）。") : null),
        h("div", { className: "wppc-grid2" },
          h("label", { className: "wppc-field" }, h("span", { className: "wppc-label" }, "私聊策略"),
            h("select", { className: "wppc-input wppc-select", value: form.policy.dmPolicy,
              onChange: function (e) { setPolicy("dmPolicy", e.target.value); } },
              ["open", "allowlist", "blacklist", "pairing"].map(function (v) { return h("option", { key: v, value: v }, v); }))),
          h("label", { className: "wppc-field" }, h("span", { className: "wppc-label" }, "群策略"),
            h("select", { className: "wppc-input wppc-select", value: form.policy.groupPolicy,
              onChange: function (e) { setPolicy("groupPolicy", e.target.value); } },
              ["open", "allowlist", "blacklist", "disabled", "closed"].map(function (v) { return h("option", { key: v, value: v }, v); })))),
        h("div", { className: "wppc-grid2" },
          h(TextField, { label: "私聊白名单 allowFrom（每行一个 wxid）", value: form.policy.allowFrom, rows: 4, onChange: function (v) { setPolicy("allowFrom", v); } }),
          h(TextField, { label: "私聊黑名单 blacklistUsers", value: form.policy.blacklistUsers, rows: 4, onChange: function (v) { setPolicy("blacklistUsers", v); } }),
          h(TextField, { label: "群白名单 groupAllowFrom（每行一个 *@chatroom）", value: form.policy.groupAllowFrom, rows: 4, onChange: function (v) { setPolicy("groupAllowFrom", v); } }),
          h(TextField, { label: "群黑名单 blacklistGroups", value: form.policy.blacklistGroups, rows: 4, onChange: function (v) { setPolicy("blacklistGroups", v); } }),
          h(TextField, { label: "管理员 adminUsers", value: form.policy.adminUsers, rows: 3, onChange: function (v) { setPolicy("adminUsers", v); } }),
          h(TextField, { label: "永不自动加入 employeeDenyList", value: form.policy.employeeDenyList, rows: 3,
            hint: "从白名单手工删人后，若不写这里，下次全员群同步会把人加回来。", onChange: function (v) { setPolicy("employeeDenyList", v); } })),
        h(TextField, { label: "已自动同步的员工 employeeAllowFrom（只读）", value: joinItems(account.employeeAllowFrom), rows: 2,
          onChange: function () { } }),
        h("div", { className: "wppc-actions" },
          h(Button, { disabled: saving === "policy", onClick: function () { save("policy"); } },
            saving === "policy" ? "保存中…" : "保存策略"))),

      form && tab === "welcome" && h(Section, { title: "进群欢迎" },
        h("div", { className: "wppc-grid2" },
          h(CheckField, { label: "启用进群欢迎", checked: form.welcome.enabled, onChange: function (v) { setWelcome("enabled", v); } }),
          h(CheckField, { label: "LLM 润色 llmPolish", checked: form.welcome.llmPolish, onChange: function (v) { setWelcome("llmPolish", v); } }),
          h(CheckField, { label: "@ 新成员 mention", checked: form.welcome.mention, onChange: function (v) { setWelcome("mention", v); } }),
          h("label", { className: "wppc-field" }, h("span", { className: "wppc-label" }, "模式"),
            h("select", { className: "wppc-input wppc-select", value: form.welcome.mode,
              onChange: function (e) { setWelcome("mode", e.target.value); } },
              ["allowlist", "blocklist", "all"].map(function (v) { return h("option", { key: v, value: v }, v); })))),
        h(TextField, { label: "生效群 groups（每行一个 *@chatroom）", value: form.welcome.groups, rows: 3,
          onChange: function (v) { setWelcome("groups", v); } }),
        h(TextField, { label: "欢迎语模板", value: form.welcome.template, rows: 5,
          hint: "可写 {name}；运行时若缺失会自动补在末尾（只影响生效值，不改写本文件）。",
          onChange: function (v) { setWelcome("template", v); } }),
        h("div", { className: "wppc-grid2" },
          h("label", { className: "wppc-field" }, h("span", { className: "wppc-label" }, "静默时段（小时）"),
            h("input", { className: "wppc-input", value: form.welcome.quietHours,
              placeholder: "例如 23-7 或 0,1,2",
              onChange: function (e) { setWelcome("quietHours", e.target.value); } }),
            h("span", { className: "wppc-hint" }, "提交前会展开成 0–23 的整数数组（跨午夜区间如 23-7 可用）。")),
          h("label", { className: "wppc-field" }, h("span", { className: "wppc-label" }, "单批 @ 上限 maxBatch"),
            h("input", { type: "number", min: 1, max: 20, className: "wppc-input",
              value: form.welcome.maxBatch,
              onChange: function (e) { setWelcome("maxBatch", e.target.value); } }))),
        h("div", { className: "wppc-hint" }, "生效值：" +
          JSON.stringify(account.joinWelcome.effective)),
        h("div", { className: "wppc-actions" },
          h(Button, { disabled: saving === "welcome", onClick: function () { save("welcome"); } },
            saving === "welcome" ? "保存中…" : "保存欢迎语"))),

      form && tab === "features" && h(Section, { title: "能力开关" },
        h("div", { className: "wppc-features" },
          ["heartflow", "affection", "jargon", "style", "tts", "proactive"].map(function (k) {
            var labels = { heartflow: "心流", affection: "好感度", jargon: "黑话学习", style: "风格模仿", tts: "语音 TTS", proactive: "主动发言" };
            var disabled = k === "heartflow" && account && account.heartflowModelConfigured === false;
            return h("div", { key: k, className: "wppc-feature" },
              h(CheckField, {
                label: labels[k] + "（" + k + "）",
                checked: !!form.features[k],
                disabled: disabled,
                hint: disabled ? "未配置 heartflow.model，先在账号文件里配置后再开启。" : "",
                onChange: function (v) { setFeature(k, v); },
              }));
          }),
          h("div", { className: "wppc-feature" },
            h(CheckField, { label: "小薇 xiaoweiEnabled", checked: !!form.features.xiaowei,
              onChange: function (v) { setFeature("xiaowei", v); } }))),
        h("div", { className: "wppc-actions" },
          h(Button, { disabled: saving === "features", onClick: function () { save("features"); } },
            saving === "features" ? "保存中…" : "保存能力开关"))),

      tab === "groups" && h(Section, { title: "群活跃（只读，来自插件数据库）",
        right: h("label", { className: "wppc-inline" }, "时间窗 ",
          h("select", { className: "wppc-input wppc-select", value: groupsHours,
            onChange: function (e) { setGroupsHours(parseInt(e.target.value, 10)); } },
            [[6, "6 小时"], [24, "24 小时"], [72, "3 天"], [168, "7 天"]].map(function (o) {
              return h("option", { key: o[0], value: o[0] }, o[1]);
            }))) },
        !groups ? h("div", { className: "wppc-empty" }, "加载中…") : null,
        groups && groups.db_available === false
          ? h("div", { className: "wppc-warn" }, "数据库不可用：群统计已降级（策略与开关不受影响）。请检查 dashboard 进程能否读到 WECHATPRO_DB_* 配置。")
          : null,
        groups && groups.db_available ? h("div", { className: "wppc-table-wrap" },
          h("table", { className: "wppc-table" },
            h("thead", null, h("tr", null,
              ["群", "群 id", "成员（厂商/本地）", "入站", "出站", "最后同步"].map(function (c, i) {
                return h("th", { key: i }, c);
              }))),
            h("tbody", null, (groups.groups || []).map(function (g) {
              return h("tr", { key: g.chatroom_id },
                h("td", { title: g.name }, g.name || "（未同步群名）"),
                h("td", { className: "wppc-mono" }, g.chatroom_id),
                h("td", null, (g.member_count == null ? "—" : g.member_count) + " / " + g.local_members),
                h("td", null, g.inbound),
                h("td", null, g.outbound),
                h("td", null, g.last_synced_at ? String(g.last_synced_at).replace("T", " ").slice(0, 19) : "—"));
            }))),
          groups.truncated ? h("div", { className: "wppc-hint" }, "结果已截断（最多 200 个群）。") : null)
          : null));
  }

  if (window.__HERMES_PLUGINS__ && typeof window.__HERMES_PLUGINS__.register === "function") {
    window.__HERMES_PLUGINS__.register("wechatpadpro-console", ConsolePage);
  }
})();
