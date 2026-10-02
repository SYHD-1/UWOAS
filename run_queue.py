"""运行队列：把跑商方案排成一队，由后端线程按顺序一趟一趟跑。

数据文件 run_queue.json 与 route_plan.json 同级：

    {"items": ["北京买油画 → 热那亚卖", "汉堡买啤酒 → 里斯本卖"], "mode": "once", "repeat": 1}

2026-09-30 结构重构后的三份文件分工：
- **route_presets.json** = 方案库，界面「跑商设置」栏编辑，每个方案都是一整套完整流程
  （买货 → 可选中转 → 出货，`run_module` 恒为 trip）；
- **run_queue.json（本文件）** = 这一队按什么顺序跑哪几个方案、跑几遍；
- **route_plan.json** = **由队列线程在每趟启动前铺写**（方案站次 + OCR 读到的当前港），
  人不再手填那一份 —— 盘上那份是谁写的，看这里就清楚。

为什么队列线程在后端（2026-09-30 你拍板）：关掉浏览器也要继续跑；前端 JS 编排一关页面就断。

循环模式 MODES：
- `"once"`     队列里每个方案各跑一遍，跑完就结束；
- `"repeat"`   整队重复 `repeat` 遍（`repeat` ≥ 1）；
- `"infinite"` 一直循环，直到手动停止、或某一趟没正常走完。

**队列接不接下一个，只看引擎的 `stop_kind == "done"`**（机器可读的停止类别，见
state_machine.StateMachineEngine.__init__ 的注释），不看 `stop_reason` 中文串：
改一个字判断就错，而且错得无声无息。特别注意 `stop_action`（states.json 里的 stop 动作）
**不算正常走完** —— 它在现有链里正是卖货出错那条路（没看到结算结果窗）。

「当前所在港」本文件不再让人填：每趟启动前 OCR 读一次。读到的港在方案的进货港里 → 就从那一站
走起；**不在 → 不停队列、也不问人**，直接把「从现在这个港开去第一个进货港」当第 0 站接着跑
（2026-10-02 你指出「当前港口不在进货港口不应该停止而是应该前往第一个进货港口」）；
连港名都读不出 → 停队列报错（船不在港口画面上，连「从哪儿开走」都写不出来）。

**前置移动靠「多给引擎一站」实现，不新增动作、不改状态链**：现有引擎里
`transit 站 = 从这儿开去下一站`（见 state_machine 的 `_depart_binding`），所以把「当前港」当成
第 0 个 transit 站，它自然就变成「先航行到第一个进货港」，到港后由移动链尾的
`trip_next after=arrive` 接力进下一站。

**这一站只交给引擎、不写进 route_plan.json**：盘上那份在 `run_module=trip` 保存时会自动把
中转排到所有买货**之后**（2026-09-29 你拍的 A），「先开去进货港」就会被排成「买完货才开」，
起步那一站直接错。所以 route_plan.json 里只有方案本身那几站，`current_port` 写读到的那个港 ——
界面看到「这个港不在站次里」是对的，那一次开船由引擎收到的第 0 站负责。
"""

import ctypes
import json
import os
import threading
import time

import route_plan
import route_presets
from mumu_controller import MuMuController, capture
from vision import ocr_text_by_region

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
QUEUE_JSON = os.path.join(BASE_DIR, "run_queue.json")

MODES = ("once", "repeat", "infinite")

DEFAULTS = {"items": [], "mode": "once", "repeat": 1}


def _repeat_count(value):
    """重复遍数收成一个 ≥1 的整数。前端 input 传回来可能是数字串，两种都认。"""
    if isinstance(value, bool):
        raise ValueError("重复遍数必须是一个整数，不能是 true/false")
    if isinstance(value, int):
        n = value
    elif isinstance(value, str) and value.strip().lstrip("+").isdigit():
        n = int(value.strip())
    else:
        raise ValueError(f"重复遍数必须是一个整数，现在是 {value!r}")
    if n < 1:
        raise ValueError(f"重复遍数至少是 1，现在是 {n} —— 一遍都不跑没有意义")
    return n


