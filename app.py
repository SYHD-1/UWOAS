"""MuMu 图像识别可视化调试后端。

FastAPI + 原生 HTML/JS，浏览器打开 http://127.0.0.1:8000
提供：截图、模板增删改查、模板匹配测试、测试函数调用。
"""

import base64
import hashlib
import io
import json
import logging
import os
import signal
import time
from contextlib import asynccontextmanager

import cv2
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

import purchase_plan
import route_plan
import route_presets
import run_queue
import state_machine
from mumu_controller import MuMuController, capture
from vision import (OCR_REGIONS_JSON, find_any_template, find_in_list,
                    find_template, ocr_find, ocr_text, ocr_text_by_region)

# ==================== 配置 ====================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
TEMPLATES_JSON = os.path.join(BASE_DIR, "templates.json")
SCREEN_PATH = os.path.join(BASE_DIR, "screen.png")
# OCR 用独立截图文件：避免和前端每 3 秒轮询的 /api/screenshot 争抢同一个文件
# （screenshot() 会先删除旧文件再拉取，共用会出现"读取瞬间文件被删"的竞争）
OCR_SCREEN_PATH = os.path.join(BASE_DIR, "ocr_screen.png")
# 状态机用独立截图文件：避免和其他接口争抢同一个文件
STATE_SCREEN_PATH = os.path.join(BASE_DIR, "state_screen.png")
# 运行队列读港口名用独立截图文件：队列线程和上面三个各读各的，
# 共用一个文件时 capture() 会先删旧图，另一个线程正读到半截就会拿到空文件
QUEUE_SCREEN_PATH = os.path.join(BASE_DIR, "queue_screen.png")

ADB_PATH = r"C:\Program Files\Netease\MuMu Player 12\shell\adb.exe"
ADB_PORT = 16384
RESOLUTION = (1600, 900)

ADB_WAIT_SECONDS = 20   # 启动时等待设备就绪的最长时间
ADB_RETRY_SECONDS = 3   # 截图接口发现设备未就绪时的短等待（避免阻塞前端轮询）

# 认「船现在停在哪个港」读的是哪块 OCR 区域 —— 和买货 / 卖货链头核对港口名同一块。
# 放在这里是因为队列线程也要用它（队列每趟启动前自己 OCR 读一次港口），
# 定义必须早于下面那个 queue 单例，否则构造时拿不到它。
PORT_REGION = "港口名字"

# 状态机引擎（后台线程）：每次 start() 时从 states.json 重新加载配置
engine = state_machine.StateMachineEngine(
    adb_path=ADB_PATH, port=ADB_PORT, screen_path=STATE_SCREEN_PATH)

# 运行队列（后台线程）：按顺序把方案铺进 route_plan.json 再起引擎，关掉浏览器也继续跑。
# 注入的是**同一个引擎单例**，不新建 —— 「引擎正在跑」这个判断全服务只有一个事实来源。
queue = run_queue.QueueRunner(
    engine=engine, adb_path=ADB_PATH, port=ADB_PORT,
    screen_path=QUEUE_SCREEN_PATH, port_region=PORT_REGION)

# ==================== 通用工具 ====================
def load_templates():
    """读取 templates.json，不存在则返回空列表。"""
    if not os.path.exists(TEMPLATES_JSON):
        return []
    with open(TEMPLATES_JSON, "r", encoding="utf-8") as f:
        return json.load(f)


def save_templates(templates):
    """写入 templates.json。"""
    with open(TEMPLATES_JSON, "w", encoding="utf-8") as f:
        json.dump(templates, f, ensure_ascii=False, indent=2)


def clean_category(value):
    """整理模板分类名：最多两级「一级/二级」，全角斜杠和多余空格都归一。

    返回 "" 表示没有分类（前端会退回按名字前缀自动归类）。
    """
    if not isinstance(value, str):
        return ""
    text = value.replace("／", "/").replace(" ", "").strip("/")
    if not text:
        return ""
    parts = [p for p in text.split("/") if p]
    return "/".join(parts[:2])


def load_ocr_regions():
    """读取 ocr_regions.json，不存在则返回空列表。"""
    if not os.path.exists(OCR_REGIONS_JSON):
        return []
    with open(OCR_REGIONS_JSON, "r", encoding="utf-8") as f:
        return json.load(f)


def save_ocr_regions(regions):
    """写入 ocr_regions.json。"""
    with open(OCR_REGIONS_JSON, "w", encoding="utf-8") as f:
        json.dump(regions, f, ensure_ascii=False, indent=2)


def normalize_ocr_region(record):
    """规整前端提交的命名区域：{name, scene, roi, preprocess:{scale, binary_method}}。"""
    name = (record.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "区域名称不能为空")
    scene = (record.get("scene") or "").strip()
    roi = record.get("roi")
    if roi:
        roi = [int(v) for v in roi]
        if len(roi) != 4:
            raise HTTPException(400, "ROI 必须是 x,y,w,h 四个数")
    pp = record.get("preprocess") or {}
    binary_method = pp.get("binary_method")
    if binary_method in ("", "none"):
        binary_method = None
    return {
        "name": name,
        "scene": scene,
        "roi": roi,
        "preprocess": {"scale": int(pp.get("scale", 2)), "binary_method": binary_method},
    }


def img_to_b64(path):
    """读取图片并返回 base64 data-url。"""
    with open(path, "rb") as f:
        data = base64.b64encode(f.read()).decode()
    ext = os.path.splitext(path)[1].lstrip(".") or "png"
    return f"data:image/{ext};base64,{data}"


# ==================== ADB 连接管理 ====================
def _device_state(ctl):
    """执行 adb devices，返回目标设备的状态（device/offline/unauthorized），未列出返回 None。"""
    proc = ctl._run("devices")
    if proc.returncode != 0:
        return None
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == ctl.addr:
            return parts[1]
    return None


def ensure_adb_connected(timeout=ADB_WAIT_SECONDS):
    """确保 ADB 设备就绪：connect 一次后轮询 adb devices，最多等 timeout 秒。

    返回最终状态字符串（"device" 表示就绪），超时或 adb 不存在只打印警告、
    不抛异常，避免模拟器未启动时阻塞后端服务。
    """
    ctl = MuMuController(port=ADB_PORT, adb_path=ADB_PATH)
    state = None
    try:
        ctl._run("-s", ctl.addr, "connect", ctl.addr)
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = _device_state(ctl)
            if state == "device":
                print(f"[ADB] 设备已就绪: {ctl.addr}")
                return state
            if state == "offline":
                # offline 时直接 connect 通常无效，先 disconnect 再重连
                print(f"[ADB] 设备 offline，断开后重连: {ctl.addr}")
                ctl._run("disconnect", ctl.addr)
                time.sleep(1)
                ctl._run("-s", ctl.addr, "connect", ctl.addr)
            time.sleep(1)
    except FileNotFoundError:
        print(f"[ADB] 警告：找不到 adb 可执行文件: {ADB_PATH}，服务继续启动")
        return None

    print(f"[ADB] 警告：等待 {timeout} 秒后设备仍未就绪"
          f"（{ctl.addr} 状态: {state or '未找到设备'}），服务继续启动")
    return state


