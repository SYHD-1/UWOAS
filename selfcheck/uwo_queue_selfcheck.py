"""运行队列（run_queue）离线自检：真队列代码 + 假引擎 + 假截图，一次都不点 ADB、不花真金币。

为什么要单独一个脚本：队列这一层的逻辑全在 `QueueRunner._run_one` 里 —— 它管的是
「OCR 读到的港算什么、交给引擎那份站次长什么样、盘上那份怎么写」。它最坏的错不是报错，
是**铺错一站**：顺序错了船会先去买货、或者在没货的港开船，而每一次出港都花真金币。
真跑一遍要 adb、要游戏画面、要花钱，所以这里把引擎和截图两口都换成假的，
中间那段（读港 → 认站 → 拼站次 → 落盘）走的是一字不真的真代码。

覆盖：
 A 读到的港是进货港（含 OCR 那串带第二行）→ 从那一站走起，交引擎的站次里没有任何合成站
 B 读到的港**不是**进货港 → 不停队列、不弹窗，第 0 站是「开去第一个进货港」的中转站
   （2026-10-02 你拍的口径：「当前港口不在进货港口不应该停止而是应该前往第一个进货港口」）
 B2 完全认不出的港名（OCR 认错字）→ 照样先开去第一个进货港，读到的原样进日志
 C 读不出港名 → 停队列、喊人，引擎一次都没被调用、盘上那份一个字都没写
 D 方案里一个进货港都没排 → 停队列（没有「第一个进货港」可去），引擎没被调用
 E 落盘那份 route_plan.json → 合成站**不进去**（进去就会被自动重排到所有买货之后 = 起步就错），
   current_port 写的确实是读到的那个港
 F 交引擎的每一站形状对 + 真引擎代码认第 0 站：绑的是「开船去第一个进货港」、起点是移动链链头，
   第 1 站才是买货那一站
 G 文本契约：弹窗那条路整个删掉了（`_ask_ok_cancel` / 「等人工确认」不在源文件里）、
   `ctypes` 还留着（`_alert` 要用它）、界面那两句话说的是「先开去第一个进货港」不是「弹窗问」
"""
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 项目根 = selfcheck/ 的上一级
sys.path.insert(0, BASE)

import purchase_plan
import route_plan
import route_presets
import run_queue
from state_machine import StateMachineEngine

TMP = os.environ.get("TEMP", "/tmp")
SCR = os.path.join(TMP, "uwo_queue_unused.png")     # 本脚本一次截图都不做，只是构造函数要这个参数
ROUTE = os.path.join(TMP, "uwo_queue_route.json")
PRESETS = os.path.join(TMP, "uwo_queue_presets.json")
PLAN = os.path.join(TMP, "uwo_queue_purchase_plan.json")

FAILS = []


def check(label, cond, detail=""):
    print("  %-4s %s %s" % ("PASS" if cond else "FAIL", label, detail))
    if not cond:
        FAILS.append(label)


# ---- 个人数据一个都不碰：队列要读写的三份全部指到 TEMP 下 ----
route_plan.ROUTE_JSON = ROUTE
route_presets.PRESETS_JSON = PRESETS
purchase_plan.PLAN_JSON = PLAN

PRESET_TRIP = {
    "name": "自检·北京买画希洪中转热那亚卖",
    "run_module": "trip",
    "stops": [
        {"stage": "buy", "port": "北京", "goods": ["中国画"], "note": ""},
        {"stage": "transit", "port": "希洪", "goods": [], "note": "补水粮"},
        {"stage": "sell", "port": "热那亚", "goods": [], "note": ""},
    ],
    "options": {"switch_config": {
        "before_buy": {"enabled": True, "config_name": "买货配置"},
        "before_sail": {"enabled": True, "config_name": "航速配置"},
        "before_sell": {"enabled": True, "config_name": "卖货配置"},
    }},
}
PRESET_NO_BUY = {
    "name": "自检·一个进货港都没排",
    "run_module": "trip",
    "stops": [
        {"stage": "transit", "port": "希洪", "goods": [], "note": ""},
        {"stage": "sell", "port": "热那亚", "goods": [], "note": ""},
    ],
}


