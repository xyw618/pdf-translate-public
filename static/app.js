pdfjsLib.GlobalWorkerOptions.workerSrc = "/pdfjs/pdf.worker.min.js";

const $ = (s) => document.querySelector(s);
const state = {
  task: null,
  variant: "original", // original | dual | mono
  zoomK: 1,            // 单栏缩放
  zoomKL: 1, zoomKR: 1, // 双语两栏独立缩放
  lastCol: "L",
  sync: true,
  baseScale: 1.3, baseScaleL: 1, baseScaleR: 1,
  zoomTimer: 0, zoomTimerL: 0, zoomTimerR: 0,
  pdf: null, pdfL: null, pdfR: null,
  rendered: new Set(), renderedL: new Set(), renderedR: new Set(),
  pageItems: new Map(), // "taskId:setName:pageno" -> {items, pageH}
};

/* ---------- 服务状态(绿=全正常 / 黄=翻译服务离线 / 红=后端未启动) ---------- */
async function checkSvc() {
  const el = $("#svc");
  try {
    const h = await (await fetch("/api/health", { cache: "no-store" })).json();
    if (h.llm) {
      el.textContent = "服务正常";
      el.className = "svc ok";
    } else {
      el.textContent = "翻译服务离线";
      el.className = "svc warn";
    }
  } catch {
    el.textContent = "后端未启动";
    el.className = "svc bad";
  }
}

/* ---------- 任务列表(语义/日期分组) ---------- */
function groupTasks(list, mode) {
  const now = Date.now(), day = 86400000;
  if (mode === "date") {
    const g = [["今天", []], ["昨天", []], ["7 天内", []], ["更早", []]];
    for (const t of list) {
      const d = Math.floor((now - (t.createdAt || now)) / day);
      (d < 1 ? g[0][1] : d < 2 ? g[1][1] : d < 7 ? g[2][1] : g[3][1]).push(t);
    }
    return g.filter(([, v]) => v.length);
  }
  const m = new Map();
  for (const t of list) {
    const c = (t.meta && t.meta.category) || "未分类";
    if (!m.has(c)) m.set(c, []);
    m.get(c).push(t);
  }
  return [...m.entries()];
}

let listMode = localStorage.getItem("listMode") || "cat";

async function refreshTasks() {
  const list = await (await fetch("/api/tasks")).json();
  const el = $("#tasklist");
  el.innerHTML = "";
  for (const [gname, items] of groupTasks(list, listMode)) {
    const head = document.createElement("div");
    head.className = "group-head";
    head.textContent = gname;
    el.appendChild(head);
    for (const t of items) {
      const d = document.createElement("div");
      d.className = "task" + (state.task?.id === t.id ? " on" : "");
      const stMap = {
        queued: ["排队中", "st"],
        running: ["翻译中…", "st run"],
        done: ["✓ 完成", "st done"],
        failed: ["✗ 失败", "st fail"],
      };
      const [stText, stCls] = stMap[t.status];
      const sum = (t.meta && t.meta.summary) ? `<div class="sum" title="${t.meta.summary}">${t.meta.summary}</div>` : "";
      const tag = t.analyzing ? `<div class="st">🏷 语义分析中…</div>`
        : (t.status === "done" && !(t.meta && t.meta.category)) ? `<div class="st fail" data-retry="${t.id}" title="点击重试">🏷 分析失败,点我重试</div>` : "";
      d.innerHTML = `<div class="name">${t.name}</div><div class="${stCls}">${stText}</div>${tag}${sum}` +
        (t.status === "failed" ? `<div class="st fail" title="${t.error || ""}">${(t.error || "").slice(0, 40)}</div>` : "");
      d.onclick = () => openTask(t);
      el.appendChild(d);
    }
  }
}

document.getElementById("tasklist").addEventListener("click", (e) => {
  const retry = e.target.closest("[data-retry]");
  if (retry) {
    fetch(`/api/analyze/${retry.dataset.retry}`, { method: "POST" }).then(refreshTasks);
    e.stopPropagation();
  }
});

document.querySelectorAll("#listmode button").forEach((b) => {
  b.classList.toggle("on", b.dataset.m === listMode);
  b.onclick = () => {
    listMode = b.dataset.m;
    localStorage.setItem("listMode", listMode);
    document.querySelectorAll("#listmode button").forEach(x => x.classList.toggle("on", x === b));
    refreshTasks();
  };
});

async function upload(file) {
  const fd = new FormData();
  fd.append("file", file);
  await fetch("/api/upload", { method: "POST", body: fd });
  refreshTasks();
}