def grab_screen(save_path):
    """确保 ADB 就绪后截一张新图到 save_path。

    截图本身走 mumu_controller.capture —— 和状态机引擎共用同一把锁，
    引擎在后台跑的时候点「刷新截图」只会排队，不会两个线程同时抢 ADB。
    任一步失败都立刻抛出带具体原因的 HTTPException，绝不在没有新截图的情况下
    继续去读旧文件（旧文件可能已被删除或过期）。
    """
    ctl = MuMuController(port=ADB_PORT, adb_path=ADB_PATH)
    state = _device_state(ctl)
    if state != "device":
        # 启动时没连上（模拟器晚开），这里再短等一次，避免直接 500
        state = ensure_adb_connected(timeout=ADB_RETRY_SECONDS)
    if state != "device":
        raise HTTPException(
            503, f"adb 未连接: {ctl.addr} {state or '未找到设备'}，请确认模拟器已启动")

    # 走和状态机引擎同一把锁的截图入口（内部已校验文件存在且非空）
    ok, info = capture(ctl, save_path, "调试台截图")
    if not ok:
        state = _device_state(ctl)
        raise HTTPException(
            500, f"{info}（设备 {ctl.addr} 状态={state or '未找到设备'}）")
    return ctl


# ==================== FastAPI 应用 ====================
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时打印实际使用的路径，方便排查"找不到文件"类问题
    print(f"[路径] BASE_DIR         = {BASE_DIR}", flush=True)
    print(f"[路径] SCREEN_PATH      = {SCREEN_PATH}", flush=True)
    print(f"[路径] OCR_SCREEN_PATH  = {OCR_SCREEN_PATH}", flush=True)
    print(f"[路径] TEMPLATES_JSON   = {TEMPLATES_JSON}", flush=True)
    print(f"[路径] OCR_REGIONS_JSON = {OCR_REGIONS_JSON}", flush=True)
    print(f"[路径] PURCHASE_PLAN    = {purchase_plan.PLAN_JSON}", flush=True)
    print(f"[路径] STATES_JSON      = {state_machine.STATES_JSON}", flush=True)
    print(f"[路径] STATE_SCREEN     = {STATE_SCREEN_PATH}", flush=True)
    print(f"[路径] RUN_QUEUE_JSON   = {run_queue.QUEUE_JSON}", flush=True)
    print(f"[路径] QUEUE_SCREEN     = {QUEUE_SCREEN_PATH}", flush=True)
    # 启动时先尝试建立 ADB 连接（模拟器可能还没开完，最多等 20 秒）
    ensure_adb_connected()
    yield
    # 关闭逻辑（目前没有需要清理的资源）


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "frontend")), name="static")


@app.get("/")
def index():
    """返回前端页面 HTML。"""
    with open(os.path.join(BASE_DIR, "frontend", "index.html"), "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/api/screenshot")
def get_screenshot():
    """从模拟器截图，返回图片地址和分辨率。"""
    grab_screen(SCREEN_PATH)
    # 用内容指纹判断截图是否变化（截图每次都重写文件，文件 mtime 无法反映内容差异）
    with open(SCREEN_PATH, "rb") as f:
        last_modified = hashlib.md5(f.read()).hexdigest()
    return {
        "url": f"/api/screen.png?t={last_modified}",
        "width": RESOLUTION[0],
        "height": RESOLUTION[1],
        "last_modified": last_modified,
    }


@app.get("/api/screen.png")
def screen_file():
    """返回当前截图图片文件。"""
    if not os.path.exists(SCREEN_PATH):
        raise HTTPException(404, "还没有截图，请先刷新")
    return FileResponse(SCREEN_PATH, media_type="image/png")


@app.get("/api/templates")
def list_templates():
    """返回所有模板列表。"""
    templates = load_templates()
    for t in templates:
        im = t.get("image")
        if im and os.path.exists(os.path.join(BASE_DIR, im)):
            t["thumb"] = img_to_b64(os.path.join(BASE_DIR, im))
        else:
            t["thumb"] = None
    return templates


@app.post("/api/templates")
def create_template(record: dict):
    """新建模板：record = {name, image, roi, threshold, category?}。
    image 可能为 base64 数据(data-url)，或相对于 templates/ 的文件名，或 None。
    category 可选，形如 "UI/船舱"；留空则前端按名字前缀自动归类。
    """
    name = record.get("name") or "未命名模板"
    roi = record.get("roi") or [0, 0, 0, 0]
    threshold = float(record.get("threshold", 0.8)) if record.get("threshold") is not None else 0.8
    image_src = record.get("image")

    # 保存模板图片（文件名用 ASCII 时间戳，避免中文路径导致 cv2.imread 失败）
    if image_src and image_src.startswith("data:"):
        header, data = image_src.split(",", 1)
        ext = "png" if "png" in header else "jpg"
        filename = f"tpl_{int(time.time())}_{len(load_templates())}.{ext}"
        with open(os.path.join(TEMPLATES_DIR, filename), "wb") as f:
            f.write(base64.b64decode(data))
        image_path = f"templates/{filename}"
    elif image_src:
        image_path = image_src  # 已存在文件（手动上传走文件服务器）
    else:
        image_path = None

    templates = load_templates()
    new_rec = {"name": name, "image": image_path, "roi": roi, "threshold": threshold}
    category = clean_category(record.get("category"))
    if category:
        new_rec["category"] = category
    templates.append(new_rec)
    save_templates(templates)
    return new_rec


@app.delete("/api/templates/{index}")
def delete_template(index: int):
    """删除指定下标的模板。"""
    templates = load_templates()
    if not 0 <= index < len(templates):
        raise HTTPException(404, "模板不存在")
    rec = templates.pop(index)
    # 尝试删除对应的模板图片文件
    im = rec.get("image")
    if im and os.path.exists(os.path.join(BASE_DIR, im)):
        os.remove(os.path.join(BASE_DIR, im))
    save_templates(templates)
    return {"ok": True}


@app.put("/api/templates/{index}")
def update_template(index: int, record: dict):
    """更新指定下标模板的名称/ROI/阈值/分类。

    category 传空字符串 = 删掉这个字段，退回「按名字前缀自动归类」。
    """
    templates = load_templates()
    if not 0 <= index < len(templates):
        raise HTTPException(404, "模板不存在")
    rec = templates[index]
    rec.update({
        "name": record.get("name", rec["name"]),
        "roi": record.get("roi", rec["roi"]),
        "threshold": record.get("threshold", rec["threshold"]),
    })
    if "category" in record:
        category = clean_category(record["category"])
        if category:
            rec["category"] = category
        else:
            rec.pop("category", None)
    save_templates(templates)
    return rec


@app.post("/api/templates/{index}/test")
def test_template(index: int):
    """先实时截图，再对这张新图测试匹配指定模板。

    以前直接读磁盘上残留的 screen.png（前端自动刷新或上一次手动截图留下的），
    画面已经变了却仍按旧图判定，置信度和叠加框都会误导人。
    """
    templates = load_templates()
    if not 0 <= index < len(templates):
        raise HTTPException(404, "模板不存在")
    rec = templates[index]
    image_path = os.path.join(BASE_DIR, rec["image"]) if rec.get("image") else None
    if not image_path or not os.path.exists(image_path):
        raise HTTPException(400, f"模板图片不存在: {rec.get('image')}")
    grab_screen(SCREEN_PATH)

    try:
        result = find_template(
            SCREEN_PATH, image_path,
            roi=tuple(rec["roi"]) if rec["roi"] else None,
            threshold=rec["threshold"],
        )
    except Exception as e:
        raise HTTPException(400, str(e))

    overlay = draw_result(SCREEN_PATH, rec["image"], rec["roi"], result)
    return {"result": result, "overlay": overlay}


