/* MuMu 图像识别调试台 - 前端逻辑 */

const $ = (id) => document.getElementById(id);
let screen = { image: null, width: 1600, height: 900, disW: 0, disH: 0 };
let templates = [];
let pendingUpload = null;            // 手动上传待用的模板图 (data-url + 尺寸)
let cropRect = null;                 // 新建模板时在截图上的选区 {x,y,w,h}（原始坐标）
let selectedIndex = null;            // 当前选中的模板下标
let lastCaptureRect = null;          // 点击记录的区域（原始坐标）
let lastScreenshotKey = null;        // 上次截图内容指纹，相同则跳过重绘
let zoomLevel = 1.0;                 // 缩放倍率（1.0 = 原始 1600px）
let ocrHighlight = null;             // OCR 关键词命中位置 {cx, cy}（原始坐标），绿色方框
let ocrRegions = [];                 // OCR 命名区域库
let ocrEditingName = null;           // 正在编辑的命名区域名（null=新建模式）
let engineRunning = false;           // 引擎是否在跑：切到哪一栏都要知道，导航栏小红点靠它

/* ============ 缩放换算 ============ */
function loadScale() {
  const img = $("screen-img");
  screen.disW = img.clientWidth;
  screen.disH = img.clientHeight;
}
function disp2orig(sx, sy) {
  loadScale();
  const origX = Math.round(sx / screen.disW * screen.width);
  const origY = Math.round(sy / screen.disH * screen.height);
  return [origX, origY];
}
function orig2disp(ox, oy) {
  loadScale();
  return [Math.round(ox / screen.width * screen.disW), Math.round(oy / screen.height * screen.disH)];
}

/* 可调整裁剪框：8 个控制点定义（name, 水平比例, 垂直比例, cursor） */
const HANDLE_DEFS = [
  ["nw", 0,   0,   "nwse-resize"],
  ["n",  0.5, 0,   "ns-resize"],
  ["ne", 1,   0,   "nesw-resize"],
  ["e",  1,   0.5, "ew-resize"],
  ["se", 1,   1,   "nwse-resize"],
  ["s",  0.5, 1,   "ns-resize"],
  ["sw", 0,   1,   "nesw-resize"],
  ["w",  0,   0.5, "ew-resize"],
];
function handleOrigPoints(rect) {
  return HANDLE_DEFS.map(([name, fx, fy]) => [name, rect.x + rect.w * fx, rect.y + rect.h * fy]);
}
function handleCursor(name) {
  return HANDLE_DEFS.find(d => d[0] === name)[3];
}

/* ============ 画面渲染（截图 + 画框） ============ */
function redrawOverlay() {
  const canvas = $("overlay-canvas");
  const img = $("screen-img");
  if (!img.src || !img.complete) return;
  // 画面折叠时 clientWidth=0，按这个尺寸换算会把坐标除成 NaN，画出来的框全是错的
  if (!img.clientWidth) return;
  canvas.width = img.clientWidth;
  canvas.height = img.clientHeight;
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);

  // 画 ROI 框（选中模板）
  if (selectedIndex !== null && templates[selectedIndex]) {
    const roi = templates[selectedIndex].roi;
    if (roi && roi[2] > 0) {
      const [dx, dy] = orig2disp(roi[0], roi[1]);
      const [dx2, dy2] = orig2disp(roi[0] + roi[2], roi[1] + roi[3]);
      ctx.strokeStyle = "#89b4fa"; ctx.lineWidth = 2; ctx.setLineDash([6, 4]);
      ctx.strokeRect(dx, dy, dx2 - dx, dy2 - dy);
      ctx.setLineDash([]);
    }
  }
  // 画新建模板的选区（可调整裁剪框，OCR 也复用这个框）
  if (cropRect) {
    const [dx, dy] = orig2disp(cropRect.x, cropRect.y);
    const [dx2, dy2] = orig2disp(cropRect.x + cropRect.w, cropRect.y + cropRect.h);
    ctx.strokeStyle = "#f9e2af"; ctx.lineWidth = 2;
    ctx.strokeRect(dx, dy, dx2 - dx, dy2 - dy);
    drawHandles(ctx, cropRect);
  }
  // 画 OCR 关键词命中位置：绿色小方框 + 中心点
  if (ocrHighlight) {
    const [dx, dy] = orig2disp(ocrHighlight.cx, ocrHighlight.cy);
    const s = 14;
    ctx.strokeStyle = "#a6e3a1"; ctx.lineWidth = 2; ctx.setLineDash([]);
    ctx.strokeRect(dx - s / 2, dy - s / 2, s, s);
    ctx.fillStyle = "#a6e3a1";
    ctx.fillRect(dx - 2, dy - 2, 4, 4);
  }
}

function drawHandles(ctx, rect) {
  // 8 个控制点：白色填充 + 深色边框，8x8 显示像素
  const HS = 8;
  ctx.fillStyle = "#ffffff";
  ctx.strokeStyle = "#1e1e2e";
  ctx.lineWidth = 1;
  for (const [, ox, oy] of handleOrigPoints(rect)) {
    const [px, py] = orig2disp(ox, oy);
    ctx.fillRect(px - HS / 2, py - HS / 2, HS, HS);
    ctx.strokeRect(px - HS / 2, py - HS / 2, HS, HS);
  }
}

async function refreshScreen(keepDrawing = true) {
  try {
    const res = await fetch("/api/screenshot").then(r => r.json());
    screen.width = res.width;
    screen.height = res.height;
    const key = res.last_modified;
    $("refresh-status").textContent = "已刷新 " + new Date().toLocaleTimeString();
    if (key && key === lastScreenshotKey) return;  // 内容没变，跳过重绘避免闪烁
    lastScreenshotKey = key;
    $("screen-img").src = res.url;
    $("screen-img").onload = () => { if (keepDrawing) redrawOverlay(); };
  } catch (e) {
    $("refresh-status").textContent = "刷新失败: " + e.message;
  }
}

/* ============ 鼠标交互：可调整裁剪框 ============ */
(function initDrag() {
  const canvas = $("overlay-canvas");
  const img = $("screen-img");
  const HIT_DIST = 6;                 // 控制点命中半径（显示像素）
  const MIN_SIZE = 4;                 // 裁剪框最小边长（原始像素）
  let dragMode = "none";              // none | draw | move | resize-<方向>
  let drawStart = null, moveStart = null, moveStartRect = null;
  let resizeStart = null, resizeStartRect = null;

  function getDisp(e) {
    const rect = canvas.getBoundingClientRect();
    return [e.clientX - rect.left, e.clientY - rect.top];
  }

  function handleDisplayPositions() {
    if (!cropRect) return [];
    return handleOrigPoints(cropRect).map(([name, ox, oy]) => {
      const [dx, dy] = orig2disp(ox, oy);
      return [name, dx, dy];
    });
  }

  function getHandleAt(sx, sy) {
    for (const [name, hx, hy] of handleDisplayPositions()) {
      if (Math.abs(sx - hx) <= HIT_DIST && Math.abs(sy - hy) <= HIT_DIST) return name;
    }
    return null;
  }

  function isInside(sx, sy) {
    if (!cropRect) return false;
    const [ox, oy] = disp2orig(sx, sy);
    return ox >= cropRect.x && ox <= cropRect.x + cropRect.w && oy >= cropRect.y && oy <= cropRect.y + cropRect.h;
  }

  function updateCursor(sx, sy) {
    const h = getHandleAt(sx, sy);
    if (h) { canvas.style.cursor = handleCursor(h); return; }
    canvas.style.cursor = isInside(sx, sy) ? "move" : "crosshair";
  }

  function resizeRect(ox, oy) {
    const r = resizeStartRect;
    let left = r.x, top = r.y, right = r.x + r.w, bottom = r.y + r.h;
    const dir = dragMode.replace("resize-", "");
    if (dir.includes("w")) left = ox;
    if (dir.includes("e")) right = ox;
    if (dir.includes("n")) top = oy;
    if (dir.includes("s")) bottom = oy;
    if (right - left < MIN_SIZE) {
      if (dir.includes("w")) left = right - MIN_SIZE; else if (dir.includes("e")) right = left + MIN_SIZE;
    }
    if (bottom - top < MIN_SIZE) {
      if (dir.includes("n")) top = bottom - MIN_SIZE; else if (dir.includes("s")) bottom = top + MIN_SIZE;
    }
    cropRect = { x: Math.round(left), y: Math.round(top), w: Math.round(right - left), h: Math.round(bottom - top) };
  }

  function updateSelInfo() {
    $("sel-info").textContent = `${cropRect.x},${cropRect.y},${cropRect.w},${cropRect.h}`;
  }

  canvas.style.cursor = "crosshair";
  // 阻止浏览器把截图当图片拖走（虚影）
  canvas.addEventListener("dragstart", (e) => e.preventDefault());
  img.addEventListener("dragstart", (e) => e.preventDefault());

  canvas.addEventListener("mousedown", (e) => {
    e.preventDefault();
    const [sx, sy] = getDisp(e);
    const handle = getHandleAt(sx, sy);
    if (handle) {
      dragMode = "resize-" + handle;
      resizeStart = disp2orig(sx, sy);
      resizeStartRect = { ...cropRect };
      return;
    }
    if (isInside(sx, sy)) {
      dragMode = "move";
      moveStart = disp2orig(sx, sy);
      moveStartRect = { ...cropRect };
      return;
    }
    // 框外按下：画新矩形（替换旧的）
    dragMode = "draw";
    drawStart = disp2orig(sx, sy);
    cropRect = { x: drawStart[0], y: drawStart[1], w: 0, h: 0 };
  });

  canvas.addEventListener("mousemove", (e) => {
    const [sx, sy] = getDisp(e);
    const [ox, oy] = disp2orig(sx, sy);
    $("coord").textContent = `x=${ox}, y=${oy}`;

    if (dragMode === "draw") {
      const x = Math.min(drawStart[0], ox), y = Math.min(drawStart[1], oy);
      cropRect = { x, y, w: Math.abs(ox - drawStart[0]), h: Math.abs(oy - drawStart[1]) };
      updateSelInfo(); redrawOverlay();
    } else if (dragMode === "move") {
      cropRect = {
        x: moveStartRect.x + ox - moveStart[0], y: moveStartRect.y + oy - moveStart[1],
        w: moveStartRect.w, h: moveStartRect.h,
      };
      updateSelInfo(); redrawOverlay();
    } else if (dragMode.startsWith("resize-")) {
      resizeRect(ox, oy);
      updateSelInfo(); redrawOverlay();
    } else {
      updateCursor(sx, sy);   // 空闲态：按悬停位置切换光标
    }
  });

  canvas.addEventListener("mouseup", (e) => {
    if (dragMode === "none") return;
    const mode = dragMode;
    dragMode = "none";
    // 仅 draw 模式下过小的框视为点击，扩展为 40x40 点选框
    if (mode === "draw" && cropRect && cropRect.w < 5 && cropRect.h < 5) {
      const cx = cropRect.x, cy = cropRect.y;
      cropRect = { x: cx - 20, y: cy - 20, w: 40, h: 40 };
    }
    startSelect();
    redrawOverlay();
  });
})();

/* 记录当前 cropRect 为待用选区，更新选区信息 */
function startSelect() {
  lastCaptureRect = cropRect ? { ...cropRect } : null;
  $("sel-info").textContent = cropRect
    ? `${cropRect.x},${cropRect.y},${cropRect.w},${cropRect.h}`
    : "未选择区域";
  $("btn-new-template").hidden = false;
}

function copySelection() {
  const parts = [];
  if (lastCaptureRect) parts.push("x,y,w,h: " + [lastCaptureRect.x, lastCaptureRect.y, lastCaptureRect.w, lastCaptureRect.h].join(","));
  if (lastCaptureRect) parts.push("cx,cy: " + [lastCaptureRect.x + lastCaptureRect.w / 2, lastCaptureRect.y + lastCaptureRect.h / 2].map(Math.round).join(","));
  navigator.clipboard.writeText(parts.join("\n"));
}

/* ============ 模板库：分类 + 分组渲染 ============ */
let tplFilter = "";                  // 顶部筛选框的内容
const collapsedCats = new Set();     // 哪些分组被折叠了（只记在这次会话，不存盘）
let catsAllCollapsed = false;

/* 没填分类时按名字前缀推：商品-油画 → 商品；UI-船舱-可搭乘-… → UI/船舱 */
function deriveCategory(name) {
  const parts = String(name || "").split("-").map(s => s.trim()).filter(Boolean);
  if (!parts.length) return "未分类";
  return parts.length >= 3 ? `${parts[0]}/${parts[1]}` : parts[0];
}

function categoryOf(t) {
  return (t.category || "").trim() || deriveCategory(t.name);
}

/* 归一化用户输入的分类：全角斜杠、空格都收掉，最多两级 */
function normalizeCat(value) {
  const text = String(value || "").replace(/／/g, "/").replace(/\s+/g, "").replace(/^\/+|\/+$/g, "");
  return text.split("/").filter(Boolean).slice(0, 2).join("/");
}

function categoryChoices() {
  const set = new Set();
  templates.forEach(t => {
    const c = categoryOf(t);
    set.add(c.split("/")[0]);
    set.add(c);
  });
  return [...set];
}

function renderCatOptions() {
  const dl = $("tpl-cat-options");
  if (!dl) return;
  dl.innerHTML = categoryChoices()
    .map(c => `<option value="${escapeHtml(c)}"></option>`).join("");
}

function groupHeader(label, shownN, totalN, collapsed, onToggle, isSub) {
  const box = document.createElement("div");
  box.className = "tpl-group" + (isSub ? " tpl-subgroup" : "");
  const btn = document.createElement("button");
  btn.className = "tpl-group-btn";
  btn.onclick = onToggle;
  const arrow = document.createElement("span");
  arrow.className = "tpl-group-arrow";
  arrow.textContent = collapsed ? "▸" : "▾";
  const text = document.createElement("span");
  text.textContent = label;
  const count = document.createElement("span");
  count.className = "tpl-group-count";
  count.textContent = totalN != null && totalN !== shownN ? `${shownN}/${totalN}` : `${shownN}`;
  btn.append(arrow, text, count);
  box.appendChild(btn);
  return box;
}

function templateItem(i, t) {
  const div = document.createElement("div");
  div.className = "template-item" + (i === selectedIndex ? " selected" : "");
  div.innerHTML = `
    <div class="t-item-head">
      ${t.thumb ? `<img src="${t.thumb}" alt="">` : `<img alt="">`}
      <input class="t-item-name" value="${escapeHtml(t.name)}" onchange="renameTemplate(${i}, this.value)">
    </div>
    <div class="t-item-cat">
      <input class="t-item-cat-input" list="tpl-cat-options"
             value="${escapeHtml(t.category || "")}" placeholder="自动：${escapeHtml(deriveCategory(t.name))}"
             title="留空就按名字前缀自动归类，写成 一级/二级 就是两级"
             onchange="setTemplateCategory(${i}, this.value)">
    </div>
    <div class="t-item-meta">
      <label>ROI：<input value="${escapeHtml(t.roi.join(","))}" onchange="editROI(${i}, this.value)"></label>
      <label>阈值：<input type="number" step="0.05" min="0" max="1" value="${escapeHtml(t.threshold)}" onchange="editThreshold(${i}, this.value)"></label>
    </div>
    <div class="t-item-actions">
      <button class="btn small" onclick="selectTemplate(${i})">选中</button>
      <button class="btn small" onclick="testTemplate(${i})">测试</button>
      <button class="btn small" onclick="deleteTemplate(${i})">删除</button>
    </div>`;
  return div;
}

function renderTemplates() {
  const box = $("template-list");
  box.innerHTML = "";
  renderCatOptions();

  const kw = tplFilter.trim().toLowerCase();
  const totals = new Map();          // 一级分类 -> 该组总条数（不受筛选影响）
  const tree = new Map();            // 一级 -> (二级 -> [{i, t}])，按模板出现顺序
  let shown = 0;
  templates.forEach((t, i) => {
    const [l1, l2 = ""] = categoryOf(t).split("/");
    totals.set(l1, (totals.get(l1) || 0) + 1);
    if (kw && !t.name.toLowerCase().includes(kw) && !categoryOf(t).toLowerCase().includes(kw)) return;
    if (!tree.has(l1)) tree.set(l1, new Map());
    const subs = tree.get(l1);
    if (!subs.has(l2)) subs.set(l2, []);
    subs.get(l2).push({ i, t });
    shown += 1;
  });

  const head = document.createElement("div");
  head.className = "tpl-count";
  head.textContent = kw ? `筛选出 ${shown} / 共 ${templates.length} 条` : `共 ${templates.length} 条`;
  box.appendChild(head);

  tree.forEach((subs, l1) => {
    const n1 = [...subs.values()].reduce((s, a) => s + a.length, 0);
    const collapsed1 = collapsedCats.has(l1);
    box.appendChild(groupHeader(l1, n1, totals.get(l1), collapsed1,
      () => toggleCat(l1, null), false));
    if (collapsed1) return;
    subs.forEach((items, l2) => {
      const key = l2 ? `${l1}/${l2}` : l1;
      const collapsed2 = collapsedCats.has(key);
      if (l2) {
        box.appendChild(groupHeader(l2, items.length, null, collapsed2,
          () => toggleCat(l1, l2), true));
      }
      if (collapsed2) return;
      const wrap = document.createElement("div");
      wrap.className = "tpl-items";
      items.forEach(({ i, t }) => wrap.appendChild(templateItem(i, t)));
      box.appendChild(wrap);
    });
  });

  if (tree.size === 0) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = "没有匹配的模板";
    box.appendChild(empty);
  }
}

function toggleCat(l1, l2) {
  const key = l2 ? `${l1}/${l2}` : l1;
  if (collapsedCats.has(key)) collapsedCats.delete(key);
  else collapsedCats.add(key);
  renderTemplates();
}

function toggleAllCats() {
  collapsedCats.clear();
  if (!catsAllCollapsed) {
    new Set(templates.map(t => categoryOf(t).split("/")[0])).forEach(c => collapsedCats.add(c));
  }
  catsAllCollapsed = !catsAllCollapsed;
  $("btn-tpl-collapse").textContent = catsAllCollapsed ? "展开全部" : "折叠全部";
  renderTemplates();
}

function filterTemplates(value) {
  tplFilter = value || "";
  renderTemplates();
}

async function setTemplateCategory(i, value) {
  const cat = normalizeCat(value);
  await fetch(`/api/templates/${i}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ...templates[i], category: cat }),
  });
  await loadTemplates();   // 以文件为准重画：换分类后条目会跳到别的组里
}

async function loadTemplates() {
  templates = await fetch("/api/templates").then(r => r.json());
  renderTemplates();
  fillTemplateSelects();
}

function selectTemplate(i) {
  selectedIndex = i;
  renderTemplates();
  redrawOverlay();
}

async function renameTemplate(i, name) {
  const t = templates[i]; t.name = name;
  await saveTemplate(i);
  // 没手动填分类时，分类是从名字前缀推的，改完名重画一次才会落到对的组里
  if (!t.category) await loadTemplates();
}
function editROI(i, str) {
  const t = templates[i]; t.roi = str.split(",").map(v => parseInt(v.trim()));
  saveTemplate(i); redrawOverlay();
}
function editThreshold(i, v) {
  const t = templates[i]; t.threshold = parseFloat(v); saveTemplate(i);
}

function saveTemplate(i) {
  return fetch(`/api/templates/${i}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(templates[i]),
  });
}

function deleteTemplate(i) {
  if (!confirm("确定删除该模板？")) return;
  fetch(`/api/templates/${i}`, { method: "DELETE" }).then(loadTemplates);
  if (selectedIndex === i) { selectedIndex = null; $("btn-new-template").hidden = true; }
}

async function testTemplate(i) {
  const res = await fetch(`/api/templates/${i}/test`, { method: "POST" })
    .then(async r => ({ ok: r.ok, body: await r.json() }));
  if (!res.ok) { $("sel-info").textContent = "测试错误: " + JSON.stringify(res.body); return; }
  renderOverlayFromTest(res.body.overlay);
  $("test-result").textContent = "测试结果: " + JSON.stringify(res.body.result, null, 2);
}

function renderOverlayFromTest(overlay) {
  // 临时用测试返回的画好框的图展示，并保留 ROI 叠加逻辑
  $("screen-img").src = overlay;
  $("screen-img").onload = () => {
    const canvas = $("overlay-canvas");
    canvas.width = $("screen-img").clientWidth;
    canvas.height = $("screen-img").clientHeight;
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, canvas.width, canvas.height);
  };
}

/* ============ 新建模板 ============ */
function openNewTemplate() {
  const modal = $("new-template-modal");
  modal.classList.add("show");
  modal.style.display = "flex";       // 双保险：类 + 内联同时生效
  if (!cropRect) cropRect = lastCaptureRect || { x: 0, y: 0, w: 40, h: 40 };
  $("nt-roi").value = [cropRect.x, cropRect.y, cropRect.w, cropRect.h].join(",");
  updatePreview();
}
function closeModal() {
  const modal = $("new-template-modal");
  modal.classList.remove("show");     // 移除显示类
  modal.style.display = "none";       // 内联强制隐藏，双保险
}
// 点击对话框外背景（遮罩）也能取消
$("new-template-modal").addEventListener("click", (e) => {
  if (e.target === $("new-template-modal")) closeModal();
});
// 按 ESC 关闭弹窗
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeModal();
});

function updatePreview() {
  const canvas = $("nt-preview-canvas");
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, 120, 120);
  const mode = document.querySelector('input[name="nt-mode"]:checked').value;
  if (mode === "crop" && cropRect && $("screen-img").complete) {
    const img = $("screen-img");
    ctx.drawImage(img, cropRect.x, cropRect.y, cropRect.w, cropRect.h, 0, 0, 120, 120);
  } else if (mode === "upload" && pendingUpload) {
    const img = new Image();
    img.onload = () => ctx.drawImage(img, 0, 0, 120, 120);
    img.src = pendingUpload.dataUrl;
  }
}

document.addEventListener("change", () => {
  // 模式切换时刷新预览
  if ($("new-template-modal") && $("new-template-modal").classList.contains("show")) updatePreview();
});

$("btn-upload").addEventListener("click", () => $("file-upload").click());
$("file-upload").addEventListener("change", (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = async () => {
    const img = new Image();
    img.onload = () => {
      pendingUpload = { dataUrl: reader.result, width: img.width, height: img.height };
      $("nt-upload-name").textContent = `${file.name} (${img.width}x${img.height})`;
      // 自动把 ROI 设为整张上传图
      const w = $("screen-img").clientWidth / screen.width;
      const h = $("screen-img").clientHeight / screen.height;
      // 把上传图左上角放在截图 (100,100) 处作为模板区域中心参考
    };
    img.src = reader.result;
  };
  reader.readAsDataURL(file);
});

function waitScreenReady() {
  /* 截图图片可能还在加载（刷新后立即框选保存会触发竞态），等它加载完成 */
  const img = $("screen-img");
  if (img.complete && img.naturalWidth > 0) return Promise.resolve();
  return new Promise((resolve) => {
    const done = () => { img.removeEventListener("load", done); img.removeEventListener("error", done); resolve(); };
    img.addEventListener("load", done);
    img.addEventListener("error", done);
  });
}