/* ---------- 阅读器 ---------- */
function clearAll() {
  state.pdf?.destroy(); state.pdfL?.destroy(); state.pdfR?.destroy();
  state.pdf = state.pdfL = state.pdfR = null;
  state.rendered.clear(); state.renderedL.clear(); state.renderedR.clear();
  state.pageItems.clear();
  $("#pages").innerHTML = "";
  $("#colL").innerHTML = "";
  $("#colR").innerHTML = "";
}

function clearPageHolders() {
  state.rendered.clear(); state.renderedL.clear(); state.renderedR.clear();
  $("#pages").innerHTML = "";
  $("#colL").innerHTML = "";
  $("#colR").innerHTML = "";
}

function updateZoomLabel() {
  if (state.variant === "dual") {
    $("#zoomval").textContent = `英${Math.round(state.zoomKL * 100)}% 中${Math.round(state.zoomKR * 100)}%`;
  } else {
    $("#zoomval").textContent = Math.round(state.zoomK * 100) + "%";
  }
}

function updateToolbar() {
  const hasTask = !!state.task;
  $("#variant-switch").classList.toggle("hidden", !hasTask);
  $("#download").classList.toggle("hidden", !hasTask);
  document.querySelectorAll("#variant-switch button").forEach((b) =>
    b.classList.toggle("on", b.dataset.variant === state.variant));
  const dl = $("#download");
  dl.href = `/api/files/${state.task?.id}/${state.variant}`;
  dl.textContent = state.variant === "original" ? "⬇ 原文" : state.variant === "dual" ? "⬇ 双语 PDF" : "⬇ 译文 PDF";
}

async function renderPage(container, setName, pdf, pageNo, scale) {
  if (!pdf) return;
  const page = await pdf.getPage(pageNo);
  const vp = page.getViewport({ scale });
  const div = document.createElement("div");
  div.className = "page";
  div.dataset.pageno = pageNo;
  const canvas = document.createElement("canvas");
  const dpr = window.devicePixelRatio || 1;
  canvas.width = vp.width * dpr;
  canvas.height = vp.height * dpr;
  canvas.style.width = vp.width + "px";
  canvas.style.height = vp.height + "px";
  div.style.width = vp.width + "px";
  div.style.height = vp.height + "px";
  div.appendChild(canvas);
  container.appendChild(div);
  await page.render({ canvasContext: canvas.getContext("2d"), viewport: vp, transform: dpr !== 1 ? [dpr, 0, 0, dpr, 0, 0] : null }).promise;
  const tc = await page.getTextContent();
  const vp1 = page.getViewport({ scale: 1 });
  state.pageItems.set(`${state.task?.id}:${setName}:${pageNo}`, { items: tc.items, pageH: vp1.height });
  const tlDiv = document.createElement("div");
  tlDiv.className = "textLayer";
  div.appendChild(tlDiv);
  const tl = pdfjsLib.renderTextLayer({ textContentSource: tc, container: tlDiv, viewport: vp });
  tl.promise?.catch(() => {});
}

function makeObserver(root, setName) {
  return new IntersectionObserver((entries) => {
    for (const e of entries) {
      if (!e.isIntersecting) continue;
      const no = +e.target.dataset.pageno;
      const key = +e.target.dataset.key;
      const store = setName === "main" ? state.rendered : setName === "L" ? state.renderedL : state.renderedR;
      if (store.has(no)) continue;
      store.add(no);
      const pdf = setName === "main" ? state.pdf : setName === "L" ? state.pdfL : state.pdfR;
      renderPage(e.target, setName, pdf, no, key);
    }
  }, { root, rootMargin: "700px" });
}

const obsMain = makeObserver($("#viewer"), "main");
const obsL = makeObserver($("#colL"), "L");
const obsR = makeObserver($("#colR"), "R");