@app.post("/api/test/any")
def test_any(payload: dict):
    """测试任意匹配：先实时截图，再在最新画面上匹配。前端传 template_indexes + roi。"""
    templates = load_templates()
    indexes = payload.get("template_indexes", [])
    roi = payload.get("roi") or None
    if not indexes:
        raise HTTPException(400, "请先选择模板")
    paths = []
    for idx in indexes:
        rec = templates[idx]
        p = os.path.join(BASE_DIR, rec["image"]) if rec.get("image") else None
        if p and os.path.exists(p):
            paths.append(p)
    if not paths:
        raise HTTPException(400, "所选模板图片不存在")
    grab_screen(SCREEN_PATH)
    try:
        result = find_any_template(
            SCREEN_PATH, paths, roi=tuple(roi) if roi else None,
            threshold=0.8,
        )
    except Exception as e:
        raise HTTPException(400, str(e))
    return {"result": result}


@app.post("/api/test/list")
def test_list(payload: dict):
    """测试列表查找：支持双向滑动（先向下看 max_swipes 次，再向上看 max_swipes_up 次）。"""
    templates = load_templates()
    idx = payload["template_index"]
    rec = templates[idx]
    image_path = os.path.join(BASE_DIR, rec["image"]) if rec.get("image") else None
    list_roi = tuple(payload["list_roi"]) if payload.get("list_roi") else None
    swipe_range = tuple(payload["swipe_range"]) if payload.get("swipe_range") else None
    max_swipes = int(payload.get("max_swipes", 5))
    max_swipes_up = int(payload.get("max_swipes_up", 0))

    ctl = MuMuController(port=ADB_PORT, adb_path=ADB_PATH)
    capture_path = os.path.join(BASE_DIR, "list_screen.png")

    def snap():
        """每次滑动后重新截一张。走公共入口 = 和引擎共用一把锁；
        截图失败就直接报错，绝不让它拿着上一张旧画面继续找（会给出假结果）。"""
        ok, info = capture(ctl, capture_path, "列表查找截图")
        if not ok:
            raise RuntimeError(info)
        return capture_path

    try:
        result = find_in_list(
            snap(),
            image_path,
            list_roi=list_roi if list_roi else [0, 0, RESOLUTION[0], RESOLUTION[1]],
            swipe_range=swipe_range,
            max_swipes=max_swipes,
            capture=snap,
            swipe_func=ctl.swipe,
            max_swipes_up=max_swipes_up,
        )
    except Exception as e:
        raise HTTPException(400, str(e))
    return {"result": result}


@app.post("/api/ocr/test")
def ocr_test(payload: dict):
    """实时截图后做 OCR。EasyOCR Reader 在进程内常驻，仅首次调用加载模型（10~30 秒）。

    请求体：{roi?: [x,y,w,h], scale?: 2, binary_method?: "adaptive"|"otsu"|null, keyword?: str}
    不传 keyword 返回 {"mode": "text", "text": ...}；
    传 keyword 返回 {"mode": "find", found, text, cx, cy}。
    用同步 def：模型加载/推理是阻塞调用，跑在线程池里，不会卡住事件循环。
    截图写入独立的 OCR_SCREEN_PATH，避免与 /api/screenshot 的自动刷新互相踩踏。
    """
    grab_screen(OCR_SCREEN_PATH)

    roi_raw = payload.get("roi")
    roi = tuple(int(v) for v in roi_raw) if roi_raw else None
    scale = int(payload.get("scale", 2))
    binary_method = payload.get("binary_method")  # None / "adaptive" / "otsu"
    if binary_method in ("", "none"):
        binary_method = None
    keyword = payload.get("keyword") or None

    try:
        if keyword:
            result = ocr_find(OCR_SCREEN_PATH, keyword, roi=roi,
                              scale=scale, binary_method=binary_method)
            return {"mode": "find", **result}
        text = ocr_text(OCR_SCREEN_PATH, roi=roi, scale=scale, binary_method=binary_method)
        return {"mode": "text", "text": text}
    except FileNotFoundError as e:
        raise HTTPException(500, f"读取截图失败: {e}（当前截图路径: {OCR_SCREEN_PATH}）")
    except Exception as e:
        raise HTTPException(500, f"OCR 失败: {e}")


@app.get("/api/ocr/regions")
def list_ocr_regions():
    """返回所有命名 OCR 区域。"""
    return load_ocr_regions()


@app.post("/api/ocr/regions")
def create_ocr_region(record: dict):
    """新建命名区域，同名返回 400。"""
    region = normalize_ocr_region(record)
    regions = load_ocr_regions()
    if any(r["name"] == region["name"] for r in regions):
        raise HTTPException(400, f"已存在同名区域: {region['name']}")
    regions.append(region)
    save_ocr_regions(regions)
    return region


@app.put("/api/ocr/regions/{name}")
def update_ocr_region(name: str, record: dict):
    """按名称修改命名区域（允许改名，新名字不能与其他区域冲突）。"""
    region = normalize_ocr_region(record)
    regions = load_ocr_regions()
    for i, r in enumerate(regions):
        if r["name"] == name:
            if region["name"] != name and any(x["name"] == region["name"] for x in regions):
                raise HTTPException(400, f"已存在同名区域: {region['name']}")
            regions[i] = region
            save_ocr_regions(regions)
            return region
    raise HTTPException(404, f"区域不存在: {name}")


@app.delete("/api/ocr/regions/{name}")
def delete_ocr_region(name: str):
    """按名称删除命名区域。"""
    regions = load_ocr_regions()
    remaining = [r for r in regions if r["name"] != name]
    if len(remaining) == len(regions):
        raise HTTPException(404, f"区域不存在: {name}")
    save_ocr_regions(remaining)
    return {"ok": True}


# ==================== 跑商购物表格 ====================
def _validated_rows(rows_raw):
    """把前端提交的整张表格规整成 {rows:[...]}，「港口+货物名称」重复直接报错。"""
    if not isinstance(rows_raw, list):
        raise HTTPException(400, "rows 必须是数组")
    rows = []
    for rec in rows_raw:
        try:
            rows.append(purchase_plan.normalize_row(rec))
        except ValueError as e:
            raise HTTPException(400, str(e))
    seen = {}
    for i, r in enumerate(rows):
        key = purchase_plan.row_key(r)
        if key in seen:
            raise HTTPException(400, f"第 {i + 1} 行与第 {seen[key] + 1} 行重复（港口+货物名称）: {key}")
        seen[key] = i
    return {"rows": rows}


def _decorated(plan):
    """返回给前端时带上算出来的船舱名和行键（文件里不存这两个字段）。"""
    return {"rows": [purchase_plan.decorate(r) for r in plan["rows"]]}


def _goods_options():
    """表格「货物名称」搜索框的候选：模板库里所有「商品-XX」模板去掉前缀。
    买货是拿模板去找货，名字对不上就永远命不中，所以这一列只让搜出来点、不让手打。"""
    names = [purchase_plan.goods_name_from_template(t.get("name")) for t in load_templates()]
    return sorted({n for n in names if n})