async function saveNewTemplate() {
  const name = $("nt-name").value || "未命名";
  const threshold = parseFloat($("nt-threshold").value) || 0.8;
  const roi = $("nt-roi").value.split(",").map(v => parseInt(v.trim()));
  const category = normalizeCat($("nt-category").value);
  const mode = document.querySelector('input[name="nt-mode"]:checked').value;

  let image = null;
  if (mode === "upload" && pendingUpload) {
    image = pendingUpload.dataUrl;  // 整个上传图作为模板图
  } else if (mode === "crop" && cropRect) {
    await waitScreenReady();
    if (!$("screen-img").naturalWidth) { alert("截图尚未加载完成，请刷新截图后重试"); return; }
    image = cropFromScreen(cropRect);
  } else {
    alert("请框选或上传模板图");
    return;
  }

  await fetch("/api/templates", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, image, roi, threshold, category }),
  });
  closeModal();
  pendingUpload = null;
  $("nt-upload-name").textContent = "";
  $("nt-category").value = "";
  cropRect = null;
  $("btn-new-template").hidden = true;
  await loadTemplates();
}

function cropFromScreen(rect) {
  // 用 canvas 从当前截图裁剪出模板区域，返回 data-url（rect 为原始像素坐标，直接裁剪）
  const img = $("screen-img");
  const c = document.createElement("canvas");
  c.width = rect.w; c.height = rect.h;
  c.getContext("2d").drawImage(img, rect.x, rect.y, rect.w, rect.h, 0, 0, rect.w, rect.h);
  return c.toDataURL("image/png");
}

/* ============ 测试函数区域 ============ */
function fillTemplateSelects() {
  // 下拉按分类分组（optgroup），值仍是模板在原数组里的下标
  const groups = new Map();
  templates.forEach((t, i) => {
    const c = categoryOf(t);
    if (!groups.has(c)) groups.set(c, []);
    groups.get(c).push(`<option value="${i}">${escapeHtml(t.name)}</option>`);
  });
  const html = [...groups].map(([c, opts]) =>
    `<optgroup label="${escapeHtml(c)}">${opts.join("")}</optgroup>`).join("");
  $("test-any-select").innerHTML = html;
  $("test-list-select").innerHTML = html;
}

async function runFindAny() {
  const indexes = Array.from($("test-any-select").selectedOptions).map(o => +o.value);
  const roi = parseRoi($("test-any-roi").value);
  const res = await fetch("/api/test/any", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ template_indexes: indexes, roi }),
  }).then(async r => JSON.stringify(await r.json(), null, 2));
  $("test-result").textContent = res;
}

async function runFindList() {
  const idx = +$("test-list-select").value;
  const listRoi = parseRoi($("test-list-roi").value);
  const swipe = $("test-list-swipe").value.split(",").map(v => parseInt(v.trim()));
  const finalSwipe = swipe.length === 4 ? swipe : null;
  const res = await fetch("/api/test/list", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      template_index: idx, list_roi: listRoi,
      swipe_range: finalSwipe, max_swipes: +$("test-list-swipes").value,
      max_swipes_up: +$("test-list-swipes-up").value,
    }),
  }).then(async r => {
    const body = await r.json();
    if (!r.ok && body.detail) return "错误: " + body.detail;
    return JSON.stringify(body, null, 2);
  });
  $("test-result").textContent = res;
}

function parseRoi(s) {
  if (!s) return null;
  const a = s.split(",").map(v => parseInt(v.trim()));
  return a.length === 4 ? a : null;
}

/* ============ 图像缩放 ============ */
const ZOOM_MIN = 0.5, ZOOM_MAX = 3.0;
function clampZoom(v) {
  return Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, v));
}
function applyZoom() {
  const img = $("screen-img");
  img.style.width = Math.round(screen.width * zoomLevel) + "px";
  img.style.height = "auto";
  $("zoom-level").textContent = "缩放: " + Math.round(zoomLevel * 100) + "%";
  if (img.complete) redrawOverlay();
}
function setZoom(level) {
  zoomLevel = clampZoom(level);
  applyZoom();
}
function zoomAt(clientX, clientY, factor) {
  // 以鼠标位置为中心缩放，缩放后调整滚动让鼠标下的点保持不动
  const img = $("screen-img");
  const main = document.querySelector(".main");
  const rect = img.getBoundingClientRect();
  const rx = (clientX - rect.left) / rect.width;
  const ry = (clientY - rect.top) / rect.height;
  zoomLevel = clampZoom(zoomLevel * factor);
  applyZoom();
  const rect2 = img.getBoundingClientRect();
  main.scrollLeft += rect2.left - (clientX - rx * rect2.width);
  main.scrollTop += rect2.top - (clientY - ry * rect2.height);
}

/* ============ 左侧一级导航 + 画布折叠 ============ */
const PAGES = ["run", "material", "debug", "flow", "plan", "route", "settings"];
/* 素材/调试要框选，默认展开画面；其余栏默认折叠。手动开关按栏各记一份，下次进来沿用 */
const SCREEN_NEEDED = { material: true, debug: true, run: false, flow: false, plan: false, route: false, settings: false };
let currentPage = "run";

function screenWanted(page) {
  const saved = localStorage.getItem("uwo.screen." + page);
  return saved === null ? !!SCREEN_NEEDED[page] : saved === "1";
}

function setScreenOpen(open) {
  $("screen-box").dataset.open = open ? "1" : "0";
  $("canvas-wrap").style.display = open ? "" : "none";
  $("btn-screen-toggle").textContent = open ? "收起画面" : "展开画面";
  localStorage.setItem("uwo.screen." + currentPage, open ? "1" : "0");
  if (open) redrawOverlay();     // 折叠时容器宽度是 0，画框只能等展开后重画
}

function toggleScreen() {
  setScreenOpen($("screen-box").dataset.open !== "1");
}

function switchPage(page) {
  if (!PAGES.includes(page)) page = "run";
  /* 跑商设置这一栏的表单是「填完要按保存才写文件」，切走会重新读取、把没存的丢掉，
     所以丢了要说一声 —— 别让人以为刚才填的中转港已经存过了 */
  if (currentPage === "route" && page !== "route" && routeDirty()
      && !confirm("跑商设置有改动还没保存，切走这些改动就没了。确定切过去？")) return;
  currentPage = page;
  localStorage.setItem("uwo.page", page);
  document.querySelectorAll(".nav-item").forEach(b =>
    b.classList.toggle("active", b.dataset.page === page));
  PAGES.forEach(p => { $("page-" + p).hidden = p !== page; });
  setScreenOpen(screenWanted(page));

  // 引擎在跑就一直轮询状态（导航栏小红点和「运行中」那行字靠它），空闲时不轮询
  if (engineRunning) startPolling();

  if (page === "flow") { loadStates(); return; }
  if (page === "plan") { loadPlan(); return; }
  if (page === "route") {
    // 先当场收起，再去拉方案库和目录：loadPresetEditor 那两次 fetch 要一两百毫秒，
    // 等它回来才收的话，进栏的一瞬间上一块还摊着，看着像没按你要的收。
    switchRouteTab(null);
    loadPresetEditor();
    return;
  }
  if (page === "settings") { loadSettings(); return; }
  if (page === "debug") { loadStepOptions(); return; }
  if (page === "run") { loadQueue(); pollStatus(); }
}

/* 「调试」栏里的二级 tab：模板测试 / OCR 测试 / 单步调试，共用上面那张截图和框选 */
function switchDebugTab(tab) {
  document.querySelectorAll(".tab[data-dtab]").forEach(b =>
    b.classList.toggle("active", b.dataset.dtab === tab));
  $("panel-template").hidden = tab !== "template";
  $("panel-ocr").hidden = tab !== "ocr";
  $("panel-step").hidden = tab !== "step";
  if (tab === "step") loadStepOptions();
  redrawOverlay();
}

/* 当前框选 ROI → [x,y,w,h]，没框选则 null（识别全屏） */
function currentRoiArray() {
  return cropRect ? [cropRect.x, cropRect.y, cropRect.w, cropRect.h] : null;
}

/* 把 ROI 和预处理参数填到界面（加载命名区域时用） */
function setOcrControls(roi, scale, binary) {
  if (roi && roi.length === 4) {
    cropRect = { x: roi[0], y: roi[1], w: roi[2], h: roi[3] };
    $("sel-info").textContent = roi.join(",");
  }
  if (scale != null) $("ocr-scale").value = scale;
  $("ocr-scale-val").textContent = $("ocr-scale").value + "x";
  $("ocr-binary").value = binary || "none";
  redrawOverlay();
}

$("ocr-scale").addEventListener("input", () => {
  $("ocr-scale-val").textContent = $("ocr-scale").value + "x";
});