async function loadVariant() {
  clearAll();
  $("#empty").classList.add("hidden");
  if (!state.task) return;
  const dual = state.variant === "dual";
  $("#viewer").classList.toggle("dual", dual);
  $("#dualwrap").classList.toggle("hidden", !dual);
  $("#pages").classList.toggle("hidden", dual);
  $("#syncbtn").classList.toggle("hidden", !dual);
  updateToolbar();

  if (!dual) {
    const url = `/api/files/${state.task.id}/${state.variant}`;
    const pdf = await pdfjsLib.getDocument(url).promise;
    state.pdf = pdf;
    const p1 = await pdf.getPage(1);
    const v1 = p1.getViewport({ scale: 1 });
    state.pageW = v1.width; state.pageH = v1.height;
    state.baseScale = Math.min(($("#viewer").clientWidth - 60) / 612, 1.8);
    buildMain();
    $("#pageno").textContent = `1 / ${pdf.numPages}`;
  } else {
    const [pl, pr] = await Promise.all([
      pdfjsLib.getDocument(`/api/files/${state.task.id}/original`).promise,
      pdfjsLib.getDocument(`/api/files/${state.task.id}/mono`).promise,
    ]);
    state.pdfL = pl; state.pdfR = pr;
    const [p1l, p1r] = await Promise.all([pl.getPage(1), pr.getPage(1)]);
    const v1l = p1l.getViewport({ scale: 1 }), v1r = p1r.getViewport({ scale: 1 });
    state.pageWL = v1l.width; state.pageHL = v1l.height;
    state.pageWR = v1r.width; state.pageHR = v1r.height;
    state.baseScaleL = Math.max(0.3, ($("#colL").clientWidth - 28) / 612);
    state.baseScaleR = Math.max(0.3, ($("#colR").clientWidth - 28) / 612);
    buildColL(); buildColR();
    $("#pageno").textContent = `1 / ${pl.numPages}`;
  }
  updateZoomLabel();
}

function buildMain() {
  const scale = state.baseScale * state.zoomK;
  const w = state.pageW * scale, h = state.pageH * scale;
  const wrap = $("#pages");
  for (let i = 1; i <= state.pdf.numPages; i++) {
    const holder = document.createElement("div");
    holder.className = "page placeholder";
    holder.dataset.pageno = i;
    holder.dataset.key = scale;
    holder.style.width = w + "px";
    holder.style.height = h + "px";
    wrap.appendChild(holder);
    obsMain.observe(holder);
  }
}

function buildColL() {
  const s = state.baseScaleL * state.zoomKL;
  const w = state.pageWL * s, h = state.pageHL * s;
  for (let i = 1; i <= state.pdfL.numPages; i++) {
    const h2 = document.createElement("div");
    h2.className = "page placeholder";
    h2.dataset.pageno = i; h2.dataset.key = s;
    h2.style.width = w + "px"; h2.style.height = h + "px";
    $("#colL").appendChild(h2);
    obsL.observe(h2);
  }
}

function buildColR() {
  const s = state.baseScaleR * state.zoomKR;
  const w = state.pageWR * s, h = state.pageHR * s;
  for (let i = 1; i <= state.pdfR.numPages; i++) {
    const h2 = document.createElement("div");
    h2.className = "page placeholder";
    h2.dataset.pageno = i; h2.dataset.key = s;
    h2.style.width = w + "px"; h2.style.height = h + "px";
    $("#colR").appendChild(h2);
    obsR.observe(h2);
  }
}

/* ---------- 锚点保持 ---------- */
function captureAnchor(col) {
  const pages = [...col.querySelectorAll(".page")];
  if (!pages.length) return null;
  const cRect = col.getBoundingClientRect();
  const midY = cRect.top + col.clientHeight / 2;
  let page = pages.find((p) => {
    const r = p.getBoundingClientRect();
    return r.top <= midY && r.bottom > midY;
  }) || pages[pages.length - 1];
  const r = page.getBoundingClientRect();
  return { pageno: +page.dataset.pageno, inPage: r.height ? (midY - r.top) / r.height : 0 };
}

function restoreAnchor(col, anchor) {
  if (!anchor) return;
  const page = col.querySelector(`.page[data-pageno="${anchor.pageno}"]`);
  if (!page) return;
  const cRect = col.getBoundingClientRect();
  const pr = page.getBoundingClientRect();
  col.scrollTop += (pr.top + anchor.inPage * pr.height) - (cRect.top + col.clientHeight / 2);
}

/* ---------- 缩放 ---------- */
function applyZoomMain(newK) {
  newK = Math.min(3, Math.max(0.4, newK));
  if (Math.abs(newK - state.zoomK) < 0.001) return;
  state.zoomK = newK;
  const scroller = $("#viewer");
  const a = captureAnchor(scroller);
  clearPageHolders();
  buildMain();
  requestAnimationFrame(() => restoreAnchor(scroller, a));
  updateZoomLabel();
}

function applyZoomCol(which, newK) {
  newK = Math.min(3, Math.max(0.4, newK));
  const key = which === "L" ? "zoomKL" : "zoomKR";
  if (Math.abs(newK - state[key]) < 0.001) return;
  state[key] = newK;
  const col = which === "L" ? $("#colL") : $("#colR");
  const anchor = captureAnchor(col);
  col.innerHTML = "";
  if (which === "L") { state.renderedL.clear(); buildColL(); }
  else { state.renderedR.clear(); buildColR(); }
  requestAnimationFrame(() => restoreAnchor(col, anchor));
  updateZoomLabel();
}