@app.get("/api/plan")
def plan_list():
    """返回购物表格 + 类别候选 + 货物候选（来自模板库），前端表格那栏的匹配搜索用。

    另外带上 `port_options` / `catalog`：2026-09-30 起「跑商设置」（编方案）和「调试 → 单步调试」
    都要读**目录**（港口候选、每个港买得到哪些货）来画勾选框，而目录就长在表格里 ——
    从这里给，它们就不用再去读 route_plan.json 那份（那一份现在是队列线程的运行态产物，
    不该被界面当成目录读）。
    """
    return {
        "cargo_types": purchase_plan.CARGO_TYPES,
        "goods_options": _goods_options(),
        "port_options": _plan_ports(),
        "catalog": _plan_catalog(),
        **_decorated(purchase_plan.load_plan()),
    }


@app.put("/api/plan")
def plan_replace(record: dict):
    """整体替换购物表格：{rows:[{port, goods_name, cargo_type, note}, ...]}。"""
    plan = _validated_rows(record.get("rows"))
    purchase_plan.save_plan(plan)
    return _decorated(plan)


@app.post("/api/plan/rows")
def plan_add_row(record: dict):
    """新增一行。港口可以重复，但「港口+货物名称」不能和已有行撞。"""
    try:
        row = purchase_plan.normalize_row(record)
    except ValueError as e:
        raise HTTPException(400, str(e))
    plan = purchase_plan.load_plan()
    key = purchase_plan.row_key(row)
    if any(purchase_plan.row_key(r) == key for r in plan["rows"]):
        raise HTTPException(400, f"已存在同一行: {key}")
    plan["rows"].append(row)
    purchase_plan.save_plan(plan)
    return purchase_plan.decorate(row)


@app.put("/api/plan/rows/{index}")
def plan_update_row(index: int, record: dict):
    """按下标改一行（允许改港口名/货物名，改完不能和别的行撞键）。

    用下标而不是名字定位：一行现在由「港口+货物名称」两列共同决定，
    拿名字当键既容易撞、又没法在 URL 里安全地带上中文和分隔符。
    """
    try:
        row = purchase_plan.normalize_row(record)
    except ValueError as e:
        raise HTTPException(400, str(e))
    plan = purchase_plan.load_plan()
    if not 0 <= index < len(plan["rows"]):
        raise HTTPException(404, f"第 {index + 1} 行不存在，表格现在 {len(plan['rows'])} 行")
    key = purchase_plan.row_key(row)
    for i, other in enumerate(plan["rows"]):
        if i != index and purchase_plan.row_key(other) == key:
            raise HTTPException(400, f"与第 {i + 1} 行重复（港口+货物名称）: {key}")
    plan["rows"][index] = row
    purchase_plan.save_plan(plan)
    return purchase_plan.decorate(row)


@app.delete("/api/plan/rows/{index}")
def plan_delete_row(index: int):
    """按下标删除一行。"""
    plan = purchase_plan.load_plan()
    if not 0 <= index < len(plan["rows"]):
        raise HTTPException(404, f"第 {index + 1} 行不存在，表格现在 {len(plan['rows'])} 行")
    plan["rows"].pop(index)
    purchase_plan.save_plan(plan)
    return {"ok": True}


# ==================== 跑商规划（跑商设置栏） ====================
def _plan_ports():
    """购物表格里出现过的港口名（去重、保持首次出现的顺序），「买货港口」下拉用。"""
    ports = []
    for row in purchase_plan.load_plan()["rows"]:
        p = (row.get("port") or "").strip()
        if p and p not in ports:
            ports.append(p)
    return ports


def _plan_catalog():
    """购物表格按港口分组：{港口名: [{goods_name, cargo_type, cabin_name, note}, ...]}。

    「跑商设置」里每个买货站的货物勾选框就是从这个来的 —— 表格只是目录，
    「这一趟买哪几件」不在这里定（在 route_plan.json 每一站的 goods 里）。
    顺序沿用表格里人工填的先后。
    """
    out = {}
    for row in purchase_plan.load_plan()["rows"]:
        p = (row.get("port") or "").strip()
        if not p:
            continue
        out.setdefault(p, []).append(purchase_plan.decorate(row))
    return out


def _goods_by_port(catalog):
    """{港口: [货物名, ...]}，只留名字，给 route_plan.warnings 核对用。"""
    return {p: [r["goods_name"] for r in rows] for p, rows in catalog.items()}


def _route_view(route):
    """规划返回给前端时，顺带把可选港口、目录、算出来的「本次这一站/下一站」和提醒一起给。

    「下一站」「各段港口」「本次买货港 + 本次买哪几件货」这些都是 `route_plan.derive()` 算的，
    **只在 Python 算一处**：前端再算一遍就会出现两份说法，界面说的和引擎用的可能对不上。
    """
    ports = _plan_ports()
    catalog = _plan_catalog()
    return {**route, "port_options": ports, "catalog": catalog,
            "derived": route_plan.derive(route),
            "warnings": route_plan.warnings(route, ports, _goods_by_port(catalog))}


@app.get("/api/route")
def route_get():
    """读当前跑商规划（买货 / 中转 / 出货三段）。文件不存在返回全空。"""
    return _route_view(route_plan.load_route())


@app.put("/api/route")
def route_put(record: dict):
    """整体保存跑商规划到 route_plan.json。

    引擎在跑的时候拒绝：改规划不会影响正在跑的这一趟（港口和本次清单是启动那一刻定死的），
    让你以为改成功了比直接报错更糟。
    买货模块的「本次这一站」= 类型是买货 且 港口 == current_port 的那一站（你 2026-09-27 拍的），
    该站 `goods` 里挂哪几件就买哪几件。出货段（sell）从 2026-09-28 起会真跑；中转段（transit）
    只有「完整一趟」会真走（途经那个港：进一次港、再出一次港，不进交易所）—— 单模块不看它。

    模块是「完整一趟」时顺序由后端排（2026-09-29 你选的 A）：站次按 `买货 → 中转 → 卖货`
    重排后才落盘，同类内部保持你排的先后。这次动了哪几站原样回在 `reorder_moves` 里，
    界面上面那句「保存时会自动重排」就是靠它对上的 —— 不回报就等于悄悄改了人的计划。
    """
    if engine.is_running() or queue.is_running():
        raise HTTPException(409, "引擎或队列正在跑，本次港口已经定死了，这时候改规划不会生效")
    moves = []
    try:
        route = route_plan.normalize_route(record, moves)
    except ValueError as e:
        raise HTTPException(400, str(e))
    route_plan.save_route(route)
    return dict(_route_view(route), reordered=bool(moves), reorder_moves=moves)


# ==================== 跑商方案（preset）：一整套计划存个名字，随时读回来 ====================
@app.get("/api/route/presets")
def route_preset_list():
    """列出全部方案。每条带一句人话摘要（几站、怎么走、几件货、什么时候存的）+ 完整站次，
    界面读出来不用再打一次请求就能预览。"""
    try:
        items = route_presets.load_presets()
    except ValueError as e:
        raise HTTPException(500, f"方案文件读不出来：{e}")
    return {"presets": [dict(route_presets.summary(p), stops=p["stops"]) for p in items]}