async function runOcr(findMode) {
  const keyword = $("ocr-keyword").value.trim();
  if (findMode && !keyword) {
    alert("请先输入关键词，或点“识别文字”");
    return;
  }
  const payload = {
    roi: currentRoiArray(),
    scale: parseInt($("ocr-scale").value, 10),
    binary_method: $("ocr-binary").value === "none" ? null : $("ocr-binary").value,
    keyword: findMode ? keyword : null,
  };

  const btnText = $("btn-ocr-text"), btnFind = $("btn-ocr-find");
  btnText.disabled = btnFind.disabled = true;
  $("ocr-result").textContent = "识别中…（首次调用需加载模型，约 20~30 秒，请耐心等待）";
  const t0 = performance.now();
  try {
    const res = await fetch("/api/ocr/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const body = await res.json();
    const elapsed = ((performance.now() - t0) / 1000).toFixed(2);
    if (!res.ok) {
      ocrHighlight = null;
      redrawOverlay();
      $("ocr-result").textContent = "错误: " + (body.detail || res.status);
      return;
    }

    if (body.mode === "find") {
      if (body.found) {
        ocrHighlight = { cx: body.cx, cy: body.cy };
        $("ocr-result").textContent =
          `✅ 找到关键词「${keyword}」  中心=(${body.cx}, ${body.cy})  耗时 ${elapsed}s\n` +
          `完整文字: ${body.text}`;
      } else {
        ocrHighlight = null;
        $("ocr-result").textContent =
          `❌ 未找到关键词「${keyword}」  耗时 ${elapsed}s\n完整文字: ${body.text}`;
      }
    } else {
      ocrHighlight = null;
      $("ocr-result").textContent = `识别文字（耗时 ${elapsed}s）:\n${body.text || "（空）"}`;
    }
    redrawOverlay();
    refreshScreen(false);  // 服务端用的是刚截的新图，刷新一帧与绿框对齐
  } catch (e) {
    $("ocr-result").textContent = "请求失败: " + e.message + "（请确认 python app.py 已启动）";
  } finally {
    btnText.disabled = btnFind.disabled = false;
  }
}

/* ============ OCR 命名区域库 ============ */
async function loadOcrRegions() {
  ocrRegions = await fetch("/api/ocr/regions").then(r => r.json());
  renderOcrRegions();
}

function renderOcrRegions() {
  const box = $("ocr-region-list");
  box.innerHTML = "";
  if (!ocrRegions.length) {
    box.innerHTML = `<div class="muted">还没有保存的区域</div>`;
    return;
  }
  ocrRegions.forEach((r, i) => {
    const pp = r.preprocess || {};
    const div = document.createElement("div");
    div.className = "template-item";
    div.innerHTML = `
      <div class="ocr-item-scene">${r.scene ? escapeHtml(r.scene) : "（未分场景）"}</div>
      <div style="font-weight:600;margin:2px 0">${escapeHtml(r.name)}</div>
      <div class="ocr-item-params">
        ROI: ${r.roi ? r.roi.join(",") : "全屏"}<br>
        scale=${pp.scale != null ? pp.scale : 2}，二值化=${pp.binary_method || "None"}
      </div>
      <div class="t-item-actions">
        <button class="btn small" onclick="loadOcrRegion(${i})">加载</button>
        <button class="btn small" onclick="testOcrRegion(${i})">测试</button>
        <button class="btn small" onclick="editOcrRegion(${i})">编辑</button>
        <button class="btn small" onclick="deleteOcrRegion(${i})">删除</button>
      </div>`;
    box.appendChild(div);
  });
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

/* 加载：把区域的 ROI 和参数填入界面 */
function loadOcrRegion(i) {
  const r = ocrRegions[i];
  const pp = r.preprocess || {};
  setOcrControls(r.roi, pp.scale != null ? pp.scale : 2, pp.binary_method || "none");
  switchPage("debug");
  switchDebugTab("ocr");
}

/* 测试：加载区域参数后直接识别文字 */
async function testOcrRegion(i) {
  loadOcrRegion(i);
  $("ocr-keyword").value = "";
  await runOcr(false);
}

/* 编辑：载入参数并进入“保存修改”模式（PUT） */
function editOcrRegion(i) {
  const r = ocrRegions[i];
  loadOcrRegion(i);
  ocrEditingName = r.name;
  $("ocr-region-name").value = r.name;
  $("ocr-region-scene").value = r.scene || "";
  $("btn-ocr-save-region").textContent = "保存修改";
  $("btn-ocr-edit-cancel").hidden = false;
}

function cancelEditOcrRegion() {
  ocrEditingName = null;
  $("ocr-region-name").value = "";
  $("ocr-region-scene").value = "";
  $("btn-ocr-save-region").textContent = "保存为命名区域";
  $("btn-ocr-edit-cancel").hidden = true;
}

async function deleteOcrRegion(i) {
  const r = ocrRegions[i];
  if (!confirm(`确定删除 OCR 区域「${r.name}」？`)) return;
  const res = await fetch("/api/ocr/regions/" + encodeURIComponent(r.name), { method: "DELETE" });
  if (!res.ok) {
    alert("删除失败: " + (await res.json()).detail);
    return;
  }
  if (ocrEditingName === r.name) cancelEditOcrRegion();
  await loadOcrRegions();
}

async function saveOcrRegion() {
  const name = $("ocr-region-name").value.trim();
  if (!name) {
    alert("请填写区域名称");
    return;
  }
  const record = {
    name,
    scene: $("ocr-region-scene").value.trim(),
    roi: currentRoiArray(),
    preprocess: {
      scale: parseInt($("ocr-scale").value, 10),
      binary_method: $("ocr-binary").value === "none" ? null : $("ocr-binary").value,
    },
  };
  const url = ocrEditingName
    ? "/api/ocr/regions/" + encodeURIComponent(ocrEditingName)
    : "/api/ocr/regions";
  const res = await fetch(url, {
    method: ocrEditingName ? "PUT" : "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(record),
  });
  if (!res.ok) {
    alert("保存失败: " + (await res.json()).detail);
    return;
  }
  cancelEditOcrRegion();
  await loadOcrRegions();
}

/* ============ 状态机编辑器 ============ */
let stateData = null;            // {config, states} 从 /api/state/states 读入
let selectedStateIndex = null;   // 当前编辑的状态下标

const CONDITION_TYPES = ["template", "ocr", "any", "all", "plan_pending", "plan_port", "flag"];
const ACTION_TYPES = [
  "click", "click_template", "click_template_optional", "click_ocr",
  "click_after_template", "swipe", "input", "key", "wait", "negotiation",
  "buy_commodities", "plan_advance", "set_flag", "retry_watch", "wait_restock",
  "wait_arrival", "trip_next", "ensure_input", "confirm_city_move", "exit_to_port",
  "goto", "stop", "if", "run_actions",
];
// 运行态开关的名字必须和后端 run_state.py 里的 FLAGS 一字不差：
// 这里只是下拉框的候选，写错后端会当场报错，不会静默写进一个没人读的文件字段。
const RUN_FLAGS = ["sell_pending"];
// plan_port 的 from 能选哪几个启动参数 —— 后端 PORT_FIELD_LABELS 是唯一的名单（自检会比对这两处）。
const PORT_FIELDS = ["buy_port", "sell_port", "sail_port"];
// 跑「完整一趟」时段的种类 —— 和后端 route_plan.STAGE_LABELS 一致（自检会比对这两处）。
// 一个状态用 trip_entry 认领自己是哪一段的入口，引擎不写死「买货一定从 in_port 进」。
const TRIP_STAGES = [["buy", "买货"], ["transit", "中转"], ["sell", "出货"]];

function templateNames() { return templates.map(t => t.name); }
function ocrRegionNames() { return ocrRegions.map(r => r.name); }
function commodityTemplates() {
  const names = templates.map(t => t.name);
  const goods = names.filter(n => n.startsWith("商品-"));
  return goods.length ? goods : names;   // 没有「商品-」前缀时退回全部模板
}

function defaultCondition(type) {
  if (type === "template") return { type: "template", name: "", threshold: 0.8 };
  if (type === "ocr") return { type: "ocr", region: "", contains: "" };
  if (type === "any") return { type: "any", conditions: [] };
  if (type === "all") return { type: "all", conditions: [] };
  if (type === "plan_pending") return { type: "plan_pending" };
  if (type === "plan_port") return { type: "plan_port", region: "港口名字", from: "buy_port" };
  if (type === "flag") return { type: "flag", name: RUN_FLAGS[0], equals: true };
  return { type: "template", name: "", threshold: 0.8 };
}

function defaultAction(type) {
  switch (type) {
    case "click": return { type: "click", x: 0, y: 0 };
    case "click_template":
      return { type: "click_template", name: "", threshold: 0.85, timeout_ms: 0, wait_before_ms: 0, wait_ms: 0 };
    case "click_template_optional":
      return { type: "click_template_optional", name: "", threshold: 0.85, timeout_ms: 1500, poll_interval_ms: 300, max_clicks: 1, wait_before_ms: 0, wait_ms: 0 };
    case "click_ocr": return { type: "click_ocr", region: "", keyword: "" };
    case "click_after_template":
      return { type: "click_after_template", anchor: "", name: "", search_box: null, offset: [0, 0], threshold: 0.8, wait_before_ms: 0 };
    case "swipe": return { type: "swipe", x1: 0, y1: 0, x2: 0, y2: 0, duration: 300 };
    case "input": return { type: "input", text: "", text_from: "", clear_first: false };
    case "key": return { type: "key", digit: 2, hold_ms: 80, wait_ms: 2500 };
    case "wait": return { type: "wait", ms: 1000 };
    case "negotiation": return { type: "negotiation" };
    case "stop": return { type: "stop", reason: "" };
    case "retry_watch":
      return { type: "retry_watch", name: "", click: [0, 0], click_rect: null, interval_ms: 5000, max_attempts: 5, threshold: 0.8, fail_reason: "" };
    case "goto": return { type: "goto", state: "" };
    case "buy_commodities":
      return { type: "buy_commodities", templates: [], templates_from_plan: false, list_roi: null, swipe_range: null, max_swipes: 3, max_swipes_up: 3, swipe_pause_ms: 800, reset_to_top: true, click_wait_ms: 800, threshold: 0.8, negotiation: false };
    case "plan_advance": return { type: "plan_advance" };
    case "set_flag": return { type: "set_flag", name: RUN_FLAGS[0], value: true };
    case "wait_restock":
      return { type: "wait_restock", region: "补货倒计时", poll_interval_ms: 15000,
               max_wait_seconds: 2400, unknown_seconds: 120, timeout_seconds: 2520 };
    case "wait_arrival":
      return { type: "wait_arrival", name: "UI-港口标志", threshold: 0.8,
               poll_interval_ms: 5000, max_wait_seconds: 2400, timeout_seconds: 2520,
               timeout_reason: "", interrupt: defaultAction("retry_watch") };
    case "ensure_input":
      return { type: "ensure_input", click: [125, 124], click_rect: [90, 105, 345, 45],
               text: "", text_from: "run_port", region: "地图搜索框",
               clear_first: true, settle_ms: 900, read_ms: 800, max_attempts: 5,
               timeout_seconds: 180 };
    case "confirm_city_move":
      return { type: "confirm_city_move", name: "UI-城市移动", threshold: 0.85,
               region: "地图选中城市", timeout_ms: 6000, poll_interval_ms: 800,
               timeout_seconds: 60 };
    case "exit_to_port":
      return { type: "exit_to_port", max_clicks: 3, wait_ms: 1200,
               port_marker: "UI-港口标志", timeout_seconds: 20 };
    case "trip_next":
      return { type: "trip_next", after: "work", reason: "", done_reason: "" };
    case "if": return { type: "if", condition: defaultCondition("template"), condition_timeout_ms: 0, then: [], else: [] };
    case "run_actions": return { type: "run_actions", actions: [] };
    default: return { type: "click", x: 0, y: 0 };
  }
}

/* ---------- 加载 / 列表 ---------- */
async function loadStates() {
  stateData = await fetch("/api/state/states").then(r => r.json());
  if (!Array.isArray(stateData.states)) stateData.states = [];
  if (selectedStateIndex == null && stateData.states.length) selectedStateIndex = 0;
  if (selectedStateIndex != null && selectedStateIndex >= stateData.states.length)
    selectedStateIndex = stateData.states.length - 1;
  refreshStateUI();
}

function refreshStateUI() {
  renderStateList();
  renderEditor();
}

function curState() { return stateData && stateData.states[selectedStateIndex]; }

function renderStateList() {
  const box = $("state-list");
  box.innerHTML = "";
  if (!stateData || !stateData.states.length) {
    box.innerHTML = `<div class="muted">还没有状态，点上面「+ 新建状态」</div>`;
    return;
  }
  stateData.states.forEach((s, i) => {
    const div = document.createElement("div");
    div.className = "state-item" + (i === selectedStateIndex ? " selected" : "");
    const condType = s.condition && s.condition.type ? s.condition.type : "无条件";
    const entryTag = s.entry ? `<span class="tag tag-entry">入口</span>` : "";
    const globalTag = s.global ? `<span class="tag tag-global">全局</span>` : "";
    // 全局状态哪个模块都看得见，所以它不需要 module；其余漏填的，引擎启动会直接拒绝
    const moduleTag = s.global
      ? ""
      : (s.module ? `<span class="tag tag-module">${escapeHtml(moduleLabel(s.module))}</span>`
                  : `<span class="tag tag-missing">未填 module</span>`);
    div.innerHTML = `
      <div class="state-item-head" onclick="selectState(${i})">
        <div class="state-item-name">${escapeHtml(s.name || s.id)}</div>
        <div class="state-item-id">${escapeHtml(s.id)}</div>
        <div class="state-item-meta">${condType}${moduleTag}${entryTag}${globalTag}</div>
      </div>
      <div class="state-item-actions">
        <button class="btn small" onclick="moveState(${i}, -1)">↑</button>
        <button class="btn small" onclick="moveState(${i}, 1)">↓</button>
        <button class="btn small" onclick="deleteState(${i})">删除</button>
      </div>`;
    box.appendChild(div);
  });
}

function selectState(i) {
  selectedStateIndex = i;
  refreshStateUI();
}

function newState() {
  if (!stateData) stateData = { config: {}, states: [] };
  let n = stateData.states.length + 1;
  let id = "state_" + n;
  while (stateData.states.some(s => s.id === id)) { n++; id = "state_" + n; }
  stateData.states.push({
    id, name: id, entry: false, global: false, module: "",
    condition: defaultCondition("template"), actions: [], next: null,
  });
  selectedStateIndex = stateData.states.length - 1;
  refreshStateUI();
}

function deleteState(i) {
  const s = stateData.states[i];
  if (!confirm(`确定删除状态「${s.name || s.id}」？`)) return;
  stateData.states.splice(i, 1);
  if (selectedStateIndex != null && selectedStateIndex >= stateData.states.length)
    selectedStateIndex = stateData.states.length - 1;
  refreshStateUI();
}

function moveState(i, dir) {
  const j = i + dir;
  if (j < 0 || j >= stateData.states.length) return;
  const tmp = stateData.states[i];
  stateData.states[i] = stateData.states[j];
  stateData.states[j] = tmp;
  selectedStateIndex = j;
  refreshStateUI();
}

async function saveStates() {
  const status = $("state-save-status");
  status.textContent = "保存中…";
  try {
    const res = await fetch("/api/state/states", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ config: stateData.config || {}, states: stateData.states }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) { status.textContent = "保存失败: " + (body.detail || res.status); return; }
    status.textContent = "已保存 " + new Date().toLocaleTimeString();
  } catch (e) {
    status.textContent = "保存失败: " + e.message + "（请确认 python app.py 已启动）";
  }
}

/* ---------- 编辑器的表单字段构建 ---------- */
function fieldWrap(label, control) {
  const f = document.createElement("div");
  f.className = "field";
  const lab = document.createElement("label");
  lab.appendChild(document.createTextNode(label));
  lab.appendChild(control);
  f.appendChild(lab);
  return f;
}
function textField(label, value, onCommit) {
  const inp = document.createElement("input");
  inp.type = "text"; inp.value = value != null ? value : "";
  inp.onchange = () => onCommit(inp.value);
  return fieldWrap(label, inp);
}
function numField(label, value, onCommit) {
  const inp = document.createElement("input");
  inp.type = "number"; inp.step = "any"; inp.value = value != null ? value : 0;
  inp.onchange = () => onCommit(parseFloat(inp.value) || 0);
  return fieldWrap(label, inp);
}
function boolField(label, value, onCommit) {
  const lab = document.createElement("label");
  lab.className = "checkbox";
  const inp = document.createElement("input");
  inp.type = "checkbox"; inp.checked = !!value;
  inp.onchange = () => onCommit(inp.checked);
  lab.appendChild(inp);
  lab.appendChild(document.createTextNode(" " + label));
  return lab;
}
function selectField(label, options, value, onCommit) {
  const sel = document.createElement("select");
  const empty = document.createElement("option");
  empty.value = ""; empty.textContent = "（未选择）";
  sel.appendChild(empty);
  options.forEach(name => {
    const o = document.createElement("option");
    o.value = name; o.textContent = name; o.selected = (name === value);
    sel.appendChild(o);
  });
  sel.onchange = () => onCommit(sel.value || null);
  return fieldWrap(label, sel);
}
function multiSelectField(label, options, value, onCommit) {
  const sel = document.createElement("select");
  sel.multiple = true;
  options.forEach(name => {
    const o = document.createElement("option");
    o.value = name; o.textContent = name;
    o.selected = Array.isArray(value) && value.includes(name);
    sel.appendChild(o);
  });
  sel.onchange = () => onCommit(Array.from(sel.selectedOptions).map(o => o.value));
  return fieldWrap(label, sel);
}
function arrayField(label, value, onCommit) {
  const inp = document.createElement("input");
  inp.type = "text";
  inp.value = value ? value.join(",") : "";
  inp.placeholder = "逗号分隔，如 307,167,760,604";
  inp.onchange = () => {
    const arr = inp.value.split(",").map(v => parseInt(v.trim())).filter(v => !isNaN(v));
    onCommit(arr.length ? arr : null);
  };
  return fieldWrap(label, inp);
}
function divHint(text) {
  const d = document.createElement("div");
  d.className = "muted hint";
  d.textContent = text;
  return d;
}

/* ---------- 条件编辑器 ---------- */
function renderCondition(node, container) {
  container.innerHTML = "";
  container.appendChild(buildConditionNode(node, null));
}
function buildConditionNode(node, parentArray) {
  const wrap = document.createElement("div");
  wrap.className = "cond-node";

  const row = document.createElement("div");
  row.className = "cond-row";
  const typeSel = document.createElement("select");
  CONDITION_TYPES.forEach(t => {
    const o = document.createElement("option");
    o.value = t; o.textContent = t; o.selected = (node.type === t);
    typeSel.appendChild(o);
  });
  typeSel.onchange = () => {
    Object.keys(node).forEach(k => delete node[k]);
    Object.assign(node, defaultCondition(typeSel.value));
    refreshStateUI();
  };
  row.appendChild(typeSel);
  if (parentArray) {
    const del = document.createElement("button");
    del.className = "btn small"; del.textContent = "× 删除";
    del.onclick = () => { parentArray.splice(parentArray.indexOf(node), 1); refreshStateUI(); };
    row.appendChild(del);
  }
  wrap.appendChild(row);

  const body = document.createElement("div");
  body.className = "cond-body";
  if (node.type === "template") {
    body.appendChild(selectField("模板", templateNames(), node.name, v => { node.name = v; }));
    body.appendChild(numField("threshold", node.threshold, v => { node.threshold = v; }));
  } else if (node.type === "ocr") {
    body.appendChild(selectField("region", ocrRegionNames(), node.region, v => { node.region = v; }));
    body.appendChild(textField("contains", node.contains, v => { node.contains = v; }));
  } else if (node.type === "any" || node.type === "all") {
    if (!Array.isArray(node.conditions)) node.conditions = [];
    const sub = document.createElement("div");
    sub.className = "cond-sublist";
    node.conditions.forEach(c => sub.appendChild(buildConditionNode(c, node.conditions)));
    body.appendChild(sub);
    const add = document.createElement("button");
    add.className = "btn small"; add.textContent = "+ 添加子条件";
    add.onclick = () => { node.conditions.push(defaultCondition("template")); refreshStateUI(); };
    body.appendChild(add);
  } else if (node.type === "plan_pending") {
    body.appendChild(divHint("不看画面、不截图：只看引擎里购物表格的游标。启动时选的港口还剩没买的行就成立，"
      + "全部跑完就不成立 —— 用在买货动作之后，配合 plan_advance 实现「本港口的几行买完就停」。"));
  } else if (node.type === "flag") {
    body.appendChild(selectField("name（运行态开关）", RUN_FLAGS, node.name, v => { node.name = v; }));
    body.appendChild(boolField("equals（要求它等于这个值才成立）", node.equals === undefined ? true : node.equals, v => { node.equals = v; }));
    body.appendChild(divHint("不看画面、不截图：读 run_state.json 里引擎自己记的账。为什么用它而不是去画面里认按钮："
      + "2026-09-28 实测「出售」按钮亮和灰分不出来（同一块裁图金色 1.0000、灰色 0.9039，换只留底色的裁法又会被购买页那个金色槽位 1.0000 命中）。"
      + "所以按定下的规则记 —— 每次买完货置 1、卖出完成置 0，卖货链的链头只认这个开关。"));
  } else if (node.type === "plan_port") {
    body.appendChild(selectField("region", ocrRegionNames(), node.region, v => { node.region = v; }));
    body.appendChild(selectField("from（要比的港口名取自启动参数的哪一个）", PORT_FIELDS, node.from || "buy_port", v => { node.from = v; }));
    body.appendChild(divHint("港口名核对：把这块区域 OCR 出来的字和「本次港口」比对（只做包含判断，不用正则）。"
      + "对不上就整条链一步都不走，更不会按 3 进交易所买货。港口名不写死在这里，换港口只改启动前那个下拉。"));
    body.appendChild(divHint("from 决定「本次港口」是哪个港：买货链比 buy_port（当前所在港那个买货站），"
      + "卖货链比 sell_port（当前所在港那个出货站）—— 同一个港既买又卖很常见，但两个段的站不是同一站，"
      + "比错港就会拿着 A 港的名字去认 B 港的画面，永远不匹配。一个模块只能有一个来源，写乱了引擎拒绝启动。"));
  }
  wrap.appendChild(body);
  return wrap;
}

/* ---------- 动作编辑器 ---------- */
function renderActions(list, container) {
  container.innerHTML = "";
  if (!list.length) {
    container.innerHTML = `<div class="muted">暂无动作，点下方「+ 添加动作」</div>`;
    return;
  }
  list.forEach(a => container.appendChild(buildActionNode(a, list)));
}
function buildActionNode(action, parentList) {
  const wrap = document.createElement("div");
  wrap.className = "action-node";

  const head = document.createElement("div");
  head.className = "action-head";
  const typeSel = document.createElement("select");
  ACTION_TYPES.forEach(t => {
    const o = document.createElement("option");
    o.value = t; o.textContent = t; o.selected = (action.type === t);
    typeSel.appendChild(o);
  });
  typeSel.onchange = () => {
    Object.keys(action).forEach(k => delete action[k]);
    Object.assign(action, defaultAction(typeSel.value));
    refreshStateUI();
  };
  head.appendChild(typeSel);

  const idx = parentList.indexOf(action);
  const up = document.createElement("button");
  up.className = "btn small"; up.textContent = "↑";
  up.onclick = () => moveItem(parentList, idx, -1);
  const down = document.createElement("button");
  down.className = "btn small"; down.textContent = "↓";
  down.onclick = () => moveItem(parentList, idx, 1);
  const del = document.createElement("button");
  del.className = "btn small"; del.textContent = "删除";
  del.onclick = () => { parentList.splice(parentList.indexOf(action), 1); refreshStateUI(); };
  head.append(up, down, del);
  wrap.appendChild(head);

  const body = document.createElement("div");
  body.className = "action-body";
  renderActionFields(action, body);
  wrap.appendChild(body);
  return wrap;
}
function moveItem(list, i, dir) {
  const j = i + dir;
  if (j < 0 || j >= list.length) return;
  const tmp = list[i]; list[i] = list[j]; list[j] = tmp;
  refreshStateUI();
}
function renderActionFields(a, body) {
  const tmpl = templateNames();
  const regions = ocrRegionNames();
  switch (a.type) {
    case "click":
      body.appendChild(numField("x", a.x, v => a.x = v));
      body.appendChild(numField("y", a.y, v => a.y = v));
      break;
    case "click_template":
      body.appendChild(selectField("name", tmpl, a.name, v => a.name = v));
      body.appendChild(numField("threshold", a.threshold, v => a.threshold = v));
      body.appendChild(numField("timeout_ms", a.timeout_ms, v => a.timeout_ms = v));
      body.appendChild(numField("wait_before_ms", a.wait_before_ms, v => a.wait_before_ms = v));
      body.appendChild(numField("wait_ms", a.wait_ms, v => a.wait_ms = v));
      break;
    case "click_template_optional":
      body.appendChild(selectField("name", tmpl, a.name, v => a.name = v));
      body.appendChild(numField("threshold", a.threshold, v => a.threshold = v));
      body.appendChild(numField("timeout_ms", a.timeout_ms, v => a.timeout_ms = v));
      body.appendChild(numField("poll_interval_ms", a.poll_interval_ms, v => a.poll_interval_ms = v));
      body.appendChild(numField("max_clicks", a.max_clicks, v => a.max_clicks = v));
      body.appendChild(numField("wait_before_ms", a.wait_before_ms, v => a.wait_before_ms = v));
      body.appendChild(numField("wait_ms", a.wait_ms, v => a.wait_ms = v));
      break;
    case "click_ocr":
      body.appendChild(selectField("region", regions, a.region, v => a.region = v));
      body.appendChild(textField("keyword", a.keyword, v => a.keyword = v));
      break;
    case "click_after_template":
      body.appendChild(selectField("anchor（锚点：这扇窗全图独有的标题/图案模板名）", tmpl, a.anchor, v => a.anchor = v));
      body.appendChild(selectField("name（窗里要点哪张模板；填了它就必须填 search_box，留空=用下面的 offset）", tmpl, a.name || "", v => a.name = v));
      body.appendChild(arrayField("search_box（相对锚点中心的矩形 dx,dy,w,h —— name 只在这块范围里找）", a.search_box, v => a.search_box = v));
      body.appendChild(arrayField("offset（锚点中心 -> 目标点的像素偏移 dx,dy，可为负；填了 name 就别填它）", a.offset, v => a.offset = v));
      body.appendChild(numField("threshold（锚点置信度阈值）", a.threshold, v => a.threshold = v));
      body.appendChild(numField("wait_before_ms（找锚点前先等，给弹窗动画时间）", a.wait_before_ms, v => a.wait_before_ms = v));
      body.appendChild(divHint("先整张画面找锚点（模板自带的 ROI 在这里忽略，就是全图匹配），命中后才允许点这扇窗里的东西。"
        + "锚点没出现 = 那扇窗根本没弹，记一条 warn 后跳过，不会报错中断。"));
      body.appendChild(divHint("窗里的按钮两种定位法，二选一：① name + search_box = 在锚点下方那块矩形里匹配按钮模板，点它匹配到的中心（按钮在哪由画面说了算，不用量像素）；"
        + "② offset = 点「锚点中心 + 固定偏移」。优先用①，②换个窗型就废 —— 2026-09-29 实机：卖货结算窗比买货那扇多六行、整窗往上顶，"
        + "照买货量的 [0,318] 点到了「总额」那行字上，窗没关。"));
      body.appendChild(divHint("为什么要有这个动作：金色「确定」这一个控件在购买面板(901,851)、结算结果、"
        + "改船舱(895,689)、退出游戏(893,745) 几扇窗里都长一样，直接全图匹配按钮取最高分就可能点到最不该点的那个。"
        + "窗口标题全图唯一，先证明是哪扇窗再点 —— 所以 anchor 一定要选标题类模板，别选按钮。"
        + "search_box 也别框太大：把购买面板那一格 (811,827) 框进去，就又回到两个候选取最高分了。"));
      break;
    case "swipe":
      body.appendChild(numField("x1", a.x1, v => a.x1 = v));
      body.appendChild(numField("y1", a.y1, v => a.y1 = v));
      body.appendChild(numField("x2", a.x2, v => a.x2 = v));
      body.appendChild(numField("y2", a.y2, v => a.y2 = v));
      body.appendChild(numField("duration", a.duration, v => a.duration = v));
      break;
    case "input":
      body.appendChild(textField("text（要打的字；填了 text_from 就留空）", a.text, v => a.text = v));
      body.appendChild(selectField("text_from（打字内容从哪来：留空=用上面的 text）",
        ["", "run_port"], a.text_from || "", v => a.text_from = v));
      body.appendChild(boolField("clear_first（先广播清空输入框，再打）", !!a.clear_first, v => a.clear_first = v));
      body.appendChild(divHint("中文只能走 ADBKeyboard 广播（adb input text 只吃 ASCII）。含中文时这个动作会先把输入法切成 ADBKeyboard。"));
      body.appendChild(divHint("⚠ 打字前画面底部那条白色输入栏必须是开着的（实测：点搜索框**左端 (125,124)** 才唤起，点框的中心 (267,124) 不弹）。"
        + "栏没开时 INPUT_TEXT / CLEAR_TEXT 两条广播都会返回 result=0 但什么都不落地，所以广播返回值不能当成功依据。"));
      body.appendChild(divHint("⇒ 因此这个动作只适合「人已经在画面前、栏是开着的」场合（比如手动单步）。"
        + "自动跑的那一步请用 ensure_input：它自己点框、自己认栏、打完字 OCR 读回来核对，读不到就重来。"));
      body.appendChild(divHint("text_from=run_port：打的是启动前人在「运行」栏选的那个港口名，不写死在配置里 —— 每换一个港不必改这份 JSON。"));
      break;
    case "retry_watch":
      body.appendChild(selectField("name（中断判据模板：横幅上那串「重启自动移动」）", tmpl, a.name, v => a.name = v));
      body.appendChild(arrayField("click（要点的落点 cx,cy）", a.click, v => a.click = v));
      body.appendChild(arrayField("click_rect（可选 x,y,w,h：给了就在框内随机落点）", a.click_rect, v => a.click_rect = v));
      body.appendChild(numField("interval_ms（每次点完之后的等待）", a.interval_ms, v => a.interval_ms = v));
      body.appendChild(numField("max_attempts（最多点几次，点满仍没恢复就停止+弹窗）", a.max_attempts, v => a.max_attempts = v));
      body.appendChild(numField("threshold（留模板默认就填 0.8）", a.threshold, v => a.threshold = v));
      body.appendChild(textField("fail_reason（失败时弹窗和停止原因上的文字）", a.fail_reason, v => a.fail_reason = v));
      body.appendChild(divHint("航行中每轮认一次这个模板：没命中（横幅还是「预计到达时间」）就什么都不做、一次截图都不额外花；"
        + "命中就点 click 那个位置重新发起自动移动，等 interval_ms 后再认，仍然命中就再点。"));
      body.appendChild(divHint("计数是**连续**的：只要有一次认不到中断就算恢复，计数作废。点满 max_attempts 次仍没恢复 → 记 error 日志 + 停止 + "
        + "弹一个置顶阻塞窗（人点掉之前不往下走）。为什么单独做一个动作而不复用 click_template_optional："
        + "那个动作只有「等模板出现、点几次」，没有「点完之后到底恢复了没有」这个概念，也就数不出连续几次。"));
      break;
    case "wait_arrival":
      if (!a.interrupt || typeof a.interrupt !== "object") a.interrupt = defaultAction("retry_watch");
      body.appendChild(selectField("name（到港判据模板）", tmpl, a.name, v => a.name = v));
      body.appendChild(numField("threshold", a.threshold, v => a.threshold = v));
      body.appendChild(numField("poll_interval_ms（每隔多久认一次画面）", a.poll_interval_ms, v => a.poll_interval_ms = v));
      body.appendChild(numField("max_wait_seconds（最多等这么久，到点还没到港就停止喊人）", a.max_wait_seconds, v => a.max_wait_seconds = v));
      body.appendChild(numField("timeout_seconds（本动作的看门狗，要比 max_wait_seconds 长）", a.timeout_seconds, v => a.timeout_seconds = v));
      body.appendChild(textField("timeout_reason（等不到港时写在停止原因里的话）", a.timeout_reason, v => a.timeout_reason = v));
      body.appendChild(divHint("在海上阻塞等到港：每 poll_interval_ms 重截一次图认「到港判据」，认到了就往下走。"
        + "为什么做成一个动作而不是让状态机在海上一直「未匹配」：主循环 max_no_match 只有 20 轮（约两分钟），"
        + "航行动辄十几分钟，不当成等待的话它会把自己判成卡死。"));
      body.appendChild(divHint("interrupt（等待期间每轮顺手照看的动作，一般就是 retry_watch：航行中断了就点「重启自动移动」）。"
        + "它是这一步的一部分，不是动作列表里单独的一条，所以上面那排↑↓和删除对它没有作用。"));
      body.appendChild(buildActionNode(a.interrupt, [a.interrupt]));
      break;
    case "exit_to_port":
      body.appendChild(numField("max_clicks（最多点几次右上角房子/X）", a.max_clicks, v => a.max_clicks = v));
      body.appendChild(numField("wait_ms（每次点完等多久再重截屏）", a.wait_ms, v => a.wait_ms = v));
      body.appendChild(selectField("port_marker（确认已经退回港口的模板）", tmpl, a.port_marker, v => a.port_marker = v));
      body.appendChild(numField("timeout_seconds（本动作的看门狗，要够点完 max_clicks 轮）", a.timeout_seconds, v => a.timeout_seconds = v));
      body.appendChild(divHint("只在链条收尾时用：看到右上角房子 / X 才点，点完重截屏，直到能用 port_marker 确认已经回到港口界面。"));
      body.appendChild(divHint("卖货后必须先退回港口再 trip_next；否则下一段买货会从交易所页面读港口名，读不到就整趟卡住。退不回港口时这个动作会停止并弹窗喊人，不继续接下一段。"));
      break;
    case "trip_next":
      if (a.after !== "work" && a.after !== "arrive") a.after = "work";
      body.appendChild(selectField("after（这一支是在哪一刻收尾）", ["work", "arrive"], a.after, v => a.after = v));
      body.appendChild(textField("reason（单模块跑法在这里停止时写的停止原因）", a.reason, v => a.reason = v));
      body.appendChild(textField("done_reason（跑完整趟、后面没站了时写的停止原因）", a.done_reason, v => a.done_reason = v));
      body.appendChild(divHint("三条链（买货 / 港口间移动 / 卖货）跑到底都走这一支。没在跑「完整一趟」时它就等于以前那条 stop，"
        + "文字用 reason；跑整趟时由它决定接哪一段 —— after=work 是「这一站的正事做完了，退到码头开去下一站」，"
        + "after=arrive 是「船刚靠港，认一认这是第几站、进门该做什么」。"));
      body.appendChild(divHint("一条链的结尾只写这一处，单模块和整趟共用同一份 states.json，不必为整趟复制一份。"
        + "跑整趟时每一站在启动前都先核对一遍（买货站要在购物表格里查得到、出货站要有待卖账、中转站后面必须有下一站），"
        + "对不上就直接拒绝启动 —— 出港和买货都花真金币，走到第 3 站才发现来不及退。"));
      break;
    case "goto":
      body.appendChild(selectField("state（本轮动作跑完后跳去哪个状态）", stateData.states.map(x => x.id), a.state, v => a.state = v));
      body.appendChild(divHint("按画面决定跳转，跳过状态上写死的那个 next。为什么需要它：一个状态只有一个写死的 next，"
        + "可「在海上」这一格要等的其实是「左上角什么时候从帆船换成灯塔」，那是条件跳转。"
        + "没有 goto 就只能让状态机在海上一直「未匹配」，而 max_no_match 一共才 20 轮（约两分钟），航行动辄十几分钟，会被自己判成卡死。"));
      body.appendChild(divHint("通常配在 if 的 then 里：if 认到 UI-港口标志 → then 里 goto in_port。goto 之后的同层动作不再执行。"));
      break;
    case "key":
      body.appendChild(numField("digit（游戏快捷键数字 0-9）", a.digit, v => a.digit = v));
      body.appendChild(numField("hold_ms（按住多少毫秒）", a.hold_ms, v => a.hold_ms = v));
      body.appendChild(numField("wait_ms（按键后等待毫秒）", a.wait_ms, v => a.wait_ms = v));
      body.appendChild(divHint("走 sendevent 原始输入事件，不是 adb input keyevent（框架层注入，这个游戏收不到）。数字键是「导航键」：角色会先走到建筑门口再进入，实测耗时 3~12 秒随距离变化，所以 wait_ms 别指望兜底。正确用法是 key 之后立刻交给下一个状态的 condition 去判定（如 UI-交易所标志 / UI-超载出售 等），不要在同一个动作列表里连着点新页面上的按钮。只在港口界面有效：1=出港所 2=造船所 3=交易所。"));
      break;
    case "wait":
      body.appendChild(numField("ms", a.ms, v => a.ms = v));
      break;
    case "negotiation":
      body.appendChild(divHint("无需参数，使用 config.negotiation 里的按钮坐标"));
      break;
    case "plan_advance":
      body.appendChild(divHint("无需参数。购物表格游标前进一格 = 本港口这一行已经买完，不碰画面也不点击。"
        + "配合条件 plan_pending 用：放在买货动作之后，后面接 if plan_pending，还有行就继续买、没有就 stop。"));
      break;
    case "set_flag":
      body.appendChild(selectField("name（运行态开关）", RUN_FLAGS, a.name, v => a.name = v));
      body.appendChild(boolField("value（勾选 = 置 1，不勾 = 置 0）", a.value, v => a.value = v));
      body.appendChild(divHint("不碰画面：把引擎自己记的账写进 run_state.json 并落盘，进程重启也还在。"
        + "定下的规则是「每次买完货置 1、卖出完成置 0」，所以它成对出现 —— 买货链末尾置 1，卖货链末尾置 0，"
        + "卖货链的链头用条件 flag 认它。为什么不让引擎看画面：见条件 flag 的说明（出售按钮亮/灰分不出来）。"));
      body.appendChild(divHint("名字只能从下拉里选：写错一个字母会落盘一个引擎永远不会读的字段，看起来存成功了实际没人认，"
        + "所以后端遇到不认识的名字是当场报错，不是默默收下。"));
      break;
    case "wait_restock":
      body.appendChild(selectField("region（读倒计时的 OCR 区域）", regions, a.region, v => a.region = v));
      body.appendChild(numField("poll_interval_ms（等待期间隔多久醒来看一次表）", a.poll_interval_ms, v => a.poll_interval_ms = v));
      body.appendChild(numField("max_wait_seconds（最多等这么久，到点还没等到就停止喊人）", a.max_wait_seconds, v => a.max_wait_seconds = v));
      body.appendChild(numField("unknown_seconds（画面连续认不出多久就报告）", a.unknown_seconds, v => a.unknown_seconds = v));
      body.appendChild(numField("timeout_seconds（本动作的看门狗，要比 max_wait_seconds 长）", a.timeout_seconds, v => a.timeout_seconds = v));
      body.appendChild(textField("tab_region（到点后重进的标签区域，留空用『购买出售标签』）", a.tab_region, v => a.tab_region = v));
      body.appendChild(textField("tab_keyword（到点后重进的标签文字，留空用『购买』）", a.tab_keyword, v => a.tab_keyword = v));
      body.appendChild(divHint("要不要等不看画面上那串数字，看引擎按港口记在 run_state.json 里的「下次补货时刻」："
        + "这个港没记过（今天第一次来买，隔夜离线必定超过 30 分钟、货架早刷过了）或记的时刻已经过了 —— 直接买，一秒都不等；"
        + "时刻还在将来 —— 本地等到那个点，等完再点一次「购买」标签（到点游戏不会自己刷新列表）。"
        + "每次买货这一刻 OCR 读一次倒计时，换算成时刻记回账本；读不出就按 30 分钟记 —— 宁可下次多等，不可早买。"
        + "等待期间画面连续认不出任何已知页面（充值弹窗、全屏宣传页盖上来）超过 unknown_seconds 就停止喊人，"
        + "不会闷头等到上限。"));
      body.appendChild(divHint("为什么不直接盯画面上那串倒计时：它是「下一次刷新」的钟点，不是「货架空了」的证据。"
        + "2026-09-30 实测每天首次购买本来就是刷新好的，每次都等那串数字纯属浪费时间，所以改成自己记时刻、按港口分开记"
        + "（每个港的刷新点不同，热那亚记的时刻不能拿去卡汉堡）。"));
      body.appendChild(divHint("为什么做成一个动作而不是一个状态：主循环每轮都要过 max_rounds / max_no_match，"
        + "等 40 分钟会把这两个计数直接撑爆（现配置 max_rounds=60），引擎会把自己判成卡死。等待放在动作里，"
        + "一轮就是一整段等待，中途点「停止」也能立刻打断。"));
      break;
    case "ensure_input":
      body.appendChild(arrayField("click（点输入框哪个位置：实测只能点搜索框左端 125,124）", a.click, v => a.click = v));
      body.appendChild(arrayField("click_rect（可选 x,y,w,h：给了就在框内随机落点）", a.click_rect, v => a.click_rect = v));
      body.appendChild(textField("text（写死要打的字，留空则看下面 text_from）", a.text, v => a.text = v));
      body.appendChild(selectField("text_from（要打的字从哪来：留空=用上面的 text）",
        ["", "run_port"], a.text_from || "", v => a.text_from = v));
      body.appendChild(selectField("region（打完字读哪一行来核对字进没进框）", regions, a.region, v => a.region = v));
      body.appendChild(boolField("clear_first（先清空再打，默认勾上）", a.clear_first !== false, v => a.clear_first = v));
      body.appendChild(numField("settle_ms（点完框到打字之间等多久，白栏弹出来要时间）", a.settle_ms, v => a.settle_ms = v));
      body.appendChild(numField("read_ms（打完字到截图去读之间等多久）", a.read_ms, v => a.read_ms = v));
      body.appendChild(numField("max_attempts（最多几轮，几轮都读不到字就停止喊人）", a.max_attempts, v => a.max_attempts = v));
      body.appendChild(numField("timeout_seconds（本动作的看门狗，要够 max_attempts 轮跑完）", a.timeout_seconds, v => a.timeout_seconds = v));
      body.appendChild(divHint("和 input 的区别就一句话：input 打完字就走，这个动作打完字要回头看一眼。"
        + "每轮做三件事 —— 点输入框、认白色输入栏开没开（dumpsys，不截图不 OCR）、打字，"
        + "然后截图 OCR 读 region 那一行，读到要打的字才算成功；读不到就重新点、重新清、重新打。"));
      body.appendChild(divHint("为什么非做不可（2026-09-30 实机）：白栏收着时 ADB_INPUT_TEXT / ADB_CLEAR_TEXT "
        + "两条广播照样回 result=0，但一个字都不落地。老写法就这么带着空搜索框往下走，"
        + "列表没筛出目的港、下一步「城市移动」找不到，船在海上白等。广播的返回值不能当成功依据，"
        + "唯一算数的是「框里读得到那几个字」。"));
      body.appendChild(divHint("click 必须偏到搜索框左端（实测 125,124）：点框的中心 (267,124) 正落在已有文字上，"
        + "只是移光标、不重新唤起白栏 —— 这条是录制里试了五种姿势才定位到的（港口间移动-流程记录.md 2.4b）。"));
      break;
    case "confirm_city_move":
      body.appendChild(selectField("name（要点开的按钮模板）", tmpl, a.name, v => a.name = v));
      body.appendChild(numField("threshold（留模板默认就填 0.85）", a.threshold, v => a.threshold = v));
      body.appendChild(selectField("region（读哪一行确认选中的城市）", regions, a.region, v => a.region = v));
      body.appendChild(numField("timeout_ms（核对通过后，等按钮出现的最长毫秒）", a.timeout_ms, v => a.timeout_ms = v));
      body.appendChild(numField("poll_interval_ms（隔多久认一次）", a.poll_interval_ms, v => a.poll_interval_ms = v));
      body.appendChild(numField("timeout_seconds（本动作的看门狗，要比 timeout_ms 长）", a.timeout_seconds, v => a.timeout_seconds = v));
      body.appendChild(divHint("点「城市移动」之前先核对：region 那行读出来的城市名必须含本次港口名，"
        + "对上了再等按钮真出现在画面上才点。任一不过 → 记错误 + 停止 + 弹置顶窗喊人，绝不点下去。"));
      body.appendChild(divHint("为什么这一步不能省：列表第一行是谁完全取决于搜索框里那几个字。"
        + "框空着时第一行是默认列表头一座城（实机那次是『马赛』），按固定坐标点下去就把船开去别人的港，"
        + "而点「城市移动」没有二次确认（录制 2.7 已确认），一下去就是几十分钟真航程。"));
      body.appendChild(divHint("为什么按钮要「等它出现」：点列表那一行会让白栏收起、地图平移、城市信息面板弹出，"
        + "「城市移动」就在栏盖住过的那一条（y 810~900）上，要等它露出来才认得到。"
        + "老写法匹配不上直接抛错，引擎记下错误照样跳到 voyage，等于带着一个没点成的按钮往下跑。"));
      break;
    case "stop":
      body.appendChild(textField("reason（停止原因，可留空）", a.reason, v => a.reason = v));
      body.appendChild(divHint("执行到这里会主动停止状态机，本状态里 stop 之后的动作不再执行"));
      break;
    case "buy_commodities":
      if (a.templates_from_plan === undefined) a.templates_from_plan = false;
      body.appendChild(boolField("templates_from_plan（要买什么由「跑商设置」里这一站勾的货物决定）", a.templates_from_plan, v => a.templates_from_plan = v));
      body.appendChild(multiSelectField("templates（商品，按住 Ctrl 多选；勾了上面这项就不看这里）", commodityTemplates(), a.templates, v => a.templates = v));
      body.appendChild(arrayField("list_roi", a.list_roi, v => a.list_roi = v));
      body.appendChild(arrayField("swipe_range（向下看的手势）", a.swipe_range, v => a.swipe_range = v));
      body.appendChild(numField("max_swipes（向下找几次）", a.max_swipes, v => a.max_swipes = v));
      body.appendChild(numField("max_swipes_up（向下找完再向上找几次，0=关闭）", a.max_swipes_up, v => a.max_swipes_up = v));
      body.appendChild(numField("swipe_pause_ms（每次滑动后等待毫秒）", a.swipe_pause_ms, v => a.swipe_pause_ms = v));
      body.appendChild(boolField("reset_to_top（整批买完后把列表滑回顶部）", a.reset_to_top, v => a.reset_to_top = v));
      body.appendChild(numField("click_wait_ms", a.click_wait_ms, v => a.click_wait_ms = v));
      body.appendChild(numField("threshold", a.threshold, v => a.threshold = v));
      body.appendChild(boolField("negotiation", a.negotiation, v => a.negotiation = v));
      body.appendChild(divHint("方向以「看到列表哪一段」为准：swipe_range 是向下看（手指上拖），向上找的手势由它自动反向推出。找不到时先向下找满 max_swipes 次，再向上找 max_swipes_up 次。"));
      body.appendChild(divHint("templates_from_plan=true 时这一批只买「跑商设置」里本次那一站勾上的货，按勾选的先后一件一件买，模板名 = 商品-<货物名称>；"
        + "一件都没勾就拒绝启动（不许空跑港口）。「表格」栏只是目录：它只决定这个港有没有这种货、以及它的类别，"
        + "不决定这一趟买不买它 —— 要换买什么去「跑商设置」栏改勾选，不用动这里。"));
      break;
    case "if":
      if (!a.condition) a.condition = defaultCondition("template");
      if (!Array.isArray(a.then)) a.then = [];
      if (!Array.isArray(a.else)) a.else = [];
      body.appendChild(divHint("条件："));
      body.appendChild(buildConditionNode(a.condition, null));
      body.appendChild(numField("condition_timeout_ms", a.condition_timeout_ms, v => a.condition_timeout_ms = v));
      body.appendChild(divHint("then（条件成立时执行）："));
      body.appendChild(buildActionsList(a.then));
      body.appendChild(divHint("else（条件不成立时执行）："));
      body.appendChild(buildActionsList(a.else));
      break;
    case "run_actions":
      if (!Array.isArray(a.actions)) a.actions = [];
      body.appendChild(divHint("子动作："));
      body.appendChild(buildActionsList(a.actions));
      break;
  }
}
function buildActionsList(list) {
  const wrap = document.createElement("div");
  wrap.className = "action-sublist";
  list.forEach(act => wrap.appendChild(buildActionNode(act, list)));
  const add = document.createElement("button");
  add.className = "btn small"; add.textContent = "+ 添加动作";
  add.onclick = () => { list.push(defaultAction("click")); refreshStateUI(); };
  wrap.appendChild(add);
  return wrap;
}
function addAction() {
  const s = curState();
  if (!s) return;
  if (!Array.isArray(s.actions)) s.actions = [];
  s.actions.push(defaultAction("click"));
  refreshStateUI();
}

/* ---------- 选中状态的基本字段 ---------- */
function renderEditor() {
  const box = $("state-editor");
  const s = curState();
  if (!s) {
    box.innerHTML = `<div class="muted">左侧选择一个状态，或点「+ 新建状态」</div>`;
    return;
  }
  if (!s.condition || typeof s.condition !== "object") s.condition = defaultCondition("template");
  if (!Array.isArray(s.actions)) s.actions = [];

  const allIds = stateData.states.map(x => x.id);
  box.innerHTML = `
    <div class="editor-section">
      <div class="field-row">
        <label class="field">名称<input type="text" value="${escapeHtml(s.name)}" onchange="editStateName(this.value)"></label>
        <label class="field">ID<input type="text" value="${escapeHtml(s.id)}" onchange="editStateId(this.value)"></label>
      </div>
      <div class="field-row">
        <label class="checkbox"><input type="checkbox" ${s.entry ? "checked" : ""} onchange="editStateEntry(this.checked)"> entry（入口）</label>
        <label class="checkbox"><input type="checkbox" ${s.global ? "checked" : ""} onchange="editStateGlobal(this.checked)"> global（全局）</label>
        <label class="field">module（哪个模块的状态）
          <input type="text" value="${escapeHtml(s.module || "")}" onchange="editStateModule(this.value)" placeholder="${s.global ? "全局状态不用填" : "如 buy / sail"}">
        </label>
        <label class="field">trip_entry（跑「完整一趟」时哪一类站从这一步进门）
          <select onchange="editStateTripEntry(this.value)">
            <option value="">（不是任何一段的入口）</option>
            ${TRIP_STAGES.map(([v, t]) => `<option value="${v}" ${v === s.trip_entry ? "selected" : ""}>${t}（${v}）</option>`).join("")}
          </select>
        </label>
      </div>
      ${s.trip_entry ? `<div class="muted hint">完整一趟走到「${(TRIP_STAGES.find(x => x[0] === s.trip_entry) || ["", s.trip_entry])[1]}」这一类站时，就从这一步开始 —— 引擎靠这句话找到链头，不在代码里写死状态 id。</div>` : ""}
      ${s.global ? "" : `<div class="muted hint">启动时按「跑商设置」里选的模块裁候选：module 不等于本次模块的状态这一趟根本不会被看到；漏填的话引擎会拒绝启动。</div>`}
      <div class="field-row">
        <label class="field">next（下一个状态）<select onchange="editStateNext(this.value)">
          <option value="">（无 / 终点）</option>
          ${allIds.map(id => `<option value="${escapeHtml(id)}" ${id === s.next ? "selected" : ""}>${escapeHtml(id)}</option>`).join("")}
        </select></label>
      </div>
    </div>

    <div class="editor-section">
      <h4>条件（condition）</h4>
      <div id="state-cond"></div>
      <button class="btn" onclick="testStateCondition()">测试此条件</button>
      <span class="muted"> 测的是已保存的条件，改完请先「保存」再测</span>
      <div id="state-cond-result" class="result-box" style="display:none"></div>
    </div>

    <div class="editor-section">
      <h4>动作（actions）</h4>
      <div id="state-actions"></div>
      <button class="btn" onclick="addAction()">+ 添加动作</button>
    </div>
  `;

  renderCondition(s.condition, $("state-cond"));
  renderActions(s.actions, $("state-actions"));
}
function editStateName(v) { const s = curState(); if (s) { s.name = v; renderStateList(); } }
function editStateId(v) { const s = curState(); if (s) { s.id = v; renderStateList(); } }
function editStateEntry(v) { const s = curState(); if (s) s.entry = v; }
function editStateGlobal(v) { const s = curState(); if (s) s.global = v; }
function editStateModule(v) { const s = curState(); if (s) { s.module = (v || "").trim(); renderStateList(); } }
function editStateTripEntry(v) {
  const s = curState();
  if (!s) return;
  if (v) s.trip_entry = v; else delete s.trip_entry;   // 留空就整个删掉，不留一个空字符串字段
}
function editStateNext(v) { const s = curState(); if (s) s.next = v || null; }

/* ---------- 条件测试 ---------- */
async function testStateCondition() {
  const s = curState();
  if (!s) return;
  const box = $("state-cond-result");
  if (!box) return;
  box.style.display = "block";
  box.className = "result-box";
  box.textContent = "测试中…（每次都要重新截图，OCR 条件较慢，请稍候）";
  try {
    const res = await fetch("/api/state/test_condition", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ state_id: s.id }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      box.className = "result-box ocr-fail";
      box.textContent = "测试失败: " + (body.detail || res.status);
      return;
    }
    const conf = body.confidence != null ? `，置信度 ${body.confidence}` : "";
    box.className = "result-box " + (body.matched ? "ocr-ok" : "ocr-fail");
    box.textContent = (body.matched ? `✅ 匹配成功${conf}` : `❌ 未匹配${conf}`)
      + "\n" + (body.detail || "");
  } catch (e) {
    box.className = "result-box ocr-fail";
    box.textContent = "请求失败: " + e.message + "（请确认 python app.py 已启动）";
  }
}