function zoomStep(dir) {
  const steps = [0.4, 0.5, 0.67, 0.8, 1, 1.15, 1.33, 1.5, 1.75, 2, 2.4, 3];
  const nextOf = (cur) => dir > 0
    ? (steps.find(s => s > cur + 0.001) ?? steps[steps.length - 1])
    : ([...steps].reverse().find(s => s < cur - 0.001) ?? steps[0]);
  if (state.variant === "dual") applyZoomCol(state.lastCol, nextOf(state.lastCol === "L" ? state.zoomKL : state.zoomKR));
  else applyZoomMain(nextOf(state.zoomK));
}

/* ---------- 工具条 ---------- */
document.querySelectorAll("#variant-switch button").forEach((b) =>
  b.onclick = () => { state.variant = b.dataset.variant; loadVariant(); });
$("#zoomin").onclick = () => zoomStep(1);
$("#zoomout").onclick = () => zoomStep(-1);
$("#syncbtn").onclick = () => {
  state.sync = !state.sync;
  $("#syncbtn").classList.toggle("on", state.sync);
  $("#syncbtn").textContent = state.sync ? "⇅ 同步滚动:开" : "⇅ 同步滚动:关";
};
function activeCol() {
  return $("#viewer").classList.contains("dual")
    ? (state.lastCol === "R" ? $("#colR") : $("#colL"))
    : $("#viewer");
}
$("#prev").onclick = () => activeCol().scrollBy({ top: -activeCol().clientHeight * 0.9, behavior: "smooth" });
$("#next").onclick = () => activeCol().scrollBy({ top: activeCol().clientHeight * 0.9, behavior: "smooth" });

$("#fileinput").onchange = (e) => {
  if (e.target.files[0]) upload(e.target.files[0]);
  e.target.value = "";
};

/* 单栏 Ctrl+滚轮 */
$("#viewer").addEventListener("wheel", (e) => {
  if (!e.ctrlKey || !state.pdf) return;
  if (e.target.closest(".col")) return;
  e.preventDefault();
  clearTimeout(state.zoomTimer);
  state.zoomTimer = setTimeout(() => zoomStep(e.deltaY < 0 ? 1 : -1), 160);
}, { passive: false });

/* 双语两栏独立 Ctrl+滚轮:在哪一栏滚动就只缩放那一栏 */
function bindColZoom(colEl, which, timerKey) {
  colEl.addEventListener("wheel", (e) => {
    if (!e.ctrlKey || !state.pdfL) return;
    e.preventDefault();
    state.lastCol = which;
    clearTimeout(state[timerKey]);
    state[timerKey] = setTimeout(() => zoomStep(e.deltaY < 0 ? 1 : -1), 160);
  }, { passive: false });
  colEl.addEventListener("mouseenter", () => { state.lastCol = which; });
}
bindColZoom($("#colL"), "L", "zoomTimerL");
bindColZoom($("#colR"), "R", "zoomTimerR");

/* 同步滚动 */
let syncing = false;
function bindSync(src, dst) {
  src.addEventListener("scroll", () => {
    if (!state.sync || !$("#viewer").classList.contains("dual")) return;
    if (Date.now() - lastSync < 60) return;
    lastSync = Date.now();
    dst.scrollTop = src.scrollTop / Math.max(src.scrollHeight, 1) * dst.scrollHeight;
  });
}
let lastSync = 0;
bindSync($("#colL"), $("#colR"));
bindSync($("#colR"), $("#colL"));

$("#colL").addEventListener("scroll", () => {
  const first = [...$("#colL").querySelectorAll(".page")].find(p => p.offsetTop + p.offsetHeight > $("#colL").scrollTop + 60);
  if (first && state.pdfL) $("#pageno").textContent = `${first.dataset.pageno} / ${state.pdfL.numPages}`;
});

async function openTask(t) {
  state.task = t;
  state.variant = "original";
  await loadVariant();
  refreshTasks();
  updateChatCtx();
  switchChat();
}

/* ---------- 划词翻译与文本提取 ---------- */
const pop = $("#pop");
let popCtrl = 0;