def write_presets(*items):
    with open(PRESETS, "w", encoding="utf-8") as f:
        json.dump({"presets": list(items)}, f, ensure_ascii=False, indent=2)


class FakeEngine:
    """只记账，不起线程 —— 真引擎会去点真鼠标、花真金币。"""

    def __init__(self):
        self.calls = []
        self.final = {"stop_kind": "done", "stop_reason": "整趟走完了"}

    def start(self, **kw):
        self.calls.append(kw)
        return {"ok": True, "message": "假引擎：已启动"}

    def is_running(self):
        return False

    def status(self):
        return dict(self.final)


class StubRunner(run_queue.QueueRunner):
    """把「截图 + OCR」和「弹窗」两口换成假的，其余全用真代码。

    `_alert` 必须换掉：真弹窗是阻塞式的，自检跑到那儿就永远回不来。
    """

    def __init__(self, engine, port_text):
        super().__init__(engine, "自检不用的 adb", 7555, SCR, "港口名字")
        self.port_text = port_text
        self.reads = 0
        self.alerts = []

    def _read_port_text(self):
        self.reads += 1
        return self.port_text

    def _alert(self, title, message):
        self.alerts.append((title, message))


def run_one(name, port_text):
    eng = FakeEngine()
    r = StubRunner(eng, port_text)
    ok = r._run_one(name)
    return eng, r, ok


def reset_route_file():
    if os.path.exists(ROUTE):
        os.remove(ROUTE)


def route_on_disk():
    with open(ROUTE, "r", encoding="utf-8") as f:
        return json.load(f)


write_presets(PRESET_TRIP, PRESET_NO_BUY)
with open(PLAN, "w", encoding="utf-8") as f:
    json.dump({"rows": [{"port": "北京", "goods_name": "中国画",
                         "cargo_type": "艺术作品", "note": ""}]}, f, ensure_ascii=False)

print("=" * 72)
print("1) 读到的港是进货港：从这一站走起，不拼任何合成站")
reset_route_file()
eng, r, ok = run_one(PRESET_TRIP["name"], "北京")
call = eng.calls[0] if eng.calls else {}
legs = call.get("trip_stops") or []
check("跑的是完整一趟（module=trip）", call.get("module") == route_plan.TRIP_MODULE, str(call.get("module")))
check("current_port 用规范港名『北京』", call.get("current_port") == "北京", str(call.get("current_port")))
check("站次三段就是方案那三站，没有多出来的一站", len(legs) == 3, str(len(legs)))
check("第 0 站是买货『北京』", legs and legs[0]["stage"] == "buy" and legs[0]["port"] == "北京",
      str(legs[:1]))
check("没有任何一站的备注写着「开去第一个进货港」",
      not any("开去第一个进货港" in (l.get("note") or "") for l in legs))
check("方案里那三个切换配置时点连同目标名一起交给引擎（界面上填的名字只有这条路能到引擎）",
      (call.get("trip_options") or {}).get("switch_config", {}).get("before_buy")
      == {"enabled": True, "config_name": "买货配置"}
      and call["trip_options"]["switch_config"]["before_sail"]["config_name"] == "航速配置"
      and call["trip_options"]["switch_config"]["before_sell"]["config_name"] == "卖货配置",
      str(call.get("trip_options"))[:130])
check("一次弹窗都没有（本来也没到那一步）", r.alerts == [], str(r.alerts))
check("OCR 只读一次", r.reads == 1, str(r.reads))
check("这一趟算正常走完 → 队列可以继续", ok is True)

print("2) OCR 那串带第二行：认得出就照样用规范港名")
eng, r, ok = run_one(PRESET_TRIP["name"], "北京市 距离刷新 12:34")
call = eng.calls[0] if eng.calls else {}
check("交给引擎的港名是『北京』（不是那一长串原文）", call.get("current_port") == "北京",
      str(call.get("current_port")))
check("站次仍是三站", len(call.get("trip_stops") or []) == 3)