def normalize_queue(record):
    """规整一份队列配置：{items, mode, repeat}。不认识的键直接报错（照 route_plan 的口径）。

    `items` 里**允许同一个方案出现两次** —— 队列本来就是序列，「A 跑完再跑一次 A」是正当需求，
    在方案库里才会拦重名。
    """
    if not isinstance(record, dict):
        raise ValueError("队列必须是一个对象 {items, mode, repeat}")
    unknown = [k for k in record if k not in ("items", "mode", "repeat")]
    if unknown:
        raise ValueError("队列里有界面不认识的项目: " + "、".join(sorted(unknown)))

    raw_items = record.get("items")
    if raw_items is None:
        raw_items = []
    if not isinstance(raw_items, list):
        raise ValueError(f"队列的『items』必须是一个列表，现在是 {type(raw_items).__name__}")
    items = []
    for it in raw_items:
        if not isinstance(it, str):
            raise ValueError(f"队列里的方案名只能是文字，现在是 {type(it).__name__}")
        name = it.strip()
        if not name:
            raise ValueError("队列里有一项是空的 —— 去掉它，别留空项")
        items.append(name)

    mode = record.get("mode")
    if mode is None:
        mode = "once"
    if not isinstance(mode, str):
        raise ValueError(f"队列循环模式只能是文字，现在是 {type(mode).__name__}")
    mode = mode.strip() or "once"
    if mode not in MODES:
        raise ValueError(f"队列循环模式只能是 {'、'.join(MODES)}，现在是 {mode!r}")

    repeat = _repeat_count(record.get("repeat", 1)) if mode == "repeat" else 1
    return {"items": items, "mode": mode, "repeat": repeat}


def load_queue():
    """读 run_queue.json；不存在返回默认空队列（第一次用还没排过）。内容不认就抛，不返回空骗人。"""
    if not os.path.exists(QUEUE_JSON):
        return dict(DEFAULTS)
    with open(QUEUE_JSON, "r", encoding="utf-8") as f:
        data = json.load(f)
    return normalize_queue(data)


def save_queue(record):
    """规整后写入 run_queue.json，返回真正落盘的那份。"""
    q = normalize_queue(record)
    with open(QUEUE_JSON, "w", encoding="utf-8") as f:
        json.dump(q, f, ensure_ascii=False, indent=2)
    return q


def _legs(stops):
    """站次 → 引擎要的 `trip_stops` 形状（每站 {idx, stage, port, goods, note}）。

    只补下标、不做任何判断：「从第几站起走」仍然只有 `route_plan.derive()` 一处算
    （见那个文件的口径「别在 JS 里再算一遍」）。这里要用它是因为「先开去进货港」那一站
    是本次现场拼出来的、根本不在盘上那份站次里，`derive()` 自然切片不出它。
    """
    return [{"idx": i, "stage": s["stage"], "port": s["port"],
             "goods": list(s.get("goods") or []), "note": s.get("note") or ""}
            for i, s in enumerate(stops)]