/* ---------- 运行控制 + 日志轮询 ---------- */
let statusTimer = null;     // 只在运行中轮询，停止时清掉
let lastLogKey = "";        // 日志指纹，内容没变就不重绘（避免闪烁/丢滚动位置）

function setRunButtons(running) {
  $("btn-state-start").disabled = running;
  $("btn-state-stop").disabled = !running;
  /* 2026-09-30：单步调试那两颗也归这里管 —— 「引擎在不在跑」全服务只有一个事实，
     之前 runDebugStep 自己 disable 之后就没有再放开的那一步，一趟跑完「运行一次」
     永远灰着，再点毫无反应（看着就像状态机没起来）。 */
  const sr = $("step-run"), ss = $("step-stop");
  if (sr) sr.disabled = running;
  if (ss) ss.disabled = !running;
  engineRunning = running;
  const dot = $("nav-dot-run");
  if (dot) dot.hidden = !running;   // 人在别的栏也要看得见「还在跑」
}

/* 秒数说成人话，和后端 restock.remaining_text 一个口径（前端不自己算剩余，只换算显示） */
function cnSeconds(sec) {
  const s = Math.max(0, Math.floor(sec || 0));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
  if (h) return `${h} 小时 ${m} 分`;
  if (m) return `${m} 分 ${x} 秒`;
  return `${x} 秒`;
}

/* 补货倒计时这一行：只把引擎 restock 字段里的事实念出来 —— 前端不倒数、不猜、
   也不给任何按钮（用户 2026-09-28 拍板：只读显示，不给按钮）。
   2026-09-30 起判据换成「按港口记的下次补货时刻」，所以这里多了一句「为什么等/为什么不等」，
   数字仍是上一次读数的结论，带更新时间，避免看着它在跳却不知道隔了多久。 */
function renderRestock(r) {
  const box = $("state-restock-info");
  if (!box) return;
  if (!r) {
    box.hidden = true;
    box.textContent = "";
    box.className = "restock-info";
    return;
  }
  const page = (r.page || []).join("、") || "认不出";
  const port = r.port || "（没有港口名）";
  const ledger = `账本: ${port} → ${r.stored_text || "没记过"}`
    + (r.next_text ? `；本次读到的记为 ${r.next_text}${r.saved === false ? "（没写进文件）" : ""}` : "");
  let text, cls;
  if (r.phase === "failed") {
    text = `补货等待失败：${r.reason || "引擎没给原因"}（醒来看过 ${r.polls} 次表，画面：${page}）`;
    cls = "restock-err";
  } else if (r.phase === "ready") {
    text = `补货等到点了：已重新进过一次「购买」标签，开始买货`
      + `（一共等了 ${cnSeconds(r.waited_seconds)}，${ledger}）`;
    cls = "restock-ok";
  } else if (r.phase === "direct") {
    text = `不用等补货，直接买：${r.decision_text || r.decision || "引擎没说原因"}`
      + `（${ledger}，读数『${r.read_text || "(空)"}』/ ${r.source || "?"}）`;
    cls = "restock-ok";
  } else {
    text = `等补货中：还剩约 ${r.remaining_text || cnSeconds(r.remaining_seconds)}`
      + `；已等 ${cnSeconds(r.waited_seconds)}，最多等 ${cnSeconds(r.max_wait_seconds)}`
      + `；醒来看表 ${r.polls} 次 ${r.updated_at}，画面：${page}；${ledger}`;
    cls = "restock-wait";
  }
  box.hidden = false;
  box.textContent = text;
  box.className = "restock-info " + cls;
}

function renderRunInfo(s) {
  const total = (s.run_rows || []).length;
  const done = Math.min(s.run_row_index || 0, total);
  const flag = s.run_state && s.run_state.error === undefined
    ? ` | 待卖账: ${s.run_state.sell_pending ? 1 : 0}` : "";
  const port = s.run_port
    ? (s.run_port_field === "sell_port"
        ? ` | 本次港口: ${s.run_port}（出货站，货舱里有什么卖什么）`
        : ` | 本次港口: ${s.run_port}（已跑完 ${done}/${total}${s.run_row ? `，待买『${s.run_row.goods_name}』` : "，全部买完"}）`)
    : "";
  /* 整趟进度：念引擎自己那份 trip（第几站 / 后面还有哪些站），前端不数站。
     单模块跑法 s.trip 是 null，这一段整块不出现。 */
  const t = s.trip;
  const trip = t && t.total
    ? ` | 整趟: ${t.pos >= 0 ? `第 ${t.pos + 1}/${t.total} 站` : `共 ${t.total} 站（还没定站在哪一站）`}`
      + `${t.port ? `『${t.port}』` : ""}（${(t.legs || []).map(l => l.text).join(" → ")}）`
    : "";
  $("state-run-info").textContent =
    (s.running ? "运行中" : "已停止")
    + ` | 当前状态: ${s.current_state || "—"}`
    + ` | 轮次: ${s.round != null ? s.round : 0}`
    + ` | 连续未匹配: ${s.no_match_count != null ? s.no_match_count : 0}`
    + trip
    + port
    + flag
    + (s.stop_reason ? ` | 停止原因: ${s.stop_reason}` : "");
}

function renderLogs(logs) {
  const last = (logs || []).slice(-20);
  const tail = last.length ? last[last.length - 1] : null;
  const key = last.length + "|" + (tail ? tail.time + tail.message : "");
  if (key === lastLogKey) return;      // 没新日志，跳过重绘
  lastLogKey = key;
  const box = $("state-logs");
  box.innerHTML = last.length
    ? last.map(l =>
        `<div class="log-line log-${escapeHtml(l.event)}">` +
        `<span class="log-time">${escapeHtml(l.time)}</span>` +
        `[${escapeHtml(l.event)}] ${escapeHtml(l.message)}</div>`).join("")
    : "暂无日志";
  box.scrollTop = box.scrollHeight;    // 自动滚到底
}

async function pollStatus() {
  try {
    /* 只打这一个接口：队列进度和引擎日志本来就是一起看的，分两次请求会出现
       「队列说在跑、引擎说没跑」那种中间态（见 app.py 的 queue_status 说明）。 */
    const s = await fetch("/api/queue/status").then(r => r.json());
    const q = s.queue || {};
    const e = s.engine || {};
    renderQueueProgress(q);
    renderRunInfo(e);
    renderRestock(e.restock);
    renderLogs(e.logs);
    renderStepResult(e);
    const running = !!q.running || !!e.running;
    setRunButtons(running);
    $("state-poll-status").textContent = running ? "（每 2 秒刷新）" : "";
    if (!running) stopPolling();     // 已停止就不继续轮询
  } catch (err) {
    $("state-poll-status").textContent = "状态查询失败: " + err.message;
    stopPolling();
  }
}

