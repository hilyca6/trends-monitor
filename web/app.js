/* TrendRadar front-end: pure static, reads data/*.json */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };

  var NEW_TAG = '<span class="tag rt">🆕 新</span> ';
  var MARKET_LABELS = {
    greenfield: "空白市场", contested: "已有早期竞争",
    occupied: "已被占位", established: "成熟老词", unknown: "未确认",
  };

  function signalBadge(r) {
    var h = "";
    var s = r && r.signal;
    if (s) {
      if (s.classification === "breakout") h += '<span class="tag sig-hot">🚀 爆发</span> ';
      else if (s.classification === "rising") h += '<span class="tag sig-rise">📈 上涨</span> ';
      if (s.freshness === "new") h += '<span class="tag sig-new">🆕 新词</span> ';
      if (s.entityConflict) h += '<span class="tag sig-conflict">⚠️ 实体冲突</span> ';
      if (s.ratio) h += '<span class="tag sig-ratio">×' + s.ratio + ' 基准</span> ';
    }
    var f = r && r.fast;
    if (f) {
      var n = (f.newSuggestionCount || 0) + (f.newSerpPageCount || 0);
      if (n) h += '<span class="tag fast-badge">🔥 搜索新增 ' + n + "</span> ";
    }
    return h;
  }

  function metaBadges(r) {
    var h = "";
    if (r && (r.cat || r.subcat)) {
      h += '<span class="tag cat">' + esc([r.cat, r.subcat].filter(Boolean).join("/")) + "</span> ";
    }
    if (r && r.trigger) h += '<span class="tag trig">⚡' + esc(r.trigger) + "</span> ";
    return h;
  }

  function llmCatNames() {
    var set = {};
    [sitemap].forEach(function (d) {
      ((d && d.items) || []).forEach(function (r) { if (r.cat) set[r.cat] = true; });
    });
    return Object.keys(set).sort();
  }

  function buildDataCatBars() {
    var cats = llmCatNames();
    var html = '<button data-cat="all"' + (dataCat === "all" ? ' class="on"' : "") + ">全部分类</button>" +
      cats.map(function (c) {
        return '<button data-cat="' + esc(c) + '"' + (dataCat === c ? ' class="on"' : "") + ">" + esc(c) + "</button>";
      }).join("");
    ["sitemap-cat-bar"].forEach(function (id) {
      var el = $(id);
      if (el) el.innerHTML = html;
    });
  }

  function passDataCat(r) {
    return dataCat === "all" || r.cat === dataCat;
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function fmtVol(v) {
    if (!v) return "—";
    if (v >= 1e6) return (v / 1e6) + "M";
    if (v >= 1e3) return (v / 1e3) + "K";
    return String(v);
  }

  function rel(iso) {
    if (!iso) return "";
    var m = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
    if (m < 60) return m + " 分钟前";
    if (m < 1440) return Math.round(m / 60) + " 小时前";
    return Math.round(m / 1440) + " 天前";
  }

  function fetchJson(u) {
    return fetch(u).then(function (r) {
      if (!r.ok) throw new Error(r.status);
      return r.json();
    });
  }

  function starChars(n) {
    var s = "";
    for (var i = 1; i <= 5; i++) s += i <= n ? "★" : "☆";
    return s;
  }
  function starsSpan(adv, q) {
    if (!adv || !adv.stars) return "";
    var chars = starChars(adv.stars);
    var hasText = adv.audience || adv.main_keyword || (adv.directions || []).length;
    if (!hasText) return '<span class="stars">' + chars + "</span>";
    return '<span class="stars" data-q="' + esc(q) + '" data-adv="' +
      encodeURIComponent(JSON.stringify(adv)) + '">' + chars + "</span>";
  }
  function openAdvice(q, adv) {
    var lvl = adv.level ? ' <span class="tag level">' + esc(adv.level) + "</span>" : "";
    var h = "<h3>" + esc(q) + ' <span class="stars">' + starChars(adv.stars) + "</span>" + lvl + "</h3>";
    function r(label, val) {
      return val ? '<div class="adv-row"><span class="adv-k">' + label +
        '</span><span class="adv-v">' + esc(val) + "</span></div>" : "";
    }
    h += r("谁在搜", adv.audience);
    h += r("可做方向", (adv.directions || []).join(" / "));
    h += r("主攻词", adv.main_keyword);
    h += r("变现方式", (adv.monetize || []).join(" / "));
    h += r("窗口期", adv.window);
    h += r("风险", adv.risk);
    if (adv.market && adv.market.status) {
      var ml = MARKET_LABELS[adv.market.status] || adv.market.status;
      h += r("市场", ml + (adv.market.reason ? " · " + adv.market.reason : ""));
    }
    if (typeof adv.nameRisk === "number") h += r("命名风险", adv.nameRisk + "/30");
    h += r("依据", adv.evidence);
    var ops = adv.opportunities || [];
    if (ops.length) {
      h += '<div class="adv-row"><span class="adv-k">可做内页</span><span class="adv-v">' +
        ops.map(function (o) {
          return esc(o.query) + ' <span class="dim">' + esc(o.intent || "") +
            (o.priority ? " · " + esc(String(o.priority)) : "") + "</span>";
        }).join("<br>") + "</span></div>";
    }
    $("advice-body").innerHTML = h;
    $("advice-modal").hidden = false;
  }

  /* Catmull-Rom -> bezier smooth sparkline */
  function smoothPath(pts) {
    if (pts.length < 3) return "M" + pts.map(function (p) { return p[0] + "," + p[1]; }).join("L");
    var d = "M" + pts[0][0] + "," + pts[0][1];
    for (var i = 0; i < pts.length - 1; i++) {
      var p0 = pts[Math.max(0, i - 1)], p1 = pts[i], p2 = pts[i + 1], p3 = pts[Math.min(pts.length - 1, i + 2)];
      d += "C" + (p1[0] + (p2[0] - p0[0]) / 6) + "," + (p1[1] + (p2[1] - p0[1]) / 6) +
           " " + (p2[0] - (p3[0] - p1[0]) / 6) + "," + (p2[1] - (p3[1] - p1[1]) / 6) +
           " " + p2[0] + "," + p2[1];
    }
    return d;
  }

  var _sparkN = 0;
  function fmtDate(d) {
    var m = d.getMonth() + 1, day = d.getDate();
    return m + "-" + (day < 10 ? "0" + day : day);
  }
  /* 0-100 chart with y axis (0/50/100) and x axis (7-day window) labels */
  function gameChart(series, iso) {
    var W = 240, H = 60, n = series.length;
    if (n < 2) return "";
    var gid = "ggrad" + _sparkN++;
    var y0 = H - 2, top = 2;
    var yy = function (v) { return y0 - (Math.max(0, Math.min(100, v)) / 100) * (y0 - top); };
    var pts = series.map(function (v, i) {
      return [(i / (n - 1)) * W, yy(v)];
    });
    var line = smoothPath(pts);
    var area = line + "L" + W + "," + y0 + "L0," + y0 + "Z";
    var grid = [0, 50, 100].map(function (g) {
      var y = yy(g);
      return '<line x1="0" y1="' + y + '" x2="' + W + '" y2="' + y +
        '" stroke="#2b333d" stroke-width="1" vector-effect="non-scaling-stroke"/>';
    }).join("");
    var end = iso ? new Date(iso) : new Date();
    var start = new Date(end.getTime() - 7 * 864e5);
    var svg = '<svg viewBox="0 0 ' + W + " " + H + '" preserveAspectRatio="none">' + grid +
      '<defs><linearGradient id="' + gid + '" x1="0" y1="0" x2="0" y2="1">' +
      '<stop offset="0" stop-color="#2f81f7" stop-opacity="0.28"/>' +
      '<stop offset="1" stop-color="#2f81f7" stop-opacity="0.02"/></linearGradient></defs>' +
      '<path style="fill:url(#' + gid + ')" d="' + area + '"/>' +
      '<path class="gline" d="' + line + '" vector-effect="non-scaling-stroke"/></svg>';
    return '<div class="gchart"><div class="plot">' + svg +
      '<div class="xrow"><span>' + fmtDate(start) + "</span><span>至</span><span>" + fmtDate(end) + "</span></div>" +
      "</div></div>";
  }

  /* ---- state ---- */
  var view = "live", geo = "ALL", cat = "all", vol = 0, growth = 0, noise = false, sortBy = "vol";
  var trends = null, history = null, games = null, sitemap = null, gameradar = null, capcut = null;
  var gamesShown = 30, rowsShown = 200, sitemapShown = 200, gameradarShown = 200, capcutShown = 200;
  var sitemapNoise = false, gameradarNoise = false, dataCat = "all", hotOnly = false;
  var capcutSort = "growth3d", capcutNewOnly = false;
  var gamesTab = "trending-google";
  var CAPCUT_LEVEL = {
    surging: ['sig-hot', "🚀 爆发"], rising: ['sig-rise', "📈 上涨"],
    new: ['rt', "🆕 新"], up: ['', "📈 上涨"], steady: ['', ""],
  };

  function isHot(r) {
    var s = r && r.signal;
    if (s && (s.classification === "breakout" || s.classification === "rising"
      || s.freshness === "new" || s.entityConflict)) return true;
    var f = r && r.fast;
    return !!(f && f.classification === "pass");
  }

  function bySignal(a, b) {
    return ((b.signal && b.signal.score) || 0) - ((a.signal && a.signal.score) || 0)
      || ((b.vol || b.vol_peak || 0) - (a.vol || a.vol_peak || 0));
  }

  function buildBars() {
    var geos = ["ALL"].concat((trends && trends.geos) || []);
    $("geo-bar").innerHTML = geos.map(function (g) {
      return '<button data-geo="' + g + '"' + (g === geo ? ' class="on"' : "") + ">" +
        (g === "ALL" ? "🌐 全球" : g) + "</button>";
    }).join("");

    var cats = (trends && trends.cats) || {};
    $("cat-bar").innerHTML = '<button data-cat="all" class="on">全部分类</button>' +
      Object.keys(cats).map(function (c) {
        return '<button data-cat="' + c + '">' + esc(cats[c]) + "</button>";
      }).join("");

    $("vol-bar").innerHTML = [[0, "全部量"], [20000, "≥2万"], [100000, "≥10万"], [500000, "≥50万"]]
      .map(function (p) { return '<button data-vol="' + p[0] + '"' + (p[0] === vol ? ' class="on"' : "") + ">" + p[1] + "</button>"; })
      .join("");
    $("growth-bar").innerHTML = [[0, "全部增速"], [200, "≥200%"], [500, "≥500%"], [1000, "≥1000%"]]
      .map(function (p) { return '<button data-growth="' + p[0] + '"' + (p[0] === growth ? ' class="on"' : "") + ">" + p[1] + "</button>"; })
      .join("");
  }

  function catNames(ids) {
    var cats = (trends && trends.cats) || {};
    return (ids || []).map(function (c) { return cats[c]; }).filter(Boolean).slice(0, 2).join(" · ");
  }

  function trendsLink(q, g) {
    return "https://trends.google.com/trends/explore?date=now%20" +
      (view === "history" ? "7-d" : "1-d") + "&q=" + encodeURIComponent(q) +
      (g && g !== "ALL" ? "&geo=" + g : "");
  }

  function passFilters(r, v, g) {
    if (noise && r.noise) return false;
    if (cat !== "all" && (r.cats || []).indexOf(Number(cat)) < 0) return false;
    if (vol && (v || 0) < vol) return false;
    if (growth && (g || 0) < growth) return false;
    return true;
  }

  function rowHtml(r, showGeo, geoTxt, volTxt, growthVal, extra) {
    var rising = (r.related && r.related.rising) || [];
    var stars = starsSpan(r.advice, r.q);
    return "<tr><td>" + (stars ? stars + " " : "") + (r.new ? NEW_TAG : "") + signalBadge(r) + esc(r.q) +
      (r.zh ? '<div class="sub">' + esc(r.zh) + "</div>" : "") +
      (rising.length ? '<div class="sub rel">↑ ' + esc(rising.slice(0, 4).join(" · ")) + "</div>" : "") + "</td>" +
      (showGeo ? '<td class="dim">' + esc(geoTxt) + "</td>" : "") +
      '<td class="dim hide-sm">' + (catNames(r.cats) || "—") +
      (r.noise ? ' <span class="tag noise">🔇' + esc(r.noise) + "</span>" : "") + "</td>" +
      '<td class="num">' + volTxt + "</td>" +
      '<td class="num ' + ((growthVal || 0) >= 1000 ? "hot" : "up") + '">' +
      (growthVal ? "+" + growthVal + "%" : "—") + "</td>" +
      (extra || "") +
      '<td><a href="' + trendsLink(r.q, r._geo) + '" target="_blank" rel="noopener">趋势</a></td></tr>';
  }

  function moreBtn(total, id) {
    if (total <= rowsShown) return "";
    return '<button class="more" id="' + id + '">显示更多(共 ' + total + " 条)</button>";
  }

  function renderTrends() {
    var el = $("trend-table");
    if (!trends) { el.innerHTML = '<p class="empty">加载中…</p>'; return; }
    $("updated").textContent = "更新于 " + rel(view === "history" && history ? history.updated : trends.updated);

    var rows = [];
    var isAll = geo === "ALL";

    function starVal(r) { return (r.advice && r.advice.stars) || 0; }
    function byStars(a, b, key) {
      return starVal(b) - starVal(a) || ((b[key] || 0) - (a[key] || 0));
    }

    if (view === "live") {
      if (isAll) rows = mergeAll(trends.items, "vol", "growth");
      else rows = (trends.items[geo] || []).map(function (r) { return Object.assign({}, r, { _geo: geo }); });
      rows = rows.filter(function (r) { return passFilters(r, r.vol, r.growth); });
      if (hotOnly) rows = rows.filter(isHot);
      if (sortBy === "stars") rows.sort(function (a, b) { return byStars(a, b, "vol"); });
      else if (sortBy === "signal") rows.sort(bySignal);
      if (!rows.length) { el.innerHTML = '<p class="empty">该筛选下暂无数据</p>'; return; }
      el.innerHTML = '<div class="table-wrap"><table><thead><tr><th>热搜词</th>' +
        (isAll ? "<th>地区</th>" : "") +
        '<th class="hide-sm">分类</th><th class="num">搜索量</th><th class="num">涨幅</th><th></th></tr></thead><tbody>' +
        rows.slice(0, rowsShown).map(function (r) {
          return rowHtml(r, isAll, (r._geos || [r._geo]).slice(0, 6).join(" "), fmtVol(r.vol), r.growth);
        }).join("") + "</tbody></table></div>" + moreBtn(rows.length, "rows-more");
      return;
    }

    if (!history) {
      el.innerHTML = '<p class="empty">加载中…</p>';
      loadHistory(function () { renderTrends(); });
      return;
    }
    var items = history.items || [];
    if (isAll) rows = mergeAll(items, "vol_peak", "growth_peak", true);
    else rows = items.filter(function (r) { return r.geo === geo; })
      .map(function (r) { return Object.assign({}, r, { _geo: r.geo }); });
    rows = rows.filter(function (r) { return passFilters(r, r.vol_peak, r.growth_peak); });
    if (hotOnly) rows = rows.filter(isHot);
    if (sortBy === "stars") rows.sort(function (a, b) { return byStars(a, b, "vol_peak"); });
    else if (sortBy === "signal") rows.sort(bySignal);
    if (!rows.length) { el.innerHTML = '<p class="empty">该筛选下暂无留档</p>'; return; }
    el.innerHTML = '<div class="table-wrap"><table><thead><tr><th>热搜词</th>' +
      (isAll ? "<th>地区</th>" : "") +
      '<th class="hide-sm">分类</th><th class="num">峰值量</th><th class="num">峰值涨幅</th><th>最后在榜</th><th></th></tr></thead><tbody>' +
      rows.slice(0, rowsShown).map(function (r) {
        return rowHtml(r, isAll, (r._geos || [r._geo]).slice(0, 6).join(" "), fmtVol(r.vol_peak), r.growth_peak,
          '<td class="dim">' + rel(r.last) + "</td>");
      }).join("") + "</tbody></table></div>" + moreBtn(rows.length, "rows-more");
  }

  function mergeAll(items, volKey, growthKey, isHistory) {
    var byQ = {};
    Object.keys(items).forEach(function (g) {
      (items[g] || []).forEach(function (r) {
        var q = r.q;
        if (!byQ[q]) {
          byQ[q] = Object.assign({}, r, { _geo: r.geo || g, _geos: [] });
        } else if ((r[volKey] || 0) > (byQ[q][volKey] || 0)) {
          var gs = byQ[q]._geos;
          byQ[q] = Object.assign({}, r, { _geo: r.geo || g, _geos: gs });
        }
        if (byQ[q]._geos.indexOf(g) < 0) byQ[q]._geos.push(g);
      });
    });
    return Object.keys(byQ).map(function (q) { return byQ[q]; })
      .sort(function (a, b) { return (b[volKey] || 0) - (a[volKey] || 0); });
  }

  function loadHistory(done) {
    fetchJson("data/history.json").then(function (d) {
      history = d;
      done();
      for (var i = 2; i <= (d.chunks || 1); i++) {
        fetchJson("data/history-p" + i + ".json").then(function (p) {
          history.items = history.items.concat(p.items || []);
          history.items.sort(function (a, b) { return (b.vol_peak || 0) - (a.vol_peak || 0); });
          if (view === "history") renderTrends();
        }).catch(function () {});
      }
    }).catch(function () { $("trend-table").innerHTML = '<p class="empty">历史数据暂不可用</p>'; });
  }

  function growthBadge(v) {
    if (!v) return '<span class="dim">—</span>';
    return '<span class="' + (v > 0 ? "up" : "dim") + '">+' + (Math.round(v * 1000) / 10) + "%</span>";
  }

  function gameCard(g, mode) {
    var snap = (g.chart_at || g.first || "").slice(0, 10);
    var ageDays = (Date.now() - new Date(g.first).getTime()) / 864e5;
    var block;
    if (mode === "self") {
      block = (g.history || []).length >= 2
        ? '<div class="gmeta">自建7天 ' + sparkline(g.history) + "</div>" +
          '<div class="gmeta">近1天 ' + growthBadge(g.growth1d) + " · 7天 " + growthBadge(g.growth7d) + "</div>"
        : '<div class="nochart">自建走势采集中…</div>';
    } else {
      block = (g.series || []).length > 1
        ? gameChart(g.series, g.chart_at || g.first) +
          '<div class="gmeta snapnote">📷 快照于 ' + snap + " · 此后走势不含在图内</div>"
        : '<div class="nochart">' + (ageDays < 7 ? "热度曲线采集中…" : "暂无热度曲线") + "</div>";
    }
    var badges = "";
    if (g.web_game) badges += '<span class="tag web">🌐 网页游戏</span> ';
    if (g.breakout) badges += '<span class="tag hot">🔥 爆发中</span> ';
    if (g.new) badges += NEW_TAG;
    var starLine = (g.advice && g.advice.stars)
      ? '<div class="gmeta">' + starsSpan(g.advice, g.name) + " 推荐指数</div>" : "";
    var times = "首次发现 " + rel(g.first);
    if (g.last && g.last !== g.first) times += " · 最新信号 " + rel(g.last);
    if (g.sightings > 1) times += " · ×" + g.sightings;
    if (g.source) times += " · " + esc(g.source);
    var metrics = [];
    if (g.rank && g.size) metrics.push("榜单 #" + g.rank + "/" + g.size);
    if (g.rise) metrics.push("环比 +" + Math.round(g.rise * 100) + "%");
    if (g.growth) metrics.push("涨幅 +" + g.growth + "%");
    var metricsHtml = metrics.length ? '<div class="gmeta metrics">' + metrics.join(" · ") + "</div>" : "";
    var srcLink = g.url
      ? ' · <a href="' + esc(g.url) + '" target="_blank" rel="noopener">来源</a>'
      : "";
    return '<div class="gcard"><h3>' + esc(g.name) + "</h3>" +
      (badges ? '<div class="gbadges">' + badges + "</div>" : "") +
      starLine +
      (g.zh ? '<div class="gmeta sub">' + esc(g.zh) + "</div>" : "") +
      '<div class="gmeta">' + times + "</div>" + metricsHtml + block +
      '<div class="gmeta" style="margin-top:8px"><a href="https://trends.google.com/trends/explore?date=now%207-d&q=' +
      encodeURIComponent(g.name) + '" target="_blank" rel="noopener">查看趋势 →</a>' + srcLink + "</div></div>";
  }

  function renderGames() {
    var el = $("game-cards");
    if (!games) { el.innerHTML = '<p class="empty">加载中…</p>'; return; }
    var parts = gamesTab.split("-");
    var group = parts[0], chart = parts[1] || "google";
    var isListed = group === "listed";
    var tab = (games.items || []).filter(function (g) { return (g.group === "listed") === isListed; });
    if (chart === "self") {
      tab.sort(function (a, b) {
        return (b.growth1d || 0) - (a.growth1d || 0) || (b.growth7d || 0) - (a.growth7d || 0);
      });
    }
    if ($("games-count")) $("games-count").textContent = "共 " + tab.length + " 个";
    if (!tab.length) { el.innerHTML = '<p class="empty">暂无数据（新游戏雷达待接入）</p>'; return; }
    el.innerHTML = '<div class="ggrid">' +
      tab.slice(0, gamesShown).map(function (g) { return gameCard(g, chart); }).join("") + "</div>";
    if (tab.length > gamesShown) {
      el.innerHTML += '<button class="more" id="games-more">显示更多</button>';
      $("games-more").onclick = function () { gamesShown += 60; renderGames(); };
    }
  }

  function relatedLink(q, g) {
    return "https://trends.google.com/trends/explore?date=now%207-d&q=" +
      encodeURIComponent(q) + (g ? "&geo=" + g : "");
  }

  function renderSitemap() {
    var el = $("sitemap-table");
    if (!sitemap) { el.innerHTML = '<p class="empty">加载中…</p>'; return; }
    $("sitemap-updated").textContent = "更新于 " + rel(sitemap.updated);
    var items = (sitemap.items || []).filter(function (r) { return !(sitemapNoise && r.noise) && passDataCat(r); });
    if (hotOnly) items = items.filter(isHot);
    if (!items.length) { el.innerHTML = '<p class="empty">暂无站点信号（在 config 的 sitemap.watch_urls 配置竞争站 Sitemap）</p>'; return; }
    el.innerHTML = '<div class="table-wrap"><table><thead><tr><th>抢先词</th>' +
      '<th class="num">收录站点</th><th class="num">7日新增</th>' +
      '<th class="hide-sm">收录域名</th><th class="hide-sm">首次发现</th><th></th></tr></thead><tbody>' +
      items.slice(0, sitemapShown).map(function (r) {
        var stars = starsSpan(r.advice, r.q);
        var doms = (r.domains || []).slice(0, 4).join(" · ");
        return "<tr><td>" + (stars ? stars + " " : "") + (r.new ? NEW_TAG : "") + signalBadge(r) +
          (r.noise ? '<span class="tag noise">🔇' + esc(r.noise) + "</span> " : "") +
          esc(r.q) +
          (r.cat || r.subcat || r.trigger ? '<div class="sub">' + metaBadges(r) + "</div>" : "") +
          (r.zh ? '<div class="sub">' + esc(r.zh) + "</div>" : "") + "</td>" +
          '<td class="num">' + (r.domain_count || 0) + "</td>" +
          '<td class="num ' + ((r.domain_count_7d || 0) > 0 ? "up" : "dim") + '">+' + (r.domain_count_7d || 0) + "</td>" +
          '<td class="dim hide-sm">' + esc(doms) + "</td>" +
          '<td class="dim hide-sm">' + (r.first ? rel(r.first) : "基线") + "</td>" +
          '<td><a href="' + relatedLink(r.q, "US") + '" target="_blank" rel="noopener">趋势</a></td></tr>';
      }).join("") + "</tbody></table></div>" +
      (items.length > sitemapShown
        ? '<button class="more" id="sitemap-more">显示更多(共 ' + items.length + " 条)</button>"
        : "");
  }

  function sparkline(hist) {
    if (!hist || hist.length < 2) return '<span class="dim">采集中</span>';
    var W = 96, H = 22, n = hist.length;
    var vals = hist.map(function (p) { return p[1]; });
    var min = Math.min.apply(null, vals), max = Math.max.apply(null, vals);
    var span = (max - min) || 1;
    var pts = hist.map(function (p, i) {
      var x = (i / (n - 1)) * W;
      var y = H - 2 - ((p[1] - min) / span) * (H - 4);
      return x.toFixed(1) + "," + y.toFixed(1);
    }).join(" ");
    var up = vals[vals.length - 1] >= vals[0];
    return '<svg class="spark ' + (up ? "spark-up" : "spark-down") +
      '" viewBox="0 0 ' + W + " " + H + '" preserveAspectRatio="none">' +
      '<polyline points="' + pts + '"/></svg>';
  }

  function renderCapcut() {
    var el = $("capcut-table");
    if (!capcut) { el.innerHTML = '<p class="empty">加载中…</p>'; return; }
    $("capcut-updated").textContent = "更新于 " + rel(capcut.updated);
    var items = (capcut.items || []).slice();
    if (capcutNewOnly) items = items.filter(function (r) { return r.new; });
    if (capcutSort === "uses") {
      items.sort(function (a, b) { return (b.uses || 0) - (a.uses || 0); });
    } else if (capcutSort === "delta") {
      items.sort(function (a, b) {
        return (b.deltaPerHour || 0) - (a.deltaPerHour || 0) ||
          (b.usesDelta || 0) - (a.usesDelta || 0) || (b.uses || 0) - (a.uses || 0);
      });
    } else {
      items.sort(function (a, b) {
        return (b.growth3d || 0) - (a.growth3d || 0) ||
          (b.usesGain3d || 0) - (a.usesGain3d || 0) || (b.uses || 0) - (a.uses || 0);
      });
    }
    if (!items.length) { el.innerHTML = '<p class="empty">暂无 CapCut 数据（等待采集）</p>'; return; }
    el.innerHTML = '<div class="table-wrap"><table><thead><tr><th>模板</th>' +
      '<th class="hide-sm">类型</th><th class="num">使用量</th>' +
      '<th class="hide-sm">近3天</th><th class="num">三天增速</th>' +
      '<th class="num">新增</th><th class="num">增速</th>' +
      '<th class="hide-sm">首次发现</th><th class="hide-sm">描述</th><th></th></tr></thead><tbody>' +
      items.slice(0, capcutShown).map(function (r) {
        var lv = CAPCUT_LEVEL[r.level] || CAPCUT_LEVEL.steady;
        var badge = lv[1] ? '<span class="tag ' + lv[0] + '">' + lv[1] + "</span> " : "";
        var typeBadge = r.type === "image"
          ? '<span class="tag cat">图片</span>' : '<span class="tag web">视频</span>';
        var statBits = [];
        if (r.likes) statBits.push("❤ " + fmtVol(r.likes) + (r.likesDelta ? " (+" + fmtVol(r.likesDelta) + ")" : ""));
        if (r.comments) statBits.push("💬 " + fmtVol(r.comments));
        var desc = r.desc || "";
        var descTxt = desc ? esc(desc.slice(0, 60)) + (desc.length > 60 ? "…" : "") : "—";
        return "<tr><td>" + (r.new ? NEW_TAG : "") + badge + esc(r.title || r.id) +
          (statBits.length ? '<div class="sub">' + statBits.join(" · ") + "</div>" : "") + "</td>" +
          '<td class="hide-sm">' + typeBadge + "</td>" +
          '<td class="num">' + fmtVol(r.uses) + "</td>" +
          '<td class="hide-sm">' + sparkline(r.history) + "</td>" +
          '<td class="num ' + ((r.growth3d || 0) >= 0.1 ? "hot" : "up") + '">' +
          (r.growth3d ? "+" + (Math.round(r.growth3d * 1000) / 10) + "%" : "—") + "</td>" +
          '<td class="num ' + ((r.usesDelta || 0) > 0 ? "up" : "dim") + '">' +
          ((r.usesDelta || 0) > 0 ? "+" + fmtVol(r.usesDelta) : "—") + "</td>" +
          '<td class="num ' + ((r.growth || 0) >= 0.1 ? "hot" : "up") + '">' +
          (r.growth ? "+" + (Math.round(r.growth * 1000) / 10) + "%" : "—") + "</td>" +
          '<td class="dim hide-sm">' + (r.baseline ? "基线" : rel(r.first)) + "</td>" +
          '<td class="dim hide-sm capcut-desc"' + (desc ? ' title="' + esc(desc) + '"' : "") + ">" + descTxt + "</td>" +
          "<td>" + (r.url
            ? '<a href="' + esc(r.url) + '" target="_blank" rel="noopener">来源</a>' : "") +
          "</td></tr>";
      }).join("") + "</tbody></table></div>" +
      (items.length > capcutShown
        ? '<button class="more" id="capcut-more">显示更多(共 ' + items.length + " 条)</button>"
        : "");
  }

  function renderGameRadar() {
    var el = $("gameradar-table");
    if (!gameradar) { el.innerHTML = '<p class="empty">加载中…</p>'; return; }
    $("gameradar-updated").textContent = "更新于 " + rel(gameradar.updated);
    var items = (gameradar.items || []).filter(function (r) { return !(gameradarNoise && r.noise); });
    if (!items.length) { el.innerHTML = '<p class="empty">暂无游戏名（等待采集，或平台抓取失败）</p>'; return; }
    el.innerHTML = '<div class="table-wrap"><table><thead><tr><th>游戏名</th>' +
      '<th class="hide-sm">来源平台</th><th class="num">竞争度</th><th class="hide-sm">市场</th>' +
      '<th class="num hide-sm">命名风险</th><th class="num">★</th><th class="hide-sm">首次发现</th><th></th></tr></thead><tbody>' +
      items.slice(0, gameradarShown).map(function (r) {
        var stars = starsSpan(r.advice, r.name);
        var seo = r.seo || {};
        var fast = r.fast || {};
        var market = (seo.market || {}).status;
        var lvl = (r.advice && r.advice.level) ? ' <span class="tag level">' + esc(r.advice.level) + "</span>" : "";
        var conflict = seo.entityConflict ? '<span class="tag sig-conflict">⚠️ 同名实体</span> ' : "";
        var opp = r.opportunity || {};
        var oppMap = { independent: "正式独立站", "test-now": "测试局", watch: "观察", reject: "淘汰" };
        var oppCls = { independent: "opp-independent", "test-now": "opp-test", watch: "opp-watch", reject: "opp-reject" };
        var tierBadge = oppMap[opp.decision]
          ? ' <span class="tag ' + (oppCls[opp.decision] || "") + '">机会 ' + oppMap[opp.decision] +
            (typeof opp.score === "number" ? " " + opp.score : "") + "</span>"
          : lvl;
        var intent = seo.gameIntent;
        var subs = "";
        if (intent && typeof intent.gameRatio === "number") {
          subs += '<div class="sub">游戏占比 ' + Math.round(intent.gameRatio * 100) + "%" +
            (intent.nonGameRatio ? " · 非游戏 " + Math.round(intent.nonGameRatio * 100) + "%" : "") +
            (intent.newsRatio ? " · 新闻 " + Math.round(intent.newsRatio * 100) + "%" : "") + "</div>";
        }
        if (typeof fast.onlinePlatformCount === "number") {
          subs += '<div class="sub">在线平台 ' + fast.onlinePlatformCount +
            " · 24h新增 " + (fast.sourceAdded24h || 0) +
            " · 快速层 " + esc(fast.classification || "—") + "</div>";
        }
        var yt = r.youtube || {};
        if (yt.videoCount || yt.totalViews) {
          subs += '<div class="sub">YouTube 7天 ' + (yt.videoCount || 0) + "视频 / " +
            (yt.channelCount || 0) + "频道 / " + Number(yt.totalViews || 0).toLocaleString("en-US") + "播放</div>";
        }
        var soc = r.social || {};
        if (soc.checkedAt || (soc.checkedProviders && soc.checkedProviders.length)) {
          var socLabel = { viral: "社媒爆发", pass: "社媒通过", watch: "社媒观察", weak: "社媒不足", pending: "待验证" }[soc.classification] || "待验证";
          subs += '<div class="sub">社媒 ' + (soc.crossPlatformCount || 0) + "平台 · " + esc(socLabel) +
            " · " + (soc.score || 0) + "分" + (soc.hasExternalSpillover ? " · 跨平台外溢" : "") + "</div>";
        }
        return "<tr><td>" + (stars ? stars + " " : "") + (r.new ? NEW_TAG : "") + signalBadge(r) + conflict + esc(r.name) +
          (r.zh ? '<div class="sub">' + esc(r.zh) + "</div>" : "") + subs + tierBadge + "</td>" +
          '<td class="dim hide-sm">' + esc((r.sources || []).join(" · ")) + "</td>" +
          '<td class="num">' + (typeof seo.score === "number" ? seo.score + (seo.label ? " " + esc(seo.label) : "") : "—") + "</td>" +
          '<td class="dim hide-sm">' + esc(market ? (MARKET_LABELS[market] || market) : "—") + "</td>" +
          '<td class="num hide-sm">' + (typeof seo.nameRisk === "number" ? seo.nameRisk : "—") + "</td>" +
          '<td class="num">' + ((r.advice && r.advice.stars) || "—") + "</td>" +
          '<td class="dim hide-sm">' + rel(r.first) + "</td>" +
          '<td><a href="' + relatedLink(r.name, "US") + '" target="_blank" rel="noopener">趋势</a>' +
          (r.url ? ' · <a href="' + esc(r.url) + '" target="_blank" rel="noopener">来源</a>' : "") + "</td></tr>";
      }).join("") + "</tbody></table></div>" +
      (items.length > gameradarShown
        ? '<button class="more" id="gameradar-more">显示更多(共 ' + items.length + " 条)</button>"
        : "");
  }

  /* ---- events ---- */
  $("tabs").addEventListener("click", function (ev) {
    var b = ev.target.closest("button[data-tab]");
    if (!b) return;
    Array.prototype.forEach.call(document.querySelectorAll("#tabs button"), function (x) {
      x.classList.toggle("on", x === b);
    });
    $("panel-trends").hidden = b.dataset.tab !== "trends";
    $("panel-sitemap").hidden = b.dataset.tab !== "sitemap";
    $("panel-games").hidden = b.dataset.tab !== "games";
    $("panel-gameradar").hidden = b.dataset.tab !== "gameradar";
    $("panel-capcut").hidden = b.dataset.tab !== "capcut";
    if (b.dataset.tab === "sitemap") renderSitemap();
    if (b.dataset.tab === "gameradar") renderGameRadar();
    if (b.dataset.tab === "capcut") renderCapcut();
  });

  $("view-bar").addEventListener("click", function (ev) {
    var vb = ev.target.closest && ev.target.closest("button[data-view]");
    if (vb) {
      view = vb.dataset.view;
      Array.prototype.forEach.call(document.querySelectorAll("#view-bar button[data-view]"), function (x) {
        x.classList.toggle("on", x === vb);
      });
    }
    var sb = ev.target.closest && ev.target.closest("button[data-sort]");
    if (sb) {
      sortBy = sb.dataset.sort;
      Array.prototype.forEach.call(document.querySelectorAll("#sort-bar button"), function (x) {
        x.classList.toggle("on", x === sb);
      });
    }
    if (vb || sb) renderTrends();
  });

  function bindBar(id, attr, setter) {
    $(id).addEventListener("click", function (ev) {
      var b = ev.target.closest("button[data-" + attr + "]");
      if (!b) return;
      setter(b.dataset[attr]);
      Array.prototype.forEach.call($(id).querySelectorAll("button"), function (x) {
        x.classList.toggle("on", x === b);
      });
      renderTrends();
    });
  }
  bindBar("geo-bar", "geo", function (v) { geo = v; });
  bindBar("cat-bar", "cat", function (v) { cat = v; });
  bindBar("vol-bar", "vol", function (v) { vol = Number(v); });
  bindBar("growth-bar", "growth", function (v) { growth = Number(v); });

  document.addEventListener("click", function (ev) {
    if (ev.target && ev.target.id === "rows-more") { rowsShown += 500; renderTrends(); }
    if (ev.target && ev.target.id === "sitemap-more") { sitemapShown += 500; renderSitemap(); }
    if (ev.target && ev.target.id === "gameradar-more") { gameradarShown += 500; renderGameRadar(); }
    if (ev.target && ev.target.id === "capcut-more") { capcutShown += 500; renderCapcut(); }
    var lg = $("legend");
    if (ev.target && ev.target.id === "legend-btn") { lg.hidden = !lg.hidden; return; }
    if (lg && !lg.hidden && !lg.contains(ev.target)) lg.hidden = true;
    var advEl = ev.target && ev.target.closest ? ev.target.closest("[data-adv]") : null;
    if (advEl) {
      openAdvice(advEl.getAttribute("data-q"), JSON.parse(decodeURIComponent(advEl.getAttribute("data-adv"))));
    }
  });

  $("advice-modal").addEventListener("click", function (ev) {
    if (ev.target && (ev.target.id === "advice-modal" || ev.target.id === "advice-close")) {
      $("advice-modal").hidden = true;
    }
  });

  $("noise-btn").addEventListener("click", function () {
    noise = !noise;
    this.classList.toggle("on", noise);
    renderTrends();
  });

  $("hot-btn").addEventListener("click", function () {
    hotOnly = !hotOnly;
    this.classList.toggle("on", hotOnly);
    renderTrends();
    renderGameRadar();
  });

  $("sitemap-noise-btn").addEventListener("click", function () {
    sitemapNoise = !sitemapNoise;
    this.classList.toggle("on", sitemapNoise);
    renderSitemap();
  });

  $("gameradar-noise-btn").addEventListener("click", function () {
    gameradarNoise = !gameradarNoise;
    this.classList.toggle("on", gameradarNoise);
    renderGameRadar();
  });

  $("capcut-sort-bar").addEventListener("click", function (ev) {
    var b = ev.target.closest("button[data-csort]");
    if (!b) return;
    capcutSort = b.dataset.csort;
    Array.prototype.forEach.call(this.querySelectorAll("button"), function (x) {
      x.classList.toggle("on", x === b);
    });
    renderCapcut();
  });

  $("capcut-new-btn").addEventListener("click", function () {
    capcutNewOnly = !capcutNewOnly;
    this.classList.toggle("on", capcutNewOnly);
    renderCapcut();
  });

  $("games-sub-bar").addEventListener("click", function (ev) {
    var b = ev.target.closest("button[data-gsub]");
    if (!b) return;
    gamesTab = b.dataset.gsub;
    gamesShown = 30;
    Array.prototype.forEach.call(this.querySelectorAll("button"), function (x) {
      x.classList.toggle("on", x === b);
    });
    renderGames();
  });

  ["sitemap-cat-bar"].forEach(function (id) {
    var el = $(id);
    if (!el) return;
    el.addEventListener("click", function (ev) {
      var b = ev.target.closest("button[data-cat]");
      if (!b) return;
      dataCat = b.dataset.cat;
      buildDataCatBars();
      if (!$("panel-sitemap").hidden) renderSitemap();
    });
  });

  /* ---- bootstrap ---- */
  fetchJson("data/trends.json").then(function (d) {
    trends = d;
    buildBars();
    renderTrends();
  }).catch(function () {
    $("trend-table").innerHTML = '<p class="empty">数据加载失败，稍后再试</p>';
  });
  fetchJson("data/games.json").then(function (d) { games = d; renderGames(); }).catch(function () {});
  fetchJson("data/sitemap.json").then(function (d) { sitemap = d; buildDataCatBars(); renderSitemap(); }).catch(function () {});
  fetchJson("data/gameradar.json").then(function (d) { gameradar = d; renderGameRadar(); }).catch(function () {});
  fetchJson("data/capcut.json").then(function (d) { capcut = d; renderCapcut(); }).catch(function () {});
})();
