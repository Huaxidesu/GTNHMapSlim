(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };

  var state = {
    root: "",
    units: [],
    selected: new Set(),
    running: false,
    timer: null
  };

  // -- 工具 -------------------------------------------------------------

  function fmtNum(n) {
    return (n || 0).toLocaleString("zh-CN");
  }

  function fmtBytes(n) {
    n = n || 0;
    if (n < 1024) return n + " B";
    var units = ["KiB", "MiB", "GiB", "TiB"];
    var i = -1;
    do { n /= 1024; i++; } while (n >= 1024 && i < units.length - 1);
    return n.toFixed(n >= 100 ? 0 : 1) + " " + units[i];
  }

  function fmtDur(sec) {
    sec = Math.max(0, Math.round(sec || 0));
    var h = Math.floor(sec / 3600);
    var m = Math.floor((sec % 3600) / 60);
    var s = sec % 60;
    if (h > 0) return h + " 小时 " + m + " 分";
    if (m > 0) return m + " 分 " + s + " 秒";
    return s + " 秒";
  }

  function shortPath(p, max) {
    max = max || 78;
    if (!p) return "";
    return p.length <= max ? p : "…" + p.slice(p.length - max);
  }

  function post(path, body) {
    return fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {})
    }).then(function (r) { return r.json(); });
  }

  function hint(text, kind) {
    var el = $("pick-hint");
    el.textContent = text || "";
    el.className = "hint" + (kind ? " " + kind : "");
  }

  // -- 载入目录 ---------------------------------------------------------

  function loadPath(path) {
    path = (path || "").trim().replace(/^"|"$/g, "");
    if (!path) return;
    hint("正在检查目录…");
    $("candidates").innerHTML = "";
    $("btn-run").disabled = true;

    post("/api/discover", { root: path }).then(function (data) {
      if (data.error) {
        hint(data.error, "error");
        $("root-tag").textContent = "未选择";
        $("worlds-card").classList.add("hidden");
        $("options-card").classList.add("hidden");
        $("actions").classList.add("hidden");
        return;
      }
      state.root = data.root;
      state.units = data.units;
      state.selected = new Set(data.units.map(function (u) { return u.region_dir; }));
      $("path-input").value = data.root;
      $("root-tag").textContent = data.root;

      var t = data.totals;
      hint("找到 " + t.dirs + " 个世界，共 " + fmtNum(t.files) + " 个 region 文件，" +
           fmtBytes(t.bytes) + "。", "ok");

      renderUnits();
      $("worlds-card").classList.remove("hidden");
      $("options-card").classList.remove("hidden");
      $("actions").classList.remove("hidden");
      updateBackupHint();
      updateRunState();
    }).catch(function (err) {
      hint("无法连接本地服务：" + err.message, "error");
    });
  }

  // -- 世界列表 ---------------------------------------------------------

  function renderUnits() {
    var box = $("worlds");
    box.innerHTML = "";

    state.units.forEach(function (u) {
      var row = document.createElement("label");
      row.className = "world";

      var cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = state.selected.has(u.region_dir);
      cb.addEventListener("change", function () {
        if (cb.checked) state.selected.add(u.region_dir);
        else state.selected.delete(u.region_dir);
        updateRunState();
        updateFoot();
      });

      var name = document.createElement("span");
      name.className = "w-name";
      name.textContent = u.name;

      var rel = document.createElement("span");
      rel.className = "w-rel";
      rel.textContent = u.rel === "." ? "（根目录）" : u.rel;
      rel.title = u.region_dir;

      var meta = document.createElement("span");
      meta.className = "w-meta";
      meta.textContent = fmtNum(u.files) + " 文件 · " + fmtBytes(u.bytes);

      row.appendChild(cb);
      row.appendChild(name);
      row.appendChild(rel);
      row.appendChild(meta);
      box.appendChild(row);
    });

    updateFoot();
  }

  function updateFoot() {
    var sel = state.units.filter(function (u) { return state.selected.has(u.region_dir); });
    var files = sel.reduce(function (a, u) { return a + u.files; }, 0);
    var bytes = sel.reduce(function (a, u) { return a + u.bytes; }, 0);
    $("worlds-foot").textContent =
      "已选 " + sel.length + " / " + state.units.length + " 个世界，" +
      fmtNum(files) + " 个文件，" + fmtBytes(bytes);
  }

  function updateBackupHint() {
    var sel = state.units.filter(function (u) { return state.selected.has(u.region_dir); });
    var bytes = sel.reduce(function (a, u) { return a + u.bytes; }, 0);
    var el = $("backup-hint");
    if ($("backup").checked) {
      el.textContent = "备份为整文件复制，最坏情况额外占用约 " + fmtBytes(bytes) +
                       "，请确认目标磁盘空间充足。";
    } else {
      el.textContent = "已关闭备份，误操作无法回滚。";
    }
  }

  function updateRunState() {
    var btn = $("btn-run");
    if (state.running) { btn.disabled = true; return; }
    btn.disabled = state.selected.size === 0 || !state.root;
  }

  // -- 执行 -------------------------------------------------------------

  function start() {
    if (state.running) return;
    var minutes = parseFloat($("minutes").value);
    if (!(minutes > 0)) { hint("阈值必须大于 0", "error"); return; }

    var mode = document.querySelector('input[name=mode]:checked').value;
    var payload = {
      root: state.root,
      units: Array.from(state.selected),
      minutes: minutes,
      mode: mode,
      backup: $("backup").checked
    };

    state.running = true;
    updateRunState();
    $("btn-run").disabled = true;
    $("result").innerHTML = "";
    $("progress-card").classList.remove("hidden");
    $("btn-cancel").classList.remove("hidden");
    $("btn-cancel").disabled = false;
    $("prog-state").textContent = "正在启动…";
    $("prog-percent").textContent = "0%";
    $("bar-fill").style.width = "0%";
    $("bar-fill").classList.remove("done");
    $("current").textContent = "";

    post("/api/start", payload).then(function (data) {
      if (data.error) {
        finishWith({ state: "failed", message: data.error });
        return;
      }
      state.timer = setInterval(poll, 300);
      poll();
    }).catch(function (err) {
      finishWith({ state: "failed", message: err.message });
    });
  }

  function poll() {
    fetch("/api/progress").then(function (r) { return r.json(); }).then(function (s) {
      if (s.state === "idle") return;
      render(s);
      if (s.state !== "running") {
        clearInterval(state.timer);
        state.timer = null;
        state.running = false;
        updateRunState();
        finishWith(s);
      }
    }).catch(function () { /* 服务短暂不可用时跳过本次轮询 */ });
  }

  function render(s) {
    var pct = s.percent || 0;
    $("prog-percent").textContent = pct.toFixed(pct >= 10 ? 0 : 1) + "%";
    $("bar-fill").style.width = Math.min(100, pct) + "%";
    $("st-files").textContent = fmtNum(s.files_done) + " / " + fmtNum(s.files_total);
    $("st-deleted").textContent = fmtNum(s.deleted);
    $("st-freed").textContent = fmtBytes(s.freed);
    $("st-speed").textContent = s.speed ? s.speed.toFixed(0) + " 文件/秒" : "-";
    $("st-eta").textContent = s.eta ? fmtDur(s.eta) : "-";
    $("current").textContent = shortPath(s.current);
    $("current").title = s.current || "";
  }

  function finishWith(s) {
    state.running = false;
    updateRunState();
    $("btn-cancel").classList.add("hidden");

    var ok = s.state === "done";
    if (ok) {
      $("prog-state").textContent = "已完成";
      $("prog-percent").textContent = "100%";
      $("bar-fill").style.width = "100%";
      $("bar-fill").classList.add("done");
    } else if (s.state === "cancelled") {
      $("prog-state").textContent = "已取消";
      $("bar-fill").classList.add("done");
    } else {
      $("prog-state").textContent = "执行失败";
    }

    var rows = [];
    if (s.files_done) rows.push(["处理文件", fmtNum(s.files_done) + " 个"]);
    if (s.deleted) rows.push(["删除区块", fmtNum(s.deleted) + " 个"]);
    if (s.freed) rows.push(["回收空间", fmtBytes(s.freed)]);
    if (s.elapsed) rows.push(["耗时", fmtDur(s.elapsed)]);
    if (s.errors) rows.push(["出错文件", fmtNum(s.errors) + " 个"]);
    if (s.backup) rows.push(["备份目录", s.backup]);

    var html = rows.map(function (r) {
      return '<div class="line"><span>' + r[0] + "</span><span>" +
             escapeHtml(r[1]) + "</span></div>";
    }).join("");

    if (s.message) {
      html += '<div class="msg ' + (ok ? "ok" : "err") + '">' +
              escapeHtml(s.message) + "</div>";
    } else if (ok) {
      html += '<div class="msg ok">精简完成，可以重新启动服务器。</div>';
    }
    $("result").innerHTML = html;
  }

  function escapeHtml(text) {
    return String(text).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  // -- 拖放 -------------------------------------------------------------

  function bindDrop() {
    var zone = $("dropzone");

    ["dragenter", "dragover"].forEach(function (evt) {
      zone.addEventListener(evt, function (e) {
        e.preventDefault();
        e.stopPropagation();
        zone.classList.add("over");
      });
    });

    ["dragleave", "dragend"].forEach(function (evt) {
      zone.addEventListener(evt, function (e) {
        e.preventDefault();
        zone.classList.remove("over");
      });
    });

    zone.addEventListener("drop", function (e) {
      e.preventDefault();
      e.stopPropagation();
      zone.classList.remove("over");

      var file = e.dataTransfer.files && e.dataTransfer.files[0];
      // 原生窗口宿主（如 pywebview）会附带真实路径，优先使用
      var full = file && file.pywebviewFullPath;
      if (full) { loadPath(full); return; }

      var name = "";
      var items = e.dataTransfer.items;
      if (items && items.length && items[0].webkitGetAsEntry) {
        var entry = items[0].webkitGetAsEntry();
        if (entry) name = entry.name;
      }
      if (!name && file) name = file.name;
      if (!name) { hint("没有识别到文件夹", "error"); return; }

      hint("浏览器不会提供拖入目录的绝对路径，正在按名称「" + name + "」定位…");
      post("/api/resolve", { name: name }).then(function (data) {
        var list = data.candidates || [];
        var box = $("candidates");
        box.innerHTML = "";

        if (list.length === 1) {
          loadPath(list[0]);
          return;
        }
        if (list.length === 0) {
          hint("没有找到名为「" + name + "」的目录，请使用「选择文件夹」或直接粘贴路径。", "error");
          return;
        }

        hint("找到 " + list.length + " 个同名目录，请选择：");
        list.forEach(function (p) {
          var btn = document.createElement("button");
          btn.type = "button";
          btn.className = "candidate";
          btn.textContent = p;
          btn.title = p;
          btn.addEventListener("click", function () {
            box.innerHTML = "";
            loadPath(p);
          });
          box.appendChild(btn);
        });
      });
    });

    ["dragover", "drop"].forEach(function (evt) {
      window.addEventListener(evt, function (e) { e.preventDefault(); });
    });
  }

  // -- 绑定 -------------------------------------------------------------

  function init() {
    bindDrop();

    $("btn-pick").addEventListener("click", function () {
      hint("正在打开选择窗口…");
      post("/api/pick", {}).then(function (data) {
        if (data.path) { loadPath(data.path); }
        else { hint("已取消选择"); }
      }).catch(function (err) { hint("打开失败：" + err.message, "error"); });
    });

    $("btn-load").addEventListener("click", function () { loadPath($("path-input").value); });
    $("path-input").addEventListener("keydown", function (e) {
      if (e.key === "Enter") loadPath($("path-input").value);
    });

    $("btn-all").addEventListener("click", function () {
      state.units.forEach(function (u) { state.selected.add(u.region_dir); });
      renderUnits(); updateRunState(); updateBackupHint();
    });

    $("btn-none").addEventListener("click", function () {
      state.selected.clear();
      renderUnits(); updateRunState(); updateBackupHint();
    });

    $("minutes").addEventListener("input", function () {
      var m = parseFloat($("minutes").value) || 0;
      $("ticks-note").textContent = "约 " + fmtNum(Math.round(m * 1200)) + " ticks，低于该时长的区块会被删除";
    });

    $("backup").addEventListener("change", updateBackupHint);
    $("btn-run").addEventListener("click", start);
    $("btn-cancel").addEventListener("click", function () {
      $("btn-cancel").disabled = true;
      post("/api/cancel", {}).then(function () { $("prog-state").textContent = "正在取消…"; });
    });
  }

  document.addEventListener("DOMContentLoaded", init);
})();