/* 从 PDF 结构按几何位置提取选中文本(修复半行/乱序/断词) */
function extractFromItems(items, pageH, rects, scale) {
  // 按字体统计乱码命中率:图内嵌入字体映射错误时整批丢弃
  const fontStats = {};
  for (const it of items) {
    if (!it.str) continue;
    const f = it.fontName || "?";
    fontStats[f] = fontStats[f] || { n: 0, bad: 0 };
    fontStats[f].n++;
    if (/[^\x20-\x7E]/.test(it.str) || (/%/.test(it.str) && /[A-Z]{2,}/.test(it.str)) || /^[^a-z]{4,}$/.test(it.str.replace(/\s/g, ""))) fontStats[f].bad++;
  }
  const badFonts = new Set(Object.keys(fontStats).filter(f => fontStats[f].n > 0 && fontStats[f].bad / fontStats[f].n > 0.3));
  const picked = [];
  for (const it of items) {
    if (!it.str || !it.str.trim()) continue;
    if (badFonts.has(it.fontName)) continue;
    if (/[\u0000-\u001F\u007F-\u009F]/.test(it.str)) continue; // 控制字符乱码
    if (/%/.test(it.str) && /[A-Z]{2,}/.test(it.str)) continue;
    const pw = it.width * scale;
    const ph = Math.max((it.height || 10) * scale, 4);
    const px = it.transform[4] * scale;
    const py = (pageH - it.transform[5]) * scale; // 基线距页顶
    const hit = rects.some(rc =>
      px + pw > rc.x0 && px < rc.x1 &&
      py + ph * 0.4 > rc.y0 && py - ph * 0.6 < rc.y1
    );
    if (hit) picked.push({ str: it.str, px, py, pw, ph });
  }
  if (!picked.length) return null;
  picked.sort((a, b) => a.py - b.py);
  const lines = [];
  for (const p of picked) {
    const last = lines[lines.length - 1];
    if (!last || Math.abs(p.py - last.y) > Math.max(6, p.ph * 0.7)) {
      lines.push({ y: p.py, items: [p] });
    } else {
      last.items.push(p);
      last.y = (last.y + p.py) / 2;
    }
  }
  let text = "";
  const lineRects = [];
  lines.forEach((line, li) => {
    line.items.sort((a, b) => a.px - b.px);
    let s = "";
    let prevEnd = null;
    for (const p of line.items) {
      if (prevEnd !== null && p.px - prevEnd > 1.5 && !/\s$/.test(s) && !/^\s/.test(p.str)) s += " ";
      s += p.str;
      prevEnd = p.px + p.pw;
    }
    s = s.replace(/\s+/g, " ").trim();
    if (!s) return;
    const x0 = Math.min(...line.items.map(p => p.px));
    const x1 = Math.max(...line.items.map(p => p.px + p.pw));
    const y0 = Math.min(...line.items.map(p => p.py - p.ph));
    const y1 = Math.max(...line.items.map(p => p.py + p.ph * 0.25));
    lineRects.push({ x0, y0, x1, y1 });
    if (li === 0) { text = s; return; }
    if (/[A-Za-z]-$/.test(text) && /^[a-z]/.test(s)) text = text.slice(0, -1) + s;
    else text += " " + s;
  });
  return { text: text.trim() || null, lineRects };
}

/* 按选区几何范围从 PDF 结构提取干净文本 + 行矩形(用于自绘高亮) */
function extractSelectionDetailed() {
  const sel = window.getSelection();
  if (!sel.rangeCount) return null;
  const range = sel.getRangeAt(0);
  const rects = [...range.getClientRects()].filter(r => r.width > 1 && r.height > 1);
  if (!rects.length) return null;
  const allPages = [...document.querySelectorAll(".page")];
  const groups = new Map();
  for (const r of rects) {
    const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    const pg = allPages.find(pd => {
      const pr = pd.getBoundingClientRect();
      return cx >= pr.left && cx <= pr.right && cy >= pr.top && cy <= pr.bottom;
    });
    if (!pg) continue;
    if (!groups.has(pg)) groups.set(pg, []);
    const pr = pg.getBoundingClientRect();
    groups.get(pg).push({ x0: r.left - pr.left, x1: r.right - pr.left, y0: r.top - pr.top, y1: r.bottom - pr.top });
  }
  let text = "";
  const highlights = [];
  for (const pg of allPages) {
    if (!groups.has(pg)) continue;
    const pageno = +pg.dataset.pageno;
    const scale = +pg.dataset.key || 1;
    const setName = pg.closest("#colL") ? "L" : pg.closest("#colR") ? "R" : "main";
    const rec = state.pageItems.get(`${state.task?.id}:${setName}:${pageno}`);
    if (!rec) continue;
    const part = extractFromItems(rec.items, rec.pageH, groups.get(pg), scale);
    if (!part || !part.text) continue;
    text += (text ? " " : "") + part.text;
    highlights.push({ pageEl: pg, rects: part.lineRects });
  }
  return { text: text.trim() || null, highlights };
}