@app.post("/api/route/presets")
def route_preset_save(record: dict):
    """把传进来的这套（站次 + 每站勾选）存成一个方案；**同名就是覆盖**。

    方案一律是完整一趟（`normalize_preset` 把 run_module 写死成 trip），前端传模块也不认：
    2026-09-30 起「跑商设置」那一栏只编方案，不再分类型。

    队列在跑时拒绝：队列线程**每趟开始前都会重读方案库**（`_run_one` 调 `load_presets`），
    跑到一半改名 / 改站次会让后面那些队项读到另一份东西，队列就会以「方案不在方案库里了」
    停住 —— 与其让人以为改成功了，不如当场说清楚。
    （引擎自己在跑不影响存方案：写的是方案库，不碰引擎正在读的 route_plan.json。）
    """
    if queue.is_running():
        raise HTTPException(409, "队列正在跑，它每趟开始前都会重读方案库 —— 先停队列再改方案")
    moves = []
    try:
        preset = route_presets.normalize_preset(record, moves)
        items = route_presets.load_presets()
    except ValueError as e:
        raise HTTPException(400, str(e))
    at = next((i for i, p in enumerate(items) if p["name"] == preset["name"]), -1)
    replaced = at >= 0
    if replaced:
        items[at] = preset          # 覆盖在原位置，方案列表的顺序不因为改一次就跳到最后
    else:
        items.append(preset)
    route_presets.save_presets(items)
    return {"ok": True, "replaced": replaced, "reordered": bool(moves),
            "reorder_moves": moves, "preset": route_presets.summary(preset)}


@app.post("/api/route/presets/load")
def route_preset_load(record: dict):
    """读一个方案 = **立刻写进 route_plan.json**（你拍的「立即生效」，少一步、不会出现「读了其实没存」）。

    只换三样：本次模块、站次、每站勾选的货。**不换「当前所在港」** —— 方案里根本没存它
    （2026-09-29 你选的口径），盘上现在填的是什么，读完还是什么。
    站次里对不上这个港时，`derived` 的 reason 会当场写明「本次这一站认不出来」，不会拿旧港名蒙。
    """
    if engine.is_running() or queue.is_running():
        raise HTTPException(409, "引擎或队列正在跑，本次港口已经定死了，这时候换方案不会生效")
    name = ((record or {}).get("name") or "").strip()
    if not name:
        raise HTTPException(400, "没说要读哪个方案")
    moves = []
    try:
        preset = route_presets.find(route_presets.load_presets(), name)
        if not preset:
            raise HTTPException(400, f"没有叫『{name}』的方案 —— 先刷新一下方案列表")
        route = route_plan.normalize_route(
            route_presets.to_route(preset, route_plan.load_route()["current_port"]), moves)
    except ValueError as e:
        raise HTTPException(400, str(e))
    route_plan.save_route(route)
    return dict(_route_view(route), loaded_from=name,
                reordered=bool(moves), reorder_moves=moves)


@app.delete("/api/route/presets/{name}")
def route_preset_delete(name: str):
    """删掉一个方案。**只删方案库这一条**，不动 route_plan.json —— 现在这一趟照旧在跑/照旧能跑。

    队列在跑时拒绝，理由和存方案那一条相同：删掉的正好是队列里等着的名字，
    队列下一趟就会以「方案不在方案库里了」停住。
    """
    if queue.is_running():
        raise HTTPException(409, "队列正在跑，删了它等着跑的方案会让队列停住 —— 先停队列")
    name = (name or "").strip()
    try:
        items = route_presets.load_presets()
    except ValueError as e:
        raise HTTPException(500, f"方案文件读不出来：{e}")
    if not route_presets.find(items, name):
        raise HTTPException(404, f"没有叫『{name}』的方案，没删任何东西")
    route_presets.save_presets([p for p in items if p["name"] != name])
    return {"ok": True, "deleted": name, "left": len(items) - 1}


# ==================== 运行队列：把方案排成一队，交给后端线程按顺序跑 ====================
def _queue_view(q):
    """队列配置 + 方案库摘要 + 队列里已经失效的方案名。

    一次给全是刻意的：前端渲染队列要同时知道「队列里排了谁」和「方案库里还有没有它」，
    分两次请求会出现「列表已经更新、失效标记还是上一轮」的错位。
    """
    try:
        presets = route_presets.load_presets()
    except ValueError as e:
        raise HTTPException(500, f"方案库读不出来：{e}")
    known = {p["name"] for p in presets}
    return {**q, "presets": [route_presets.summary(p) for p in presets],
            "missing": [n for n in q["items"] if n not in known]}


@app.get("/api/queue")
def queue_get():
    """读队列配置（run_queue.json）；文件不存在就是一份空队列，不是错误。"""
    return _queue_view(run_queue.load_queue())


@app.put("/api/queue")
def queue_put(record: dict):
    """保存队列编排（items / mode / repeat）。

    队列在跑时拒绝：队列线程读的是启动那一刻拷进内存的那份，改了文件它也不看 ——
    让人以为换了一队，比直接报错更糟。
    """
    if queue.is_running():
        raise HTTPException(409, "队列正在跑，队列线程读的是启动那一刻那份 —— 先停队列再改")
    try:
        q = run_queue.save_queue(record)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except OSError as e:
        raise HTTPException(500, f"写 run_queue.json 失败: {e}")
    return _queue_view(q)


@app.post("/api/queue/start")
def queue_start(record: dict = None):
    """启动队列线程。

    body 可以完全没有（用 run_queue.json 里存的那份），也可以带 `{items, mode, repeat}`：
    前端点「启动队列」时队列表格可能还有没点保存的编排，这时候先把这份规整一遍再跑，
    免得「界面显示 3 个方案、实际跑了 2 个」。
    """
    rec = record or {}
    result = queue.start(items=rec.get("items"), mode=rec.get("mode"), repeat=rec.get("repeat"))
    if not result["ok"]:
        raise HTTPException(400, result["message"])
    return result


@app.post("/api/queue/stop")
def queue_stop():
    """停队列：正在跑的那一趟引擎也一起停（队列线程正等着它收尾）。"""
    result = queue.stop()
    if not result["ok"]:
        raise HTTPException(409, result["message"])
    return result


@app.get("/api/queue/status")
def queue_status():
    """队列进度 + 引擎状态一次给全。

    前端轮询只打这一个接口就够了：队列那几行（第几个方案 / 第几遍 / 阶段）和引擎日志
    本来就是一起看的，分两次请求会出现「队列说在跑、引擎说没跑」的中间态。
    """
    return {"queue": queue.status(), "engine": engine.status()}


# ==================== 设置页：环境信息 + 引擎参数 ====================
@app.get("/api/info")
def api_info():
    """设置页的「设备与环境」：一次读齐 ADB、分辨率、数据文件路径和各库条目数。

    设备状态要真的跑一次 `adb devices`（约 0.3 秒），所以这接口只在打开设置栏时调一次，
    不进任何轮询。adb 可执行文件不在或模拟器没开时 device_state 返回 None，不抛异常。
    """
    ctl = MuMuController(port=ADB_PORT, adb_path=ADB_PATH)
    try:
        device_state = _device_state(ctl)
    except FileNotFoundError:      # adb 根本不在，不让整个接口 500
        device_state = None
    return {
        "backend": "http://127.0.0.1:8000",
        "base_dir": BASE_DIR,
        "adb_path": ADB_PATH,
        "adb_exists": os.path.exists(ADB_PATH),
        "device_addr": ctl.addr,
        "device_state": device_state,
        "device_ready": device_state == "device",
        "resolution": list(RESOLUTION),
        "files": {
            "模板库": TEMPLATES_JSON,
            "OCR 区域库": OCR_REGIONS_JSON,
            "购物表格": purchase_plan.PLAN_JSON,
            "跑商规划": route_plan.ROUTE_JSON,
            "状态机（含引擎参数）": state_machine.STATES_JSON,
        },
        "counts": {
            "模板": len(load_templates()),
            "OCR 命名区域": len(load_ocr_regions()),
            "购物表格行": len(purchase_plan.load_plan().get("rows") or []),
            "状态": len(state_machine.load_states()["states"]),
        },
    }