class QueueRunner:
    """队列线程：按顺序把方案铺进 route_plan.json → 起引擎跑 trip → 等它停 → 接下一个。

    `engine` 由 app.py 注入（同一个单例，不新建）—— 队列和调试台 / 手动启动共用一台引擎，
    这样「引擎正在跑」这个判断才是同一个事实。
    `screen_path` 用**独立文件**（queue_screen.png），沿用「截图文件按用途分开」的既有口径：
    共用一个文件时 `capture()` 会先删旧图，另一个线程正读到半截就会拿到空文件。
    """

    def __init__(self, engine, adb_path, port, screen_path, port_region="港口名字"):
        self.engine = engine
        self.adb_path = adb_path
        self.port = port
        self.screen_path = screen_path
        self.port_region = port_region

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = None
        self._logs = []

        self.running = False
        self.items = []
        self.mode = "once"
        self.repeat = 1
        self.total = 0
        self.pass_no = 0
        self.index = 0
        self.current = ""
        self.phase = ""
        self.stop_reason = None

    # ---------- 日志 ----------
    def _log(self, event, message):
        """格式和引擎日志一致（{time, event, message}），前端同一个渲染函数就能吃。"""
        with self._lock:
            self._logs.append({
                "time": time.strftime("%H:%M:%S"),
                "event": event,
                "message": message,
            })
            if len(self._logs) > 100:
                self._logs = self._logs[-100:]

    def _set_phase(self, phase):
        with self._lock:
            self.phase = phase

    # ---------- 对外控制 ----------
    def start(self, items=None, mode=None, repeat=None):
        """启动队列线程。三个参数传了就先用它规整一份，不传读文件里那份。

        返回 {"ok": bool, "message": str}。起来之前把「能不能跑」全查一遍（队列非空、
        方案都在库里、引擎没在跑、队列没在跑），查完再起线程 —— 不让线程起来之后
        才发现跑不了，那时人已经以为它在跑了。
        """
        if self.is_running():
            return {"ok": False, "message": "队列已经在跑了"}
        if self.engine.is_running():
            return {"ok": False,
                    "message": "引擎正在跑（可能是单步调试，或上一次还没停干净）—— 先停掉再启动队列"}
        try:
            base = load_queue()
            q = normalize_queue({
                "items": base["items"] if items is None else items,
                "mode": base["mode"] if mode is None else mode,
                "repeat": base["repeat"] if repeat is None else repeat,
            })
        except ValueError as e:
            return {"ok": False, "message": f"队列配置有问题: {e}"}
        if not q["items"]:
            return {"ok": False, "message": "队列是空的 —— 先在「运行」栏把方案加进队列"}
        try:
            presets = route_presets.load_presets()
        except ValueError as e:
            return {"ok": False, "message": f"读方案库失败: {e}"}
        known = {p["name"] for p in presets}
        missing = [n for n in q["items"] if n not in known]
        if missing:
            return {"ok": False,
                    "message": "队列里有方案不在方案库里了： " + "、".join(missing)
                               + " —— 去「跑商设置」看一眼，或者把它从队列里去掉"}
        try:
            q = save_queue(q)   # 前端可能没点保存就点了启动，这里兜一次
        except (OSError, ValueError) as e:
            return {"ok": False, "message": f"保存队列失败: {e}"}

        with self._lock:
            self.items = list(q["items"])
            self.mode = q["mode"]
            self.repeat = q["repeat"]
            self.total = len(self.items)
            self.pass_no = 0
            self.index = 0
            self.current = ""
            self.phase = ""
            self.stop_reason = None
            self._logs = []
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="uwo-queue", daemon=True)
        with self._lock:
            self.running = True
        self._log("start", f"队列启动：{self.total} 个方案，模式 {q['mode']}"
                           + (f"，重复 {q['repeat']} 遍" if q["mode"] == "repeat" else ""))
        self._thread.start()
        return {"ok": True, "message": "队列已启动"}

    def stop(self):
        """停止队列：置停止标志，引擎若在跑就一起停（队列线程正等着它收尾）。

        返回 {"ok": bool, "message": str}。
        """
        if not self.is_running():
            return {"ok": False, "message": "队列没在跑"}
        self._stop_event.set()
        if self.engine.is_running():
            self.engine.stop()
        self._log("stop", "手动停止队列")
        return {"ok": True, "message": "已停止队列"}

    def is_running(self):
        with self._lock:
            return self.running

    def status(self):
        with self._lock:
            return {
                "running": self.running,
                "mode": self.mode,
                "repeat": self.repeat,
                "pass_no": self.pass_no,
                "index": self.index,
                "total": self.total,
                "current": self.current,
                "phase": self.phase,
                "stop_reason": self.stop_reason,
                "items": list(self.items),
                "logs": list(self._logs),
            }

    # ---------- 主循环 ----------
    def _run(self):
        try:
            self._run_loop()
        except Exception as e:
            self._finish(f"队列异常: {type(e).__name__}: {e}")

    def _run_loop(self):
        with self._lock:
            items = list(self.items)
            mode = self.mode
            total_passes = self.repeat
        pass_no = 0
        while True:
            pass_no += 1
            for index, name in enumerate(items):
                if self._stop_event.is_set():
                    self._finish("人工停止")
                    return
                with self._lock:
                    self.pass_no = pass_no
                    self.index = index + 1
                    self.current = name
                if not self._run_one(name):
                    return     # _run_one 里已经写明停止原因
            if mode == "infinite":
                self._log("plan", "一轮跑完，接着从头再来一轮")
                continue
            if pass_no >= total_passes:
                break
        self._finish("队列已跑完")

    def _run_one(self, name):
        """跑队列里的一个方案（完整一趟）。返回 True = 正常走完可以接下一个，False = 队列已停。"""
        try:
            presets = route_presets.load_presets()
        except ValueError as e:
            self._fail(f"读方案库失败: {e}")
            return False
        preset = route_presets.find(presets, name)
        if not preset:
            self._fail(f"方案『{name}』不在方案库里了")
            return False

        stops = [dict(s, goods=list(s.get("goods") or [])) for s in preset.get("stops") or []]
        buy_ports = [s["port"] for s in stops if s.get("stage") == "buy"]

        # ① 读当前港：只在每趟启动前读一次（用户 2026-09-30 拍的口径，不再手填）
        self._set_phase("读港口")
        try:
            text = self._read_port_text()
        except RuntimeError as e:
            self._fail(f"读港口名失败: {e}")
            return False
        if not text:
            self._fail("读不出港口名 —— 船大概不在港口画面上（还在海上 / 在交易所里 / 界面被弹窗盖住）")
            return False

        idx, _hits = route_plan.find_stop_by_name(stops, text)
        hit_port = stops[idx]["port"] if idx >= 0 else ""
        if hit_port and hit_port in buy_ports:
            # 画面读到的港就是这一趟的某个进货港 → 就从那一站走起
            current_port = hit_port
            pre_move = False
            self._log("plan", f"『{name}』：画面读到『{text}』，是方案里的进货港，从这一站走起")
        else:
            if not buy_ports:
                self._fail(f"方案『{name}』里一个进货港都没排，「去第一个进货港」没有去处")
                return False
            # ② 不在进货港里：不停下来问人，直接先开去第一个进货港（2026-10-02 你拍的口径）
            # 读到的港是方案里已有的一站（中转 / 出货）就用那个规范港名，OCR 那串原文常常带第二行。
            current_port = hit_port or text
            pre_move = True
            self._log("plan", f"『{name}』：画面读到『{text}』不是进货港，"
                              f"先从『{current_port}』开去第一个进货港『{buy_ports[0]}』")

        # ③ 铺进 route_plan.json（这一趟的规划由本线程写，人不再手填）
        self._set_phase("启动")
        try:
            route = route_plan.normalize_route({
                "run_module": route_plan.TRIP_MODULE,
                "current_port": current_port,
                "stops": stops,
            })
        except ValueError as e:
            self._fail(f"方案『{name}』铺不出这一趟: {e}")
            return False
        route_plan.save_route(route)
        derived = route_plan.derive(route)
        if pre_move:
            # 拼上第 0 站「从现在这个港开去第一个进货港」，只交给引擎、不进盘上那份
            # （写进去会被自动重排到所有买货之后，见本文件头部）。
            trip_stops = _legs([{"stage": "transit", "port": current_port, "goods": [],
                                 "note": f"从当前港开去第一个进货港『{buy_ports[0]}』"}]
                               + route["stops"])
        else:
            trip_stops = derived.get("trip_stops") or []
        if not trip_stops:
            self._fail(f"方案『{name}』不知道从哪一站走起: "
                       + (derived.get("trip_reason") or "站次是空的"))
            return False

        # ④ 起引擎跑 trip
        result = self.engine.start(
            buy_port=(derived.get("buy_port") or "").strip(),
            buy_goods=[str(g).strip() for g in (derived.get("buy_goods") or [])],
            sail_port=(derived.get("next_port") or "").strip(),
            sell_port=(derived.get("sell_port") or "").strip(),
            module=route_plan.TRIP_MODULE,
            current_port=current_port,
            trip_stops=trip_stops,
            trip_options=preset.get("options"),
        )
        if not result.get("ok"):
            self._fail(f"方案『{name}』启动失败: {result.get('message')}")
            return False
        self._set_phase("运行中")
        self._log("plan", f"『{name}』已启动（当前港 {current_port}）")

        # ⑤ 等这一趟结束（引擎自己会跑到 running=False）
        while self.engine.is_running():
            time.sleep(1)
        if self._stop_event.is_set():
            self._finish("人工停止")
            return False

        st = self.engine.status()
        kind = st.get("stop_kind")
        reason = st.get("stop_reason") or ""
        if kind != "done":
            # kind == "alert" 时引擎自己已经弹过窗了（见 _loop 的 finally），别弹第二次；
            # 其余（error / stop_action / manual）引擎不弹，这里补一条，不然人只看到队列悄悄停了。
            self._fail(f"方案『{name}』没有正常走完（{kind or '未知'}）：{reason}",
                       alert=(kind != "alert"))
            return False
        self._log("plan", f"『{name}』跑完：{reason}")
        return True

    # ---------- 内部工具 ----------
    def _read_port_text(self):
        """截一张图 + OCR 港口名那块，返回画面读到的原始文字（空串 = 读不出）。

        `capture` 走 mumu_controller 那把共用截图锁，和引擎 / 调试台不会同时抢 ADB；
        OCR 用独立文件 queue_screen.png。读不出不抛，交给调用方决定停队列并说明原因。
        """
        ctl = MuMuController(port=self.port, adb_path=self.adb_path)
        ok, info = capture(ctl, self.screen_path, "队列读港口")
        if not ok:
            raise RuntimeError(info)
        try:
            return (ocr_text_by_region(self.screen_path, self.port_region) or "").strip()
        except FileNotFoundError as e:
            raise RuntimeError(f"读取截图失败: {e}（{self.screen_path}）")

    def _fail(self, reason, alert=True):
        """停队列并记原因；`alert` 为真时再弹一条置顶提示（默认弹，引擎自己弹过的那种除外）。"""
        self._finish(reason)
        if alert:
            self._alert("UWO 队列已停止", reason)
        return False

    def _finish(self, reason):
        with self._lock:
            self.running = False
            self.phase = ""
            self.stop_reason = reason
        self._log("stop", f"队列结束：{reason}")

    def _alert(self, title, message):
        """Windows 阻塞式置顶提示（只有确定）。弹不出去只记一条 warn，不影响停止本身生效。"""
        self._log("error", f"[需要人工] {title}：{message}")
        try:
            MB_OK = 0x0
            MB_ICONERROR = 0x10
            MB_TOPMOST = 0x40000
            ctypes.windll.user32.MessageBoxW(0, str(message), str(title),
                                             MB_OK | MB_ICONERROR | MB_TOPMOST)
        except Exception as e:
            self._log("warn", f"弹窗发不出去（{type(e).__name__}: {e}），只留日志")