/* 队列进度那一行（「运行」栏）。队列没在跑时念的是队列配置 + 「改了没保存」的提示。 */
function renderQueueProgress(q) {
  const box = $("queue-run-info");
  if (!box) return;
  if (!q.running) { box.textContent = queueIdleInfo(); return; }
  const modeTxt = { once: "跑一遍", repeat: `重复 ${q.repeat} 遍`, infinite: "无限循环" }[q.mode] || q.mode;
  const passTxt = (q.mode === "repeat" || q.mode === "infinite") && q.pass_no > 1
    ? ` · 第 ${q.pass_no} 遍` : "";
  box.textContent = `队列运行中：${modeTxt}${passTxt}`
    + ` | 第 ${q.index}/${q.total} 个方案『${q.current || "—"}』`
    + (q.phase ? ` | ${q.phase}` : "")
    + (q.stop_reason ? ` | 上次停止：${q.stop_reason}` : "");
}

/* 单步调试的结果框：调试栏在跑的那条链的实时状态。只在「调试」栏写着，
   免得在别的栏也悄悄改一个看不见的框。 */
function renderStepResult(e) {
  const box = $("step-result");
  if (!box || currentPage !== "debug") return;
  if (!e.running) {
    if (e.stop_reason || e.current_state) {
      box.textContent = `已停止${e.current_state ? `（最后状态 ${e.current_state}，轮次 ${e.round != null ? e.round : 0}）` : ""}`
        + `${e.stop_reason ? "：" + e.stop_reason : ""}`;
    }
    return;
  }
  box.textContent = `运行中 · 当前状态 ${e.current_state || "—"}`
    + ` · 轮次 ${e.round != null ? e.round : 0}`
    + ` · 连续未匹配 ${e.no_match_count != null ? e.no_match_count : 0}`;
}

function startPolling() {
  if (statusTimer) return;
  pollStatus();
  statusTimer = setInterval(pollStatus, 2000);
}
function stopPolling() {
  if (statusTimer) { clearInterval(statusTimer); statusTimer = null; }
}

/* ---------- 运行队列（「运行」栏）：挑 1~n 个方案排成一队，交给后端线程按顺序跑 ----------
   队列编排本身存在 run_queue.json，但真正跑它的是后端线程 —— 关掉浏览器也继续跑。
   界面这份是草稿：点「启动队列」时把草稿一起交过去（后端 start 认 items/mode/repeat），
   所以「改了没点保存就启动」也不会跑成别的一队。 */
let queueData = null;     // 界面草稿：{items, mode, repeat}
let queueSaved = null;    // run_queue.json 里那份（用来判断「有改动还没保存」）
let queueView = null;     // 最近一次 /api/queue 回来的整份（含方案库摘要）

function queueIdleInfo() {
  if (!queueData) return "队列未运行";
  const n = (queueData.items || []).length;
  const modeTxt = { once: "跑一遍", repeat: `重复 ${queueData.repeat} 遍`, infinite: "无限循环" }[queueData.mode] || queueData.mode;
  return `队列未运行：${n} 个方案，${modeTxt}`
    + (queueDirty() ? "（上面的改动还没保存 —— 点「启动队列」会直接用这份，点「保存队列」才写 run_queue.json）" : "");
}

function queueDirty() {
  return !!queueSaved && !!queueData
    && JSON.stringify(queueData) !== JSON.stringify(queueSaved);
}

async function loadQueue() {
  let view;
  try {
    view = await getJson("/api/queue");
  } catch (e) {
    const box = $("queue-run-info");
    if (box) box.textContent = "读队列失败: " + e.message + "（请确认后端已启动）";
    return;
  }
  queueView = view;
  queueSaved = { items: view.items.slice(), mode: view.mode, repeat: view.repeat };
  queueData = { items: view.items.slice(), mode: view.mode, repeat: view.repeat };
  const modeEl = $("queue-mode");
  if (modeEl) modeEl.value = queueData.mode;
  const repEl = $("queue-repeat");
  if (repEl) { repEl.value = queueData.repeat; repEl.disabled = queueData.mode !== "repeat"; }
  renderQueuePresetOptions();
  renderQueue();
}

/* 下拉里是方案库（后端摘要）。队列里加的是**方案名**，所以 value 直接是名字。 */
function renderQueuePresetOptions() {
  const sel = $("queue-preset-select");
  if (!sel) return;
  const list = (queueView && queueView.presets) || [];
  sel.innerHTML = list.length
    ? list.map(p => `<option value="${escapeHtml(p.name)}">${escapeHtml(p.name)} · ${p.stop_count} 站</option>`).join("")
    : `<option value="">（方案库是空的，先去「跑商设置」编一个）</option>`;
}

function renderQueue() {
  const box = $("queue-list");
  if (!box) return;
  const items = (queueData && queueData.items) || [];
  if (!items.length) {
    box.innerHTML = `<div class="muted">（队列是空的）—— 上面挑一个方案点「+ 加入队列」</div>`;
    renderQueueProgress({ running: false });
    return;
  }
  /* 「失效」由方案库现算，不看后端 missing 字段：missing 是按**文件里那份**算的，
     而这里的列表可能是还没保存的草稿。 */
  const known = new Set(((queueView && queueView.presets) || []).map(p => p.name));
  box.innerHTML = items.map((n, i) => `
    <div class="stop-row">
      <span class="stop-seq">${i + 1}</span>
      <span class="queue-name">${escapeHtml(n)}</span>
      ${known.has(n) ? "" : `<span class="route-off">方案库里已经没有它了</span>`}
      <span class="stop-btns">
        <button class="btn small" onclick="queueMove(${i}, -1)"${i <= 0 ? " disabled" : ""} title="和上一个换位置">↑</button>
        <button class="btn small" onclick="queueMove(${i}, 1)"${i >= items.length - 1 ? " disabled" : ""} title="和下一个换位置">↓</button>
        <button class="btn small danger" onclick="queueRemove(${i})" title="从队列里去掉">删</button>
      </span>
    </div>`).join("");
  renderQueueProgress({ running: false });
}

function queueAdd() {
  const sel = $("queue-preset-select");
  const name = ((sel && sel.value) || "").trim();
  if (!name) { alert("方案库是空的 —— 先去「跑商设置」栏编一个方案。"); return; }
  if (!queueData) queueData = { items: [], mode: "once", repeat: 1 };
  queueData.items.push(name);   // 同一个方案可以排两次（队列是序列，不是集合）
  renderQueue();
}
function queueRemove(i) {
  if (!queueData) return;
  queueData.items.splice(i, 1);
  renderQueue();
}
function queueMove(i, delta) {
  if (!queueData) return;
  const items = queueData.items;
  const to = i + delta;
  if (i < 0 || to < 0 || to >= items.length) return;
  [items[i], items[to]] = [items[to], items[i]];
  renderQueue();
}
function onQueueModePick(v) {
  if (!queueData) return;
  queueData.mode = v;
  const repEl = $("queue-repeat");
  if (repEl) repEl.disabled = v !== "repeat";
  renderQueue();
}
function onQueueRepeat(v) {
  if (!queueData) return;
  const n = parseInt(v, 10);
  if (n >= 1) queueData.repeat = n;
  renderQueueProgress({ running: false });
}

/* 「刷新方案库」只重读方案库摘要、更新下拉和失效标记 —— **不动队列草稿**（免得把没保存的编排冲掉）。 */
async function queueRefresh() {
  let view;
  try {
    view = await getJson("/api/queue");
  } catch (e) {
    const box = $("queue-run-info");
    if (box) box.textContent = "刷新方案库失败: " + e.message;
    return;
  }
  queueView = view;
  renderQueuePresetOptions();
  renderQueue();
}

async function saveQueue() {
  if (!queueData) return;
  let res, out;
  try {
    res = await fetch("/api/queue", {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(queueData),
    });
    out = await res.json().catch(() => ({}));
  } catch (e) { alert("保存队列失败: " + e.message); return; }
  if (!res.ok) { alert("保存队列失败：" + (out.detail || res.status)); return; }
  queueView = out;
  queueSaved = { items: out.items.slice(), mode: out.mode, repeat: out.repeat };
  queueData = { items: out.items.slice(), mode: out.mode, repeat: out.repeat };
  renderQueuePresetOptions();
  renderQueue();
}

async function startQueue() {
  const btn = $("btn-state-start");
  btn.disabled = true;
  if (!queueData) { btn.disabled = false; alert("队列还没读出来，等一下再点。"); return; }
  const items = queueData.items || [];
  if (!items.length) {
    btn.disabled = false;
    alert("队列是空的 —— 先挑 1~n 个方案排进去。");
    return;
  }
  const modeTxt = { once: "每个方案各跑一遍就结束", repeat: `整队重复 ${queueData.repeat} 遍`,
                    infinite: "无限循环（要手动停）" }[queueData.mode] || queueData.mode;
  /* 买货、出港、卖货都花真金币、不可逆，所以点下去之前把「跑谁、跑几遍」念清楚再问一次。 */
  if (!confirm(`启动队列？\n顺序：${items.join(" → ")}\n循环：${modeTxt}\n\n`
    + "每一趟都是完整流程（买货 → 可选中转 → 出货），跑在后端线程里 —— 关掉浏览器也继续。\n"
    + "每趟启动前会自己 OCR 读一次当前港口；读到的港不在进货港里会弹窗问你。\n"
    + "确认人在屏幕前再点确定。")) {
    btn.disabled = false;
    return;
  }
  let res, out;
  try {
    res = await fetch("/api/queue/start", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items, mode: queueData.mode, repeat: queueData.repeat }),
    });
    out = await res.json().catch(() => ({}));
  } catch (e) {
    btn.disabled = false;
    alert("启动队列失败: " + e.message + "（请确认 python app.py 已启动）");
    return;
  }
  if (!res.ok) { btn.disabled = false; alert("启动队列失败：" + (out.detail || res.status)); return; }
  setRunButtons(true);   // 先把按钮和导航栏红点亮起来，不等第一次轮询回来
  startPolling();
}

async function stopQueue() {
  let res, out;
  try {
    res = await fetch("/api/queue/stop", { method: "POST" });
    out = await res.json().catch(() => ({}));
  } catch (e) { alert("停止队列失败: " + e.message); return; }
  if (!res.ok) { alert("停止队列失败：" + (out.detail || res.status)); return; }
  pollStatus();   // 拉一次最新状态，都停了 pollStatus 会自己停掉轮询
}

/* ---------- 调试栏 · 单步调试：选一个模块 + 一个港口直接跑一次 ----------
   不写 route_plan.json、不参与方案库和队列（理由见 app.py 的 debug_step）。 */
let stepCatalog = {};     // 港口 -> 该港买得到哪些货（从 /api/plan 读，和方案编辑器同一个来源）
let stepGoods = [];       // 买货要勾的货，顺序 = 点的先后
let stepGoodsPort = "";   // 上面那串货是哪个港口的（换港就清空，免得挂着买不到的货）

async function loadStepOptions() {
  const sel = $("step-module");
  if (sel && !sel.options.length) {
    sel.innerHTML = STEP_MODULES.map(m => `<option value="${m}">${escapeHtml(moduleLabel(m))}</option>`).join("");
  }
  try {
    const plan = await getJson("/api/plan");
    stepCatalog = plan.catalog && typeof plan.catalog === "object" ? plan.catalog : {};
    const ports = Array.isArray(plan.port_options) ? plan.port_options : [];
    const dl = $("step-port-options");
    if (dl) dl.innerHTML = ports.map(p => `<option value="${escapeHtml(p)}"></option>`).join("");
  } catch (e) {
    const st = $("step-status");
    if (st) st.textContent = "读「表格」栏目录失败: " + e.message + "（港口候选和勾货框会不完整）";
  }
  renderStepGoods();
}

function renderStepGoods() {
  const box = $("step-goods");
  if (!box) return;
  const mod = ($("step-module").value || "").trim();
  if (mod !== "buy") { box.hidden = true; box.innerHTML = ""; return; }
  box.hidden = false;
  const port = ($("step-port").value || "").trim();
  if (port !== stepGoodsPort) { stepGoods = []; stepGoodsPort = port; }
  if (!port) {
    box.innerHTML = `<div class="goods-tip">先填「本次港口」—— 填好这里列出它在「表格」栏买得到的货，勾上的是这次要买的。</div>`;
    return;
  }
  const rows = stepCatalog[port] || [];
  if (!rows.length) {
    box.innerHTML = `<div class="goods-tip"><b class="route-off">「表格」栏里港口『${escapeHtml(port)}』一行货都没有</b>`
      + ` —— 挑不出货，跑买货点运行会被拒绝。<button class="btn tiny" onclick="switchPage('plan')">去表格栏加货</button></div>`;
    return;
  }
  box.innerHTML = `<div class="goods-head">这次买什么（从『${escapeHtml(port)}』的目录里勾，编号 = 先后）</div>`
    + rows.map(r => {
        const name = (r.goods_name || "").trim();
        const pos = stepGoods.indexOf(name);
        const on = pos >= 0;
        const nm = escapeHtml(name);
        return `<div class="goods-row${on ? " on" : ""}">
          <label><input type="checkbox" data-name="${nm}"${on ? " checked" : ""}
                 onchange="onStepGoods(this.dataset.name, this.checked)">
          <span class="goods-name">${nm}</span></label>
          <span class="goods-cargo">${escapeHtml(r.cargo_type || "—")}</span>
          ${on ? `<span class="goods-order">${pos + 1}</span>` : ""}
        </div>`;
      }).join("");
}

function onStepGoods(name, checked) {
  if (!name) return;
  const keep = stepGoods.filter(g => g !== name);
  stepGoods = checked ? [...keep, name] : keep;
  renderStepGoods();
}

async function runDebugStep() {
  const btn = $("step-run");
  const st = $("step-status");
  const mod = ($("step-module").value || "").trim();
  const port = ($("step-port").value || "").trim();
  const cur = ($("step-current").value || "").trim();
  if (!port) { alert("先填「本次港口」—— 要买 / 要开去 / 要卖的港口。"); return; }
  if (mod === "buy" && !stepGoods.length) {
    alert("买货要勾至少一件货 —— 买货花真金币、不可逆，空清单不许起。");
    return;
  }
  if (!confirm(`单步跑一次？\n模块：${moduleLabel(mod)}\n本次港口：${port}\n`
    + (mod === "buy" ? `要买的货：${stepGoods.join(" → ")}\n` : "")
    + `当前所在港：${cur || "（留空 → 点运行先 OCR 读一次）"}\n\n`
    + "这是试跑：不写 route_plan.json、不参与方案库和队列；买货花真金币、不可逆。")) return;
  btn.disabled = true;      // POST 在飞，先防连点；成败都交给 setRunButtons 收尾
  // 留空港口时后端要先 OCR 读一次（第一次要等 EasyOCR 模型加载，实测可能几十秒）。
  // 这段时间不给话，人只会以为「点了没反应」然后反复点 —— 所以先写一句正在做什么。
  if (st) st.textContent = cur
    ? "正在启动…"
    : "正在启动…（「当前所在港」留空，先 OCR 读一次港口，第一次读要等模型加载）";
  let res, out;
  try {
    res = await fetch("/api/debug/step", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ module: mod, port, current_port: cur, goods: stepGoods }),
    });
    out = await res.json().catch(() => ({}));
  } catch (e) {
    setRunButtons(false);
    if (st) st.textContent = "";
    alert("运行失败: " + e.message + "（请确认 python app.py 已启动）"); return;
  }
  if (!res.ok) {
    setRunButtons(false);
    if (st) st.textContent = "";
    alert("运行失败：" + (out.detail || res.status)); return;
  }
  if (st) st.textContent = `已启动：${moduleLabel(mod)} · ${port}`
    + (out.current_port ? `（当前港 ${out.current_port}）` : "");
  setRunButtons(true);
  startPolling();
}

async function stopDebugStep() {
  let res, out;
  try {
    res = await fetch("/api/state/stop", { method: "POST" });
    out = await res.json().catch(() => ({}));
  } catch (e) { alert("停止失败: " + e.message); return; }
  if (!res.ok) { alert("停止失败：" + (out.detail || res.status)); return; }
  pollStatus();
}

/* ============ 设置：引擎参数（states.json 的 config 段）+ 设备信息 ============ */
let cfgData = null;     // 从服务端读到的 config 原样
let cfgDraft = null;    // 这一栏页面上的草稿，保存时只提交表单里有的项

/* 表单结构和后端 CONFIG_SPEC 一一对应：范围也照抄，免得填了才被 400 退回。
   group 表示这一节改的是嵌套对象里的子键；不带 group 的就是 config 顶层键。 */
const CONFIG_FORM = [
  {
    title: "循环与停止条件",
    hint: "每轮 = 截一张图 → 找一个命中的状态 → 执行它的动作。间隔太小会连点，太大自然慢。下限锁 200ms：单张 ADB 截图实测就要 ~2.8 秒，比这再小也只是让引擎空转。",
    fields: [
      { key: "interval_ms", label: "每轮间隔 (ms)", type: "num", min: 200, max: 600000 },
      { key: "max_rounds", label: "最多跑几轮", type: "num", min: 1, max: 10000 },
      { key: "max_no_match", label: "连续几轮不匹配就停", type: "num", min: 1, max: 1000 },
      { key: "action_timeout_seconds", label: "单个动作超时 (秒)", type: "num", min: 1, max: 3600 },
    ],
  },
  {
    group: "human_click",
    title: "人类点击模拟",
    hint: "落点在矩形里内缩 inset_ratio 后随机取，点击前随机等 delay_min~delay_max 毫秒。",
    fields: [
      { sub: "enabled", label: "开启", type: "bool" },
      { sub: "delay_min_ms", label: "点前最少等 (ms)", type: "num", min: 0, max: 60000 },
      { sub: "delay_max_ms", label: "点前最多等 (ms)", type: "num", min: 0, max: 60000 },
      { sub: "inset_ratio", label: "内缩比例 (0~0.45)", type: "num", min: 0, max: 0.45 },
    ],
  },
  {
    group: "exit_to_port",
    title: "启动前退回港口",
    hint: "启动时先按这几张模板找「房子 / X」并点掉，最多 max_clicks 次，再用 port_marker 确认已经到港口。",
    fields: [
      { sub: "enabled", label: "开启", type: "bool" },
      { sub: "buttons", label: "要点掉的按钮模板（可多选）", type: "multiTpl" },
      { sub: "port_marker", label: "到港标志模板", type: "tplSelect" },
      { sub: "max_clicks", label: "最多点几次", type: "num", min: 0, max: 20 },
      { sub: "wait_ms", label: "每次点完等 (ms)", type: "num", min: 0, max: 60000 },
    ],
  },
  {
    group: "negotiation",
    title: "谈价（协商）按钮",
    hint: "协商弹窗里的三个按钮位置写死成坐标；region 是认「进行1次/进行所有剩余机会」这些字的 OCR 命名区域。",
    fields: [
      { sub: "region", label: "OCR 命名区域", type: "ocrSelect" },
      { sub: "max_clicks", label: "一轮最多谈几次", type: "num", min: 0, max: 20 },
      { sub: "click_interval_ms", label: "两次点击间隔 (ms)", type: "num", min: 0, max: 60000 },
      { sub: "buttons", label: "三个按钮坐标", type: "points" },
    ],
  },
];
const POINT_LABELS = { no: "取消", once: "只谈一次", all: "全部剩余机会" };
let cfgInvalid = new Set();     // 留空或填了非数字的字段路径；有一个就不许保存

/* 设置栏专用的数字框：不能用现成的 numField —— 它把空着的内容 parseFloat 成 0 直接交上去，
   「每轮间隔」被误清空就会写成 0 毫秒，等于告诉引擎别停顿。这里宁可拦住不保存。 */
function cfgNumField(label, value, path, min, max) {
  const inp = document.createElement("input");
  inp.type = "number"; inp.step = "any"; inp.value = value == null ? 0 : value;
  const f = fieldWrap(label, inp);
  const msg = document.createElement("div");
  msg.className = "cfg-bad"; msg.hidden = true;
  f.appendChild(msg);
  const flag = text => {
    cfgInvalid.add(path.join("."));
    f.classList.add("field-bad");
    msg.hidden = false; msg.textContent = text;
  };
  inp.onchange = () => {
    const text = String(inp.value).trim();
    const n = Number(text);
    if (!text || Number.isNaN(n)) return flag("这里要填一个数字，留空不算数");
    if (min != null && n < min) return flag(`这一项最小是 ${min}`);
    if (max != null && n > max) return flag(`这一项最大是 ${max}`);
    cfgInvalid.delete(path.join("."));
    f.classList.remove("field-bad"); msg.hidden = true;
    cfgSet(path, n);
  };
  return f;
}

function cfgGet(path) {
  let node = cfgDraft;
  for (const p of path) {
    if (node == null) return null;
    node = node[p];
  }
  return node === undefined ? null : node;
}

function cfgSet(path, value) {
  let node = cfgDraft;
  for (let i = 0; i < path.length - 1; i++) {
    if (typeof node[path[i]] !== "object" || node[path[i]] === null) node[path[i]] = {};
    node = node[path[i]];
  }
  node[path[path.length - 1]] = value;
}

function renderConfigForm() {
  const box = $("cfg-form");
  box.innerHTML = "";
  cfgInvalid = new Set();      // 重新渲染就是重新校验，旧的红框记录不算了
  CONFIG_FORM.forEach(sec => {
    const wrap = document.createElement("div");
    wrap.className = "cfg-group";
    const h = document.createElement("h4");
    h.textContent = sec.title;
    wrap.appendChild(h);
    if (sec.hint) wrap.appendChild(divHint(sec.hint));
    const row = document.createElement("div");
    row.className = "field-row";
    wrap.appendChild(row);

    sec.fields.forEach(f => {
      const path = f.key ? [f.key] : [sec.group, f.sub];
      const value = cfgGet(path);
      if (f.type === "points") {
        const dict = value || {};
        Object.keys(POINT_LABELS).forEach(k => {
          row.appendChild(arrayField(`按钮坐标 ${POINT_LABELS[k]} (x,y)`, dict[k], v => {
            const next = Object.assign({}, cfgGet(path));
            if (v && v.length === 2) next[k] = v; else delete next[k];
            cfgSet(path, next);
          }));
        });
        return;
      }
      if (f.type === "bool") {
        row.appendChild(boolField(f.label, !!value, v => cfgSet(path, v)));
      } else if (f.type === "num") {
        row.appendChild(cfgNumField(f.label, value, path, f.min, f.max));
      } else if (f.type === "multiTpl") {
        row.appendChild(multiSelectField(f.label, templateNames(), Array.isArray(value) ? value : [],
                                         v => cfgSet(path, v)));
      } else if (f.type === "tplSelect") {
        row.appendChild(selectField(f.label, templateNames(), value, v => cfgSet(path, v)));
      } else if (f.type === "ocrSelect") {
        row.appendChild(selectField(f.label, ocrRegionNames(), value, v => cfgSet(path, v)));
      } else {
        row.appendChild(textField(f.label, value, v => cfgSet(path, v)));
      }
    });
    box.appendChild(wrap);
  });

  // 表单没覆盖到的 config 项：只列出来，不编辑也不回传，服务端那段原样保留
  const known = new Set(CONFIG_FORM.flatMap(sec => sec.fields.map(f => f.key || sec.group)));
  const extra = Object.keys(cfgData || {}).filter(k => !known.has(k));
  if (extra.length) {
    const box2 = document.createElement("div");
    box2.className = "cfg-group";
    box2.innerHTML = `<h4>其它项（设置栏还不认识，只读）</h4>`;
    box2.appendChild(divHint("这几项不在表单里，保存时不会被动到；要改请直接编辑 states.json。"));
    const pre = document.createElement("pre");
    pre.className = "result-box";
    pre.textContent = JSON.stringify(
      extra.reduce((o, k) => { o[k] = cfgData[k]; return o; }, {}), null, 2);
    box2.appendChild(pre);
    box.appendChild(box2);
  }
}