function extractSelectionText() {
  const d = extractSelectionDetailed();
  return d?.text || null;
}

/* 自绘高亮:精确贴合文字行,替代错位的原生拖蓝 */
let activeHls = [];
function clearHighlights() {
  activeHls.forEach(h => h.remove());
  activeHls = [];
}
function drawHighlights(highlights) {
  clearHighlights();
  for (const h of highlights || []) {
    for (const rc of h.rects) {
      const d = document.createElement("div");
      d.className = "sel-hl";
      d.style.left = rc.x0 + "px";
      d.style.top = rc.y0 + "px";
      d.style.width = (rc.x1 - rc.x0) + "px";
      d.style.height = (rc.y1 - rc.y0) + "px";
      h.pageEl.appendChild(d);
      activeHls.push(d);
    }
  }
}

function copyText(t) {
  if (navigator.clipboard?.writeText) return navigator.clipboard.writeText(t);
  const ta = document.createElement("textarea");
  ta.value = t; document.body.appendChild(ta); ta.select();
  document.execCommand("copy"); ta.remove();
  return Promise.resolve();
}

/* Ctrl+C 复制时替换为干净文本 */
document.addEventListener("copy", (e) => {
  const sel = window.getSelection();
  if (!sel.rangeCount) return;
  const node = sel.anchorNode?.nodeType === 3 ? sel.anchorNode.parentElement : sel.anchorNode;
  if (!node?.closest?.(".textLayer")) return;
  const d = extractSelectionDetailed();
  if (d?.text) {
    e.preventDefault();
    e.clipboardData.setData("text/plain", d.text);
  }
});

document.addEventListener("mouseup", (ev) => {
  if (pop.contains(ev.target)) return;
  const sel = window.getSelection();
  const raw = sel ? sel.toString().trim() : "";
  if (!raw || raw.length < 2) {
    clearHighlights();
    return;
  }
  const d = extractSelectionDetailed();
  const text = d?.text || raw;
  const range = sel.getRangeAt(0).getBoundingClientRect();
  // 用自绘高亮替代错位的原生拖蓝,并清掉原生选区
  if (d?.highlights?.length) {
    drawHighlights(d.highlights);
    sel.removeAllRanges();
  }
  showPop(text, range);
});

function placePop(rect) {
  pop.classList.remove("hidden");
  const pw = pop.offsetWidth, ph = pop.offsetHeight;
  let x = rect.left + window.scrollX;
  let y = rect.bottom + window.scrollY + 8;
  if (x + pw > window.innerWidth + window.scrollX - 12) x = window.innerWidth + window.scrollX - pw - 12;
  if (y + ph > window.innerHeight + window.scrollY - 12) y = rect.top + window.scrollY - ph - 8;
  pop.style.left = Math.max(12, x) + "px";
  pop.style.top = Math.max(12, y) + "px";
}

async function showPop(text, rect) {
  const ctrl = ++popCtrl;
  $("#pop-src").textContent = text.slice(0, 300);
  $("#pop-out").textContent = "翻译中…";
  placePop(rect);
  try {
    const r = await fetch("/api/translate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    if (!r.ok) throw new Error(await r.text());
    const d = await r.json();
    if (ctrl !== popCtrl) return;
    $("#pop-dir").textContent = d.direction === "en2zh" ? "英 → 中" : "中 → 英";
    $("#pop-out").textContent = d.translation;
  } catch (e) {
    if (ctrl !== popCtrl) return;
    $("#pop-out").textContent = "翻译失败: " + String(e.message || e).slice(0, 120);
  }
  placePop(rect);
}

$("#pop-close").onclick = () => pop.classList.add("hidden");
$("#pop-copy-src").onclick = () => copyText($("#pop-src").textContent);
$("#pop-copy-t").onclick = () => copyText($("#pop-out").textContent);
document.addEventListener("mousedown", (ev) => {
  if (!pop.contains(ev.target)) pop.classList.add("hidden");
});

/* ---------- AI 问答(每篇论文独立对话) ---------- */
const chatMsgs = $("#chat-msgs");
const chatStore = {}; // key(taskId 或 "global") -> [{role, content}]
let chatLoadedKey = null;

function chatKey() { return state.task?.id || "global"; }

function loadHistory(key) {
  if (chatStore[key]) return chatStore[key];
  try {
    const saved = JSON.parse(localStorage.getItem("chat_" + key) || "[]");
    chatStore[key] = Array.isArray(saved) ? saved.slice(-40) : [];
  } catch { chatStore[key] = []; }
  return chatStore[key];
}

function saveHistory(key) {
  try { localStorage.setItem("chat_" + key, JSON.stringify(loadHistory(key).slice(-40))); } catch {}
}

function renderChat(key) {
  chatMsgs.innerHTML = "";
  for (const m of loadHistory(key)) addMsg(m.role === "user" ? "user" : "ai", m.content);
  chatMsgs.scrollTop = chatMsgs.scrollHeight;
  chatLoadedKey = key;
}

function switchChat() {
  const key = chatKey();
  if (key === chatLoadedKey) return;
  if (chatLoadedKey) saveHistory(chatLoadedKey);
  renderChat(key);
}

$("#aibtn").onclick = () => {
  $("#chat").classList.toggle("hidden");
  updateChatCtx();
  switchChat();
};
$("#chat-close").onclick = () => $("#chat").classList.add("hidden");
$("#chat-clear").onclick = async () => {
  const key = chatKey();
  chatStore[key] = [];
  localStorage.removeItem("chat_" + key);
  chatMsgs.innerHTML = "";
  try { await fetch("/api/chat/reset", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ key }) }); } catch {}
};

