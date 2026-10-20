/**
 * 微信助手控制台 — Hermes dashboard plugin (plain IIFE, no build step).
 *
 * Uses window.__HERMES_PLUGIN_SDK__ for React/hooks and the host's fetchJSON
 * (which handles base path, session token and ?profile=). All writes go to
 * /api/plugins/wechatpadpro-console/; the gateway picks them up within ~3s.
 *
 * Boundaries (enforced by the backend, mirrored here):
 *   - no credential is ever returned or submitted;
 *   - the dashboard never calls vendor APIs and never writes the database;
 *   - message text is displayed to the authenticated dashboard admin only.
 */
(function () {
  "use strict";

  var SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK) return;

  var React = SDK.React;
  var h = React.createElement;
  var useState = SDK.hooks.useState;
  var useEffect = SDK.hooks.useEffect;
  var useRef = SDK.hooks.useRef;
  var useMemo = SDK.hooks.useMemo;

  var API = "/api/plugins/wechatpadpro-console";

  var FEATURE_KEYS = ["heartflow", "affection", "jargon", "style", "tts", "proactive"];
  var FEATURE_LABELS = {
    heartflow: "心流", affection: "好感度", jargon: "黑话学习",
    style: "风格模仿", tts: "语音 TTS", proactive: "主动发言",
  };
  var ADVANCED_KEYS = [
    "llmIntentEnabled", "groupContextEnabled", "mcpEnabled",
    "friendCirclePublishEnabled", "adminToolsEnabled", "dmPairingEnabled",
  ];
  var ADVANCED_LABELS = {
    llmIntentEnabled: "LLM 意图识别",
    groupContextEnabled: "群上下文注入",
    mcpEnabled: "MCP 工具",
    friendCirclePublishEnabled: "朋友圈发布",
    adminToolsEnabled: "管理类工具（高风险）",
    dmPairingEnabled: "私聊配对码",
  };
  var TABS = [
    ["overview", "状态运维"],
    ["policy", "准入策略"],
    ["welcome", "进群欢迎"],
    ["features", "能力开关"],
    ["groups", "群与成员"],
    ["messages", "消息分析"],
    ["heartflow", "心流运营"],
  ];
  var SECTIONS = ["policy", "welcome", "features", "heartflow"];
  var SECTION_LABELS = {
    policy: "准入策略", welcome: "进群欢迎", features: "能力开关", heartflow: "心流",
  };

  // ---------------------------------------------------------------- helpers

  function withQuery(path, params) {
    var parts = [];
    for (var key in params) {
      if (!Object.prototype.hasOwnProperty.call(params, key)) continue;
      var value = params[key];
      if (value === undefined || value === null || value === "") continue;
      parts.push(encodeURIComponent(key) + "=" + encodeURIComponent(String(value)));
    }
    return parts.length ? path + "?" + parts.join("&") : path;
  }

  function request(method, path, body, signal) {
    var opts = { method: method, headers: {} };
    if (body !== undefined && body !== null) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    if (signal) opts.signal = signal;
    return SDK.fetchJSON(API + path, opts);
  }

  function getJSON(path, signal) { return request("GET", path, null, signal); }

  function parseApiError(err) {
    var status = 0;
    var body = null;
    if (err && typeof err.status === "number") {
      status = err.status;
      body = err.body;
    }
    var raw = err && err.message ? String(err.message) : String(err || "");
    var match = raw.match(/^(\d{3}):\s*([\s\S]*)$/);
    if (!status && match) {
      status = parseInt(match[1], 10);
      body = match[2];
    }
    if (body === null || body === undefined || body === "") body = raw;
    if (typeof body === "string") {
      try { body = JSON.parse(body); } catch (_e) { body = null; }
    }
    var message = "";
    var code = "";
    var currentMtimeNs = 0;
    if (body && typeof body === "object") {
      var detail = body.detail;
      if (typeof detail === "string") message = detail;
      else if (Array.isArray(detail)) {
        message = detail.map(function (d) {
          return (d && (d.msg || d.message)) || JSON.stringify(d);
        }).join("；");
      } else if (detail && typeof detail === "object") {
        message = String(detail.message || "");
        code = String(detail.code || "");
        if (typeof detail.current_mtime_ns === "number") currentMtimeNs = detail.current_mtime_ns;
      } else if (typeof body.message === "string") {
        message = body.message;
        code = String(body.code || "");
      }
    }
    if (!message) message = raw || "请求失败";
    if (status === 404 && !code) {
      message = "接口 404：当前 profile 未启用 wechatpadpro-console。" +
        "请把它加入该 profile 的 plugins.enabled 后重启 hermes-dashboard。";
    }
    return { status: status, code: code, message: message, currentMtimeNs: currentMtimeNs };
  }

  function isAbort(err) {
    return !!err && (err.name === "AbortError" || /abort/i.test(String(err.message || "")));
  }

  function toList(value) {
    if (Array.isArray(value)) {
      return value.map(function (x) { return String(x).trim(); })
        .filter(function (x) { return x.length > 0; });
    }
    if (typeof value === "string") {
      return value.split(/[\n,]+/).map(function (x) { return x.trim(); })
        .filter(function (x) { return x.length > 0; });
    }
    return [];
  }

  function uniq(list) {
    var out = [];
    list.forEach(function (item) {
      if (item && out.indexOf(item) < 0) out.push(item);
    });
    return out;
  }

  function has(obj, key) {
    return !!obj && Object.prototype.hasOwnProperty.call(obj, key);
  }

  function pick(raw, effective, key, fallback) {
    if (has(raw, key) && raw[key] !== undefined && raw[key] !== null) return raw[key];
    if (has(effective, key) && effective[key] !== undefined && effective[key] !== null) {
      return effective[key];
    }
    return fallback;
  }

  function canonical(value) {
    if (Array.isArray(value)) {
      return "[" + value.map(canonical).join(",") + "]";
    }
    if (value && typeof value === "object") {
      return "{" + Object.keys(value).sort().map(function (k) {
        return JSON.stringify(k) + ":" + canonical(value[k]);
      }).join(",") + "}";
    }
    return JSON.stringify(value);
  }

  function deepEqual(a, b) { return canonical(a) === canonical(b); }

  function clone(value) {
    if (value === undefined) return undefined;
    return JSON.parse(JSON.stringify(value));
  }

  function fmtTime(epochSec) {
    if (!epochSec) return "—";
    var d = new Date(Number(epochSec) * 1000);
    if (isNaN(d.getTime())) return "—";
    return d.toLocaleString();
  }

  function fmtAge(seconds) {
    if (seconds === null || seconds === undefined) return "—";
    var s = Number(seconds);
    if (s < 60) return s + " 秒前";
    if (s < 3600) return Math.floor(s / 60) + " 分钟前";
    return Math.floor(s / 3600) + " 小时前";
  }

  function fmtNum(value) {
    if (value === null || value === undefined) return "—";
    return Number(value).toLocaleString();
  }

  function fmtPct(value) {
    var n = Number(value);
    if (!isFinite(n)) return "—";
    return (n * 100).toFixed(1) + "%";
  }

  function expandQuietHours(text) {
    var parts = String(text || "").split(/[\s,]+/).filter(Boolean);
    var out = [];
    parts.forEach(function (part) {
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
    });
    out.sort(function (x, y) { return x - y; });
    return uniq(out);
  }

  function validateGroupId(value) {
    return typeof value === "string" && /@chatroom$/.test(value);
  }

  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text);
    }
    try {
      var area = document.createElement("textarea");
      area.value = text;
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.appendChild(area);
      area.select();
      document.execCommand("copy");
      document.body.removeChild(area);
      return Promise.resolve();
    } catch (err) {
      return Promise.reject(err);
    }
  }

  function statusLabel(state) {
    return ({ live: "在线", delayed: "延迟", stale: "已过期", unknown: "未知" })[state] || state || "未知";
  }

  function buildForm(account) {
    var raw = (account.joinWelcome && account.joinWelcome.raw) || {};
    var eff = (account.joinWelcome && account.joinWelcome.effective) || {};
    var hf = account.heartflow || {};
    var features = {};
    FEATURE_KEYS.forEach(function (k) {
      features[k] = !!(account.features && account.features[k]);
    });
    features.xiaowei = !!(account.features && account.features.xiaowei);
    ADVANCED_KEYS.forEach(function (k) {
      features[k] = !!(account.advanced && account.advanced[k]);
    });
    features.debounceMs = (typeof account.debounceMs === "number") ? account.debounceMs : 500;
    var quiet = pick(raw, eff, "quietHours", []);
    return {
      policy: {
        dmPolicy: account.dmPolicy || "open",
        groupPolicy: account.groupPolicy || "open",
        allowFrom: toList(account.allowFrom),
        blacklistUsers: toList(account.blacklistUsers),
        groupAllowFrom: toList(account.groupAllowFrom),
        blacklistGroups: toList(account.blacklistGroups),
        adminUsers: toList(account.adminUsers),
        employeeDenyList: toList(account.employeeDenyList),
      },
      welcome: {
        enabled: !!pick(raw, eff, "enabled", false),
        mode: pick(raw, eff, "mode", "allowlist"),
        groups: toList(pick(raw, eff, "groups", [])),
        template: String(pick(raw, eff, "template", "") || ""),
        llmPolish: !!pick(raw, eff, "llmPolish", true),
        mention: !!pick(raw, eff, "mention", true),
        quietHoursText: toList(quiet).join(","),
        maxBatch: Number(pick(raw, eff, "maxBatch", 3)) || 3,
      },
      features: features,
      heartflow: {
        thresholdText: hf.thresholdExplicit ? String(hf.replyThreshold) : "",
        whitelistGroups: toList(hf.whitelistGroups),
        profileEnabled: hf.profileEnabled !== false,
      },
    };
  }

  function sectionDirty(form, baseline, section) {
    if (!form || !baseline) return false;
    return !deepEqual(form[section], baseline[section]);
  }

  // ---------------------------------------------------------------- hooks

  function useLatest(value) {
    var ref = useRef(value);
    ref.current = value;
    return ref;
  }

  function usePolling(fn, intervalMs, enabled) {
    var ref = useRef(fn);
    ref.current = fn;
    useEffect(function () {
      if (!enabled) return undefined;
      var alive = true;
      function tick() {
        if (alive && !document.hidden) {
          try { ref.current(); } catch (err) { /* 组件内已自行处理 */ }
        }
      }
      tick();
      var timer = setInterval(tick, intervalMs);
      function onVisibility() { if (!document.hidden) tick(); }
      document.addEventListener("visibilitychange", onVisibility);
      return function () {
        alive = false;
        clearInterval(timer);
        document.removeEventListener("visibilitychange", onVisibility);
      };
    }, [enabled, intervalMs]);
  }

  // ---------------------------------------------------------------- primitives

  function Section(props) {
    return h("section", { className: "wppc-card" + (props.className ? " " + props.className : "") },
      h("div", { className: "wppc-card-head" },
        h("h3", null, props.title), props.right || null),
      props.children);
  }

  function TextField(props) {
    return h("label", { className: "wppc-field" },
      h("span", { className: "wppc-label" }, props.label),
      h("textarea", {
        className: "wppc-input wppc-textarea",
        value: props.value === undefined ? "" : props.value,
        rows: props.rows || 3,
        spellCheck: false,
        placeholder: props.placeholder || "",
        disabled: !!props.disabled,
        onChange: function (e) { props.onChange(e.target.value); },
      }),
      props.hint ? h("span", { className: "wppc-hint" }, props.hint) : null);
  }

  function InputField(props) {
    return h("label", { className: "wppc-field" },
      h("span", { className: "wppc-label" }, props.label),
      h("input", {
        className: "wppc-input",
        type: props.type || "text",
        value: props.value === undefined ? "" : props.value,
        placeholder: props.placeholder || "",
        min: props.min,
        max: props.max,
        step: props.step,
        disabled: !!props.disabled,
        onChange: function (e) { props.onChange(e.target.value); },
        onKeyDown: props.onKeyDown,
      }),
      props.hint ? h("span", { className: "wppc-hint" }, props.hint) : null);
  }

  function SelectField(props) {
    return h("label", { className: "wppc-field" },
      h("span", { className: "wppc-label" }, props.label),
      h("select", {
        className: "wppc-input wppc-select",
        value: props.value,
        disabled: !!props.disabled,
        onChange: function (e) { props.onChange(e.target.value); },
      }, props.options.map(function (opt) {
        var value = Array.isArray(opt) ? opt[0] : opt;
        var label = Array.isArray(opt) ? opt[1] : opt;
        return h("option", { key: String(value), value: value }, label);
      })),
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

  function ChipListEditor(props) {
    var [text, setText] = useState("");
    var [error, setError] = useState("");
    var kind = props.kind || "user";

    function add() {
      var items;
      try {
        items = toList(text);
        if (!items.length) return;
        if (kind === "group") {
          items.forEach(function (item) {
            if (!validateGroupId(item)) throw new Error("群 id 必须以 @chatroom 结尾：" + item);
          });
        } else {
          items.forEach(function (item) {
            if (item.indexOf("@chatroom") >= 0) throw new Error("私聊名单里不能出现群 id：" + item);
          });
        }
        var next = uniq((props.value || []).concat(items));
        if (next.length > 500) throw new Error("名单最多 500 项");
        props.onChange(next);
        setText("");
        setError("");
      } catch (err) {
        setError(String(err.message || err));
      }
    }

    function remove(item) {
      props.onChange((props.value || []).filter(function (x) { return x !== item; }));
    }

    if (props.readOnly) {
      return h("div", { className: "wppc-field" },
        h("span", { className: "wppc-label" }, props.label),
        h("div", { className: "wppc-chips" },
          (props.value || []).length
            ? props.value.map(function (item) {
              return h("span", { key: item, className: "wppc-chip wppc-chip--readonly wppc-chip--" + kind }, item);
            })
            : h("span", { className: "wppc-hint" }, "（空）")),
        props.hint ? h("span", { className: "wppc-hint" }, props.hint) : null);
    }

    return h("div", { className: "wppc-field" },
      h("span", { className: "wppc-label" }, props.label),
      h("div", { className: "wppc-chips" },
        (props.value || []).map(function (item) {
          return h("span", { key: item, className: "wppc-chip wppc-chip--" + kind },
            h("span", { className: "wppc-mono" }, item),
            h("button", {
              type: "button", className: "wppc-chip-remove", "aria-label": "删除 " + item,
              onClick: function () { remove(item); },
            }, "×"));
        })),
      h("div", { className: "wppc-chip-add" },
        h("input", {
          className: "wppc-input",
          value: text,
          placeholder: props.placeholder || (kind === "group" ? "wxid@chatroom" : "wxid"),
          onChange: function (e) { setText(e.target.value); },
          onKeyDown: function (e) {
            if (e.key === "Enter" || e.key === ",") { e.preventDefault(); add(); }
          },
        }),
        h("button", { type: "button", className: "wppc-btn", onClick: add }, "添加")),
      error ? h("span", { className: "wppc-chip-error" }, error) : null,
      props.hint ? h("span", { className: "wppc-hint" }, props.hint) : null);
  }

  function CopyButton(props) {
    var [done, setDone] = useState(false);
    return h("button", {
      type: "button",
      className: "wppc-btn wppc-btn--ghost",
      onClick: function (e) {
        e.stopPropagation();
        copyText(String(props.text || "")).then(function () {
          setDone(true);
          setTimeout(function () { setDone(false); }, 1500);
        });
      },
    }, done ? "已复制" : (props.label || "复制"));
  }

  function StatusBadge(props) {
    var state = props.state || "unknown";
    return h("span", { className: "wppc-badge wppc-badge--" + state }, statusLabel(state));
  }

  function Modal(props) {
    useEffect(function () {
      function onKey(e) { if (e.key === "Escape" && props.onClose) props.onClose(); }
      document.addEventListener("keydown", onKey);
      return function () { document.removeEventListener("keydown", onKey); };
    }, [props.onClose]);
    return h("div", { className: "wppc-modal-backdrop", onClick: function (e) {
      if (e.target === e.currentTarget && props.onClose) props.onClose();
    } },
      h("div", { className: "wppc-modal", role: "dialog", "aria-modal": "true",
        "aria-label": props.title || "对话框", style: props.width ? { maxWidth: props.width } : null },
        h("div", { className: "wppc-modal-head" },
          h("h3", null, props.title || ""),
          props.onClose ? h("button", { type: "button", className: "wppc-btn wppc-btn--ghost",
            "aria-label": "关闭", onClick: props.onClose }, "×") : null),
        h("div", { className: "wppc-modal-body" }, props.children),
        props.footer ? h("div", { className: "wppc-modal-foot" }, props.footer) : null));
  }

  function EmptyState(props) {
    return h("div", { className: "wppc-empty" }, props.children || "暂无数据");
  }

  function ErrorNotice(props) {
    if (!props.message) return null;
    return h("div", { className: "wppc-error" }, props.message);
  }

  function Pagination(props) {
    var total = Number(props.total || 0);
    var offset = Number(props.offset || 0);
    var limit = Number(props.limit || 50);
    var from = total ? offset + 1 : 0;
    var to = Math.min(offset + limit, total);
    return h("div", { className: "wppc-pagination" },
      h("span", { className: "wppc-page-info" }, from + "–" + to + " / " + total),
      h("button", { type: "button", className: "wppc-btn", disabled: offset <= 0,
        onClick: function () { props.onChange(Math.max(0, offset - limit)); } }, "上一页"),
      h("button", { type: "button", className: "wppc-btn",
        disabled: offset + limit >= total,
        onClick: function () { props.onChange(offset + limit); } }, "下一页"));
  }

  function TrendBars(props) {
    var buckets = props.buckets || [];
    if (!buckets.length) return h(EmptyState, null, "窗口内没有消息");
    var max = 1;
    buckets.forEach(function (b) {
      max = Math.max(max, Number(b.inbound || 0) + Number(b.outbound || 0));
    });
    return h("div", null,
      h("div", { className: "wppc-chart-legend" },
        h("span", { className: "wppc-legend-in" }, "入站"),
        h("span", { className: "wppc-legend-out" }, "出站")),
      h("div", { className: "wppc-bars" }, buckets.map(function (b) {
        var inH = Math.round((Number(b.inbound || 0) / max) * 100);
        var outH = Math.round((Number(b.outbound || 0) / max) * 100);
        var title = fmtTime(b.start_sec) + " · 入站 " + b.inbound + " / 出站 " + b.outbound;
        return h("div", { key: b.start_sec, className: "wppc-bar-col", title: title },
          h("div", { className: "wppc-bar-stack" },
            h("div", { className: "wppc-bar wppc-bar--in", style: { height: inH + "%" } }),
            h("div", { className: "wppc-bar wppc-bar--out", style: { height: outH + "%" } })),
          props.showLabels && buckets.length <= 32
            ? h("span", { className: "wppc-bar-label" },
              new Date(b.start_sec * 1000).getHours() + "h") : null);
      })));
  }

  function TypeBars(props) {
    var types = props.types || [];
    if (!types.length) return h(EmptyState, null, "没有类型数据");
    var total = 0;
    types.forEach(function (t) { total += Number(t.count || 0); });
    var max = 1;
    types.forEach(function (t) { max = Math.max(max, Number(t.count || 0)); });
    return h("div", { className: "wppc-typebars" }, types.slice(0, 12).map(function (t) {
      var pct = total ? (Number(t.count || 0) / total) * 100 : 0;
      return h("div", { key: t.msg_type + ":" + t.direction, className: "wppc-typebar-row" },
        h("span", { className: "wppc-typebar-label" }, t.msg_type + " · " + t.direction),
        h("div", { className: "wppc-typebar-track" },
          h("div", { className: "wppc-typebar-fill",
            style: { width: Math.round((Number(t.count || 0) / max) * 100) + "%" } })),
        h("span", { className: "wppc-typebar-value" }, t.count + "（" + pct.toFixed(1) + "%）"));
    }));
  }

  function ErrorBoundary(props) {
    React.Component.call(this, props);
    this.state = { error: null };
  }
  ErrorBoundary.prototype = Object.create(React.Component.prototype);
  ErrorBoundary.prototype.constructor = ErrorBoundary;
  ErrorBoundary.getDerivedStateFromError = function (error) { return { error: error }; };
  ErrorBoundary.prototype.componentDidCatch = function (error) {
    if (window.console && console.error) console.error("[wppc] render error:", error && error.message);
  };
  ErrorBoundary.prototype.render = function () {
    if (!this.state.error) return this.props.children;
    var message = String(this.state.error && this.state.error.message || this.state.error);
    return h("div", { className: "wppc-page" },
      h("div", { className: "wppc-error" },
        h("b", null, "控制台渲染出错。"), h("div", { className: "wppc-mono" }, message),
        h("div", { className: "wppc-actions" },
          h("button", { type: "button", className: "wppc-btn",
            onClick: function () { window.location.reload(); } }, "重新加载页面"),
          h("button", { type: "button", className: "wppc-btn",
            onClick: function () { copyText(message); } }, "复制错误"))));
  };

  // ---------------------------------------------------------------- overview tab

  function OverviewTab(props) {
    var accountId = props.accountId;
    var active = props.active;
    var [diag, setDiag] = useState(null);
    var [error, setError] = useState("");
    var seq = useRef(0);

    function load() {
      if (!accountId) return;
      var my = ++seq.current;
      getJSON("/accounts/" + encodeURIComponent(accountId) + "/diagnostics")
        .then(function (data) {
          if (my !== seq.current) return;
          setDiag(data);
          setError("");
        })
        .catch(function (err) {
          if (my !== seq.current || isAbort(err)) return;
          setError(parseApiError(err).message);
        });
    }
    usePolling(load, 15000, active && !!accountId);

    var overview = props.overview || {};
    var statuses = overview.account_statuses || {};
    var accounts = overview.accounts || [];
    var snapshot = (diag && diag.snapshot) || overview.status || {};

    function reloadAccount(id) {
      request("POST", "/accounts/" + encodeURIComponent(id) + "/reload")
        .then(function (res) {
          props.notify(res.warning ? "warn" : "ok",
            res.warning || "已触发热加载（mtime 已更新）");
          props.onReloaded(id, res.mtime_ns, Date.now());
        })
        .catch(function (err) { props.notify("error", parseApiError(err).message); });
    }

    function reloadAll() {
      props.askConfirm({
        title: "重载全部账号？",
        message: "将抬高每个账号文件的 mtime，触发网关 watcher。不会修改配置内容。",
        confirmLabel: "全部重载",
        onConfirm: function () {
          request("POST", "/ops/reload-all").then(function (res) {
            var failed = (res.results || []).filter(function (r) { return !r.ok; });
            props.notify(failed.length ? "warn" : "ok",
              failed.length ? failed.length + " 个账号重载失败" : "已触发热加载");
            (res.results || []).forEach(function (r) {
              if (r.ok) props.onReloaded(r.account_id, r.mtime_ns, Date.now());
            });
          }).catch(function (err) { props.notify("error", parseApiError(err).message); });
        },
      });
    }

    return h("div", { className: "wppc-stack" },
      h(Section, { title: "全账号状态", right:
        h("div", { className: "wppc-toolbar-group" },
          h("button", { type: "button", className: "wppc-btn", onClick: load }, "刷新体检"),
          h("button", { type: "button", className: "wppc-btn wppc-btn--warn", onClick: reloadAll }, "全部重载")) },
        !accounts.length ? h(EmptyState, null, "没有账号文件") :
          h("div", { className: "wppc-table-wrap" },
            h("table", { className: "wppc-table wppc-table--clickable" },
              h("thead", null, h("tr", null,
                ["账号", "快照", "WS", "客户端", "配置 mtime", "最近热加载", "错误", ""].map(function (c, i) {
                  return h("th", { key: i }, c);
                }))),
              h("tbody", null, accounts.map(function (a) {
                var st = statuses[a.id] || {};
                return h("tr", { key: a.id,
                  className: a.id === props.selected ? "wppc-row--selected" : "",
                  onClick: function () { props.onSelect(a.id); } },
                  h("td", null, h("b", null, a.nickname || a.id), h("div", { className: "wppc-mono" }, a.id)),
                  h("td", null, h(StatusBadge, { state: st.state }),
                    h("span", { className: "wppc-hint" }, " " + fmtAge(st.age_sec))),
                  h("td", null, h("span", { className: "wppc-dot" + (st.ws_connected ? " wppc-dot--ok" : "") }),
                    " " + (st.ws_connected ? "已连" : "未连")),
                  h("td", null, st.client_ready ? "就绪" : "—"),
                  h("td", { className: "wppc-mono" }, st.config_mtime_ns ? String(st.config_mtime_ns) : "—"),
                  h("td", null, st.last_reload_at ? fmtTime(st.last_reload_at) : "—",
                    st.last_reload_ok === false ? h("span", { className: "wppc-badge wppc-badge--stale" }, "失败") : null),
                  h("td", { className: "wppc-wrap" }, st.last_reload_error || "—"),
                  h("td", null, h("button", { type: "button", className: "wppc-btn",
                    onClick: function (e) { e.stopPropagation(); reloadAccount(a.id); } }, "重载")));
              }))))),

      h("div", { className: "wppc-grid2" },
        h(Section, { title: "网关快照" },
          h("dl", { className: "wppc-kv" },
            h("dt", null, "状态"), h("dd", null, h(StatusBadge, { state: snapshot.state }),
              " ", fmtAge(snapshot.age_sec)),
            h("dt", null, "写入时刻"), h("dd", null, fmtTime(snapshot.written_at)),
            h("dt", null, "profile"), h("dd", { className: "wppc-mono" }, snapshot.profile || "—"),
            h("dt", null, "PID"), h("dd", { className: "wppc-mono" }, snapshot.pid || "—"),
            h("dt", null, "原因"), h("dd", null, snapshot.reason || "—"))),
        h(Section, { title: "数据库" },
          diag ? h("dl", { className: "wppc-kv" },
            h("dt", null, "后端"), h("dd", { className: "wppc-mono" }, diag.db.backend),
            h("dt", null, "可用"), h("dd", null, diag.db.available ? "是" : "否"),
            h("dt", null, "进程内体检"), h("dd", null, diag.db.process_health_state,
              diag.db.process_health_at ? " · " + fmtTime(diag.db.process_health_at) : ""),
            h("dt", null, "说明"), h("dd", { className: "wppc-hint" }, diag.db.note)) :
            h("div", { className: "wppc-empty" }, "加载中…"))),

      h(Section, { title: "体检项" },
        error ? h(ErrorNotice, { message: error }) : null,
        !diag ? h("div", { className: "wppc-empty" }, "加载中…") :
          h("div", { className: "wppc-health-grid" }, (diag.checks || []).map(function (c) {
            return h("div", { key: c.id, className: "wppc-check-row wppc-check--" + c.level },
              h("span", { className: "wppc-check-dot" }),
              h("div", null, h("b", null, c.message),
                h("div", { className: "wppc-hint" }, c.id)));
          }))),

      diag ? h(Section, { title: "环境存在性（只显示布尔，不显示值）" },
        h("div", { className: "wppc-env-grid" },
          Object.keys(diag.env.presence || {}).map(function (k) {
            return h("div", { key: k, className: "wppc-env-item" },
              h("span", { className: "wppc-dot" + (diag.env.presence[k] ? " wppc-dot--ok" : "") }),
              h("span", { className: "wppc-mono" }, k),
              h("span", { className: "wppc-hint" }, diag.env.presence[k] ? "已配置" : "未发现"));
          }))) : null,

      diag ? h(Section, { title: "凭据存在性" },
        h("div", { className: "wppc-env-grid" },
          Object.keys(diag.credential_presence || {}).map(function (k) {
            var ok = diag.credential_presence[k];
            return h("div", { key: k, className: "wppc-env-item" },
              h("span", { className: "wppc-dot" + (ok ? " wppc-dot--ok" : "") }),
              h("span", { className: "wppc-mono" }, k),
              h("span", { className: "wppc-hint" }, ok ? "存在" : "缺失"));
          }))) : null);
  }

  // ---------------------------------------------------------------- policy tab

  function PairingCard(props) {
    var accountId = props.accountId;
    var active = props.active;
    var [state, setState] = useState(null);
    var [error, setError] = useState("");
    var [generated, setGenerated] = useState(null);
    var [busy, setBusy] = useState(false);

    function load() {
      if (!accountId) return;
      getJSON("/accounts/" + encodeURIComponent(accountId) + "/pairing")
        .then(function (data) { setState(data); setError(""); })
        .catch(function (err) { if (!isAbort(err)) setError(parseApiError(err).message); });
    }
    useEffect(load, [accountId, active]);

    function generate() {
      setBusy(true);
      request("POST", "/accounts/" + encodeURIComponent(accountId) + "/pairing")
        .then(function (data) {
          setGenerated(data);
          setState(function (prev) {
            return Object.assign({}, prev || {}, {
              has_code: true, expires_at: data.expires_at,
              expires_in_sec: data.expires_in_sec,
              paired_count: data.paired_count,
            });
          });
        })
        .catch(function (err) { props.notify("error", parseApiError(err).message); })
        .then(function () { setBusy(false); });
    }

    function removeUser(wxid) {
      props.askConfirm({
        title: "移除已配对用户？",
        message: wxid + " 将立即失去 dmPolicy=pairing 的准入。",
        confirmLabel: "移除",
        onConfirm: function () {
          request("DELETE", "/accounts/" + encodeURIComponent(accountId) +
            "/pairing/" + encodeURIComponent(wxid))
            .then(function () { props.notify("ok", "已移除配对"); load(); })
            .catch(function (err) { props.notify("error", parseApiError(err).message); });
        },
      });
    }

    var paired = (state && state.paired_users) || [];
    return h(Section, { title: "配对码（1 小时有效，按账号隔离）",
      right: h("button", { type: "button", className: "wppc-btn wppc-btn--primary",
        disabled: busy || !accountId, onClick: generate }, busy ? "生成中…" : "生成配对码") },
      error ? h(ErrorNotice, { message: error }) : null,
      h("dl", { className: "wppc-kv" },
        h("dt", null, "当前配对码"), h("dd", null,
          state && state.has_code ? "有效 · " + fmtTime(Math.floor(state.expires_at / 1000)) + " 到期"
            : "无有效配对码"),
        h("dt", null, "已配对"), h("dd", null, (state && state.paired_count || 0) + " 人")),
      paired.length ? h("div", { className: "wppc-chips" }, paired.map(function (wxid) {
        return h("span", { key: wxid, className: "wppc-chip wppc-chip--user" },
          h("span", { className: "wppc-mono" }, wxid),
          h("button", { type: "button", className: "wppc-chip-remove",
            "aria-label": "移除 " + wxid, onClick: function () { removeUser(wxid); } }, "×"));
      })) : h("div", { className: "wppc-hint" }, "还没有已配对用户"),
      state && state.users_truncated
        ? h("div", { className: "wppc-hint" }, "只显示前 500 个已配对用户。") : null,
      generated ? h(Modal, { title: "配对码（只显示这一次）", onClose: function () { setGenerated(null); },
        footer: h("div", { className: "wppc-actions" },
          h(CopyButton, { text: generated.code, label: "复制配对码" }),
          h("button", { type: "button", className: "wppc-btn",
            onClick: function () { setGenerated(null); } }, "关闭")) },
        h("div", { className: "wppc-pair-code wppc-mono" }, generated.code),
        h("div", { className: "wppc-hint" },
          "有效期至 " + fmtTime(Math.floor(generated.expires_at / 1000)) +
          "。关闭后无法再次查看；需要时重新生成。")) : null);
  }

  // ---------------------------------------------------------------- groups tab

  function GroupsTab(props) {
    var accountId = props.accountId;
    var active = props.active;
    var [qText, setQText] = useState("");
    var [q, setQ] = useState("");
    var [hours, setHours] = useState(24);
    var [sort, setSort] = useState("activity");
    var [limit, setLimit] = useState(50);
    var [offset, setOffset] = useState(0);
    var [data, setData] = useState(null);
    var [error, setError] = useState("");
    var [drawer, setDrawer] = useState("");
    var [busy, setBusy] = useState("");
    var seq = useRef(0);

    function load() {
      if (!accountId) return;
      var my = ++seq.current;
      getJSON(withQuery("/accounts/" + encodeURIComponent(accountId) + "/groups", {
        since_hours: hours, q: q, sort: sort, limit: limit, offset: offset,
      })).then(function (res) {
        if (my !== seq.current) return;
        setData(res);
        setError("");
      }).catch(function (err) {
        if (my !== seq.current || isAbort(err)) return;
        setError(parseApiError(err).message);
      });
    }
    usePolling(load, 15000, active && !!accountId && !drawer);
    useEffect(function () { if (active && accountId) load(); }, [accountId, active, hours, sort, limit, offset, q]);

    function search() {
      setOffset(0);
      setQ(qText.trim());
      if (qText.trim() === q) load();
    }

    function act(gid, action) {
      props.askConfirm({
        title: action === "deny" ? "加入群黑名单？" : (action === "clear" ? "清除群名单标记？" : "加入群白名单？"),
        message: (action === "allow" ? "把 " : action === "deny" ? "把 " : "把 ") + gid +
          (action === "allow" ? " 加入 groupAllowFrom。" : action === "deny" ? " 加入 blacklistGroups。" : " 从两份名单移除。"),
        confirmLabel: "确定",
        onConfirm: function () {
          setBusy(gid);
          var body = { action: action };
          if (props.account && props.account.config_mtime_ns) {
            body.expected_mtime_ns = props.account.config_mtime_ns;
          }
          request("POST", "/accounts/" + encodeURIComponent(accountId) +
            "/groups/" + encodeURIComponent(gid) + "/policy", body).then(function (res) {
            props.notify("ok", res.changed ? "名单已更新，等待热加载" : "名单没有变化");
            load();
            props.onAccountChanged();
          }).catch(function (err) {
            var e = parseApiError(err);
            if (e.status === 409 && e.code === "mtime_conflict") props.onConflict(e);
            else props.notify("error", e.message);
          }).then(function () { setBusy(""); });
        },
      });
    }

    var groups = (data && data.groups) || [];
    return h("div", { className: "wppc-stack" },
      h(Section, { title: "群列表（只读数据 + 名单快捷操作）",
        right: h("div", { className: "wppc-toolbar" },
          h("input", { className: "wppc-input wppc-search", value: qText,
            placeholder: "搜索群名 / 群 id",
            onChange: function (e) { setQText(e.target.value); },
            onKeyDown: function (e) { if (e.key === "Enter") search(); } }),
          h("button", { type: "button", className: "wppc-btn", onClick: search }, "搜索"),
          h("select", { className: "wppc-input wppc-select", value: hours,
            onChange: function (e) { setHours(parseInt(e.target.value, 10)); setOffset(0); } },
            [[6, "6 小时"], [24, "24 小时"], [72, "3 天"], [168, "7 天"]].map(function (o) {
              return h("option", { key: o[0], value: o[0] }, o[1]);
            })),
          h("select", { className: "wppc-input wppc-select", value: sort,
            onChange: function (e) { setSort(e.target.value); setOffset(0); } },
            [["activity", "按活跃"], ["name", "按群名"], ["members", "按成员数"]].map(function (o) {
              return h("option", { key: o[0], value: o[0] }, o[1]);
            })),
          h("select", { className: "wppc-input wppc-select", value: limit,
            onChange: function (e) { setLimit(parseInt(e.target.value, 10)); setOffset(0); } },
            [25, 50, 100].map(function (n) { return h("option", { key: n, value: n }, n + " / 页"); }))) },
        error ? h(ErrorNotice, { message: error }) : null,
        data && data.db_available === false
          ? h("div", { className: "wppc-warn" }, "数据库不可用：群数据已降级（策略编辑不受影响）。") : null,
        !data ? h("div", { className: "wppc-empty" }, "加载中…") :
          (!groups.length ? h(EmptyState, null, "没有匹配的群") :
            h("div", { className: "wppc-table-wrap" },
              h("table", { className: "wppc-table wppc-table--clickable" },
                h("thead", null, h("tr", null,
                  ["群", "群 id", "成员（厂商/本地）", "入站", "出站", "最后同步", "名单", "操作"].map(function (c, i) {
                    return h("th", { key: i }, c);
                  }))),
                h("tbody", null, groups.map(function (g) {
                  var policy = g.policy || {};
                  return h("tr", { key: g.chatroom_id,
                    onClick: function () { setDrawer(g.chatroom_id); } },
                    h("td", { title: g.name }, g.name || "（未同步群名）"),
                    h("td", { className: "wppc-mono" }, g.chatroom_id,
                      h(CopyButton, { text: g.chatroom_id })),
                    h("td", null, (g.member_count == null ? "—" : g.member_count) + " / " + g.local_members),
                    h("td", null, fmtNum(g.inbound)),
                    h("td", null, fmtNum(g.outbound)),
                    h("td", null, g.last_synced_at ? String(g.last_synced_at).replace("T", " ").slice(0, 19) : "—"),
                    h("td", null,
                      policy.allowlisted ? h("span", { className: "wppc-badge wppc-badge--live" }, "白") : null,
                      policy.blacklisted ? h("span", { className: "wppc-badge wppc-badge--stale" }, "黑") : null,
                      policy.heartflow_whitelisted ? h("span", { className: "wppc-badge wppc-badge--delayed" }, "心流") : null),
                    h("td", null,
                      h("button", { type: "button", className: "wppc-btn wppc-btn--ghost",
                        disabled: busy === g.chatroom_id,
                        onClick: function (e) { e.stopPropagation(); act(g.chatroom_id, "allow"); } }, "加白"),
                      h("button", { type: "button", className: "wppc-btn wppc-btn--ghost",
                        disabled: busy === g.chatroom_id,
                        onClick: function (e) { e.stopPropagation(); act(g.chatroom_id, "clear"); } }, "清除"),
                      h("button", { type: "button", className: "wppc-btn wppc-btn--ghost wppc-btn--danger",
                        disabled: busy === g.chatroom_id,
                        onClick: function (e) { e.stopPropagation(); act(g.chatroom_id, "deny"); } }, "加黑")));
                }))),
              h(Pagination, { total: data.total || 0, offset: data.offset || 0,
                limit: data.limit || limit, onChange: setOffset })))),
      drawer ? h(GroupDrawer, { accountId: accountId, account: props.account, gid: drawer,
        notify: props.notify, askConfirm: props.askConfirm, onConflict: props.onConflict,
        onAccountChanged: props.onAccountChanged, onClose: function () { setDrawer(""); } }) : null);
  }

  function GroupDrawer(props) {
    var accountId = props.accountId;
    var gid = props.gid;
    var [detail, setDetail] = useState(null);
    var [members, setMembers] = useState(null);
    var [insights, setInsights] = useState(null);
    var [error, setError] = useState("");
    var [qText, setQText] = useState("");
    var [q, setQ] = useState("");
    var [offset, setOffset] = useState(0);
    var [busy, setBusy] = useState(false);
    var limit = 50;
    var seq = useRef(0);

    function load() {
      var my = ++seq.current;
      getJSON(withQuery("/accounts/" + encodeURIComponent(accountId) + "/groups/" + encodeURIComponent(gid), {
        since_hours: 24,
      })).then(function (res) {
        if (my === seq.current) { setDetail(res); setError(""); }
      }).catch(function (err) {
        if (my === seq.current && !isAbort(err)) setError(parseApiError(err).message);
      });
      getJSON(withQuery("/accounts/" + encodeURIComponent(accountId) + "/groups/" + encodeURIComponent(gid) + "/members", {
        q: q, limit: limit, offset: offset,
      })).then(function (res) {
        if (my === seq.current) setMembers(res);
      }).catch(function () { /* 成员查询失败不影响详情 */ });
      getJSON(withQuery("/accounts/" + encodeURIComponent(accountId) + "/groups/" + encodeURIComponent(gid) + "/insights"))
        .then(function (res) { if (my === seq.current) setInsights(res); })
        .catch(function () { /* 只读摘要可缺 */ });
    }
    useEffect(load, [accountId, gid, q, offset]);
    usePolling(load, 15000, true);

    useEffect(function () {
      function onKey(e) { if (e.key === "Escape") props.onClose(); }
      document.addEventListener("keydown", onKey);
      return function () { document.removeEventListener("keydown", onKey); };
    }, [props.onClose]);

    function act(action) {
      setBusy(true);
      var body = { action: action };
      if (props.account && props.account.config_mtime_ns) {
        body.expected_mtime_ns = props.account.config_mtime_ns;
      }
      request("POST", "/accounts/" + encodeURIComponent(accountId) +
        "/groups/" + encodeURIComponent(gid) + "/policy", body).then(function (res) {
        props.notify("ok", res.changed ? "名单已更新，等待热加载" : "名单没有变化");
        load();
        props.onAccountChanged();
      }).catch(function (err) {
        var e = parseApiError(err);
        if (e.status === 409 && e.code === "mtime_conflict") props.onConflict(e);
        else props.notify("error", e.message);
      }).then(function () { setBusy(false); });
    }

    var group = (detail && detail.group) || {};
    var policy = group.policy || {};
    var memberItems = (members && members.members) || [];
    var hf = (insights && insights.heartflow) || {};
    var state = hf.state || null;
    var ledger = hf.last_ledger || null;
    var style = insights && insights.style;
    var affection = insights && insights.affection;

    return h("div", { className: "wppc-drawer-backdrop", onClick: function (e) {
      if (e.target === e.currentTarget) props.onClose();
    } },
      h("aside", { className: "wppc-drawer", role: "dialog", "aria-modal": "true",
        "aria-label": "群详情" },
        h("div", { className: "wppc-drawer-head" },
          h("div", null,
            h("h3", null, group.name || "（未同步群名）"),
            h("div", { className: "wppc-mono" }, gid, h(CopyButton, { text: gid }))),
          h("button", { type: "button", className: "wppc-btn wppc-btn--ghost",
            "aria-label": "关闭详情", onClick: props.onClose }, "×")),
        h("div", { className: "wppc-drawer-body" },
          error ? h(ErrorNotice, { message: error }) : null,
          h("div", { className: "wppc-drawer-section" },
            h("div", { className: "wppc-chips" },
              policy.allowlisted ? h("span", { className: "wppc-badge wppc-badge--live" }, "群白名单") : null,
              policy.blacklisted ? h("span", { className: "wppc-badge wppc-badge--stale" }, "群黑名单") : null,
              policy.heartflow_whitelisted ? h("span", { className: "wppc-badge wppc-badge--delayed" }, "心流白名单") : null),
            h("div", { className: "wppc-stats wppc-stats--compact" },
              h("div", { className: "wppc-stat" }, h("b", null, fmtNum(group.inbound)), h("span", null, "24h 入站")),
              h("div", { className: "wppc-stat" }, h("b", null, fmtNum(group.outbound)), h("span", null, "24h 出站")),
              h("div", { className: "wppc-stat" }, h("b", null,
                (group.member_count == null ? "—" : group.member_count) + " / " + (group.local_members || 0)),
                h("span", null, "成员（厂商/本地）"))),
            h("div", { className: "wppc-actions" },
              h("button", { type: "button", className: "wppc-btn", disabled: busy,
                onClick: function () { act("allow"); } }, "加入群白名单"),
              h("button", { type: "button", className: "wppc-btn", disabled: busy,
                onClick: function () { act("clear"); } }, "清除名单标记"),
              h("button", { type: "button", className: "wppc-btn wppc-btn--danger", disabled: busy,
                onClick: function () { props.askConfirm({
                  title: "加入群黑名单？", message: "该群将被加入 blacklistGroups，并从白名单移除。",
                  confirmLabel: "加入黑名单", onConfirm: function () { act("deny"); } }); } }, "加入群黑名单"))),

          h("div", { className: "wppc-drawer-section" },
            h("div", { className: "wppc-card-head" },
              h("h4", null, "成员（" + ((members && members.total) || 0) + "）"),
              h("div", { className: "wppc-toolbar-group" },
                h("input", { className: "wppc-input wppc-search", value: qText,
                  placeholder: "搜索昵称 / wxid",
                  onChange: function (e) { setQText(e.target.value); },
                  onKeyDown: function (e) {
                    if (e.key === "Enter") { setOffset(0); setQ(qText.trim()); }
                  } }),
                h("button", { type: "button", className: "wppc-btn",
                  onClick: function () { setOffset(0); setQ(qText.trim()); } }, "搜索"))),
            memberItems.length ? h("div", { className: "wppc-table-wrap" },
              h("table", { className: "wppc-table wppc-table--compact" },
                h("thead", null, h("tr", null,
                  ["昵称", "wxid", "身份"].map(function (c, i) {
                    return h("th", { key: i }, c);
                  }))),
                h("tbody", null, memberItems.map(function (m) {
                  return h("tr", { key: m.wxid },
                    h("td", null, m.nickname || "—"),
                    h("td", { className: "wppc-mono" }, m.wxid),
                    h("td", null, m.is_owner ? "群主" : "成员"));
                })))) : h("div", { className: "wppc-empty" }, "没有成员记录"),
            members ? h(Pagination, { total: members.total || 0, offset: members.offset || 0,
              limit: members.limit || limit, onChange: setOffset }) : null),

          h("div", { className: "wppc-drawer-section" },
            h("h4", null, "心流摘要"),
            state ? h("dl", { className: "wppc-kv" },
              h("dt", null, "学习阈值"), h("dd", null, state.learned_threshold == null ? "（未学习，用账号阈值）" : state.learned_threshold),
              h("dt", null, "最近变更"), h("dd", null, state.last_change_at ? fmtTime(state.last_change_at) + " · " +
                state.last_change_reason : "—")) : h("div", { className: "wppc-hint" }, "该群没有心流状态行"),
            ledger ? h("dl", { className: "wppc-kv" },
              h("dt", null, "最近判定"), h("dd", null, ledger.status + " · " +
                (ledger.suppressed_reason || "无抑制")),
              h("dt", null, "分数 / 阈值"), h("dd", null, ledger.judge_overall + " / " + ledger.effective_threshold),
              h("dt", null, "内容"), h("dd", { className: "wppc-wrap" }, ledger.content_head || "—")) : null),

          h("div", { className: "wppc-drawer-section" },
            h("h4", null, "风格卡"),
            style ? h("dl", { className: "wppc-kv" },
              h("dt", null, "版本"), h("dd", null, "v" + style.version),
              h("dt", null, "置信度"), h("dd", null, fmtPct(style.confidence)),
              h("dt", null, "样本"), h("dd", null, style.sample_msgs + " 条 / " + style.sender_count + " 人"),
              h("dt", null, "生成时间"), h("dd", null, fmtTime(style.generated_at))) :
              h("div", { className: "wppc-hint" }, "没有风格卡")),

          h("div", { className: "wppc-drawer-section" },
            h("h4", null, "好感度 Top 10"),
            affection && affection.users && affection.users.length
              ? h("div", { className: "wppc-table-wrap" },
                h("table", { className: "wppc-table wppc-table--compact" },
                  h("thead", null, h("tr", null,
                    ["昵称", "wxid", "好感度"].map(function (c, i) { return h("th", { key: i }, c); }))),
                  h("tbody", null, affection.users.map(function (u) {
                    return h("tr", { key: u.wxid },
                      h("td", null, u.nickname || "—"),
                      h("td", { className: "wppc-mono" }, u.wxid),
                      h("td", null, fmtNum(u.level)));
                  }))))
              : h("div", { className: "wppc-hint" }, "没有好感度记录"),
            affection && affection.mood ? h("div", { className: "wppc-hint" },
              "当前情绪：" + affection.mood.mood_type + "（" + affection.mood.intensity + "）" +
              (affection.mood.expired ? " · 已过期" : "")) : null))));
  }

  // ---------------------------------------------------------------- messages tab

  function MessagesTab(props) {
    var accountId = props.accountId;
    var active = props.active;
    var [hours, setHours] = useState(24);
    var [analytics, setAnalytics] = useState(null);
    var [analyticsError, setAnalyticsError] = useState("");
    var [q, setQ] = useState("");
    var [groupId, setGroupId] = useState("");
    var [sender, setSender] = useState("");
    var [direction, setDirection] = useState("all");
    var [searchHours, setSearchHours] = useState(168);
    var [results, setResults] = useState(null);
    var [searchError, setSearchError] = useState("");
    var [loading, setLoading] = useState(false);
    var seqA = useRef(0);
    var seqS = useRef(0);

    function loadAnalytics() {
      if (!accountId) return;
      var my = ++seqA.current;
      getJSON(withQuery("/accounts/" + encodeURIComponent(accountId) + "/analytics", { hours: hours }))
        .then(function (res) {
          if (my !== seqA.current) return;
          setAnalytics(res); setAnalyticsError("");
        }).catch(function (err) {
          if (my !== seqA.current || isAbort(err)) return;
          setAnalyticsError(parseApiError(err).message);
        });
    }
    usePolling(loadAnalytics, 30000, active && !!accountId);
    useEffect(function () { if (active && accountId) loadAnalytics(); }, [accountId, active, hours]);

    function search(reset) {
      if (!accountId) return;
      var text = q.trim();
      var gid = groupId.trim();
      var from = sender.trim();
      if (!text && !gid && !from) {
        setSearchError("至少填写关键词、群 id 或发送者之一。");
        return;
      }
      if (gid && gid.indexOf("@chatroom") < 0) {
        setSearchError("群 id 必须以 @chatroom 结尾。");
        return;
      }
      var offset = reset ? 0 : ((results && results.offset) || 0) + ((results && results.limit) || 50);
      var my = ++seqS.current;
      setLoading(true);
      getJSON(withQuery("/accounts/" + encodeURIComponent(accountId) + "/messages", {
        q: text, group_id: gid, sender: from,
        direction: direction === "all" ? "" : direction,
        since_hours: searchHours, limit: 50, offset: offset,
      })).then(function (res) {
        if (my !== seqS.current) return;
        setResults(function (prev) {
          if (!reset && prev && prev.messages) {
            return Object.assign({}, res, { messages: prev.messages.concat(res.messages) });
          }
          return res;
        });
        setSearchError("");
      }).catch(function (err) {
        if (my !== seqS.current || isAbort(err)) return;
        setSearchError(parseApiError(err).message);
      }).then(function () { if (my === seqS.current) setLoading(false); });
    }

    var totals = (analytics && analytics.totals) || { inbound: 0, outbound: 0 };
    var buckets = (analytics && analytics.buckets) || [];
    var avg = buckets.length ? ((totals.inbound + totals.outbound) / buckets.length) : 0;
    var messages = (results && results.messages) || [];
    var hasMore = !!(results && results.has_more);

    return h("div", { className: "wppc-stack" },
      h(Section, { title: "收发趋势", right:
        h("div", { className: "wppc-toolbar-group" },
          h("button", { type: "button", className: "wppc-btn" + (hours === 24 ? " wppc-btn--primary" : ""),
            onClick: function () { setHours(24); } }, "24 小时"),
          h("button", { type: "button", className: "wppc-btn" + (hours === 168 ? " wppc-btn--primary" : ""),
            onClick: function () { setHours(168); } }, "7 天")) },
        analyticsError ? h(ErrorNotice, { message: analyticsError }) : null,
        !analytics ? h("div", { className: "wppc-empty" }, "加载中…") : h("div", null,
          h("div", { className: "wppc-stats wppc-stats--compact" },
            h("div", { className: "wppc-stat" }, h("b", null, fmtNum(totals.inbound + totals.outbound)), h("span", null, "总消息")),
            h("div", { className: "wppc-stat" }, h("b", null, fmtNum(totals.inbound)), h("span", null, "入站")),
            h("div", { className: "wppc-stat" }, h("b", null, fmtNum(totals.outbound)), h("span", null, "出站")),
            h("div", { className: "wppc-stat" }, h("b", null, avg.toFixed(1)), h("span", null, "每桶平均"))),
          h(TrendBars, { buckets: buckets, showLabels: true }))),

      h(Section, { title: "消息类型分布" },
        !analytics ? h("div", { className: "wppc-empty" }, "加载中…") :
          h(TypeBars, { types: analytics.types || [] })),

      h(Section, { title: "消息检索（只读，最多 30 天）" },
        h("div", { className: "wppc-toolbar" },
          h("input", { className: "wppc-input wppc-search", value: q, placeholder: "关键词（LIKE 扫描）",
            onChange: function (e) { setQ(e.target.value); },
            onKeyDown: function (e) { if (e.key === "Enter") search(true); } }),
          h("input", { className: "wppc-input", value: groupId, placeholder: "群 id（*@chatroom）",
            onChange: function (e) { setGroupId(e.target.value); },
            onKeyDown: function (e) { if (e.key === "Enter") search(true); } }),
          h("input", { className: "wppc-input", value: sender, placeholder: "发送者 wxid",
            onChange: function (e) { setSender(e.target.value); },
            onKeyDown: function (e) { if (e.key === "Enter") search(true); } }),
          h("select", { className: "wppc-input wppc-select", value: direction,
            onChange: function (e) { setDirection(e.target.value); } },
            [["all", "全部方向"], ["inbound", "入站"], ["outbound", "出站"]].map(function (o) {
              return h("option", { key: o[0], value: o[0] }, o[1]);
            })),
          h("select", { className: "wppc-input wppc-select", value: searchHours,
            onChange: function (e) { setSearchHours(parseInt(e.target.value, 10)); } },
            [[24, "24 小时"], [72, "3 天"], [168, "7 天"], [720, "30 天"]].map(function (o) {
              return h("option", { key: o[0], value: o[0] }, o[1]);
            })),
          h("button", { type: "button", className: "wppc-btn wppc-btn--primary",
            disabled: loading, onClick: function () { search(true); } }, loading ? "查询中…" : "检索")),
        h("div", { className: "wppc-hint" },
          "关键词是数据库 LIKE 扫描（% 与 _ 按字面匹配），数据量大时请缩窄时间窗/群。"),
        searchError ? h(ErrorNotice, { message: searchError }) : null,
        results && !messages.length ? h(EmptyState, null, "没有匹配的消息") : null,
        messages.length ? h("div", { className: "wppc-table-wrap" },
          h("table", { className: "wppc-table" },
            h("thead", null, h("tr", null,
              ["时间", "方向", "类型", "群 / 会话", "发送者", "内容"].map(function (c, i) {
                return h("th", { key: i }, c);
              }))),
            h("tbody", null, messages.map(function (m) {
              return h("tr", { key: m.id },
                h("td", null, fmtTime(m.at_sec)),
                h("td", null, m.direction === "inbound" ? "入" : "出"),
                h("td", null, m.msg_type || "unknown"),
                h("td", { className: "wppc-mono" }, m.chat_id || m.peer_id || "—"),
                h("td", { className: "wppc-mono" }, m.from_wxid || "—"),
                h("td", { className: "wppc-wrap" }, m.content || ""));
            }))),
          h("div", { className: "wppc-actions" },
            results ? h("span", { className: "wppc-page-info" },
              "共 " + results.total + " 条，已加载 " + messages.length + " 条") : null,
            hasMore ? h("button", { type: "button", className: "wppc-btn",
              disabled: loading, onClick: function () { search(false); } }, "加载更多") : null)) : null));
  }

  // ---------------------------------------------------------------- heartflow tab

  function HeartflowDiagnostics(props) {
    var accountId = props.accountId;
    var active = props.active;
    var [data, setData] = useState(null);
    var [error, setError] = useState("");
    var seq = useRef(0);

    function load() {
      if (!accountId) return;
      var my = ++seq.current;
      getJSON(withQuery("/accounts/" + encodeURIComponent(accountId) + "/heartflow", { hours: 24 }))
        .then(function (res) {
          if (my !== seq.current) return;
          setData(res); setError("");
        }).catch(function (err) {
          if (my !== seq.current || isAbort(err)) return;
          setError(parseApiError(err).message);
        });
    }
    usePolling(load, 20000, active && !!accountId);

    if (error) return h(Section, { title: "心流诊断" }, h(ErrorNotice, { message: error }));
    if (!data) return h(Section, { title: "心流诊断" }, h("div", { className: "wppc-empty" }, "加载中…"));
    var stats = data.stats || {};
    var status = stats.by_status || {};
    var reasons = stats.by_suppressed_reason || {};
    var budget = data.budget || [];
    var states = data.group_states || [];
    var recent = data.recent || [];

    return h("div", { className: "wppc-stack" },
      h(Section, { title: "近 24h 判定 / 发送" },
        data.db_available === false ? h("div", { className: "wppc-warn" }, "数据库不可用：诊断数据已降级。") : null,
        h("div", { className: "wppc-stats wppc-stats--compact" },
          h("div", { className: "wppc-stat" }, h("b", null, fmtNum(stats.total)), h("span", null, "判定总数")),
          h("div", { className: "wppc-stat" }, h("b", null, fmtNum(status.sent || 0)), h("span", null, "已发送")),
          h("div", { className: "wppc-stat" }, h("b", null, fmtNum(status.suppressed || 0)), h("span", null, "已抑制")),
          h("div", { className: "wppc-stat" }, h("b", null, data.last_sent_at ? fmtTime(data.last_sent_at) : "—"),
            h("span", null, "最后一次真发言"))),
        Object.keys(reasons).length ? h("div", { className: "wppc-chips" }, Object.keys(reasons).map(function (k) {
          return h("span", { key: k, className: "wppc-chip wppc-chip--user" }, k + "：" + reasons[k]);
        })) : null),

      h(Section, { title: "预算占用（库内已发送计数）" },
        budget.length ? h("div", { className: "wppc-table-wrap" },
          h("table", { className: "wppc-table wppc-table--compact" },
            h("thead", null, h("tr", null,
              ["群", "近 1 小时", "近 24 小时", "最近发送"].map(function (c, i) {
                return h("th", { key: i }, c);
              }))),
            h("tbody", null, budget.map(function (b) {
              return h("tr", { key: b.group_id },
                h("td", { className: "wppc-mono" }, b.group_id),
                h("td", null, fmtNum(b.hour_count)),
                h("td", null, fmtNum(b.day_count)),
                h("td", null, b.last_sent_at ? fmtTime(b.last_sent_at) : "—"));
            })))) : h("div", { className: "wppc-hint" }, "窗口内没有已发送记录"),
        h("div", { className: "wppc-hint" },
          "实时内存预算计数在网关进程里，控制台只显示配置和数据库回填值。")),

      h(Section, { title: "群学习阈值" },
        states.length ? h("div", { className: "wppc-table-wrap" },
          h("table", { className: "wppc-table wppc-table--compact" },
            h("thead", null, h("tr", null,
              ["群", "学习阈值", "最近变更", "旧值 → 新值", "原因"].map(function (c, i) {
                return h("th", { key: i }, c);
              }))),
            h("tbody", null, states.map(function (s) {
              return h("tr", { key: s.group_id },
                h("td", { className: "wppc-mono" }, s.group_id),
                h("td", null, s.learned_threshold == null ? "—" : s.learned_threshold),
                h("td", null, s.last_change_at ? fmtTime(s.last_change_at) : "—"),
                h("td", null, (s.last_change_old == null ? "—" : s.last_change_old) + " → " +
                  (s.last_change_new == null ? "—" : s.last_change_new)),
                h("td", null, s.last_change_reason || "—"));
            })))) : h("div", { className: "wppc-hint" }, "没有群状态行")),

      h(Section, { title: "最近判定" },
        recent.length ? h("div", { className: "wppc-table-wrap" },
          h("table", { className: "wppc-table wppc-table--compact" },
            h("thead", null, h("tr", null,
              ["时间", "群", "发送者", "分数", "阈值", "状态", "抑制原因", "内容"].map(function (c, i) {
                return h("th", { key: i }, c);
              }))),
            h("tbody", null, recent.map(function (r, i) {
              return h("tr", { key: (r.inbound_msg_id || "") + ":" + i },
                h("td", null, fmtTime(r.judged_at)),
                h("td", { className: "wppc-mono" }, r.group_id),
                h("td", { className: "wppc-mono" }, r.from_wxid || "—"),
                h("td", null, r.judge_overall == null ? "—" : r.judge_overall),
                h("td", null, r.effective_threshold == null ? "—" : r.effective_threshold),
                h("td", null, r.status || "—"),
                h("td", null, r.suppressed_reason || "—"),
                h("td", { className: "wppc-wrap" }, r.content_head || ""));
            })))) : h("div", { className: "wppc-hint" }, "没有判定记录")));
  }

  function InteractionPanel(props) {
    var accountId = props.accountId;
    var active = props.active;
    var [cards, setCards] = useState(null);
    var [styleError, setStyleError] = useState("");
    var [groupId, setGroupId] = useState("");
    var [affection, setAffection] = useState(null);
    var [affectionError, setAffectionError] = useState("");
    var seq = useRef(0);

    function loadStyle() {
      if (!accountId) return;
      var my = ++seq.current;
      getJSON("/accounts/" + encodeURIComponent(accountId) + "/style-cards")
        .then(function (res) {
          if (my !== seq.current) return;
          setCards(res); setStyleError("");
        }).catch(function (err) {
          if (my !== seq.current || isAbort(err)) return;
          setStyleError(parseApiError(err).message);
        });
    }
    usePolling(loadStyle, 30000, active && !!accountId);

    function loadAffection() {
      if (!accountId || !validateGroupId(groupId.trim())) {
        setAffectionError("请输入以 @chatroom 结尾的群 id。");
        return;
      }
      getJSON(withQuery("/accounts/" + encodeURIComponent(accountId) + "/affection", {
        group_id: groupId.trim(), limit: 100,
      })).then(function (res) {
        setAffection(res); setAffectionError("");
      }).catch(function (err) { setAffectionError(parseApiError(err).message); });
    }

    var styleCards = (cards && cards.cards) || [];
    return h("div", { className: "wppc-stack" },
      h(Section, { title: "风格卡（只读）" },
        styleError ? h(ErrorNotice, { message: styleError }) : null,
        !cards ? h("div", { className: "wppc-empty" }, "加载中…") :
          (!styleCards.length ? h("div", { className: "wppc-hint" }, "还没有风格卡") :
            h("div", { className: "wppc-cardgrid" }, styleCards.map(function (c) {
              return h("div", { key: c.group_id, className: "wppc-minicard" },
                h("div", { className: "wppc-mono" }, c.group_id),
                h("div", null, "v" + c.version + " · 置信度 " + fmtPct(c.confidence)),
                h("div", { className: "wppc-hint" },
                  c.sample_msgs + " 条样本 / " + c.sender_count + " 人 · " + fmtTime(c.generated_at)),
                h("div", { className: "wppc-hint" },
                  c.style_valid ? ("字段：" + (c.style_keys || []).join(", ")) : "JSON 解析失败"));
            })))),

      h(Section, { title: "好感度（只读）" },
        h("div", { className: "wppc-toolbar" },
          h("input", { className: "wppc-input wppc-search", value: groupId,
            placeholder: "群 id（*@chatroom）",
            onChange: function (e) { setGroupId(e.target.value); },
            onKeyDown: function (e) { if (e.key === "Enter") loadAffection(); } }),
          h("button", { type: "button", className: "wppc-btn", onClick: loadAffection }, "查询")),
        affectionError ? h(ErrorNotice, { message: affectionError }) : null,
        affection ? h("div", null,
          h("div", { className: "wppc-stats wppc-stats--compact" },
            h("div", { className: "wppc-stat" }, h("b", null, fmtNum(affection.total)), h("span", null, "群总好感")),
            h("div", { className: "wppc-stat" }, h("b", null, fmtNum(affection.user_count)), h("span", null, "用户数")),
            h("div", { className: "wppc-stat" }, h("b", null, affection.mood ? affection.mood.mood_type : "—"),
              h("span", null, "当前情绪" + (affection.mood && affection.mood.expired ? "（已过期）" : "")))),
          (affection.users || []).length ? h("div", { className: "wppc-table-wrap" },
            h("table", { className: "wppc-table wppc-table--compact" },
              h("thead", null, h("tr", null,
                ["#", "昵称", "wxid", "好感度"].map(function (c, i) { return h("th", { key: i }, c); }))),
              h("tbody", null, affection.users.map(function (u, i) {
                return h("tr", { key: u.wxid },
                  h("td", null, i + 1),
                  h("td", null, u.nickname || "—"),
                  h("td", { className: "wppc-mono" }, u.wxid),
                  h("td", null, fmtNum(u.level)));
              })))) : h("div", { className: "wppc-hint" }, "没有好感度记录")) :
          h("div", { className: "wppc-hint" }, "输入群 id 后查询（只读，不会重置过期情绪）")));
  }

  // ---------------------------------------------------------------- page

  function ConsolePage() {
    var [overview, setOverview] = useState(null);
    var [pageError, setPageError] = useState("");
    var [selected, setSelected] = useState("");
    var [account, setAccount] = useState(null);
    var [form, setForm] = useState(null);
    var [baselineForm, setBaselineForm] = useState(null);
    var [baselineMtime, setBaselineMtime] = useState(0);
    var [tab, setTab] = useState("overview");
    var [saving, setSaving] = useState("");
    var [conflict, setConflict] = useState(null);
    var [remoteChange, setRemoteChange] = useState(false);
    var [pending, setPending] = useState({});
    var [toasts, setToasts] = useState([]);
    var [confirmState, setConfirmState] = useState(null);
    var [shortcutsOpen, setShortcutsOpen] = useState(false);
    var [notice, setNotice] = useState("");

    var selectedRef = useLatest(selected);
    var formRef = useLatest(form);
    var baselineFormRef = useLatest(baselineForm);
    var baselineMtimeRef = useLatest(baselineMtime);
    var pendingRef = useLatest(pending);
    var savingRef = useLatest(saving);
    var conflictRef = useLatest(conflict);
    var tabRef = useLatest(tab);
    var seqDetail = useRef(0);
    var toastSeq = useRef(0);

    function notify(type, text) {
      var id = ++toastSeq.current;
      setToasts(function (list) { return list.concat([{ id: id, type: type, text: text }]); });
      setTimeout(function () {
        setToasts(function (list) { return list.filter(function (t) { return t.id !== id; }); });
      }, type === "error" ? 8000 : 4500);
    }

    function askConfirm(opts) { setConfirmState(opts); }

    function isDirtyNow(section) {
      if (!formRef.current || !baselineFormRef.current) return false;
      if (section) return sectionDirty(formRef.current, baselineFormRef.current, section);
      return SECTIONS.some(function (s) {
        return sectionDirty(formRef.current, baselineFormRef.current, s);
      });
    }

    function loadOverview() {
      var id = selectedRef.current;
      getJSON(withQuery("/overview", { account: id }))
        .then(function (data) {
          setOverview(data);
          setPageError("");
          if (!selectedRef.current && data.selected) {
            setSelected(data.selected);
            selectedRef.current = data.selected;
          }
          processPending(data.account_statuses || {});
          detectRemote(data.account_statuses || {});
        })
        .catch(function (err) {
          if (isAbort(err)) return;
          setPageError(parseApiError(err).message);
        });
    }
    usePolling(loadOverview, 5000, true);

    function processPending(statuses) {
      var prev = pendingRef.current || {};
      var next = Object.assign({}, prev);
      var changed = false;
      Object.keys(prev).forEach(function (aid) {
        var p = prev[aid];
        var st = statuses[aid];
        if (!st) return;
        if (st.last_reload_ok === false && st.last_reload_at * 1000 >= p.startedMs - 3000) {
          notify("error", "热加载失败：" + (st.last_reload_error || "未知错误"));
          delete next[aid]; changed = true; return;
        }
        if (st.config_mtime_ns >= p.mtimeNs &&
            ((st.last_reload_at * 1000 >= p.startedMs - 3000) || st.last_reload_ok === true)) {
          if (aid === selectedRef.current) {
            notify("ok", "网关已热加载配置（" + fmtTime(st.last_reload_at) + "）");
          }
          delete next[aid]; changed = true; return;
        }
        if (!p.timedOut && Date.now() - p.startedMs > 30000) {
          next[aid] = Object.assign({}, p, { timedOut: true });
          if (aid === selectedRef.current) {
            notify("warn", "配置已写盘，但网关 30 秒内未确认热加载；网关可能未运行或账号未连接。");
          }
          changed = true;
        }
      });
      if (changed) {
        pendingRef.current = next;
        setPending(next);
      }
    }

    function detectRemote(statuses) {
      var id = selectedRef.current;
      if (!id || !isDirtyNow() || savingRef.current) return;
      var st = statuses[id];
      var base = baselineMtimeRef.current;
      if (st && base && Number(st.config_mtime_ns) > Number(base)) {
        setRemoteChange(true);
      }
    }

    function loadDetail(id, force) {
      if (!id) return;
      if (!force && (isDirtyNow() || savingRef.current || conflictRef.current)) return;
      var my = ++seqDetail.current;
      getJSON("/accounts/" + encodeURIComponent(id)).then(function (data) {
        if (my !== seqDetail.current || id !== selectedRef.current) return;
        var built = buildForm(data.account);
        setAccount(data.account);
        setForm(built);
        setBaselineForm(clone(built));
        setBaselineMtime(Number(data.account.config_mtime_ns) || 0);
        setRemoteChange(false);
      }).catch(function (err) {
        if (my !== seqDetail.current || isAbort(err)) return;
        notify("error", parseApiError(err).message);
      });
    }

    usePolling(function () {
      var id = selectedRef.current;
      if (id && !isDirtyNow() && !savingRef.current && !conflictRef.current) loadDetail(id, true);
    }, 10000, true);

    function doSelect(id) {
      if (id === selectedRef.current) return;
      seqDetail.current += 1;
      setSelected(id);
      selectedRef.current = id;
      setAccount(null);
      setForm(null);
      setBaselineForm(null);
      setBaselineMtime(0);
      setConflict(null);
      setRemoteChange(false);
      setNotice("");
      loadDetail(id, true);
    }

    function selectAccount(id) {
      if (id === selectedRef.current) return;
      if (isDirtyNow()) {
        askConfirm({
          title: "有未保存的修改",
          message: "切换账号会丢弃当前未保存的修改。",
          confirmLabel: "丢弃并切换",
          danger: true,
          onConfirm: function () { doSelect(id); },
        });
        return;
      }
      doSelect(id);
    }

    useEffect(function () {
      if (!selected) return;
      loadDetail(selected, true);
    }, [selected]);

    useEffect(function () {
      function onBeforeUnload(e) {
        if (isDirtyNow()) {
          e.preventDefault();
          e.returnValue = "";
        }
      }
      window.addEventListener("beforeunload", onBeforeUnload);
      return function () { window.removeEventListener("beforeunload", onBeforeUnload); };
    }, []);

    function markPending(accountId, mtimeNs) {
      var next = Object.assign({}, pendingRef.current);
      next[accountId] = { mtimeNs: Number(mtimeNs) || 0, startedMs: Date.now(), timedOut: false };
      pendingRef.current = next;
      setPending(next);
    }

    function onReloaded(accountId, mtimeNs, startedMs) {
      var next = Object.assign({}, pendingRef.current);
      next[accountId] = { mtimeNs: Number(mtimeNs) || 0, startedMs: startedMs || Date.now(), timedOut: false };
      pendingRef.current = next;
      setPending(next);
    }

    function payloadFor(section) {
      var f = formRef.current;
      var b = baselineFormRef.current;
      var aid = selectedRef.current;
      if (!f || !b) throw new Error("表单尚未加载");
      if (section === "policy") {
        return {
          path: "/accounts/" + encodeURIComponent(aid) + "/policy",
          body: {
            dmPolicy: f.policy.dmPolicy,
            groupPolicy: f.policy.groupPolicy,
            allowFrom: f.policy.allowFrom,
            blacklistUsers: f.policy.blacklistUsers,
            groupAllowFrom: f.policy.groupAllowFrom,
            blacklistGroups: f.policy.blacklistGroups,
            adminUsers: f.policy.adminUsers,
            employeeDenyList: f.policy.employeeDenyList,
          },
        };
      }
      if (section === "welcome") {
        var body = {};
        ["enabled", "mode", "groups", "template", "llmPolish", "mention", "maxBatch"].forEach(function (k) {
          if (!deepEqual(f.welcome[k], b.welcome[k])) body[k] = f.welcome[k];
        });
        if (f.welcome.quietHoursText !== b.welcome.quietHoursText) {
          body.quietHours = expandQuietHours(f.welcome.quietHoursText);
        }
        return { path: "/accounts/" + encodeURIComponent(aid) + "/join-welcome", body: body };
      }
      if (section === "features") {
        var out = {};
        FEATURE_KEYS.forEach(function (k) { out[k] = !!f.features[k]; });
        out.xiaoweiEnabled = !!f.features.xiaowei;
        ADVANCED_KEYS.forEach(function (k) { out[k] = !!f.features[k]; });
        out.debounceMs = Number(f.features.debounceMs);
        return { path: "/accounts/" + encodeURIComponent(aid) + "/features", body: out };
      }
      var hf = {};
      if (f.heartflow.thresholdText !== b.heartflow.thresholdText) {
        var text = String(f.heartflow.thresholdText || "").trim();
        if (!text) throw new Error("心流阈值不能清空；请填 0–1 的数字。");
        var num = Number(text);
        if (!isFinite(num) || num < 0 || num > 1) throw new Error("心流阈值必须在 0–1 之间。");
        hf.replyThreshold = num;
      }
      if (!deepEqual(f.heartflow.whitelistGroups, b.heartflow.whitelistGroups)) {
        hf.whitelistGroups = f.heartflow.whitelistGroups;
      }
      if (f.heartflow.profileEnabled !== b.heartflow.profileEnabled) {
        hf.profileEnabled = !!f.heartflow.profileEnabled;
      }
      if (!Object.keys(hf).length) throw new Error("心流没有修改");
      return { path: "/accounts/" + encodeURIComponent(aid) + "/heartflow", body: hf };
    }

    function save(section) {
      if (!selectedRef.current || savingRef.current || !formRef.current) return;
      if (!sectionDirty(formRef.current, baselineFormRef.current, section)) {
        notify("info", SECTION_LABELS[section] + "没有修改");
        return;
      }
      var payload;
      try {
        payload = payloadFor(section);
      } catch (err) {
        notify("error", String(err.message || err));
        return;
      }
      var snapshot = clone(formRef.current);
      var aid = selectedRef.current;
      payload.body.expected_mtime_ns = baselineMtimeRef.current;
      setSaving(section);
      savingRef.current = section;
      setNotice("");
      request("PUT", payload.path, payload.body).then(function (res) {
        notify("ok", "已保存，等待网关热加载（约 3 秒）…");
        markPending(aid, res.mtime_ns);
        setBaselineMtime(Number(res.mtime_ns) || 0);
        baselineMtimeRef.current = Number(res.mtime_ns) || 0;
        setBaselineForm(function (prev) {
          var next = clone(prev || snapshot);
          next[section] = clone(snapshot[section]);
          return next;
        });
        setTimeout(function () {
          loadOverview();
          // 只在不脏时重载：否则会把用户在其他页签刚输入的修改冲掉。
          if (aid === selectedRef.current && !isDirtyNow() && !conflictRef.current) {
            loadDetail(aid, true);
          }
        }, 1200);
      }).catch(function (err) {
        var e = parseApiError(err);
        if (e.status === 409 && e.code === "mtime_conflict") {
          setConflict({ section: section, currentMtimeNs: e.currentMtimeNs, message: e.message });
        } else {
          notify("error", e.message);
        }
      }).then(function () {
        setSaving("");
        savingRef.current = "";
      });
    }

    function resetSection(section) {
      askConfirm({
        title: "重置" + SECTION_LABELS[section] + "？",
        message: "将丢弃该页签未保存的修改。",
        confirmLabel: "重置",
        danger: true,
        onConfirm: function () {
          setForm(function (prev) {
            var next = clone(prev);
            next[section] = clone(baselineFormRef.current[section]);
            return next;
          });
        },
      });
    }

    function refreshDetail(id) {
      if (!id) return;
      if (isDirtyNow()) {
        askConfirm({
          title: "刷新会丢弃未保存的修改",
          message: "当前有未保存的修改，重新加载账号配置会覆盖它们。",
          confirmLabel: "刷新并丢弃",
          danger: true,
          onConfirm: function () {
            setConflict(null);
            setRemoteChange(false);
            loadDetail(id, true);
            notify("info", "已刷新");
          },
        });
        return;
      }
      loadDetail(id, true);
      notify("info", "已刷新");
    }

    function onAccountChanged() {
      var id = selectedRef.current;
      if (!id) return;
      if (!isDirtyNow()) {
        loadDetail(id, true);
        return;
      }
      // 有未保存修改时不重载整张表单，只更新账号元数据（mtime/名单标记）。
      getJSON("/accounts/" + encodeURIComponent(id)).then(function (data) {
        if (id !== selectedRef.current) return;
        setAccount(data.account);
        setRemoteChange(true);
        notify("warn", "名单已更新，但当前页签有未保存的修改：请先保存或重置后再刷新。");
      }).catch(function (err) {
        if (!isAbort(err)) notify("error", parseApiError(err).message);
      });
    }

    function refreshAll() {
      loadOverview();
      var id = selectedRef.current;
      if (!id) { notify("info", "已刷新"); return; }
      refreshDetail(id);
    }

    function refreshTab() {
      var current = tabRef.current;
      loadOverview();
      if (current === "groups" || current === "messages") {
        // 子组件自己持筛选状态和轮询，这里只刷新总览，避免冲掉它们的请求。
        notify("info", "已刷新");
        return;
      }
      refreshDetail(selectedRef.current);
    }

    useEffect(function () {
      function onKey(e) {
        var target = e.target;
        var typing = target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" ||
          target.tagName === "SELECT" || target.isContentEditable);
        if ((e.ctrlKey || e.metaKey) && String(e.key).toLowerCase() === "s") {
          e.preventDefault();
          var dirtySections = SECTIONS.filter(function (s) {
            return sectionDirty(formRef.current, baselineFormRef.current, s);
          });
          // Ctrl+S 优先保存当前页签；在其他页签时才退回第一个未保存页签。
          var dirtySection = dirtySections.indexOf(tabRef.current) >= 0
            ? tabRef.current : dirtySections[0];
          if (dirtySection) save(dirtySection);
          return;
        }
        if (typing) return;
        if (e.key === "/") {
          e.preventDefault();
          var input = document.querySelector(".wppc-panel--active input.wppc-search");
          if (input) input.focus();
        } else if (e.key === "?") {
          setShortcutsOpen(true);
        } else if (String(e.key).toLowerCase() === "r") {
          refreshTab();
        } else if (e.altKey && /^[1-7]$/.test(String(e.key))) {
          e.preventDefault();
          setTab(TABS[parseInt(e.key, 10) - 1][0]);
        }
      }
      document.addEventListener("keydown", onKey);
      return function () { document.removeEventListener("keydown", onKey); };
    }, []);

    var accounts = (overview && overview.accounts) || [];
    var status = (overview && overview.status) || {};
    var acctStatus = status.account || {};
    var stats = (overview && overview.stats) || {};
    var warnings = (overview && overview.warnings) || [];
    var dirtySections = SECTIONS.filter(function (s) {
      return sectionDirty(form, baselineForm, s);
    });
    var pendingEntry = selected ? pending[selected] : null;

    function setPolicy(key, value) {
      setForm(function (f) { return Object.assign({}, f, { policy: Object.assign({}, f.policy, { [key]: value }) }); });
    }
    function setWelcome(key, value) {
      setForm(function (f) { return Object.assign({}, f, { welcome: Object.assign({}, f.welcome, { [key]: value }) }); });
    }
    function setFeature(key, value) {
      setForm(function (f) { return Object.assign({}, f, { features: Object.assign({}, f.features, { [key]: value }) }); });
    }
    function setHeartflow(key, value) {
      setForm(function (f) { return Object.assign({}, f, { heartflow: Object.assign({}, f.heartflow, { [key]: value }) }); });
    }

    function panel(name, body, extraClass) {
      return h("div", {
        key: name,
        className: "wppc-panel" + (tab === name ? " wppc-panel--active" : "") +
          (extraClass ? " " + extraClass : ""),
        style: tab === name ? null : { display: "none" },
        "aria-hidden": tab === name ? "false" : "true",
      }, body);
    }

    if (!overview && !pageError) {
      return h("div", { className: "wppc-page" }, h("div", { className: "wppc-empty" }, "加载中…"));
    }
    if (pageError && !overview) {
      return h("div", { className: "wppc-page" },
        h("div", { className: "wppc-error" }, pageError),
        h("div", { className: "wppc-actions" },
          h("button", { type: "button", className: "wppc-btn", onClick: loadOverview }, "重试")));
    }

    var policyBody = !form ? h("div", { className: "wppc-empty" }, account ? "加载表单…" : "请选择一个账号") :
      h("div", { className: "wppc-stack" },
        h(Section, { title: "准入策略" },
          account && account.allowAllUsers
            ? h("div", { className: "wppc-danger" }, "⚠️ allowAllUsers=true：运行时绕过 dmPolicy，私聊仍全放行。")
            : h("div", { className: "wppc-danger" },
              "网关层 default-deny 仍需 WECHATPRO_ALLOW_ALL_USERS=1 才会放行；凭据与 enabled 的变更要重启网关。"),
          h("div", { className: "wppc-grid2" },
            h(SelectField, { label: "私聊策略 dmPolicy", value: form.policy.dmPolicy,
              options: ["open", "allowlist", "blacklist", "pairing"],
              onChange: function (v) { setPolicy("dmPolicy", v); } }),
            h(SelectField, { label: "群策略 groupPolicy", value: form.policy.groupPolicy,
              options: ["open", "allowlist", "blacklist", "disabled", "closed"],
              onChange: function (v) { setPolicy("groupPolicy", v); } })),
          h(ChipListEditor, { label: "私聊白名单 allowFrom", kind: "user",
            value: form.policy.allowFrom, onChange: function (v) { setPolicy("allowFrom", v); } }),
          h(ChipListEditor, { label: "私聊黑名单 blacklistUsers", kind: "user",
            value: form.policy.blacklistUsers, onChange: function (v) { setPolicy("blacklistUsers", v); } }),
          h(ChipListEditor, { label: "群白名单 groupAllowFrom", kind: "group",
            value: form.policy.groupAllowFrom, onChange: function (v) { setPolicy("groupAllowFrom", v); } }),
          h(ChipListEditor, { label: "群黑名单 blacklistGroups", kind: "group",
            value: form.policy.blacklistGroups, onChange: function (v) { setPolicy("blacklistGroups", v); } }),
          h(ChipListEditor, { label: "管理员 adminUsers", kind: "user",
            value: form.policy.adminUsers, onChange: function (v) { setPolicy("adminUsers", v); } }),
          h(ChipListEditor, { label: "永不自动加入 employeeDenyList", kind: "user",
            value: form.policy.employeeDenyList,
            hint: "从白名单手工删人后若不写这里，下次全员群同步会把人加回来。",
            onChange: function (v) { setPolicy("employeeDenyList", v); } }),
          h(ChipListEditor, { label: "已自动同步的员工 employeeAllowFrom（只读）", kind: "user",
            readOnly: true, value: toList(account.employeeAllowFrom),
            hint: "由全员群同步维护，控制台不可编辑。" }),
          h("div", { className: "wppc-actions" },
            h("button", { type: "button", className: "wppc-btn wppc-btn--primary",
              disabled: saving === "policy" || !sectionDirty(form, baselineForm, "policy"),
              onClick: function () { save("policy"); } },
              saving === "policy" ? "保存中…" : "保存策略（Ctrl+S）"))),
        h(PairingCard, { accountId: selected, active: tab === "policy",
          notify: notify, askConfirm: askConfirm }));

    var welcomeBody = !form ? h("div", { className: "wppc-empty" }, "加载中…") :
      h(Section, { title: "进群欢迎" },
        h("div", { className: "wppc-grid2" },
          h(CheckField, { label: "启用进群欢迎", checked: form.welcome.enabled,
            onChange: function (v) { setWelcome("enabled", v); } }),
          h(CheckField, { label: "LLM 润色 llmPolish", checked: form.welcome.llmPolish,
            onChange: function (v) { setWelcome("llmPolish", v); } }),
          h(CheckField, { label: "@ 新成员 mention", checked: form.welcome.mention,
            onChange: function (v) { setWelcome("mention", v); } }),
          h(SelectField, { label: "模式 mode", value: form.welcome.mode,
            options: ["allowlist", "blocklist", "all"],
            onChange: function (v) { setWelcome("mode", v); } })),
        h(ChipListEditor, { label: "生效群 groups", kind: "group",
          value: form.welcome.groups, onChange: function (v) { setWelcome("groups", v); } }),
        h(TextField, { label: "欢迎语模板", value: form.welcome.template, rows: 5,
          hint: "可写 {name}；运行时若缺失会自动补在末尾（只影响生效值，不改写本文件）。",
          onChange: function (v) { setWelcome("template", v); } }),
        h("div", { className: "wppc-grid2" },
          h(InputField, { label: "静默时段（小时）", value: form.welcome.quietHoursText,
            placeholder: "例如 23-7 或 0,1,2",
            hint: "提交前展开成 0–23 的整数数组（跨午夜区间如 23-7 可用）。",
            onChange: function (v) { setWelcome("quietHoursText", v); } }),
          h(InputField, { label: "单批 @ 上限 maxBatch", type: "number", min: 1, max: 20,
            value: form.welcome.maxBatch,
            onChange: function (v) { setWelcome("maxBatch", Number(v)); } })),
        account ? h("div", { className: "wppc-hint" },
          "运行时生效值：" + JSON.stringify(account.joinWelcome.effective)) : null,
        h("div", { className: "wppc-actions" },
          h("button", { type: "button", className: "wppc-btn wppc-btn--primary",
            disabled: saving === "welcome" || !sectionDirty(form, baselineForm, "welcome"),
            onClick: function () { save("welcome"); } },
            saving === "welcome" ? "保存中…" : "保存欢迎语")));

    var featuresBody = !form ? h("div", { className: "wppc-empty" }, "加载中…") :
      h("div", { className: "wppc-stack" },
        h(Section, { title: "基础能力" },
          h("div", { className: "wppc-features" }, FEATURE_KEYS.map(function (k) {
            var disabled = k === "heartflow" && account && account.heartflowModelConfigured === false;
            return h("div", { key: k, className: "wppc-feature" },
              h(CheckField, { label: FEATURE_LABELS[k] + "（" + k + "）",
                checked: !!form.features[k], disabled: disabled,
                hint: disabled ? "未配置 heartflow.model，先配置后再开启。" : "",
                onChange: function (v) { setFeature(k, v); } }));
          }), h("div", { className: "wppc-feature" },
            h(CheckField, { label: "小薇 xiaoweiEnabled", checked: !!form.features.xiaowei,
              onChange: function (v) { setFeature("xiaowei", v); } })))),
        h(Section, { title: "高级开关（高风险，写回后仍受运行时门禁）" },
          h("div", { className: "wppc-features" }, ADVANCED_KEYS.map(function (k) {
            return h("div", { key: k, className: "wppc-feature" },
              h(CheckField, { label: ADVANCED_LABELS[k] + "（" + k + "）",
                checked: !!form.features[k],
                onChange: function (v) {
                  if (v && (k === "friendCirclePublishEnabled" || k === "adminToolsEnabled")) {
                    askConfirm({
                      title: "开启高风险能力？",
                      message: ADVANCED_LABELS[k] + " 会扩大机器人的外部动作范围。确认开启？",
                      confirmLabel: "开启",
                      danger: true,
                      onConfirm: function () { setFeature(k, true); },
                    });
                  } else {
                    setFeature(k, v);
                  }
                } }));
          })),
          h(InputField, { label: "防抖 debounceMs（毫秒，0–10000）", type: "number",
            min: 0, max: 10000, value: form.features.debounceMs,
            onChange: function (v) { setFeature("debounceMs", Number(v)); } })),
        h("div", { className: "wppc-actions" },
          h("button", { type: "button", className: "wppc-btn wppc-btn--primary",
            disabled: saving === "features" || !sectionDirty(form, baselineForm, "features"),
            onClick: function () { save("features"); } },
            saving === "features" ? "保存中…" : "保存能力开关")));

    var heartflowBody = !form ? h("div", { className: "wppc-empty" }, "加载中…") :
      h("div", { className: "wppc-stack" },
        h(Section, { title: "心流阈值与白名单" },
          account && account.heartflowWarnings && account.heartflowWarnings.length
            ? h("div", { className: "wppc-warn" },
              h("ul", null, account.heartflowWarnings.map(function (w, i) {
                return h("li", { key: i }, w);
              }))) : null,
          h("div", { className: "wppc-grid2" },
            h(InputField, { label: "回复阈值 replyThreshold（0–1）", type: "number",
              min: 0, max: 1, step: 0.01, value: form.heartflow.thresholdText,
              placeholder: "未设置（运行时默认 0.6）",
              hint: "留空表示沿用运行时默认值；只有填写后才会写回账号文件。",
              onChange: function (v) { setHeartflow("thresholdText", v); } }),
            h(CheckField, { label: "启用群画像学习 profile.enabled",
              checked: form.heartflow.profileEnabled,
              onChange: function (v) { setHeartflow("profileEnabled", v); } })),
          h(ChipListEditor, { label: "心流白名单 whitelistGroups", kind: "group",
            value: form.heartflow.whitelistGroups,
            hint: "为空 = 所有群都允许心流。群还需要通过 groupPolicy/groupAllowFrom 的准入。",
            onChange: function (v) { setHeartflow("whitelistGroups", v); } }),
          h("div", { className: "wppc-hint" },
            "心流总开关在「能力开关」页；这里只编辑阈值与白名单，避免两条写路径。"),
          h("div", { className: "wppc-actions" },
            h("button", { type: "button", className: "wppc-btn wppc-btn--primary",
              disabled: saving === "heartflow" || !sectionDirty(form, baselineForm, "heartflow"),
              onClick: function () { save("heartflow"); } },
              saving === "heartflow" ? "保存中…" : "保存心流配置"))),
        h(HeartflowDiagnostics, { accountId: selected, active: tab === "heartflow" }),
        h(InteractionPanel, { accountId: selected, active: tab === "heartflow" }));

    return h("div", { className: "wppc-page" },
      h("header", { className: "wppc-top" },
        h("div", null,
          h("h2", { className: "wppc-title" }, "微信助手控制台",
            h("span", { className: "wppc-version" }, "v" + (overview.version || ""))),
          h("div", { className: "wppc-sub" },
            accounts.length ? null : "没有账号文件；请先配置 accounts/*.json")),
        h("div", { className: "wppc-top-right" },
          h("label", { className: "wppc-inline" }, "账号 ",
            h("select", { className: "wppc-input wppc-select", value: selected,
              onChange: function (e) { selectAccount(e.target.value); } },
              accounts.map(function (a) {
                return h("option", { key: a.id, value: a.id },
                  (a.nickname || a.id) + "（" + a.id + "）");
              }))),
          h(StatusBadge, { state: status.state }),
          h("span", { className: "wppc-inline" }, "WS ",
            h("span", { className: "wppc-dot" + (acctStatus.ws_connected ? " wppc-dot--ok" : "") }),
            acctStatus.ws_connected ? "已连" : "未连"),
          dirtySections.length ? h("span", { className: "wppc-badge wppc-badge--delayed" },
            dirtySections.length + " 处未保存") : null,
          h("button", { type: "button", className: "wppc-btn", onClick: refreshAll }, "刷新"),
          h("button", { type: "button", className: "wppc-btn wppc-btn--ghost",
            "aria-label": "快捷键", onClick: function () { setShortcutsOpen(true); } }, "?"))),

      notice ? h("div", { className: "wppc-notice" }, notice) : null,
      pageError ? h("div", { className: "wppc-error" }, pageError) : null,
      remoteChange ? h("div", { className: "wppc-remote-banner" },
        "配置文件已被其他会话修改（当前编辑基于旧版本）。",
        h("button", { type: "button", className: "wppc-btn", onClick: function () {
          askConfirm({
            title: "刷新配置？", message: "会丢弃当前未保存的修改。",
            confirmLabel: "刷新并丢弃", danger: true,
            onConfirm: function () {
              setConflict(null);
              setRemoteChange(false);
              loadDetail(selectedRef.current, true);
            },
          });
        } }, "刷新配置"),
        h("button", { type: "button", className: "wppc-btn wppc-btn--ghost",
          onClick: function () { setRemoteChange(false); } }, "继续编辑")) : null,
      pendingEntry ? h("div", { className: "wppc-notice wppc-notice--" + (pendingEntry.timedOut ? "wait" : "wait") },
        pendingEntry.timedOut
          ? "配置已写盘，但网关 30 秒内未确认热加载；网关可能未运行或账号未连接。"
          : "已写盘，等待网关热加载（每 3 秒检查账号文件）…") : null,

      h("div", { className: "wppc-stats" },
        h("div", { className: "wppc-stat" }, h("b", null,
          stats.totals ? fmtNum(stats.totals.inbound + stats.totals.outbound) : "—"),
          h("span", null, "近 24h 收发（群）")),
        h("div", { className: "wppc-stat" }, h("b", null, stats.totals ? fmtNum(stats.totals.inbound) : "—"),
          h("span", null, "入站")),
        h("div", { className: "wppc-stat" }, h("b", null, stats.totals ? fmtNum(stats.totals.outbound) : "—"),
          h("span", null, "出站")),
        h("div", { className: "wppc-stat" }, h("b", null, fmtNum(stats.group_count || 0)),
          h("span", null, "有记录的群")),
        h("div", { className: "wppc-stat" }, h("b", null,
          acctStatus.last_reload_at ? fmtTime(acctStatus.last_reload_at) : "—"),
          h("span", null, "最近热加载"))),

      warnings.length ? h("div", { className: "wppc-warn" },
        h("b", null, "注意"), h("ul", null, warnings.map(function (w, i) {
          return h("li", { key: i }, w);
        }))) : null,

      h("div", { className: "wppc-tabbar", role: "tablist" }, TABS.map(function (t) {
        var dirty = t[0] !== "overview" && t[0] !== "groups" && t[0] !== "messages" &&
          sectionDirty(form, baselineForm, t[0]);
        return h("button", {
          key: t[0], role: "tab", id: "wppc-tab-" + t[0],
          "aria-selected": tab === t[0] ? "true" : "false",
          className: "wppc-tab" + (tab === t[0] ? " wppc-tab--active" : ""),
          onClick: function () { setTab(t[0]); },
        }, t[1], dirty ? h("span", { className: "wppc-tab-dot" }) : null);
      })),

      panel("overview", h(OverviewTab, {
        accountId: selected, active: tab === "overview",
        overview: overview, selected: selected,
        notify: notify, askConfirm: askConfirm,
        onSelect: selectAccount, onReloaded: onReloaded,
      })),
      panel("policy", policyBody),
      panel("welcome", welcomeBody),
      panel("features", featuresBody),
      panel("groups", h(GroupsTab, {
        accountId: selected, active: tab === "groups", account: account,
        notify: notify, askConfirm: askConfirm,
        onConflict: function (e) { setConflict({ section: "policy", message: e.message,
          currentMtimeNs: e.currentMtimeNs }); },
        onAccountChanged: onAccountChanged,
      })),
      panel("messages", h(MessagesTab, { accountId: selected, active: tab === "messages" })),
      panel("heartflow", heartflowBody),

      dirtySections.length ? h("div", { className: "wppc-dirty-bar" },
        h("span", null, "未保存：" + dirtySections.map(function (s) { return SECTION_LABELS[s]; }).join("、")),
        h("div", { className: "wppc-dirty-actions" }, dirtySections.map(function (s) {
          return h("span", { key: s, className: "wppc-dirty-group" },
            h("button", { type: "button", className: "wppc-btn wppc-btn--ghost",
              onClick: function () { resetSection(s); } }, "重置"),
            h("button", { type: "button", className: "wppc-btn wppc-btn--primary",
              disabled: !!saving, onClick: function () { save(s); } },
              saving === s ? "保存中…" : "保存" + SECTION_LABELS[s]));
        }))) : null,

      confirmState ? h(Modal, { title: confirmState.title,
        onClose: function () { setConfirmState(null); },
        footer: h("div", { className: "wppc-actions" },
          h("button", { type: "button", className: "wppc-btn",
            onClick: function () { setConfirmState(null); } }, "取消"),
          h("button", { type: "button",
            className: "wppc-btn " + (confirmState.danger ? "wppc-btn--danger" : "wppc-btn--primary"),
            onClick: function () {
              var fn = confirmState.onConfirm;
              setConfirmState(null);
              if (fn) fn();
            } }, confirmState.confirmLabel || "确定")) },
        h("div", null, confirmState.message || "")) : null,

      conflict ? h(Modal, { title: "配置已被其他写入者修改",
        onClose: function () { setConflict(null); },
        footer: h("div", { className: "wppc-actions" },
          h("button", { type: "button", className: "wppc-btn",
            onClick: function () { setConflict(null); } }, "保留本地修改"),
          h("button", { type: "button", className: "wppc-btn wppc-btn--primary",
            onClick: function () {
              var section = conflict.section;
              setConflict(null);
              loadDetail(selectedRef.current, true);
              setRemoteChange(false);
              notify("info", SECTION_LABELS[section] + "已重新加载，本地修改已丢弃");
            } }, "重新加载（丢弃本地修改）")) },
        h("div", null,
          h("p", null, conflict.message || "账号文件的 mtime 已变化。"),
          h("p", { className: "wppc-hint" },
            "基准 mtime：" + baselineMtime + "　服务器当前 mtime：" + (conflict.currentMtimeNs || "—")),
          h("p", { className: "wppc-hint" },
            "为避免覆盖另一个会话的修改，控制台不提供“强制覆盖”。"))) : null,

      shortcutsOpen ? h(Modal, { title: "键盘快捷键", onClose: function () { setShortcutsOpen(false); },
        footer: h("div", { className: "wppc-actions" },
          h("button", { type: "button", className: "wppc-btn",
            onClick: function () { setShortcutsOpen(false); } }, "关闭")) },
        h("ul", { className: "wppc-shortcuts" },
          h("li", null, h("kbd", null, "Ctrl/Cmd+S"), " 保存当前未保存的页签"),
          h("li", null, h("kbd", null, "/"), " 聚焦当前页签搜索框"),
          h("li", null, h("kbd", null, "r"), " 刷新当前数据"),
          h("li", null, h("kbd", null, "?"), " 打开本对话框"),
          h("li", null, h("kbd", null, "Alt+1…7"), " 切换页签"),
          h("li", null, h("kbd", null, "Esc"), " 关闭弹窗/抽屉"))) : null,

      h("div", { className: "wppc-toasts", "aria-live": "polite" }, toasts.map(function (t) {
        return h("div", { key: t.id, className: "wppc-toast wppc-toast--" + t.type },
          t.text,
          h("button", { type: "button", className: "wppc-toast-close", "aria-label": "关闭",
            onClick: function () {
              setToasts(function (list) { return list.filter(function (x) { return x.id !== t.id; }); });
            } }, "×"));
      })));
  }

  function AppRoot() {
    return h(ErrorBoundary, null, h(ConsolePage, null));
  }

  if (window.__HERMES_PLUGINS__ && typeof window.__HERMES_PLUGINS__.register === "function") {
    window.__HERMES_PLUGINS__.register("wechatpadpro-console", AppRoot);
  }
})();