/* 只把表单里出现的键交出去：服务端见到白名单外的键会 400，而且我们也不该覆盖没看过的项 */
function configPatch() {
  const patch = {};
  CONFIG_FORM.forEach(sec => sec.fields.forEach(f => {
    if (f.key) {
      if (cfgDraft[f.key] !== undefined) patch[f.key] = cfgDraft[f.key];
      return;
    }
    const group = cfgDraft[sec.group];
    if (group && group[f.sub] !== undefined) {
      patch[sec.group] = Object.assign(patch[sec.group] || {}, { [f.sub]: group[f.sub] });
    }
  }));
  return patch;
}

async function loadSettings() {
  const status = $("cfg-save-status");
  status.textContent = "读取中…";
  try {
    const doc = await fetch("/api/state/states").then(r => r.json());
    cfgData = doc.config || {};
    cfgDraft = JSON.parse(JSON.stringify(cfgData));
    renderConfigForm();
    status.textContent = "";
  } catch (e) {
    status.textContent = "读取引擎参数失败: " + e.message + "（请确认 python app.py 已启动）";
  }
  loadDeviceInfo();
}

async function saveConfig() {
  const status = $("cfg-save-status");
  if (cfgInvalid.size) {
    status.textContent = `有 ${cfgInvalid.size} 项没填对（红框那几格），先改对再保存`;
    return;
  }
  status.textContent = "保存中…";
  let res, body;
  try {
    res = await fetch("/api/state/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(configPatch()),
    });
    body = await res.json().catch(() => ({}));
  } catch (e) {
    status.textContent = "请求失败: " + e.message;
    return;
  }
  if (!res.ok) {
    status.textContent = "保存失败: " + (body.detail || res.status);
    return;
  }
  cfgData = body.config || {};
  cfgDraft = JSON.parse(JSON.stringify(cfgData));
  renderConfigForm();
  status.textContent = "已写入 states.json，下一次点「启动状态机」才生效";
}

async function loadDeviceInfo() {
  const box = $("device-info");
  box.textContent = "读取中…";
  let info;
  try {
    info = await fetch("/api/info").then(r => r.json());
  } catch (e) {
    box.textContent = "读取失败: " + e.message;
    return;
  }
  // 中文标签长短不一，用等宽空格对不齐，所以做成两列网格而不是拼字符串
  const row = (k, v, cls) =>
    `<div class="dev-row"><span class="dev-k">${escapeHtml(k)}</span>` +
    `<span class="dev-v ${cls || ""}">${escapeHtml(v)}</span></div>`;
  const html = [
    `<div class="dev-sec">运行环境</div>`,
    row("后端", info.backend),
    row("ADB 程序", info.adb_exists ? info.adb_path : "【找不到文件】" + info.adb_path, "mono"),
    row("设备地址", info.device_addr, "mono"),
    info.device_ready
      ? row("设备状态", "就绪（device）", "ok")
      : row("设备状态", `未就绪（${info.device_state || "未找到设备"}）—— 模拟器可能没开`, "bad"),
    row("画面分辨率", info.resolution.join(" × ")),
    `<div class="dev-sec">现在有多少条目</div>`,
    ...Object.entries(info.counts).map(([k, n]) => row(k, String(n))),
    `<div class="dev-sec">数据文件（改 .py 要重启进程才生效；这些 json 不用）</div>`,
    ...Object.entries(info.files).map(([k, p]) => row(k, p, "mono")),
  ].join("");
  box.innerHTML = html;
}


/* ============ 跑商购物表格 ============ */
let planData = null;      // {cargo_types:[...], rows:[...]}，从 /api/plan 读入
let planDirty = false;    // 有未保存的改动（切标签页/关页面前提醒）

async function loadPlan() {
  const status = $("plan-save-status");
  try {
    planData = await fetch("/api/plan").then(r => r.json());
  } catch (e) {
    status.textContent = "读取表格失败: " + e.message + "（请确认后端已启动）";
    return;
  }
  if (!Array.isArray(planData.rows)) planData.rows = [];
  if (!Array.isArray(planData.cargo_types)) planData.cargo_types = [];
  if (!Array.isArray(planData.goods_options)) planData.goods_options = [];
  planDirty = false;
  planEdit = null;              // 重新读取 = 回到一级清单，别停在任何旧下标上
  planPort = null;
  planPortQuery = "";
  const ports = new Set(planData.rows.map(r => (r.port || "").trim()).filter(Boolean));
  const g = planData.goods_options.length;
  $("plan-save-status").textContent =
    `表格里有 ${planData.rows.length} 行货，分在 ${ports.size} 个港口下（点港口进货物清单，再点一件货编辑）；` +
    (g ? `货物名称可搜 ${g} 件（模板库里的 商品-*，打几个字从下拉里点）`
       : "模板库里还没有 商品-* 模板，货物名称的候选是空的 —— 先去「模板库」页框一张");
  renderPlan();
}

/* 存盘是平铺的 rows，编辑时按港口分组显示：一个港口一组，组里是它的几种货。
   分组的锚点用「这一组第一行的下标」而不是港口名 —— 港口名边打字边变，
   拿下标当锚点才不会打两个字就找不到自己那一组。anchor 只在本次渲染内有效。 */
function planGroups() {
  const groups = [];
  const byLabel = new Map();
  planData.rows.forEach((row, i) => {
    const port = (row.port || "").trim();
    const label = port || "（未填港口）";
    let g = byLabel.get(label);
    if (!g) { g = { port, label, indices: [] }; byLabel.set(label, g); groups.push(g); }
    g.indices.push(i);
  });
  const k = groups.findIndex(g => !g.port);          // 没填港口的组挪到最后，别夹在中间
  if (k >= 0) groups.push(...groups.splice(k, 1));
  return groups;
}

/* 一级点的是「港口名」，所以二/三级认的是 label（港口名）而不是行下标 ——
   删行 / 加行会让下标漂移，label 不会；组头改港口名时同步改一次就好。 */
function planGroupByLabel(label) {
  return planGroups().find(g => g.label === label) || null;
}

/* 一级港口清单的排序：按港口名的拼音排（localeCompare 带 zh 排序规则，
   「北京」排在「上海」前面 = 按首字母）；没填港口名的那组永远垫底。 */
function planSortedGroups() {
  const all = planGroups();
  const named = all.filter(g => g.port)
    .sort((a, b) => a.label.localeCompare(b.label, "zh-Hans-CN"));
  return named.concat(all.filter(g => !g.port));
}

function groupByAnchor(anchor) {
  return planGroups().find(g => g.indices.includes(anchor)) || null;
}

/* 三级界面（2026-09-30 你要的：一级只剩港口名，点开才编辑）：
     一级 = 港口清单（只有港口名，可搜、按首字母排）
     二级 = 这个港的货物清单（港口名可改 + 加货 / 删组 + 每件货一行）
     三级 = 某一件货的具体内容（货物 / 类别 / 备注）
   planPort 记着一级选中的港口名，planEdit 记着三级在编辑第几行；都只活在内存里：
   不进 rows、不落盘、也不跟着 /api/plan 走 —— 重新读取表格就退回一级。 */
let planEdit = null;      // 三级正在编辑第几行；null = 不在这一层
let planPort = null;      // 一级选中的港口名（组的 label）；null = 停在一级港口清单
let planPortQuery = "";   // 一级搜索框里的字，只筛显示、不改数据

function planDetailEl() {
  return document.querySelector("#plan-list .plan-detail");
}

function renderPlan() {
  const box = $("plan-list");
  if (!planData) { box.innerHTML = ""; return; }
  const g = planPort === null ? null : planGroupByLabel(planPort);
  if (!g) { planPort = null; planEdit = null; renderPortList(); return; }  // 那个港口没了（改名 / 删光）就退回一级
  if (planEdit !== null && planData.rows[planEdit]) { renderPlanDetail(); return; }
  planEdit = null;              // 那一行已经没了（删掉 / 重新读取后行数变少）就退回二级，别对着空下标渲染
  renderPortGoods(g);
}

/* 一级：只剩港口名（+ 种数）。搜索框只筛显示；点一行进二级。 */
function renderPortList() {
  const box = $("plan-list");
  box.innerHTML = "";
  if (!planData.rows.length) {
    box.innerHTML = `<div class="muted">表格还是空的，点上面「+ 新增一个港口」开第一组</div>`;
    return;
  }
  const all = planSortedGroups();
  const named = all.filter(g => g.port);
  const unnamed = all.filter(g => !g.port);
  const q = planPortQuery.trim().toLowerCase();
  const shown = q ? named.filter(g => g.label.toLowerCase().includes(q)) : named;
  const rowHtml = g => `
    <div class="plan-port-row" data-port="${escapeHtml(g.label)}" onclick="openPlanPort(this.dataset.port)"
         title="点开看这个港有哪些货，再点一件货编辑">
      <span class="plan-port-name">${escapeHtml(g.label)}</span>
      <span class="plan-port-count">${g.indices.length} 种货</span>
      <span class="plan-port-go">打开 ›</span>
    </div>`;
  box.innerHTML = `
    <div class="plan-port-searchbar">
      <input type="text" id="plan-port-search" class="plan-port-search" placeholder="搜索港口名（打几个字就筛）"
             value="${escapeHtml(planPortQuery)}" oninput="setPortQuery(this.value)">
      <span class="plan-port-sum">${named.length} 个港口${q ? `，筛出 ${shown.length} 个` : ""}</span>
    </div>
    ${shown.map(rowHtml).join("") || (q ? `<div class="muted">没有名字带「${escapeHtml(planPortQuery.trim())}」的港口</div>` : "")}
    ${unnamed.map(rowHtml).join("")}`;
}

function setPortQuery(v) {
  planPortQuery = v;
  renderPortList();
  const inp = $("plan-port-search");   // 整个列表重画了，把光标还给搜索框、停在末尾，能接着打
  if (inp) { inp.focus(); inp.setSelectionRange(inp.value.length, inp.value.length); }
}

function openPlanPort(label) {
  planPort = label;
  planEdit = null;
  renderPlan();
}

function backToPortList() {
  planPort = null;
  planEdit = null;
  renderPlan();
}

/* 二级：这个港有哪些货（每件货一行，点一行进三级）；港口名在这里改，改一次整组生效。 */
function renderPortGoods(g) {
  const anchor = g.indices[0];
  $("plan-list").innerHTML = `
    <div class="plan-group">
      <div class="plan-group-head">
        <button class="btn small" onclick="backToPortList()">‹ 返回港口列表</button>
        <input type="text" class="plan-port" placeholder="还没填港口" value="${escapeHtml(g.port)}"
               oninput="setGroupPort(${anchor}, this.value)" title="改一次，这一组的每一行都跟着改">
        <span class="plan-group-count">${g.indices.length} 种货</span>
        <span class="plan-group-actions">
          <button class="btn small" onclick="addPlanGoods(${anchor})">+ 加一种货</button>
          <button class="btn small danger" onclick="deletePlanGroup(${anchor})">删整组</button>
        </span>
      </div>
      <div class="plan-goods">${g.indices.map(i => planLineHtml(i)).join("")}</div>
    </div>`;
}

/* 二级里的一行 = 一件货的入口，不是编辑器：只列货名，点进去才改（类别 / 备注在三级）。
   模板库里查不到的那件加个红点（整句人话在三级里）。 */
function planLineHtml(i) {
  const row = planData.rows[i];
  return `
    <div class="plan-line${goodsWarn(row.goods_name) ? " warn" : ""}"
         onclick="openPlanRow(${i})" title="点一下编辑这一件货">
      <span class="plan-line-goods">${escapeHtml(row.goods_name || "（还没选货物）")}</span>
      <span class="plan-line-go">编辑 ›</span>
    </div>`;
}

function openPlanRow(i) {
  planEdit = i;
  renderPlan();
}

function closePlanRow() {
  planEdit = null;      // 退回二级（这个港的货物清单）
  renderPlan();
}

/* 三级：这一件货的具体内容全在这里。港口不在这里填 —— 它是整组共用的一个值，
   在这里改会出现「只改了一件、别的还挂在旧港口名上」的分裂，所以只显示、要去二级改。 */
function renderPlanDetail() {
  const i = planEdit;
  const row = planData.rows[i];
  const typeOpts = planData.cargo_types.map(t =>
    `<option value="${escapeHtml(t)}"${t === row.cargo_type ? " selected" : ""}>${escapeHtml(t)}</option>`
  ).join("");
  $("plan-list").innerHTML = `
    <div class="plan-detail" data-row="${i}">
      <div class="plan-detail-head">
        <button class="btn small" onclick="closePlanRow()">‹ 返回货物清单</button>
        <span class="plan-detail-title">${escapeHtml(row.goods_name || "未填货物")}</span>
        <span class="plan-detail-port">属于港口『${escapeHtml((row.port || "").trim() || "（未填港口）")}』</span>
        <button class="btn small danger" onclick="deletePlanRow(${i})">删除本行</button>
      </div>
      <div class="field-row">
        <div class="field pgoods-field">
          <div class="pgoods-box">
            <label>货物名称（打几个字筛，从下面下拉里点一条才算选中，不能手打）
              <input type="text" class="plan-goods-search" placeholder="比如只打一个「版」字"
                     oninput="refreshGoodsPick(${i})" onfocus="refreshGoodsPick(${i})"
                     onblur="hideGoodsDrop(${i})">
            </label>
            <div class="pgoods-drop hidden" onmousedown="event.preventDefault()"></div>
          </div>
          <div class="pgoods-picked">表格里真正存的是 <span class="pgoods-current">${escapeHtml(row.goods_name || "（还没选）")}</span>
            <button class="btn small" onclick="clearGoodsPick(${i})">清空</button>
          </div>
        </div>
        <div class="field">
          <label>类别
            <select onchange="setPlanCabin(${i}, this.value)">
              <option value="">（选一个）</option>${typeOpts}
            </select>
          </label>
        </div>
        <div class="field">
          <label>船舱名（按类别自动生成，只读）
            <input type="text" class="plan-cabin" readonly value="${escapeHtml(row.cabin_name || "")}">
          </label>
        </div>
        <div class="field">
          <label>备注（脚本不读）
            <input type="text" value="${escapeHtml(row.note || "")}"
                   oninput="setPlanField(${i}, 'note', this.value)">
          </label>
        </div>
      </div>
      <div class="plan-warn">${escapeHtml(goodsWarn(row.goods_name))}</div>
    </div>`;
}

/* 匹配：名字里含这段字就算命中（不分大小写，空查询 = 全部）。
   纯字符串包含，不用正则 —— 打「版」能中「铜版画」，打「腌鲱」能中「上等腌制鲱鱼」。 */
function matchGoods(name, q) {
  const n = (name || "").toLowerCase();
  const s = (q || "").trim().toLowerCase();
  return !s || n.includes(s);
}

/* 把命中的那几个字标粗。同样是 indexOf 定位 + 三段拼接，不用正则替换。 */
function highlightMatch(name, q) {
  const s = (q || "").trim();
  if (!s) return escapeHtml(name);
  const at = name.toLowerCase().indexOf(s.toLowerCase());
  if (at < 0) return escapeHtml(name);
  return escapeHtml(name.slice(0, at)) + "<b>" + escapeHtml(name.slice(at, at + s.length)) + "</b>"
       + escapeHtml(name.slice(at + s.length));
}

/* 下拉里的候选：纯文字一行一条（2026-09-29 你要求不显示图片）。
   onclick 传的是 goods_options 里的**下标**，不传中文名字 —— 中文名当参数传来传去
   迟早遇上引号 / 空格 / 同名，下标不会。 */
function goodsPickHtml(i, q) {
  const opts = (planData && planData.goods_options) || [];
  if (!opts.length) {
    return `<div class="pgoods-empty">模板库里还没有「商品-*」模板 —— 先去「模板库」页框一张，名字就叫 商品-XX</div>`;
  }
  const hits = [];
  opts.forEach((name, idx) => { if (matchGoods(name, q)) hits.push([name, idx]); });
  if (!hits.length) {
    return `<div class="pgoods-empty">没有名字里带「${escapeHtml((q || "").trim())}」的商品`
         + `（模板库里一共 ${opts.length} 件，换个字试试）</div>`;
  }
  const picked = planData.rows[i] ? planData.rows[i].goods_name : "";
  return hits.map(([name, idx]) =>
    `<button type="button" class="pgoods-item${name === picked ? " on" : ""}"
             onclick="pickGoods(${i}, ${idx})">${highlightMatch(name, q)}</button>`).join("");
}

function goodsDropEl(i) {
  const el = planDetailEl();
  return el && Number(el.dataset.row) === i ? el.querySelector(".pgoods-drop") : null;
}

/* 打字 / 聚焦都只重画下拉那一条，输入框本身不动 —— 焦点和已打的字才不会被弄没。 */
function refreshGoodsPick(i) {
  const box = goodsDropEl(i);
  const inp = document.querySelector("#plan-list .plan-goods-search");
  if (!box || !inp) return;
  box.innerHTML = goodsPickHtml(i, inp.value);
  box.classList.remove("hidden");
}

function hideGoodsDrop(i) {
  const box = goodsDropEl(i);
  if (box) box.classList.add("hidden");
}

function pickGoods(i, idx) {
  const name = ((planData && planData.goods_options) || [])[idx];
  if (!name) return;
  setPlanField(i, "goods_name", name);
  const inp = document.querySelector("#plan-list .plan-goods-search");
  if (inp) inp.value = "";          // 输入框是筛选用的，选完之后不该留下一串看着像货名的字
  refreshGoodsPick(i);             // 让「表格里真正存的是」那件在下拉里亮起来
  hideGoodsDrop(i);
}

function clearGoodsPick(i) {
  setPlanField(i, "goods_name", "");
  const inp = document.querySelector("#plan-list .plan-goods-search");
  if (inp) inp.value = "";
  refreshGoodsPick(i);
  hideGoodsDrop(i);
}

/* 模板库里没有对应「商品-XX」时给一句人话提醒（不影响保存，只是跑之前提醒你补模板）。 */
function goodsWarn(name) {
  const n = (name || "").trim();
  if (!n) return "";
  const known = (planData && planData.goods_options) || [];
  return known.includes(n) ? "" : `模板库里没有「商品-${n}」，买货时找不到它 —— 去「模板库」页框一张，名字就叫 商品-${n}`;
}

function markPlanDirty() {
  planDirty = true;
  $("plan-save-status").textContent = "有未保存的改动，记得点「保存」";
}

function setPlanField(i, key, value) {
  if (!planData || !planData.rows[i]) return;
  planData.rows[i][key] = value;
  // 改了货物名，三级的标题、「表格里真正存的是」那一行和缺模板提醒跟着变，免得看着像没选上
  if (key === "goods_name") {
    const el = planDetailEl();
    if (el && Number(el.dataset.row) === i) {
      el.querySelector(".plan-detail-title").textContent = value || "未填货物";
      el.querySelector(".plan-warn").textContent = goodsWarn(value);
      const cur = el.querySelector(".pgoods-current");
      if (cur) cur.textContent = value || "（还没选）";
    }
  }
  markPlanDirty();
}

/* 组头改一次港口名 = 这一组所有行的港口一起改，
   所以「同一港口买几种货」只要填一遍港口名。 */
function setGroupPort(anchor, value) {
  const g = groupByAnchor(anchor);
  if (!g) return;
  g.indices.forEach(i => { planData.rows[i].port = value; });
  planPort = value.trim() || "（未填港口）";   // 二级认的是港口名，改了就跟着换，别把自己这一组丢了
  markPlanDirty();
}

/* 选类别：顺便把「大型XX管理室」显示到只读框里。
   这条拼接规则后端 purchase_plan.cabin_name 也有一份，界面只是即时预览，
   落盘的文件里没有这个字段，最终以接口返回值为准。 */
function setPlanCabin(i, value) {
  if (!planData || !planData.rows[i]) return;
  planData.rows[i].cargo_type = value;
  planData.rows[i].cabin_name = value ? "大型" + value + "管理室" : "";
  const el = planDetailEl();
  const input = el && Number(el.dataset.row) === i ? el.querySelector(".plan-cabin") : null;
  if (input) input.value = planData.rows[i].cabin_name;
  markPlanDirty();
}

// 新增一个港口 = 追加一行空货品行（港口为空自成一组）；直接进二级，让人当场把港口名填了
function newPlanRow() {
  if (!planData) planData = { cargo_types: [], rows: [] };
  planData.rows.push({ port: "", goods_name: "", cargo_type: "", cabin_name: "", note: "" });
  planEdit = null;
  planPort = "（未填港口）";
  planPortQuery = "";
  renderPlan();
  markPlanDirty();
  const inp = document.querySelector("#plan-list .plan-port");
  if (inp) inp.focus();
}

// 组内加一种货：港口沿用二级那个，不用再打一遍；加完直接进三级，让人当场选货
function addPlanGoods(anchor) {
  const g = groupByAnchor(anchor);
  if (!g) return;
  planData.rows.push({
    port: g.port, goods_name: "", cargo_type: "", cabin_name: "",
    note: "",
  });
  planEdit = planData.rows.length - 1;
  renderPlan();
  markPlanDirty();
  const inp = document.querySelector("#plan-list .plan-goods-search");
  if (inp) inp.focus();
}

function deletePlanRow(i) {
  const row = planData.rows[i];
  const name = `${row.port || "(未填港口)"} · ${row.goods_name || "(未填货物)"}`;
  if (!confirm(`确定删除「${name}」这一行？（只改内存，点「保存」才写入文件）`)) return;
  planData.rows.splice(i, 1);
  planEdit = null;          // 三级正对着这一行，删完下标就错位了，退回二级重列
  renderPlan();
  markPlanDirty();
}

function deletePlanGroup(anchor) {
  const g = groupByAnchor(anchor);
  if (!g) return;
  if (!confirm(`确定删除「${g.label}」这 ${g.indices.length} 行？（只改内存，点「保存」才写入文件）`)) return;
  // 从后往前删，否则前面的下标会先失效
  [...g.indices].sort((a, b) => b - a).forEach(i => planData.rows.splice(i, 1));
  planPort = null;          // 整组没了，退回一级港口清单
  planEdit = null;
  renderPlan();
  markPlanDirty();
}