# 设置页能改的引擎参数白名单：类型 + 取值范围，没列出来的项一律拒收。
# 这些数直接决定引擎在真机上的动作节奏和停止条件（interval_ms 填 0 会连点、
# max_rounds 填太小一趟港口跑不完），而买货花真金币、不可逆，所以按边界校验，不猜。
CONFIG_SPEC = {
    "interval_ms": ("int", 200, 600000),
    "max_rounds": ("int", 1, 10000),
    "max_no_match": ("int", 1, 1000),
    "action_timeout_seconds": ("num", 1, 3600),
    "human_click": {
        "enabled": "bool",
        "delay_min_ms": ("int", 0, 60000),
        "delay_max_ms": ("int", 0, 60000),
        "inset_ratio": ("num", 0, 0.45),
    },
    "exit_to_port": {
        "enabled": "bool",
        "buttons": "names",
        "max_clicks": ("int", 0, 20),
        "wait_ms": ("int", 0, 60000),
        "port_marker": "name",
    },
    "negotiation": {
        "region": "name",
        "max_clicks": ("int", 0, 20),
        "click_interval_ms": ("int", 0, 60000),
        "buttons": "points",
    },
}


def _checked(key, rule, value):
    """按一条规则校验一个配置值并规整返回；不合格直接 400，把话说清在哪一项。"""
    if isinstance(rule, dict):                       # 嵌套的一组
        if not isinstance(value, dict):
            raise HTTPException(400, f"{key} 要是一个对象，现在不是")
        unknown = set(value) - set(rule)
        if unknown:
            raise HTTPException(400, f"{key} 里有不认识的项: {', '.join(sorted(unknown))}")
        return {sub: _checked(f"{key}.{sub}", sub_rule, value[sub])
                for sub, sub_rule in rule.items() if sub in value}
    if rule == "bool":
        if not isinstance(value, bool):
            raise HTTPException(400, f"{key} 要勾或不勾，现在是 {value!r}")
        return value
    if rule == "name":
        text = str(value or "").strip()
        if not text:
            raise HTTPException(400, f"{key} 不能为空")
        return text
    if rule == "names":
        if not isinstance(value, list) or not all(str(v).strip() for v in value):
            raise HTTPException(400, f"{key} 要是一个由模板名组成的列表")
        return [str(v).strip() for v in value if str(v).strip()]
    if rule == "points":
        if not isinstance(value, dict):
            raise HTTPException(400, f"{key} 要写成 {名称: [x, y]} 这样的对象")
        out = {}
        for name, pt in value.items():
            ok = (isinstance(pt, (list, tuple)) and len(pt) == 2
                  and all(isinstance(v, int) and not isinstance(v, bool) for v in pt))
            if not ok:
                raise HTTPException(400, f"{key}.{name} 要写成 [x, y] 两个整数，现在是 {pt!r}")
            out[str(name)] = [int(pt[0]), int(pt[1])]
        return out
    kind, lo, hi = rule
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HTTPException(400, f"{key} 要填数字，现在是 {value!r}")
    if not lo <= value <= hi:
        raise HTTPException(400, f"{key} 只能填 {lo}~{hi}，现在是 {value}")
    if kind == "int":
        return int(round(value))
    return value      # num 原样留着：120 不该被写成 120.0，0.18 也不该被四舍五入


@app.put("/api/state/config")
def state_update_config(patch: dict):
    """只改 states.json 的 config 段，不碰 states 数组。

    为什么不用 PUT /api/state/states：那是整体替换，前端必须把整个状态列表一起提交回来。
    设置页手上那份流程可能是几分钟前读的，一提交就覆盖掉中途在「流程」栏改过的东西。
    """
    if engine.is_running():
        raise HTTPException(409, "状态机运行中，现在改参数也不会生效（引擎只在启动那一刻读 states.json），请先停止")
    if not isinstance(patch, dict):
        raise HTTPException(400, "请求体要是一个对象")
    unknown = set(patch) - set(CONFIG_SPEC)
    if unknown:
        raise HTTPException(400,
                            f"不认识的配置项: {', '.join(sorted(unknown))}（设置页没这一项，请直接改 states.json）")
    data = state_machine.load_states()
    cfg = dict(data.get("config") or {})
    for key, value in patch.items():
        cleaned = _checked(key, CONFIG_SPEC[key], value)
        if isinstance(CONFIG_SPEC[key], dict):
            cfg[key] = {**(cfg.get(key) or {}), **cleaned}   # 只覆盖勾出来的子项，其余原样留
        else:
            cfg[key] = cleaned
    data["config"] = cfg
    state_machine.save_states(data)
    return {"ok": True, "config": cfg}


# ==================== 状态机 ====================
def normalize_state(record):
    """规整单个状态：{id, name, module, entry, condition, actions, next}。"""
    sid = (record.get("id") or "").strip()
    if not sid:
        raise HTTPException(400, "状态 id 不能为空")
    state = {
        "id": sid,
        "name": record.get("name") or sid,
        "entry": bool(record.get("entry")),
        "condition": record.get("condition"),
        "actions": record.get("actions") or [],
        "next": record.get("next"),
    }
    # module 和 global 一样只在有值时保留：引擎按模块筛状态，
    # 存空字符串等于给这条链发了一张「哪个模块都不属于」的户口，
    # 界面点一次保存就把整条链筛没了（启动时报「模块下没有任何状态」）。
    module = (record.get("module") or "").strip()
    if module:
        state["module"] = module
    # global 仅在为 true 时保留，避免给普通状态多写一个冗余字段、改变现有 JSON 结构
    if record.get("global"):
        state["global"] = True
    return state


def normalize_document(record):
    """规整整体配置：{config?, states?}。config 与默认值浅合并。"""
    states = record.get("states")
    if not isinstance(states, list):
        raise HTTPException(400, "states 必须是数组")
    config = state_machine.load_states()["config"]
    config.update(record.get("config") or {})
    return {"config": config, "states": [normalize_state(s) for s in states]}


def read_port_text(screen_path=None):
    """截一张图、OCR 那块港口名，返回画面上读到的**原始文字**（空串 = 读不出）。

    单独拆出来是因为有三处要走**同一块区域、同一条路**：正式的 `/api/state/start`、
    队列线程每趟启动前读一次、调试栏单步。各写一份 OCR 迟早会漏改一处，
    出现「界面读出来的港和引擎拿来核对的不一样」。

    `screen_path` 传了就假定图已经在盘上（自检传真图进来，就不会去碰 ADB）；
    内部路径用独立的 ocr_screen.png，不和前端轮询的截图文件抢。
    """
    path = screen_path or OCR_SCREEN_PATH
    if screen_path is None:
        grab_screen(path)          # 自检传真图进来，就不会去碰 ADB
    try:
        return (ocr_text_by_region(path, PORT_REGION) or "").strip()
    except FileNotFoundError as e:
        raise HTTPException(500, f"读取截图失败: {e}（{path}）")
    except Exception as e:
        raise HTTPException(500, f"OCR 读港口名失败: {e}")