function updateChatCtx() {
  const open = !$("#chat").classList.contains("hidden");
  $("#aibtn").classList.toggle("on", open);
  $("#chat-ctx").textContent = state.task ? `已附论文: ${state.task.name}` : "未附论文(通用问答)";
}

function addMsg(cls, text) {
  const d = document.createElement("div");
  d.className = "msg " + cls;
  d.textContent = text;
  chatMsgs.appendChild(d);
  chatMsgs.scrollTop = chatMsgs.scrollHeight;
  return d;
}

async function sendChat() {
  const input = $("#chat-text");
  const text = input.value.trim();
  if (!text) return;
  const key = chatKey();
  const hist = loadHistory(key);
  input.value = "";
  addMsg("user", text);
  const loading = addMsg("ai loading", "GLM 思考中…(附论文时可能要 1 分钟)");
  $("#chat-send").disabled = true;
  try {
    const r = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message: text,
        taskId: state.task?.id || "",
        continue: hist.length > 0,
        history: hist.slice(-6),
      }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || "请求失败");
    loading.classList.remove("loading");
    loading.textContent = d.answer;
    hist.push({ role: "user", content: text }, { role: "assistant", content: d.answer });
    saveHistory(key);
  } catch (e) {
    loading.classList.remove("loading");
    loading.textContent = "出错: " + String(e.message || e).slice(0, 200);
  }
  $("#chat-send").disabled = false;
  chatMsgs.scrollTop = chatMsgs.scrollHeight;
}
$("#chat-send").onclick = sendChat;
$("#chat-text").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendChat(); }
});

/* harness 选择 */
async function loadProviders() {
  try {
    const d = await (await fetch("/api/ai/providers")).json();
    const sel = $("#ai-provider");
    sel.innerHTML = d.providers.map(p => `<option value="${p}">${p}</option>`).join("");
    sel.value = d.current;
    let current = d.current;
    sel.onchange = async () => {
      sel.disabled = true;
      try {
        const r = await fetch("/api/ai/provider", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ provider: sel.value }) });
        if (!r.ok) throw new Error((await r.json()).detail || "切换失败");
        current = sel.value;
        await Promise.all([loadModel(), loadAiVariant()]);
      } catch (e) {
        sel.value = current;
        showModelStatus("切换失败: " + e.message, true);
      } finally { sel.disabled = false; }
    };
    await Promise.all([loadModel(), loadAiVariant()]);
  } catch {}
}

async function loadAiVariant() {
  const sel = $("#ai-variant");
  try {
    const d = await (await fetch("/api/ai/variant")).json();
    sel.value = d.variant || "";
  } catch { sel.value = ""; }
}

$("#ai-variant").addEventListener("change", async (e) => {
  await fetch("/api/ai/variant", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ variant: e.target.value }) });
});

function showModelStatus(text, error = false) {
  const el = $("#ai-model-status");
  el.textContent = text;
  el.classList.toggle("error", error);
}