print("3) 读到的港不是进货港：不停、不问，第 0 站先开去第一个进货港")
reset_route_file()
eng, r, ok = run_one(PRESET_TRIP["name"], "热那亚")
check("队列没停（_run_one 返回 True）", ok is True, r.stop_reason or "")
check("一次弹窗都没弹", r.alerts == [], str(r.alerts))
check("引擎被调用了一次", len(eng.calls) == 1, str(len(eng.calls)))
call = eng.calls[0]
legs = call.get("trip_stops") or []
check("站次多出来的就是那一站（3 → 4）", len(legs) == 4, str([l["stage"] for l in legs]))
check("第 0 站是中转、港名 = 当前读到的『热那亚』",
      legs and legs[0]["stage"] == "transit" and legs[0]["port"] == "热那亚", str(legs[:1]))
check("第 0 站的备注点明去哪儿",
      legs and "第一个进货港『北京』" in (legs[0].get("note") or ""), str(legs and legs[0].get("note")))
check("第 1 站起就是方案本身：买货 → 中转 → 卖货",
      [l["stage"] for l in legs[1:]] == ["buy", "transit", "sell"]
      and [l["port"] for l in legs[1:]] == ["北京", "希洪", "热那亚"],
      str([(l["stage"], l["port"]) for l in legs[1:]]))
check("current_port 交给引擎的是读到的那个港", call.get("current_port") == "热那亚",
      str(call.get("current_port")))
check("phase 停在「运行中」，不再有「等人工确认」那种等待态", r.phase == "运行中", r.phase)
check("日志里念了一句「不是进货港，先开去」",
      any("不是进货港" in l["message"] and "第一个进货港" in l["message"] for l in r._logs),
      str([l["message"] for l in r._logs][-2:]))

print("4) 盘上那份：合成站不写进去（写进去会被重排到所有买货之后）")
disk = route_on_disk()
check("route_plan.json 只有方案那三站", len(disk["stops"]) == 3, str(len(disk["stops"])))
check("落盘的第一站是买货『北京』（不是中转『热那亚』）",
      disk["stops"][0]["stage"] == "buy" and disk["stops"][0]["port"] == "北京",
      str(disk["stops"][0]))
check("落盘的 current_port 是读到的『热那亚』", disk["current_port"] == "热那亚",
      str(disk["current_port"]))
check("模块还是 trip", disk["run_module"] == route_plan.TRIP_MODULE, str(disk["run_module"]))
# 这一条钉住「为什么不能把合成站写进文件」：数据层 2026-09-29 你选的 A 会把它排到买货之后
trap = route_plan.normalize_route({
    "run_module": route_plan.TRIP_MODULE,
    "current_port": "热那亚",
    "stops": [{"stage": "transit", "port": "热那亚", "goods": [], "note": "从当前港开去进货港"}]
             + [dict(s, goods=list(s.get("goods") or [])) for s in PRESET_TRIP["stops"]],
})
check("反面：真写进去，第一站就变成了买货（那才是「买完货才开」）",
      trap["stops"][0]["stage"] == "buy", str(trap["stops"][0]))
check("反面：那一站被排到了所有买货之后",
      [s["stage"] for s in trap["stops"]] == ["buy", "transit", "transit", "sell"],
      str([s["stage"] for s in trap["stops"]]))

print("5) 港名认错字（方案里对不上任何一个站）：照样先开去第一个进货港")
eng, r, ok = run_one(PRESET_TRIP["name"], "汊堡")
legs = (eng.calls[0].get("trip_stops") if eng.calls else []) or []
check("引擎被调了、队列没停", len(eng.calls) == 1 and ok is True)
check("第 0 站的出发港用读到的原文『汊堡』（它只进日志，不进搜索框）",
      legs and legs[0]["port"] == "汊堡", str(legs[:1]))
check("第 1 站仍是第一个进货港『北京』",
      len(legs) > 1 and legs[1]["port"] == "北京", str(legs[1:2]))
check("没弹窗", r.alerts == [])