def read_current_port_by_ocr(stops, screen_path=None):
    """截一张图、OCR 那块港口名，在这一趟的站次里认出这是第几站。

    返回 (站次里那份港名, 画面读到的原始文字, 第几站的下标)；认不出直接 400，绝不猜。
    为什么只在点启动时读一次：用户 2026-09-29 拍的「当前港口不要手填，OCR 读一次即可」。
    读错只会让这一趟从错误的站起步（用户接受这个代价，它只在日志里暴露）；
    但**读不出**必须拦下来 —— 站次认不出来就意味着不知道要在哪个港花钱，让它继续走
    等于闭眼开船。返回站次里那份港名而不是 OCR 那串：画面那块 roi 会读到两行，
    拼出来的串常带着下面那行，拿它当本次港口去核对画面永远对不上。
    """
    text = read_port_text(screen_path)
    if not text:
        raise HTTPException(
            400, f"读不出港口名（『{PORT_REGION}』那块 OCR 出来是空的）—— 船大概不在港口画面上"
                 f"（还在海上、在交易所里、或者界面被弹窗盖住）。把船开进一个港口再点启动，"
                 f"或在「当前所在港」里手填一次。")
    idx, _hits = route_plan.find_stop_by_name(stops, text)
    if idx < 0:
        raise HTTPException(
            400, f"画面读到的是『{text}』，它不在这一趟的站次里（站次: "
                 f"{'、'.join(s['port'] for s in stops) or '（一趟一站都没排）'}）—— "
                 f"要么这趟没排到这个港，要么港口名 OCR 认错了字（去看一眼截图）；"
                 f"实在要按现在这一站起步，就在「当前所在港」里手填一次。")
    return stops[idx]["port"], text, idx


@app.post("/api/state/start")
def state_start(record: dict = None):
    """启动状态机后台线程。

    请求体只需要 {"module": "buy", "current_port": "北京"}：本次买哪几件货、开去哪个港、
    在哪个港出货，全由后端从 route_plan.json 现算（`route_plan.derive()`），界面传来的那四个
    港口名 / 货物清单一概不接 —— 这一串直接决定船往哪开、在哪个港花钱，只能有一个来源。
    `current_port` 留空 = 点启动时自己 OCR 读一次（见 read_current_port_by_ocr），读完回写
    route_plan.json，界面下一轮刷新看到的就是同一个港。

    module：**本次跑哪一个模块**（买货 buy / 港口间移动 sail / 卖货 sell / 完整一趟 trip）。
    跑商流程按模块切开后，「人站在港口码头上」这一张画面同时是几条链的起点，看画面分不出该走哪条，
    所以必须由界面在启动前指定，引擎不猜。留空时：states.json 里只有一个模块就自动用它，
    多于一个则拒绝启动。

    module="trip"（完整一趟：进货 →（可选）中转 → 卖货，一口气跑到底）时多交一份站次表
    （derived.trip_stops）：从「当前所在港」那一站起、按顺序走完后面每一站，每站做完由链尾的
    trip_next 自己接下一段（见 state_machine.StateMachine._start_trip）。
    这份站次在启动前会按 `买货 → 中转 → 卖货` 重排一次（同一条口径，见上面 is_trip 那段），
    人只要把有哪几站、每站买什么排出来就行，先后由后端兜。

    哪个港口名当真，由模块自己决定（见 state_machine.StateMachine.start）：
    碰购物表格的模块吃 buy_port + buy_goods（本次这一站没挂货、或者挂的货在目录里查不到，
    直接拒绝启动 —— 买货花真金币、不可逆），
    只把港口名打进搜索框的模块吃 sail_port（目的港，不要求它出现在购物表格里，
    但**不许等于 current_port**：船已经在的这个港再出一次航，是白花一次出港金币），
    卖货模块吃 sell_port —— 它只要一个港口名去核对画面（货舱里有什么卖什么，不吃货物清单）。
    """
    rec = record or {}
    module = (rec.get("module") or "").strip()
    try:
        route = route_plan.normalize_route(route_plan.load_route())
    except ValueError as e:
        raise HTTPException(400, f"「跑商设置」里这趟计划本身有问题，没法启动：{e}")
    stops = route.get("stops") or []
    is_trip = module == route_plan.TRIP_MODULE
    if is_trip:
        # 顺序拿**这次要跑的模块**重排一遍，然后用排好的那份定站次（2026-09-29 你选的 A）：
        # 存进 route_plan.json 的 run_module 未必就是界面这次点的那个（刚切成「完整一趟」
        # 还没存盘就点启动）。不重排的话引擎会照盘上那个顺序走（可能出货在前），
        # 跟你拍的「强制按 进货 → 中转 → 卖货」对不上。
        try:
            route = route_plan.normalize_route(dict(route, run_module=module))
        except ValueError as e:
            raise HTTPException(400, f"完整一趟没法启动：{e}")
        stops = route.get("stops") or []

    current_port = (rec.get("current_port") or "").strip() or (route.get("current_port") or "").strip()
    read_port = None
    if not current_port:
        if not stops:
            raise HTTPException(
                400, "「跑商设置」里一站都没排：既不知道从哪一站起步，也没法把 OCR 读到的港名"
                     "对到某一站上 —— 先去把进货（可选中转）和卖货排好")
        current_port, screen_text, at = read_current_port_by_ocr(stops)
        read_port = {"port": current_port, "screen_text": screen_text, "stop_index": at}
    # 「当前所在港」在这一趟里只有一个说法：上面定下来是哪个港，下面的四港名 / 本次清单 /
    # 整趟站次就全按它现算。界面这次填的和文件里存的不一致时（刚改完还没存盘），
    # 不留两个说法 —— 不然引擎会拿到「按 A 站起步、却带着按 B 站算出来的清单」。
    route["current_port"] = current_port
    if read_port:
        # 读到的港名回写进规划：界面下一轮刷新、以及下一次不填就启动，都拿得到同一个说法
        route_plan.save_route(route)
    # 四个港口名 + 本次清单 + 整趟站次都从这一份现算，界面传来的那些一概不接
    derived = route_plan.derive(route)
    trip_stops = (derived.get("trip_stops") or []) if is_trip else None
    if is_trip and not trip_stops:
        raise HTTPException(400, "整趟不知道要从哪一站走起："
                                 + (derived.get("trip_reason") or "站次是空的"))
    result = engine.start(
        buy_port=(derived.get("buy_port") or "").strip(),
        buy_goods=[str(g).strip() for g in (derived.get("buy_goods") or [])],
        sail_port=(derived.get("next_port") or "").strip(),
        sell_port=(derived.get("sell_port") or "").strip(),
        module=module,
        current_port=current_port,
        trip_stops=trip_stops,
    )
    if not result["ok"]:
        raise HTTPException(400, result["message"])
    if read_port:
        result["read_port"] = read_port
    return result


@app.post("/api/state/stop")
def state_stop():
    """手动停止状态机。"""
    result = engine.stop()
    if not result["ok"]:
        raise HTTPException(409, result["message"])
    return result


@app.get("/api/state/status")
def state_status():
    """返回运行状态 + 最近日志。"""
    return engine.status()


# 单步调试认的模块：只有这三样。整趟（trip）不在里面 —— 它要一份完整站次表，
# 是「跑商设置 + 运行队列」那条正路，不是调试栏能试的东西。
STEP_MODULES = ("buy", "sail", "sell")