async function savePlan() {
  const status = $("plan-save-status");
  if (!planData) { status.textContent = "表格还没加载出来"; return; }
  status.textContent = "保存中…";
  const rows = planData.rows.map(r => ({
    port: (r.port || "").trim(),
    goods_name: (r.goods_name || "").trim(),
    cargo_type: r.cargo_type || "",
    note: r.note || "",
  }));
  let res, body;
  try {
    res = await fetch("/api/plan", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rows }),
    });
    body = await res.json().catch(() => ({}));
  } catch (e) {
    status.textContent = "保存失败: " + e.message;
    return;
  }
  if (!res.ok) {
    status.textContent = "保存失败: " + (body.detail || res.status);
    return;
  }
  planData.rows = body.rows;
  planDirty = false;
  planEdit = null;          // 存完后行下标可能变了，退回「这个港的货物清单」
  renderPlan();
  status.textContent = `已保存 ${body.rows.length} 行到 purchase_plan.json`
    + "（这一栏只定「哪个港口有哪些商品」；这一趟去哪个港买，在「跑商设置」栏定）";
}

window.addEventListener("beforeunload", (e) => {
  if (planDirty) e.preventDefault();   // 表格改动只存在内存里，误关页面会丢
});

/* ============ 跑商设置（只编方案：这一趟按顺序走哪几站 + 每站买什么 + 买之前先做哪两件事） ============ */
/* 分工（2026-09-30 结构重构后，「跑商设置」这一栏只编方案；2026-10-01 分成第二级三块）：
   「表格」栏 = purchase_plan.json = **纯目录**，只回答「哪个港口买得到哪些货」（外加改船舱要的类别）；
   这一栏 = route_presets.json = **方案库**，每个方案都是一整套完整流程：
     · stops   有序站次：买货（可多个）→ 中转（可多个）→ 出货，每段内部也按这里的先后。
               买货站额外挂 goods = 这一站要买的货（名称从该港的目录里挑，顺序就是买的顺序）。
     · options 附加步骤（二级那两块）：refit = 购买前改舱（船种 / 哪几格 / 改成什么舱），
               switch_config = 切换配置（只有个勾，占位）。**只记在方案里，引擎还没这两步**。
   方案里**不再有「本次跑哪个模块」**（后端 normalize_preset 一律按完整一趟 run_module=trip 存），
   也**没有「当前所在港」**：跑哪几个方案、按什么顺序、跑几遍 = 「运行」栏的队列（后端线程）；
   船现在停在哪个港 = 队列每趟启动前自己 OCR 读一次。
   route_plan.json 因此降级成**队列线程写出来的运行态产物**，这一栏既不读也不写它。
   「买货站买什么、开去哪一站」不由前端判断：后端 route_plan.derive() 一处算完，界面只显示结果。 */
let routeSaved = null;      // 方案库里那一份的规整结果：{name, stops, options}
let routeDraft = null;      // 编辑区草稿：{name, stops, options}
let routeCatalog = {};      // 港口 -> 该港在「表格」栏里的行（只读，用来生成勾选框）
let routePorts = [];        // 「表格」栏填过的港口名（港口输入框的 datalist 候选）
let routeCargoTypes = [];   // 「表格」栏那 17 种类别（改舱那一页的下拉候选，来自 /api/plan）

/* 第二级的两个附加步骤（2026-10-01 你要求加的）。
   REFIT_SHIPS / REFIT_SLOTS 和后端 route_presets.py 里那两个常量必须是同一份清单
   （自检逐字比对两边）：界面上能选而盘上存不进 = 白填，盘上收着而界面上选不出 = 没人看得见。
   清单只有这一种船 / 这三格 —— 船舱页判据模板就照着「改良荒木船」的那三格「可搭乘船舱」录的，
   而「无法搭乘」栏那一头要花蓝钻，2026-09-23 你明确要求不碰，所以这里根本不列。 */
const REFIT_SHIPS = ["改良荒木船"];
const REFIT_SLOTS = ["1行2列", "1行3列", "3行3列"];
const ROUTE_TABS = ["plan", "refit", "cfg"];
// 2026-10-01 你补的口径：进栏时**三块都收起**，点哪一个才展开哪一个 —— 所以初始是 null（谁都没开），
// 也不再把它记进 localStorage（记了就会在下次进栏时自己弹开一块，正好和你要的相反）。
let routeTab = null;

const STAGE_LABELS = { buy: "买货", transit: "中转", sell: "出货" };
// 每一站归哪一段就进哪张卡片；boxes 是卡片容器的元素 id
const STOP_STAGES = [
  { stage: "buy", box: "route-stops-buy" },
  { stage: "transit", box: "route-stops-transit" },
  { stage: "sell", box: "route-stops-sell" },
];

/* 模块 id 是 states.json 里人手填的英文，界面上给常见几个配中文名。
   认不出来的照常显示原 id —— 模块清单以 states.json 为准，这里不写死一份白名单。 */
const MODULE_LABELS = { buy: "买货", sail: "港口间移动", sell: "卖货", trip: "完整一趟" };
/* 名字和后端 route_plan.TRIP_MODULE 一致（自检会比对两边）。这一栏已经没有模块下拉了，
   常量留着只为认得出「这个 id 就是整趟」并把它念成中文。 */
const TRIP_MODULE = "trip";
function moduleLabel(m) { return m ? `${MODULE_LABELS[m] || m}（${m}）` : "（没选）"; }

/* 单步调试能选的模块，和后端 app.STEP_MODULES 是同一份口径：整趟（trip）不在里面 ——
   它要一份完整站次表，是「跑商设置 + 运行队列」那条正路，不是拿来试跑一条链的东西。 */
const STEP_MODULES = ["buy", "sail", "sell"];

/* 扫 states.json 里每个非 global 状态标了哪个 module —— 单步调试的模块选项要用它数状态。
   故意不复用状态编辑器里的 stateData：那个可能有人还没保存的改动，
   拿它填下拉会让「界面上能选」和「文件里真存着」两件事不一致。 */
function scanModules(states) {
  const out = new Map();
  (Array.isArray(states) ? states : []).filter(s => !s.global).forEach(s => {
    const m = (s.module || "").trim() || "（未填 module）";
    if (!out.has(m)) out.set(m, { count: 0, entries: [] });
    const g = out.get(m);
    g.count += 1;
    if (s.entry) g.entries.push(s.id);
  });
  return out;
}

function pickStops(stops) {
  return (Array.isArray(stops) ? stops : []).map(s => ({
    stage: (s.stage || "").trim(),
    port: (s.port || "").trim(),
    // 这一站要买的货：只有名字，顺序就是买的顺序。「类别 / 船舱名」不在这儿 —— 那是目录的事，
    // 存两份迟早和「表格」栏对不上。
    goods: (Array.isArray(s.goods) ? s.goods : [])
      .map(g => String(g === undefined || g === null ? "" : g).trim()).filter(Boolean),
    note: s.note || "",
  }));
}

function pickPreset(p) {
  return {
    name: (p.name || "").trim(),
    stops: pickStops(p.stops),
    options: pickOptions(p.options),
  };
}

/* 附加步骤那份：界面上那三个字段 + 两个勾。
   缺项一律补默认，不把「没填」当成「勾上了」—— 老方案文件里根本没有 options 这一项
   （2026-10-01 之前存的），读回来得照样能编辑。 */
function defaultOptions() {
  return {
    refit: { enabled: false, ship: REFIT_SHIPS[0], slots: [], cargo_type: "" },
    switch_config: { enabled: false },
  };
}
function pickOptions(o) {
  const d = defaultOptions();
  const src = (o && typeof o === "object") ? o : {};
  const r = (src.refit && typeof src.refit === "object") ? src.refit : {};
  const c = (src.switch_config && typeof src.switch_config === "object") ? src.switch_config : {};
  return {
    refit: {
      enabled: r.enabled === true,
      ship: String(r.ship || d.refit.ship).trim(),
      // 仓位按 REFIT_SLOTS 的固定顺序存：它是「第几格」不是「先改哪个」，
      // 存成点击顺序会让每次重开方案都像改动过。
      slots: REFIT_SLOTS.filter(s => (Array.isArray(r.slots) ? r.slots : [])
        .map(x => String(x).trim()).includes(s)),
      cargo_type: String(r.cargo_type || "").trim(),
    },
    switch_config: { enabled: c.enabled === true },
  };
}

/* 要保存 / 要比对的那三样（方案名 + 站次 + 附加步骤）。港口、备注和货物名在交出去之前去掉两头空格 ——
   后端也这么规整，两边口径一样，才不会一进来就满屏「有改动未保存」。键顺序要和 pickPreset 一致
   （name、stops、options），否则 routeDirty() 的字符串比对会把「没改过」判成改过。 */
function coreOf(preset) {
  return {
    name: (preset.name || "").trim(),
    stops: (preset.stops || []).map(s => ({
      stage: s.stage,
      port: (s.port || "").trim(),
      goods: (s.goods || []).map(g => String(g).trim()).filter(Boolean),
      note: (s.note || "").trim(),
    })),
    options: pickOptions(preset.options),
  };
}
function gatherRoute() { return coreOf(routeDraft); }
function routeDirty() {
  return !!routeSaved && !!routeDraft
    && JSON.stringify(gatherRoute()) !== JSON.stringify(coreOf(routeSaved));
}

/* 某一段有哪几站（返回的是「在整趟数组里的下标」，改顺序时还认得原位置）。
   这只是按 stage 分组显示，不是「哪一站才算数」的判断 —— 那个在 derive()。 */
function stopIndexes(stage) {
  const out = [];
  (routeDraft.stops || []).forEach((s, i) => { if (s.stage === stage) out.push(i); });
  return out;
}

/* 后端只回了个 404 时别当「规划是空的」混过去：这多半是改了 app.py 忘了重启进程，
   界面拿着旧后端跑新流程，最差的一次是在真金币那一步上。 */