print("6) 读不出港名：停队列喊人，引擎一次都没被调用、盘上没写")
reset_route_file()
eng, r, ok = run_one(PRESET_TRIP["name"], "")
check("队列停了（返回 False）", ok is False)
check("引擎一次都没被调用（一次金币都没花）", eng.calls == [], str(len(eng.calls)))
check("喊人一次，标题是「队列已停止」", len(r.alerts) == 1 and r.alerts[0][0] == "UWO 队列已停止",
      str(r.alerts))
check("原因说清是读不出港口名", "读不出港口名" in (r.stop_reason or ""), r.stop_reason or "")
check("盘上那份 route_plan.json 一个字都没写", not os.path.exists(ROUTE))

print("7) 方案里一个进货港都没排：没有「第一个进货港」可去，停队列")
eng, r, ok = run_one(PRESET_NO_BUY["name"], "希洪")
check("队列停了", ok is False)
check("引擎没被调用", eng.calls == [], str(len(eng.calls)))
check("原因点明是「没有进货港」", "进货港" in (r.stop_reason or ""), r.stop_reason or "")

print("8) 真引擎代码读那份站次：第 0 站 = 开船去第一个进货港")
real = StateMachineEngine("自检不用的 adb", 7555, SCR)
entries = {"transit": "state-sail-head", "buy": "state-buy-head", "sell": "state-sell-head"}
b0, e0 = real._leg_binding(legs, entries, 0)
check("第 0 站绑到移动链链头", b0 and b0["current_state"] == "state-sail-head", str(b0 or e0))
check("第 0 站本次港口 = 下一站『北京』（要开去的那个进货港）",
      b0 and b0["run_port"] == "北京" and b0["run_port_field"] == "sail_port", str(b0 or e0))
# 第 1 站要吃购物表格，用那份 TEMP 表格，不读用户真数据
buy_legs = [{"stage": "buy", "port": "北京", "goods": ["中国画"], "note": ""}]
b1, e1 = real._leg_binding(buy_legs, entries, 0, plan=purchase_plan.load_plan())
check("第 1 站（拼上第 0 站之前的方案首站）是买货，绑的是『北京』+ 1 件货",
      b1 and b1["current_state"] == "state-buy-head" and b1["run_port"] == "北京"
      and len(b1["run_rows"]) == 1, str(b1 or e1))
check("每一站都是引擎要的形状 {idx,stage,port,goods,note}",
      all(set(l) == {"idx", "stage", "port", "goods", "note"} for l in legs),
      str(sorted(legs[0]) if legs else ""))

print("9) 文本契约：弹窗那条路删干净了， ctypes 还给 _alert 留着")
src = open(os.path.join(BASE, "run_queue.py"), encoding="utf-8").read()
check("run_queue.py 里没有 _ask_ok_cancel 了", "_ask_ok_cancel" not in src)
check("没有「等人工确认」这个 phase 了", "等人工确认" not in src)
check("没有「人工取消」这个停止原因了", "人工取消" not in src)
check("MessageBoxW 只剩 _alert 那一处（只有确定，不再问人）", src.count("MessageBoxW") == 1,
      str(src.count("MessageBoxW")))
check("import ctypes 还在（_alert 要用）", "import ctypes" in src)
check("文件头写的是你 2026-10-02 那条口径", "不应该停止而是应该前往第一个进货港口" in src)
html = open(os.path.join(BASE, "frontend", "index.html"), encoding="utf-8").read()
js = open(os.path.join(BASE, "frontend", "app.js"), encoding="utf-8").read()
check("界面「运行」栏：说的是不停、不问，先开去第一个进货港", "不停、也不问" in html)
check("界面「跑商设置」栏：同一口径（不在进货港里就先开去）",
      "不在进货港里就<b>先开去第一个进货港</b>" in html)
check("两份前端文件里不再有「弹窗问」这种说法",
      "弹窗问一句" not in html and "会弹窗问你" not in js)
check("点启动前那句确认还在念「会先开去第一个进货港」", "会先开去第一个进货港" in js)

print("=" * 72)
if FAILS:
    print(f"RESULT: {len(FAILS)} FAIL -> {FAILS}")
    sys.exit(1)
print("RESULT: ALL PASS (QUEUE_OK)")