@app.post("/api/debug/step")
def debug_step(record: dict):
    """调试栏的「单步调试」：选一个模块 + 一个港口，直接跑一次。

    **刻意不写 route_plan.json**：这是一次「拿一个港名试跑」的临时动作，不参与方案库、
    也不参与队列；落了盘会让「盘上这份规划是谁写的」说不清（那份现在只由队列线程写）。
    和 `/api/state/start` 刻意只吃两个字段的理由对照一下：那边是正式跑，这边是试。

    模块只认 买货 / 港口移动 / 卖货。`current_port` 留空 = 点运行时自己 OCR 读一次
    （和正式启动同一条路，见 read_port_text），不然买货链头拿什么去核对画面都说不清。
    """
    if queue.is_running():
        raise HTTPException(409, "队列正在跑，这时候起不了单步调试 —— 先停队列")
    if engine.is_running():
        raise HTTPException(409, "引擎正在跑，先停掉再起单步调试")
    rec = record or {}
    module = (rec.get("module") or "").strip()
    if module not in STEP_MODULES:
        raise HTTPException(
            400, f"单步调试的模块只能是 买货 / 港口移动 / 卖货（现在是 {module or '空'}）"
                 f"—— 整趟请去「运行」栏排队列跑")
    port = (rec.get("port") or "").strip()
    if not port:
        raise HTTPException(400, "没说要跑哪个港口")
    current_port = (rec.get("current_port") or "").strip()
    if not current_port:
        current_port = read_port_text()
        if not current_port:
            raise HTTPException(
                400, f"读不出港口名（『{PORT_REGION}』那块 OCR 出来是空的）—— 船大概不在港口画面上，"
                     f"或者手填一次「当前所在港」")
    goods = [str(g).strip() for g in (rec.get("goods") or []) if str(g).strip()]
    if module == "buy" and not goods:
        raise HTTPException(400, "买货要勾至少一件货 —— 买货花真金币、不可逆，空清单不许起")
    result = engine.start(
        buy_port=port if module == "buy" else "",
        buy_goods=goods if module == "buy" else [],
        sail_port=port if module == "sail" else "",
        sell_port=port if module == "sell" else "",
        module=module,
        current_port=current_port,
    )
    if not result["ok"]:
        raise HTTPException(400, result["message"])
    result["current_port"] = current_port
    return result


@app.get("/api/run_state")
def run_state_read():
    """引擎自己那本运行态账（run_state.json：sell_pending 开关 + 每个港的 restock_at 时刻）—— **只读**。

    为什么单独给一个接口、不塞进 /api/state/status：status 只在运行中每 2 秒轮一次，
    点「启动」之前那一刻的账要单独读一次才作数（拿几分钟前缓存的账去决定要不要跑，
    和拿几分钟前的规划去启动是同一类错误）。界面没有改它的入口 —— 改这账的是引擎的动作，
    人手工改只在出错时用，所以这里只出不进。
    """
    return engine.run_state_snapshot()


@app.get("/api/state/states")
def state_list_states():
    """返回完整状态机配置（config + states）。"""
    return state_machine.load_states()


@app.post("/api/state/test_condition")
def state_test_condition(payload: dict):
    """对某个状态实时评估一次 condition，返回 {matched, confidence, detail}。

    每次调用都会重新截图，所以「启动状态机」前可以用它确认条件写得对不对。
    """
    sid = (payload.get("state_id") or "").strip()
    if not sid:
        raise HTTPException(400, "state_id 不能为空")
    result = engine.test_condition(sid)
    if not result.get("ok"):
        raise HTTPException(400, result.get("message", "条件测试失败"))
    return result


@app.post("/api/state/states")
def state_create_state(record: dict):
    """新建一个状态（追加到 states 数组）。"""
    if engine.is_running():
        raise HTTPException(409, "状态机运行中，请先停止再修改配置")
    state = normalize_state(record)
    data = state_machine.load_states()
    if any(s["id"] == state["id"] for s in data["states"]):
        raise HTTPException(400, f"已存在相同 id: {state['id']}")
    data["states"].append(state)
    state_machine.save_states(data)
    return state


@app.put("/api/state/states")
def state_replace_all(record: dict):
    """整体替换状态机配置（config + states）。"""
    if engine.is_running():
        raise HTTPException(409, "状态机运行中，请先停止再修改配置")
    data = normalize_document(record)
    state_machine.save_states(data)
    return data


@app.put("/api/state/states/{state_id}")
def state_update_state(state_id: str, record: dict):
    """按 id 更新单个状态。"""
    if engine.is_running():
        raise HTTPException(409, "状态机运行中，请先停止再修改配置")
    data = state_machine.load_states()
    for i, s in enumerate(data["states"]):
        if s["id"] == state_id:
            new_state = normalize_state(record)
            if new_state["id"] != state_id and any(
                    x["id"] == new_state["id"] for x in data["states"]):
                raise HTTPException(400, f"已存在相同 id: {new_state['id']}")
            data["states"][i] = new_state
            state_machine.save_states(data)
            return new_state
    raise HTTPException(404, f"状态不存在: {state_id}")


@app.delete("/api/state/states/{state_id}")
def state_delete_state(state_id: str):
    """按 id 删除状态。"""
    if engine.is_running():
        raise HTTPException(409, "状态机运行中，请先停止再修改配置")
    data = state_machine.load_states()
    remaining = [s for s in data["states"] if s["id"] != state_id]
    if len(remaining) == len(data["states"]):
        raise HTTPException(404, f"状态不存在: {state_id}")
    data["states"] = remaining
    state_machine.save_states(data)
    return {"ok": True}


def draw_result(screen_path, template_rel, roi, result):
    """在截图上叠加画 ROI 框和匹配框，返回 base64 图。"""
    img = cv2.imread(screen_path)
    if img is None:
        raise HTTPException(500, f"读取截图失败: {screen_path}")
    if roi:
        x, y, w, h = roi
        cv2.rectangle(img, (x, y), (x + w, y + h), (255, 0, 0), 2)  # 蓝色 ROI
    if result:
        x, y, w, h = result["rect"]
        cv2.rectangle(img, (x, y), (x + w, y + h), (0, 0, 255), 2)  # 红色匹配框
        cv2.circle(img, (result["cx"], result["cy"]), 4, (0, 255, 0), -1)  # 绿色中心点
    ok, buf = cv2.imencode(".png", img)
    data = base64.b64encode(buf.tobytes()).decode()
    return f"data:image/png;base64,{data}"


class _QuitOnCloseWindowServer(uvicorn.Server):
    """不响应 Ctrl+C：唯一退出方式是关闭 cmd 窗口（系统级终止进程）。"""

    def handle_exit(self, sig, frame):
        # 忽略 Ctrl+C（SIGINT），保留 Ctrl+Break / SIGTERM 等其它信号
        if sig == signal.SIGINT:
            return
        super().handle_exit(sig, frame)

    def _log_started_message(self, listeners):
        # 去掉 uvicorn 默认的 "(Press CTRL+C to quit)" 提示
        config = self.config
        host = config.host or "0.0.0.0"
        protocol = "https" if config.ssl else "http"
        logging.getLogger("uvicorn.error").info(
            f"Uvicorn running on {protocol}://{host}:{config.port}")


def run_server():
    """启动 uvicorn 服务。关闭启动它的终端窗口即停止服务（已禁用 Ctrl+C 退出）。"""
    os.makedirs(TEMPLATES_DIR, exist_ok=True)
    config = uvicorn.Config(app, host="127.0.0.1", port=8000)
    server = _QuitOnCloseWindowServer(config)
    server.run()


if __name__ == "__main__":
    run_server()