async function getJson(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${url} 返回 ${res.status}（改了 app.py 要重启后端才生效）`);
  return res.json();
}

/* 进这一栏 / 点「刷新方案列表」都走这里：把方案库和「表格」栏的目录一次读齐，然后把一份方案装进编辑区。
   装哪一份：上次编辑过的那个方案名（localStorage 记着）→ 找不到就第一个方案 → 一个都没有就给空草稿。
   目录（港口候选 + 每个港买得到哪些货）从 /api/plan 拿，**不读 route_plan.json** ——
   那一份现在是队列线程的运行态产物，界面把它当目录读就会「跑到一半目录跟着变」。 */
async function loadPresetEditor() {
  const status = $("route-status");
  let list;
  try {
    list = (await getJson("/api/route/presets")).presets || [];
  } catch (e) {
    presetRows = [];
    presetErr = e.message;
    renderPresets();
    status.textContent = "读方案库失败: " + e.message + "（请确认后端已启动）";
    return;
  }
  presetRows = list;
  presetErr = "";
  try {
    const plan = await getJson("/api/plan");
    routePorts = Array.isArray(plan.port_options) ? plan.port_options : [];
    routeCatalog = plan.catalog && typeof plan.catalog === "object" ? plan.catalog : {};
    routeCargoTypes = Array.isArray(plan.cargo_types) ? plan.cargo_types : [];
  } catch (e) {
    status.textContent = "读「表格」栏目录失败: " + e.message
      + "（港口候选和勾货框会不完整，别的照常）";
  }
  renderPresets();
  const want = localStorage.getItem("uwo.preset") || (routeDraft && routeDraft.name) || "";
  const hit = presetRows.find(p => p.name === want) || presetRows[0] || null;
  routeSaved = hit ? pickPreset(hit) : { name: "", stops: [], options: defaultOptions() };
  routeDraft = pickPreset(routeSaved);
  if (hit) localStorage.setItem("uwo.preset", hit.name);
  renderRoute();
  switchRouteTab(null);   // 进栏时三块都收起，等你点上面哪一个才展开哪一个
  status.textContent = hit
    ? `正在编辑方案『${hit.name}』：${hit.stop_count} 站 —— 改完点「保存方案」`
    : (presetRows.length
        ? "上次编辑的那份方案不在库里了，已换成第一个方案"
        : "方案库是空的 —— 在下面把站次排好、上面起个名字，点「保存方案」");
}

function fillRoutePortOptions() {
  const dl = $("route-port-options");
  if (!dl) return;
  /* 已存方案里填过、但「表格」栏没有行的港口也要进候选：不然 datalist 会让人觉得它不存在，
     而它确实存在于方案里，只是跑起来会被引擎拒绝（勾货框那里会说明）。 */
  const saved = new Set((routeDraft.stops || []).map(s => s.port).filter(Boolean));
  const list = [...new Set([...routePorts, ...saved])];
  dl.innerHTML = list.map(p => `<option value="${escapeHtml(p)}"></option>`).join("");
}

/* 一行站：序号 = 它在整趟里的第几站（跨段连续），端口 + 备注 + 段内上下移 + 删；
   买货站下面多一块勾选框（这一站买哪几件、什么顺序）。
   上下移只和**同一段**的相邻站换位置：整趟「买货→中转→出货」的分段是拍好的顺序，
   跨段乱换会把这一段拖进那一段里。 */
function stopRowHtml(s, i) {
  const idxs = stopIndexes(s.stage);
  const pos = idxs.indexOf(i);
  const port = escapeHtml(s.port || "");
  const note = escapeHtml(s.note || "");
  const goods = s.stage === "buy"
    ? `<div class="stop-goods" id="stop-goods-${i}">${goodsPickerHtml(s, i)}</div>` : "";
  return `<div class="stop-row" data-i="${i}">
    <span class="stop-seq" title="整趟的第 ${i + 1} 站">${i + 1}</span>
    <input class="stop-port" type="text" list="route-port-options" value="${port}"
           placeholder="港口名，如：北京" oninput="onStopField(${i}, 'port', this.value)">
    <input class="stop-note" type="text" value="${note}"
           placeholder="这一站做什么（可不填）" oninput="onStopField(${i}, 'note', this.value)">
    <span class="stop-btns">
      <button class="btn small" onclick="stopMove(${i}, -1)"${pos <= 0 ? " disabled" : ""} title="和这一段里上一站换位置">↑</button>
      <button class="btn small" onclick="stopMove(${i}, 1)"${pos >= idxs.length - 1 ? " disabled" : ""} title="和这一段里下一站换位置">↓</button>
      <button class="btn small danger" onclick="stopDelete(${i})" title="删掉这一站">删</button>
    </span>
    ${goods}
  </div>`;
}

/* 这一站买什么：把「表格」栏里这个港口买得到的货全列出来让人勾，勾上的编号就是买的先后。
   为什么只给勾选、不给打字：货名必须是该港目录里真存在的那一行，引擎要按名字回目录取「类别」，
   目录里没有这个名字就买不成 —— 手打入口留着只会让人打出后端不认的名字。
   为什么这一栏而不是「表格」栏：表格是「哪个港有什么货」的目录（一行一类货可能跑好几趟），
   具体这一趟买哪几件是每次定的，写在站上才对。 */
function goodsPickerHtml(s, i) {
  const port = (s.port || "").trim();
  if (!port) {
    return `<div class="goods-tip">先在上面的港口框里填港口 —— 填好这里就列出它在「表格」栏买得到的货，勾上的是这一站要买的。</div>`;
  }
  const rows = routeCatalog[port] || [];
  const sel = s.goods || [];
  const inCatalog = n => rows.some(r => (r.goods_name || "").trim() === n);
  const items = rows.map(r => {
    const name = (r.goods_name || "").trim();
    const pos = sel.indexOf(name);
    const on = pos >= 0;
    const nm = escapeHtml(name);
    const mv = on
      ? `<span class="goods-order" title="这一站第 ${pos + 1} 件买它">${pos + 1}</span>
         <span class="goods-move">
           <button class="btn tiny" data-name="${nm}"${pos <= 0 ? " disabled" : ""}
                   onclick="stopGoodsMove(${i}, this.dataset.name, -1)" title="和上一件换先后">↑</button>
           <button class="btn tiny" data-name="${nm}"${pos >= sel.length - 1 ? " disabled" : ""}
                   onclick="stopGoodsMove(${i}, this.dataset.name, 1)" title="和下一件换先后">↓</button>
         </span>` : "";
    return `<div class="goods-row${on ? " on" : ""}">
      <label title="勾上 = 这一站买它">
        <input type="checkbox" data-name="${nm}"${on ? " checked" : ""}
               onchange="onStopGoods(${i}, this.dataset.name, this.checked)">
        <span class="goods-name">${nm}</span>
      </label>
      <span class="goods-cargo">${escapeHtml(r.cargo_type || "—")}</span>
      ${mv}
    </div>`;
  }).join("");
  /* 勾过、但目录里已经没这件货了（表格栏改过名，或者这一站换了港口）。
     不偷偷删掉：删了界面看着干净、点启动却被引擎拒绝，那种「看起来没事」比一行黄字危险。 */
  const stale = sel.filter(n => !inCatalog(n));
  const staleHtml = stale.length
    ? `<div class="goods-stale">这一站还挂着 ${stale.length} 件『${escapeHtml(port)}』买不到的货：`
      + `${escapeHtml(stale.join("、"))} —— <b>已失效</b>，点启动会被拒绝。`
      + `<button class="btn tiny" onclick="dropStaleGoods(${i})">去掉它们</button></div>` : "";
  const empty = rows.length ? ""
    : `<div class="goods-tip"><b class="route-off">「表格」栏里港口『${escapeHtml(port)}』一行货都没有</b>`
      + ` —— 挑不出货，跑买货模块点启动会被拒绝。<button class="btn tiny" onclick="switchPage('plan')">去表格栏加货</button></div>`;
  return `<div class="goods-head">这一站买什么（从『${escapeHtml(port)}』的目录里勾，编号 = 先后）</div>`
    + empty + items + staleHtml;
}

/* 只重画这一行的勾选框。整片 renderStops() 会把正在打的港口输入框连光标一起冲掉，
   而换港口时最需要立刻看见的正是「刚才勾的货在新港口还剩几个」。
   重画排队到下一个任务：change 事件还在派发途中就把 <input> 从 DOM 里摘掉，
   label 会把这一次点击再转发一遍，脚本里的 element.click() 于是「一跳一删」拿到中间态
   （真人手点不会撞，但任何自动验证都会撞上 —— 那样就等于这一栏没法验）。 */
function refreshStopGoods(i) {
  setTimeout(() => {
    const box = $(`stop-goods-${i}`);
    const s = (routeDraft.stops || [])[i];
    if (box && s && s.stage === "buy") box.innerHTML = goodsPickerHtml(s, i);
  }, 0);
}

function renderStops() {
  STOP_STAGES.forEach(({ stage, box }) => {
    const el = $(box);
    const idxs = stopIndexes(stage);
    const emptyTip = { buy: " —— 这一趟不买货，跑起来引擎会拒绝",
                       sell: " —— 这一趟没有出货站，整趟没有终点",
                       transit: " —— 这一趟不途经别的港" }[stage] || "";
    el.innerHTML = idxs.length
      ? idxs.map(i => stopRowHtml(routeDraft.stops[i], i)).join("")
      : `<div class="muted">这一段一站都没排${emptyTip}</div>`;
  });
  /* 出货段整段只能一站（2026-09-29 你指出「出货港口实际不可以多个」）：
     有了就把「+ 加一个」按住，别让人排出一份后端永远不会读的站 —— 真正的拦在 route_plan 那一层。 */
  const btn = $("btn-add-sell");
  if (btn) {
    const n = stopIndexes("sell").length;
    btn.disabled = n >= 1;
    btn.textContent = n >= 1 ? `出货港只能有一个（已经有 ${n} 站）` : "+ 加一个出货港";
    btn.title = n >= 1 ? "整趟只认一个出货站（它是整趟的终点），多排的永远不会走；要路过别的港就排「中转」" : "";
  }
}

/* 整趟顺序一览：一眼看出要按什么顺序走、每站买几件，以及船现在站在第几站。
   「第几站」由后端 derive() 的 here_idx 给 —— 同一个港在一趟里出现两次是常事
   （北京买货 → … → 北京出货），前端自己挑一个描边框就成了第二套规则。
   草稿没保存时后端算不到新顺序，这时按港口名匹配并把话说清是「按名字凑的」。 */
/* 整趟顺序一览：一眼看出要按什么顺序走、每站买几件。
   「船现在站在第几站」不在这里画 —— 方案里没有「当前所在港」（那是队列每趟启动前 OCR 读的），
   编方案的时候说「船在这儿」只能是猜的。 */
function renderStopsOrder() {
  const box = $("route-stops-order");
  const stops = routeDraft.stops || [];
  if (!stops.length) {
    box.innerHTML = `<div class="muted">一站都没排 —— 下面「买货 / 中转 / 出货」随便哪一段先加一站</div>`;
    return;
  }
  const items = stops.map((s, i) => {
    const n = (s.goods || []).length;
    return `<span class="stop-item">`
      + `<b class="stop-seq">${i + 1}</b> ${escapeHtml(STAGE_LABELS[s.stage] || s.stage)} `
      + `<b>${escapeHtml(s.port || "（没填港）")}</b>`
      + (s.stage === "buy"
          ? `<span class="stop-what">${n ? `${n} 件货` : `<b class="route-off">没勾货</b>`}</span>` : "")
      + (s.note ? `<span class="stop-what">${escapeHtml(s.note)}</span>` : "")
      + `</span>`;
  }).join(`<span class="stop-arrow">→</span>`);
  box.innerHTML = `<div class="stop-seq-line">${items}</div>${renderReorderHint()}`;
}

/* 先后不用人操心（2026-09-29 你拍的 A）：保存时后端按 买货 → 中转 → 卖货 重排，
   排错了不报错、只挪位置，挪了哪几站保存后会念给你听。
   这一段是说明，不是第二套规则 —— 真正排它的是 route_plan.normalize_route。 */
function renderReorderHint() {
  return `<div class="muted">这一份的<b>先后由后端排</b>：保存时自动按 `
    + `<b>买货 → 中转 → 卖货</b> 排好，同类内部保持你排的先后（几个买货站谁先谁后 = 买货顺序）。</div>`
    + (routeDirty()
        ? `<div class="stop-stale">上面这些改动还没保存 —— 点「保存方案」才写进方案库。</div>` : "");
}

/* 本次买货汇总：把每个买货站勾的货念一遍。
   勾货的框长在每一站那一行上（见 goodsPickerHtml），这块只做汇总，免得两个地方各摆一份清单。
   「本次就是这一站」这种话不在这里说了：方案里根本没有「当前所在港」（那是队列每趟启动前
   OCR 读的），编方案的时候说「本次是哪一站」只能是猜的。 */
function renderRouteBuyGoods() {
  const box = $("route-buy-goods");
  if (!box) return;
  const buyIdx = stopIndexes("buy");
  if (!buyIdx.length) {
    box.innerHTML = `<div class="goods-tip"><b class="route-off">这一趟还没排买货站</b>`
      + ` —— 整趟没有进货站，跑起来引擎会拒绝</div>`;
    return;
  }
  const rows = buyIdx.map(i => {
    const s = routeDraft.stops[i];
    const g = s.goods || [];
    return `<div class="goods-sum">
      <span class="stop-seq">${i + 1}</span>
      <b>${escapeHtml(s.port || "（没填港）")}</b>
      <span>${g.length
        ? `买 ${g.length} 件：<i>${escapeHtml(g.join(" → "))}</i>`
        : `<b class="route-off">一件都没勾</b> —— 整趟走到这一站会买不成`}</span>
    </div>`;
  }).join("");
  box.innerHTML = `
    <div class="route-line">每站勾的就是那一站要买的（顺序 = 勾选先后，买完自动停）。</div>
    ${rows}
    ${buyIdx.length > 1 ? `<div class="goods-tip">整趟跑下来这些买货站会按上面的顺序<b>自己接着走</b>：`
      + `到第 1 站买那一站勾的货，买完自动出港开去下一站，不用各点一次启动。</div>` : ""}`;
}

/* ============ 第二级：购买前改舱 / 切换配置（两个带勾框的附加步骤） ============ */
/* 二级只在「跑商设置」这一栏内部换面板，左侧那排一级导航一个字不动：
   站次和这两步属于**同一份草稿**（方案名 + 站次 + 附加步骤），拆成一级导航就会出现
   「切到别的栏，这份草稿算谁的」；混在站次那一页里排又会让勾框被十几行货物淹没。
   两边都留一个勾框（二级标题上 + 面板里），写的是同一份草稿，刷界面时一起对齐。 */
function routeOptions() {
  if (!routeDraft) return null;
  routeDraft.options = pickOptions(routeDraft.options);   // 老方案里可能根本没这一项
  return routeDraft.options;
}

function switchRouteTab(tab) {
  if (tab !== null && !ROUTE_TABS.includes(tab)) tab = "plan";
  routeTab = tab;                                  // null = 三块全收起（进栏时就是这个状态）
  document.querySelectorAll("#page-route .subtab").forEach(b =>
    b.classList.toggle("active", b.dataset.rtab === tab));
  const panels = { plan: "route-sub-plan", refit: "route-sub-refit", cfg: "route-sub-cfg" };
  Object.entries(panels).forEach(([k, id]) => {
    const el = $(id);
    if (el) el.hidden = k !== tab;
  });
}

/* 勾框 / 参数改完都要重画这两块面板，而面板里有勾框自己 —— 同步换掉 <input> 会让
   label 把这次点击再转发一遍（见 refreshStopGoods 那条），所以一律排队到下一个任务。 */
function refreshRouteOptions() {
  setTimeout(() => { renderRouteOptions(); routeStatusLine(); }, 0);
}

function onOptionFlag(which, on) {
  const o = routeOptions();
  if (!o) return;
  const key = which === "refit" ? "refit" : "switch_config";
  o[key].enabled = !!on;
  refreshRouteOptions();
}

function onRefitField(key, value) {
  const o = routeOptions();
  if (!o) return;
  if (key === "slots" || !(key in o.refit)) return;
  o.refit[key] = String(value || "").trim();
  refreshRouteOptions();
}

function onRefitSlot(name, on) {
  const o = routeOptions();
  if (!o || !REFIT_SLOTS.includes(name)) return;
  const set = new Set(o.refit.slots.filter(s => REFIT_SLOTS.includes(s)));
  if (on) set.add(name); else set.delete(name);
  o.refit.slots = REFIT_SLOTS.filter(s => set.has(s));   // 存成固定格序，不存点击顺序
  refreshRouteOptions();
}

/* 这一趟要买的货分别是哪几类：从「表格」栏目录现算（方案里不存类别，见文件头分工）。
   放在改舱这一页是为了对一眼 —— 舱只对<b>一类</b>货有加成，选的类别不在这一趟要跑的货里，
   那这趟就是白花钱改舱，所以把它顶到眼前而不是等人自己去表格栏比。 */
function planBuyCargoTypes() {
  const out = [];
  ((routeDraft || {}).stops || []).forEach(s => {
    if (s.stage !== "buy") return;
    const rows = routeCatalog[(s.port || "").trim()] || [];
    (s.goods || []).forEach(g => {
      const row = rows.find(r => (r.goods_name || "").trim() === g);
      if (row && row.cargo_type && !out.some(x => x.cargo_type === row.cargo_type))
        out.push({ cargo_type: row.cargo_type, goods: g });
    });
  });
  return out;
}

function renderRouteOptions() {
  const o = routeOptions() || defaultOptions();
  [["flag-refit", o.refit.enabled], ["flag-cfg", o.switch_config.enabled],
   ["refit-enabled", o.refit.enabled], ["cfg-enabled", o.switch_config.enabled]]
    .forEach(([id, on]) => { const el = $(id); if (el) el.checked = on; });
  const rs = $("flag-refit-state");
  if (rs) {
    rs.textContent = o.refit.enabled ? "要改" : "不改";
    rs.classList.toggle("on", o.refit.enabled);
  }
  const cs = $("flag-cfg-state");
  if (cs) {
    cs.textContent = o.switch_config.enabled ? "已勾 · 还没实现" : "占位";
    cs.classList.toggle("on", o.switch_config.enabled);
  }
  renderRefitBody(o.refit);
  renderCfgBody(o.switch_config);
}

function renderRefitBody(o) {
  const box = $("refit-body");
  if (!box) return;
  const shipOpts = REFIT_SHIPS
    .map(s => `<option value="${escapeHtml(s)}"${s === o.ship ? " selected" : ""}>`
      + `${escapeHtml(s)}（唯一录过判据的）</option>`).join("")
    + `<option value="" disabled>（占位）别的船种 —— 船舱页一张模板都没录，选不了</option>`;
  const slotRows = REFIT_SLOTS.map(s => {
    const on = o.slots.includes(s);
    return `<div class="goods-row${on ? " on" : ""}">
      <label title="勾上 = 这一格要改成上面选的那个舱">
        <input type="checkbox" data-slot="${escapeHtml(s)}"${on ? " checked" : ""}
               onchange="onRefitSlot(this.dataset.slot, this.checked)">
        <span class="goods-name">可搭乘 · ${escapeHtml(s)}</span></label>
      <span class="goods-cargo">${escapeHtml(`UI-船舱-可搭乘-${s}-…`)}</span>
    </div>`;
  }).join("");
  // 类别候选来自 /api/plan（和「表格」栏同一份 17 种）。读不到目录时至少让自己已选的那个还在，
  // 不然下拉一打开就变成「（没选）」，看着像把方案里的值弄丢了。
  const types = routeCargoTypes.length ? routeCargoTypes : (o.cargo_type ? [o.cargo_type] : []);
  const cargoOpts = `<option value="">（没选）</option>` + types.map(t =>
    `<option value="${escapeHtml(t)}"${t === o.cargo_type ? " selected" : ""}>`
    + `大型${escapeHtml(t)}管理室</option>`).join("");
  const miss = [];
  if (!o.slots.length) miss.push("改哪几格没勾");
  if (!o.cargo_type) miss.push("改成什么舱没选");
  const buy = planBuyCargoTypes();
  const buyLine = buy.length
    ? `这一趟要买的货是这几类：<b>${escapeHtml(buy.map(x => `${x.cargo_type}（${x.goods}）`).join("、"))}</b>`
      + (o.cargo_type && !buy.some(x => x.cargo_type === o.cargo_type)
        ? ` —— <b class="route-off">『大型${escapeHtml(o.cargo_type)}管理室』不在这些类里</b>，`
          + `改成这个舱对这一趟要跑的货没有加成（确实要这么改就忽略这句）` : "")
    : `这一趟还没勾货 —— 要跑哪类货还没定，舱就按你自己的判断选。`;
  box.innerHTML = `
    <label class="opt-flag"><input type="checkbox" id="refit-enabled"${o.enabled ? " checked" : ""}
      onchange="onOptionFlag('refit', this.checked)"> <b>本次购买前先去造船所改船舱</b></label>
    <div class="opt-state">现在这一趟：<b>${o.enabled ? "要改舱" : "不改舱"}</b>
      —— 不勾就是不做这一步，下面的参数只是留着下次用。</div>
    <div class="field-row">
      <div class="field"><label>船种（进造船所点哪艘船的「变更船舱」）
        <select id="refit-ship" onchange="onRefitField('ship', this.value)">${shipOpts}</select></label></div>
      <div class="field"><label>改成什么舱（选类别，船舱名按下边那条规则自动生成）
        <select id="refit-cargo" onchange="onRefitField('cargo_type', this.value)">${cargoOpts}</select></label></div>
      <div class="field"><label>船舱名（候选列表里那一行的文字，只读）
        <input class="opt-cabin" type="text" readonly
               value="${escapeHtml(o.cargo_type ? `大型${o.cargo_type}管理室` : "（没选类别）")}"></label></div>
    </div>
    <div class="opt-slots">
      <div class="goods-head">改哪几格（勾上 = 这一格改成上面那个舱；全是「可搭乘」栏 = 金币档）</div>
      ${slotRows}
    </div>
    ${o.enabled && miss.length
      ? `<div class="route-warn-item">勾了『购买前改舱』还得说清：${escapeHtml(miss.join("、"))}`
        + ` —— 这样存不进方案库（后端会拒）。不是挑剔：这一步点下去花的是真金币、改完退不回去，`
        + `留半成品等于让将来那一步自己猜。</div>` : ""}
    <div class="goods-tip">${buyLine}</div>
    <div class="goods-tip">已经勾了 ${o.slots.length} 格，改完是 ${escapeHtml(o.cargo_type ? `大型${o.cargo_type}管理室` : "（没选类别）")}。
      存进方案后，方案列表那一行会照后端算的那句念一遍（「改舱：船 的 N 格[…] → 大型XX管理室」）。</div>`;
}

function renderCfgBody(o) {
  const box = $("cfg-body");
  if (!box) return;
  box.innerHTML = `
    <label class="opt-flag"><input type="checkbox" id="cfg-enabled"${o.enabled ? " checked" : ""}
      onchange="onOptionFlag('switch_config', this.checked)"> <b>本次购买前先换船队配置</b></label>
    <div class="opt-state">现在这一趟：<b>${o.enabled ? "要换配置" : "不换配置"}</b></div>
    <div class="field-row">
      <div class="field"><label>换成哪一档配置 <b class="route-off">（占位 · 还没有候选可选）</b>
        <input type="text" disabled placeholder="（占位）那几档配置叫什么、画面上长什么样，一个字都没记过"></label></div>
    </div>
    <div class="route-warn-item"><b>这一整块是占位符</b>：勾上只是把「这一趟要换配置」记在方案里，
      引擎没有对应的动作，点启动不会去菜单里换配置。</div>`;
}

/* 把草稿整片刷到界面上。方案名从草稿回填到输入框（换方案、另存为之后要看得见）。
   第二级那两块（改舱 / 切换配置）也在这里刷：它们和站次是同一份草稿，
   分开刷会出现「站次已经换了一份方案、附加步骤还挂着上一份的勾」。 */
function renderRoute() {
  fillRoutePortOptions();
  const nameEl = $("preset-name");
  if (nameEl) nameEl.value = routeDraft.name || "";
  renderStops();
  renderStopsOrder();
  renderRouteBuyGoods();
  renderRouteOptions();
  routeStatusLine();
}

function onStopField(i, key, v) {
  const s = (routeDraft.stops || [])[i];
  if (!s) return;
  s[key] = key === "port" ? (v || "").trim() : (v || "");
  // 换了港口 → 这一站可勾的货整个换一批，之前勾的大概率在新港口买不到，立刻显出来
  if (key === "port") refreshStopGoods(i);
  renderStopsOrder();
  renderRouteBuyGoods();
  refreshRouteOptions();   // 改舱那一页的「这一趟要跑哪几类货」跟着站次走
  routeStatusLine();
}

/* 勾 / 取消一件货：勾上就加到队尾（顺序 = 你点的先后），取消就地删掉。
   不重画整片站次，只重画这一行的勾选框 —— 见 refreshStopGoods。 */
function onStopGoods(i, name, on) {
  const s = (routeDraft.stops || [])[i];
  if (!s || !name) return;
  const keep = (s.goods || []).filter(g => g !== name);
  s.goods = on ? [...keep, name] : keep;
  refreshStopGoods(i);
  renderStopsOrder();
  renderRouteBuyGoods();
  refreshRouteOptions();   // 「这一趟要跑哪几类货」那一行跟着勾货变
  routeStatusLine();
}

/* 换先后：买货没有「数量」，只有顺序 —— 引擎按这个顺序一行一行买，买完自动停。 */
function stopGoodsMove(i, name, delta) {
  const g = ((routeDraft.stops || [])[i] || {}).goods;
  if (!Array.isArray(g)) return;
  const pos = g.indexOf(name);
  const to = pos + delta;
  if (pos < 0 || to < 0 || to >= g.length) return;
  [g[pos], g[to]] = [g[to], g[pos]];
  refreshStopGoods(i);
  renderStopsOrder();
  renderRouteBuyGoods();
  refreshRouteOptions();   // 「这一趟要跑哪几类货」那一行跟着勾货变
  routeStatusLine();
}

function dropStaleGoods(i) {
  const s = (routeDraft.stops || [])[i];
  if (!s) return;
  const port = (s.port || "").trim();
  const rows = routeCatalog[port] || [];
  s.goods = (s.goods || []).filter(n => rows.some(r => (r.goods_name || "").trim() === n));
  refreshStopGoods(i);
  renderStopsOrder();
  renderRouteBuyGoods();
  refreshRouteOptions();   // 「这一趟要跑哪几类货」那一行跟着勾货变
  routeStatusLine();
}

/* 加一站：插在同一段最后一站的后面（这段还没有站就接到整趟末尾）——
   这样「买货 → 中转 → 出货」的分段不会被新站插乱。 */
function stopAdd(stage) {
  if (stage === "sell" && stopIndexes("sell").length >= 1) {
    // 按钮平时是按住状态的，这里再挡一道：自动化/以后改版可能绕过那个 disabled
    alert("出货港只能有一个。\n卖货模块只认「当前所在港 + 类型是出货」的那一站，多排的永远不会走。\n确实要路过别的港就排成「中转」。");
    return;
  }
  const idxs = stopIndexes(stage);
  const at = idxs.length ? idxs[idxs.length - 1] + 1 : routeDraft.stops.length;
  routeDraft.stops.splice(at, 0, { stage, port: "", goods: [], note: "" });
  renderStops();
  renderStopsOrder();
  renderRouteBuyGoods();
  refreshRouteOptions();
  routeStatusLine();
  const el = document.querySelector(`.stop-row[data-i="${at}"] .stop-port`);
  if (el) el.focus();   // 点完「+ 加一个」就能直接打字，不用再瞄一次鼠标
}

function stopMove(i, delta) {
  const idxs = stopIndexes(routeDraft.stops[i].stage);
  const pos = idxs.indexOf(i);
  const to = pos + delta;
  if (pos < 0 || to < 0 || to >= idxs.length) return;
  const stops = routeDraft.stops;
  [stops[idxs[pos]], stops[idxs[to]]] = [stops[idxs[to]], stops[idxs[pos]]];
  renderStops();
  renderStopsOrder();
  renderRouteBuyGoods();
  refreshRouteOptions();
  routeStatusLine();
}

function stopDelete(i) {
  const s = routeDraft.stops[i];
  if (!s) return;
  if (s.port && !confirm(`删掉第 ${i + 1} 站（${STAGE_LABELS[s.stage] || s.stage}『${s.port}』）？\n删完它后面的站会整体往前挪一位。`)) return;
  routeDraft.stops.splice(i, 1);
  renderStops();
  renderStopsOrder();
  renderRouteBuyGoods();
  refreshRouteOptions();
  routeStatusLine();
}

function routeStatusLine() {
  const s = $("route-status");
  if (s) s.textContent = routeDirty()
    ? "有改动还没保存 —— 点「保存方案」才写进方案库"
    : "和方案库一致";
}

/* 后端这次替人重排了哪几站（存方案接口带这两个字段）。
   没排就返回空串。为什么必须说出来：顺序是这栏里唯一「界面写的和盘上存的可能不一样」的东西，
   悄悄挪了位置、下次进来一看站次变了，人只会以为是软件坏了。 */
function reorderNote(out) {
  if (!out || !out.reordered) return "";
  const moves = out.reorder_moves || [];
  return `｜已按「买货 → 中转 → 卖货」自动重排（同类内部还是你排的先后）：`
    + (moves.length ? moves.join("；") : "后端没说是哪几站");
}

/* 「保存方案」= 把编辑区现在这套（含还没保存的改动）POST 进方案库。
   同名就是覆盖 —— 但只在覆盖**别的**方案时才问一句：正在编辑的那一份反复保存不该每次都弹。
   存完重读一遍方案库，用后端规整（重排过）的那份回填编辑区 —— 不然界面显示的先后
   和盘上存的会不一样，下次打开就「莫名其妙变了」。 */
async function saveRoute() {
  const status = $("route-status");
  const name = ($("preset-name").value || "").trim();
  if (!name) {
    status.textContent = "保存失败：先给方案起个名字（方案是靠名字读出来的）";
    $("preset-name").focus();
    return;
  }
  const dup = presetRows.some(p => p.name === name)
    && name !== ((routeSaved && routeSaved.name) || "");
  if (dup && !confirm(`已经有叫『${name}』的方案了，覆盖它？\n（只改方案库这一条，不影响正在跑的那一趟）`)) return;
  routeDraft.name = name;
  const core = coreOf(routeDraft);
  let res, out;
  try {
    res = await fetch("/api/route/presets", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, stops: core.stops, options: core.options }),
    });
    out = await res.json().catch(() => ({}));
  } catch (e) {
    status.textContent = "保存失败: " + e.message;
    return;
  }
  if (!res.ok) {
    status.textContent = "保存失败：" + (out.detail || res.status);
    return;
  }
  await loadPresets();
  const hit = presetRows.find(p => p.name === name);
  routeSaved = hit ? pickPreset(hit) : { name, stops: core.stops, options: core.options };
  routeDraft = pickPreset(routeSaved);
  localStorage.setItem("uwo.preset", routeSaved.name);
  renderRoute();
  status.textContent = `已${out.replaced ? "覆盖" : "存下"}方案『${name}』`
    + `：${out.preset.route_text}（勾 ${out.preset.goods_count} 件货）`
    + (out.preset.options_text ? ` ｜ ${out.preset.options_text}` : "")
    + reorderNote(out);
}

/* 「另存为新方案」：只把名字框清空、让人重新起名，**不提交** ——
   直接改名字框的值最省事，草稿和方案库因此对不上，状态行会自动念「有改动还没保存」。 */
function presetSaveAsNew() {
  routeDraft.name = "";
  switchRouteTab("plan");          // 方案名那一格在这块里，收着的时候点它要看得见
  const el = $("preset-name");
  if (el) { el.value = ""; el.focus(); }
  routeStatusLine();
}

/* ==================== 方案库（preset）：整套流程存个名字 / 装回编辑区 ====================
   方案 = 站次 + 每站勾的货 + 附加步骤那两块（购买前改舱 / 切换配置，2026-10-01 加的第二级；
   **不含当前所在港**，2026-09-29 你选的口径；也**不含「跑哪个模块」**，
   2026-09-30 起方案一律是完整一趟 trip）。存在 route_presets.json，
   **引擎一个字都不读它** —— 真跑哪几个方案在「运行」栏排成队列，由队列线程铺 route_plan.json。
   列表按下标传参（不往 onclick 里塞中文方案名：引号/转义一错就点不动，下标不会）。 */
let presetRows = [];
let presetErr = "";

async function loadPresets() {
  try {
    presetRows = (await getJson("/api/route/presets")).presets || [];
    presetErr = "";
  } catch (e) {
    presetRows = [];
    presetErr = e.message;   // 多半是改了 app.py 没重启后端（404），下面原样显示
  }
  renderPresets();
}

function renderPresets() {
  const box = $("preset-list");
  if (!box) return;
  if (presetErr) {
    box.innerHTML = `<b class="route-off">读方案失败：${escapeHtml(presetErr)}</b>`;
    return;
  }
  if (!presetRows.length) {
    box.innerHTML = `<div class="muted">还没存过方案 —— 把站次和勾选排好、上面填个名字，点「保存方案」</div>`;
    return;
  }
  box.innerHTML = presetRows.map((p, i) => `
    <div class="preset-row">
      <div class="preset-main">
        <b class="preset-name">${escapeHtml(p.name)}</b>
        <span class="preset-meta">${p.stop_count} 站`
        + `${p.goods_count ? ` · 勾 ${p.goods_count} 件货` : " · 没勾货"} · 存于 ${escapeHtml(p.saved_at || "（没记时间）")}</span>
        <div class="preset-route">${escapeHtml(p.route_text)}</div>
        ${p.options_text ? `<div class="preset-extra">${escapeHtml(p.options_text)}</div>` : ""}
      </div>
      <div class="preset-btns">
        <button class="btn small primary" onclick="presetEditByIdx(${i})">编辑</button>
        <button class="btn small" onclick="presetDeleteByIdx(${i})">删除</button>
      </div>
    </div>`).join("");
}

/* 「编辑」= 把这一份装回编辑区（**不写任何文件、不碰 route_plan.json**）。
   真正跑哪几个方案由「运行」栏的队列定，所以这里不再有「读取（立即生效）」。 */
function presetEditByIdx(i) {
  const p = presetRows[i];
  if (!p) return;
  routeSaved = pickPreset(p);
  routeDraft = pickPreset(p);
  localStorage.setItem("uwo.preset", routeSaved.name);
  renderRoute();
  // 进栏默认是收起的：点「编辑」要看的就是这一块，不顺手展开会像点了没反应
  switchRouteTab("plan");
  const st = $("route-status");
  if (st) st.textContent = `正在编辑方案『${routeSaved.name}』：${p.stop_count} 站 —— 改完点「保存方案」`;
}
function presetDeleteByIdx(i) { const p = presetRows[i]; if (p) presetDelete(p.name); }

async function presetDelete(name) {
  const status = $("route-status");
  if (!confirm(`删掉方案『${name}』？\n只删方案库这一条，route_plan.json 里现在这一趟不受影响。`)) return;
  let res, out;
  try {
    res = await fetch("/api/route/presets/" + encodeURIComponent(name), { method: "DELETE" });
    out = await res.json().catch(() => ({}));
  } catch (e) {
    status.textContent = "删方案失败: " + e.message;
    return;
  }
  if (!res.ok) {
    status.textContent = "删方案失败：" + (out.detail || res.status);
    return;
  }
  await loadPresets();
  status.textContent = `已删掉方案『${name}』，还剩 ${out.left} 个（现在这一趟没动）`;
}

/* ============ 初始化 ============ */
$("btn-refresh").addEventListener("click", () => refreshScreen());
$("canvas-wrap").addEventListener("wheel", (e) => {
  if (!e.ctrlKey) return;
  e.preventDefault();
  zoomAt(e.clientX, e.clientY, e.deltaY < 0 ? 1.2 : 1 / 1.2);
}, { passive: false });
$("queue-repeat").addEventListener("input", (e) => onQueueRepeat(e.target.value));
applyZoom();
window.addEventListener("resize", () => { if ($("screen-img").complete) redrawOverlay(); });
loadTemplates();
loadOcrRegions();
refreshScreen(false);
switchPage(localStorage.getItem("uwo.page") || "run");   // 落在上次那一栏，第一次是「运行」

// 截图只在点击"刷新截图"时更新。
// 以后状态机跑起来后，再在状态机运行时单独启动定时器刷新截图，
// 不要在这里加全局 setInterval（会覆盖按钮状态、打断框选）。