let modelController = null;
async function loadModel() {
  modelController?.abort();
  const controller = new AbortController();
  modelController = controller;
  const sel = $("#ai-model"), custom = $("#ai-model-custom"), refresh = $("#ai-model-refresh");
  sel.disabled = true;
  custom.classList.add("hidden");
  refresh.classList.toggle("hidden", $("#ai-provider").value !== "opencode");
  showModelStatus("加载模型…");
  try {
    const r = await fetch("/api/ai/model", { signal: controller.signal });
    if (!r.ok) throw new Error("无法读取当前模型");
    const d = await r.json();
    sel.replaceChildren(new Option(d.model || "由账号决定", d.model || ""));
    sel.dataset.model = d.model || "";
    sel.dataset.editable = String(d.editable);
    sel.title = d.editable ? "选择模型后自动保存" : "模型由账号决定";
    if (d.editable) sel.add(new Option("手动输入…", ""));
    sel.value = d.model || "";
    sel.disabled = !d.editable;
    if (d.provider !== "opencode") { showModelStatus(""); return; }
    refresh.classList.remove("hidden");
    const mr = await fetch("/api/ai/models?provider=opencode", { signal: controller.signal });
    const data = await mr.json();
    if (!mr.ok) throw new Error(data.detail || "模型列表加载失败");
    sel.replaceChildren();
    if (!data.models.includes(d.model)) sel.add(new Option((d.model || "未设置模型") + "（当前）", d.model || ""));
    const groups = new Map();
    for (const model of data.models) {
      const slash = model.indexOf("/");
      const provider = model.slice(0, slash);
      if (!groups.has(provider)) {
        const group = document.createElement("optgroup");
        group.label = provider;
        groups.set(provider, group);
        sel.appendChild(group);
      }
      groups.get(provider).appendChild(new Option(model.slice(slash + 1), model));
    }
    sel.add(new Option("手动输入…", ""));
    sel.value = d.model || "";
    showModelStatus(`${data.models.length} 个模型`);
  } catch (e) {
    if (e.name !== "AbortError") showModelStatus(e.message + (refresh.classList.contains("hidden") ? "" : "，点击 ↻ 重试"), true);
  }
}

async function saveModel(model) {
  const sel = $("#ai-model"), custom = $("#ai-model-custom"), provider = $("#ai-provider");
  modelController?.abort();
  sel.disabled = custom.disabled = provider.disabled = $("#ai-model-refresh").disabled = true;
  showModelStatus("保存模型…");
  try {
    const r = await fetch("/api/ai/model", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ model, provider: provider.value }) });
    if (!r.ok) throw new Error((await r.json()).detail || "请求失败");
    if (![...sel.options].some(o => o.value === model)) sel.add(new Option(model, model), 0);
    sel.value = sel.dataset.model = model;
    custom.classList.add("hidden");
    showModelStatus("已保存");
  } catch (e) {
    sel.value = sel.dataset.model;
    showModelStatus("保存失败: " + e.message, true);
  } finally {
    sel.disabled = sel.dataset.editable !== "true";
    custom.disabled = provider.disabled = $("#ai-model-refresh").disabled = false;
  }
}

$("#ai-model").addEventListener("change", (e) => {
  if (e.target.value) { saveModel(e.target.value); return; }
  const custom = $("#ai-model-custom");
  custom.value = e.target.dataset.model;
  custom.classList.remove("hidden");
  custom.focus();
});
$("#ai-model-custom").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && e.target.value.trim()) {
    e.preventDefault();
    saveModel(e.target.value.trim());
  }
});
$("#ai-model-refresh").onclick = loadModel;

/* 底部 AI 面板拖拽调高(向上拖变高),双击恢复 */
(function () {
  const chat = $("#chat"), rz = $("#chat-resize");
  const saved = parseInt(localStorage.getItem("chatH"));
  if (saved >= 96) chat.style.height = saved + "px";
  rz.addEventListener("mousedown", (e) => {
    e.preventDefault();
    const startY = e.clientY, startH = chat.offsetHeight;
    const move = (ev) => {
      const h = Math.min(window.innerHeight - 220, Math.max(96, startH + (startY - ev.clientY)));
      chat.style.height = h + "px";
    };
    const up = () => {
      document.removeEventListener("mousemove", move);
      document.removeEventListener("mouseup", up);
      localStorage.setItem("chatH", parseInt(chat.style.height) || 280);
      chatMsgs.scrollTop = chatMsgs.scrollHeight;
    };
    document.addEventListener("mousemove", move);
    document.addEventListener("mouseup", up);
  });
  rz.addEventListener("dblclick", () => {
    chat.style.height = "280px";
    localStorage.setItem("chatH", "280");
  });
})();

/* ---------- 侧栏收起/展开 ---------- */
$("#sidebtn").onclick = () => {
  const app = $("#app");
  app.classList.toggle("collapsed");
  localStorage.setItem("sideCollapsed", app.classList.contains("collapsed") ? "1" : "0");
};
if (localStorage.getItem("sideCollapsed") === "1") $("#app").classList.add("collapsed");

/* ---------- 启动 ---------- */
checkSvc();
refreshTasks();
loadProviders();
setInterval(refreshTasks, 3000);
setInterval(checkSvc, 15000);
