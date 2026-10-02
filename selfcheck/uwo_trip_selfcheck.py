"""完整一趟（module="trip"）离线自检：真引擎代码 + 真 states.json + 假控制器（一次都不真点 ADB）。

为什么要单独一个脚本：整趟的代码全在引擎里（_start_trip / _leg_binding / _do_trip_next），
一条 assert 都碰不到画面 —— 它管的是「这一站该把 run_port 绑成哪个港、下一步进哪条链的链头」。
这类逻辑最坏的错误不是点歪，是**绑错港**：船开去另一个港、在另一个港花钱。

覆盖：
 A 正常一趟（买货 → 中转 → 卖货）：start 装进了什么 —— 裁剪的状态、第 1 站的绑定、进度快照、日志
 B 该拒绝启动的组合：没站次 / 站次形状不对 / 某站没填港 / 相邻两站同港 / 第 3 站买不了（逐站预检）/
   链没配进 config.trip.modules / 没有状态认领 trip_entry /
   起点是出货站却没账 / 空着货 / 已在运行
 C 按段推进：用 states.json 里那三份真 trip_next 动作，把整趟一步一步走完
   （work 退到码头 → arrive 认中转站 → arrive 认出货站 → work 收尾）
 D 边角：单模块跑法里 trip_next 就是原来那条 stop（文字一字不差）、
   到账却卖不成、after 写错、走到一半表格被改、状态复位不漏 trip、中转站排在最后一站
 G 点启动那一下（/api/state/start）：界面传来的四港名 / 清单不进引擎、港名留空才 OCR 一次
   （读不出、认错字、一站没排、老格式文件 = 全部拒绝且一次都不调引擎）、
   先后排错的整趟会在交出去前自动排好（2026-09-29 你选的 A，不再拒绝启动）、
   读到的港名回写规划文件、单模块那份 trip_stops=None
 H 前端那份（app.js / index.html 的文本）：2026-09-30 结构重构后的口径 ——
   跑商设置只编方案（没有模块下拉、没有当前所在港）、运行栏排队列（启动只交 items/mode/repeat）、
   调试栏单步（模块下拉不含 trip、交 module/port/current_port/goods）、
   进度念引擎给的 s.trip、和后端必须一字不差的常量（TRIP_MODULE / 段名表 / STEP_MODULES）
 H2 表格「货物名称」的匹配搜索（2026-09-30 改成三级界面 + 纯文字下拉）：
   一级只列港口名（可搜、按拼音排）、点一个港进二级看这个港有哪些货、再点一件货才进三级编辑；
   候选不显示图片。
   打字永远不写货名、只有点中候选才写（写货名的就 pickGoods / 清空两处）、
   matchGoods 那份函数抠出来交给 node 真跑一遍用例（不连着的不算命中，这条是刻意口径）

刻意**不**测的：_loop 的轮次上限（config.trip.max_rounds）要跑真循环才验得到，
这里只验 _trip_conf 读得对；真循环的推进要在实机上验（那要用户逐步授权）。
"""
import inspect  # noqa: E402  (G 节要核接口交给引擎的形参名，名字打错是当场 TypeError)
import json  # noqa: E402  (G 节要把读到的港名回写这件事从盘上验，不看返回值)
import os
import re  # noqa: E402  (H 节拿正则读前端那份 js)
import shutil  # noqa: E402  (H2 节要找 node 真跑一遍匹配函数)
import subprocess  # noqa: E402
import sys
import types

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 项目根 = selfcheck/ 的上一级（换目录/换机器都不用改）
sys.path.insert(0, BASE)

import purchase_plan as pp_mod  # noqa: E402
import route_plan  # noqa: E402
import run_state as rs_mod  # noqa: E402
import state_machine  # noqa: E402
from state_machine import StateMachineEngine  # noqa: E402

TMP = os.environ.get("TEMP", "/tmp")   # 换机器也不用改（和 route_format 那份同款写法）
SCR = os.path.join(TMP, "uwo_trip_unused.png")   # 本脚本一次截图都不做，只是构造函数要这个参数
FLAGFILE = os.path.join(TMP, "uwo_trip_selfcheck_run_state.json")

FAILS = []


def check(label, cond, detail=""):
    print("  %-4s %s %s" % ("PASS" if cond else "FAIL", label, detail))
    if not cond:
        FAILS.append(label)


class FakeCtl:
    def __init__(self):
        self.clicks = []

    def click(self, x, y):
        self.clicks.append((x, y))


DATA = state_machine.load_states()
BUY_IDS = {s["id"] for s in DATA["states"] if s.get("module") == "buy"}
SAIL_IDS = {s["id"] for s in DATA["states"] if s.get("module") == "sail"}
SELL_IDS = {s["id"] for s in DATA["states"] if s.get("module") == "sell"}
SWITCH_IDS = {s["id"] for s in DATA["states"] if s.get("module") == "switch_config"}

# 购物表格：本脚本用假目录，绝不读项目里那份真的（人会改表格，改了就把自检带崩）
PLAN_ROWS = [
    {"port": "汉堡", "goods_name": "啤酒", "cargo_type": "货物-啤酒"},
    {"port": "汉堡", "goods_name": "铜版画", "cargo_type": "货物-铜版画"},
    {"port": "汉堡", "goods_name": "油画", "cargo_type": "货物-油画"},
    {"port": "上海", "goods_name": "瓷器", "cargo_type": "货物-啤酒"},
]
fake_plan = types.SimpleNamespace(
    load_plan=lambda: {"rows": list(PLAN_ROWS)},
    rows_for_port=lambda plan, port: [x for x in plan["rows"] if x["port"] == port],
    row_key=lambda row: row["goods_name"],
    decorate=pp_mod.decorate,     # status() 要给界面念一行，这是纯函数，用真的那份
    CARGO_TYPES={"货物-啤酒", "货物-铜版画", "货物-油画"},
)
real_plan = state_machine.purchase_plan
state_machine.purchase_plan = fake_plan

# 典型一趟：进货 → 中转补水平 → 出货（站次形状照 route_plan.derive 交出来的那份，带 idx）
STOPS = [
    {"idx": 0, "stage": "buy", "port": "汉堡", "goods": ["铜版画", "啤酒"], "note": ""},
    {"idx": 1, "stage": "transit", "port": "南安普顿", "goods": [], "note": ""},
    {"idx": 2, "stage": "sell", "port": "北京", "goods": [], "note": ""},
]
SWITCH_OPTIONS = {"switch_config": {
    "before_buy": {"enabled": True, "config_name": "买货配置"},
    "before_sail": {"enabled": True, "config_name": "航速配置"},
    "before_sell": {"enabled": True, "config_name": "卖货配置"},
}}


def one_stop(i):
    return [dict(STOPS[i])]


def with_flag(value):
    """把「待卖账」写到临时文件里 —— 走真的读盘路径，不伪造 get_flag。"""
    rs_mod.RUN_STATE_JSON = FLAGFILE
    if os.path.exists(FLAGFILE):
        os.remove(FLAGFILE)
    rs_mod.set_flag("sell_pending", value)


def fresh():
    """能跑 start() 的引擎，但后台循环换成空函数：只看 start() 往引擎里装了什么。"""
    e = StateMachineEngine("fake-adb", 16384, SCR)
    e._loop = lambda: None
    e._exit_calls = []
    e._exit_to_port = lambda ctl: e._exit_calls.append(1)   # 退回码头要真点画面，本脚本不碰
    return e


def started(stops=None, flag=True, current_port="汉堡", options=None):
    with_flag(flag)
    e = fresh()
    r = e.start(module="trip", trip_stops=[dict(s) for s in (stops or STOPS)],
                current_port=current_port, trip_options=options)
    return e, r


def trip_next_actions():
    """从 states.json 取那三份真的收尾动作（不在这儿重写一份，两处说法迟早对不上）。

    要拿 _walk 展开：买货那份长在「本港还有货要买吗」这条分支的 else 里，不在动作顶层。
    """
    def tail(sid, after):
        st = next(s for s in DATA["states"] if s["id"] == sid)
        acts = [a for a in StateMachineEngine._walk(st["actions"])
                if isinstance(a, dict) and a.get("type") == "trip_next"]
        got = [a for a in acts if (a.get("after") or "") == after]
        if not got:
            raise AssertionError(f"states.json 里 {sid} 没有 after={after} 的 trip_next")
        return got[0]
    return tail("buy_at_exchange", "work"), tail("voyage", "arrive"), tail("sell_add_all", "work")


def log_text(e):
    return " | ".join(x["message"] for x in e._logs)


print("=" * 72)
print("A) 一趟 3 站：start 装进来的样子")
e, r = started()
check("自检用的是临时账本，没碰项目里的 run_state.json",
      rs_mod.RUN_STATE_JSON == FLAGFILE, rs_mod.RUN_STATE_JSON)
check("启动成功", r["ok"], r["message"])
check("module 交出去的是 trip", e.run_module == route_plan.TRIP_MODULE, str(e.run_module))
got = {s["id"] for s in e.states}
check("完整一趟一次装进买货 %d + 移动 %d + 卖货 %d + 切换配置 %d 个状态"
      % (len(BUY_IDS), len(SAIL_IDS), len(SELL_IDS), len(SWITCH_IDS)),
      got == BUY_IDS | SAIL_IDS | SELL_IDS | SWITCH_IDS, str(sorted(got)))
trip_state_ids = {s["id"] for s in DATA["states"]
                  if s.get("global") or s.get("module") in {"buy", "sail", "sell", "switch_config"}}
check("整趟四条链一个状态都没被裁掉，切换配置完整三步也在其中",
      got == trip_state_ids
      and SWITCH_IDS == {"switch_config_open_menu", "switch_config_open_assign", "switch_config_choose"},
      str(sorted(got)))
check("第 1 站是买货：本次港口绑的是它", e.run_port == "汉堡", str(e.run_port))
check("来源字段说清是 buy_port（日志/界面据此分「买货港/目的港/出货港」）",
      e.run_port_field == "buy_port", str(e.run_port_field))
check("本次清单 = 这一站勾的货、按勾选顺序",
      [x["goods_name"] for x in e.run_rows] == ["铜版画", "啤酒"],
      str([x["goods_name"] for x in e.run_rows]))
check("起点落在买货链头", e.current_state == "in_port", str(e.current_state))
check("游标从第 0 行起", e.run_row_index == 0, str(e.run_row_index))
check("运行态里有整趟：3 站、现在第 1 站",
      e.trip and e.trip["pos"] == 0 and len(e.trip["stops"]) == 3, str(e.trip and e.trip["pos"]))
check("每一类站的入口由状态自己用 trip_entry 认领（不在 Python 里写死表）",
      e.trip["entries"] == {"buy": "in_port", "transit": "sail_from_port", "sell": "sell_in_port"},
      str(e.trip["entries"]))
st = e.status()
check("status() 里界面能念到整趟进度",
      st.get("trip", {}).get("total") == 3 and st["trip"]["pos"] == 0 and st["trip"]["stage"] == "buy",
      str(st.get("trip")))
check("进度里的后面几站也排出来了",
      [x["text"] for x in st["trip"]["legs"]] == ["买货汉堡", "中转南安普顿", "出货北京"],
      str([x["text"] for x in st["trip"]["legs"]]))
logs = log_text(e)
check("启动日志把整趟念成一行",
      "整趟 3 站：买货汉堡 → 中转南安普顿 → 出货北京" in logs, logs[:200])
check("日志写明从第 1 站走起、后面由 trip_next 接力", "从第 1 站" in logs and "trip_next" in logs)
check("中转港 / 出货港都不要求出现在购物表格里（只有买货站查目录）", "走不完" not in r["message"])
e.stop()

print("=" * 72)
print("B) 该拒绝启动的组合，一个都不能放过去")
e = fresh()
r = e.start(module="trip", current_port="汉堡")
check("没交站次 -> 拒绝（引擎不自己编一站）", not r["ok"] and "没收到站次" in r["message"], r["message"])
check("拒绝时没有把引擎装成运行中", e.running is False and e.states == [] and e.trip is None)

e = fresh()
r = e.start(module="trip", trip_stops=[])
check("站次是空表 -> 拒绝", not r["ok"] and "没收到站次" in r["message"], r["message"])

e = fresh()
r = e.start(module="trip", trip_stops=[{"stage": "buy", "port": "汉堡", "goods": ["啤酒"]}, "北京"])
check("站次里混进不是对象的一条 -> 拒绝并说清是几条",
      not r["ok"] and "1 条不是一站该有的样子" in r["message"], r["message"])

e = fresh()
r = e.start(module="trip", trip_stops=[{"stage": "buy", "port": "  ", "goods": ["啤酒"]}])
check("某站港口名只有一串空格 -> 拒绝（空格不算填了港）",
      not r["ok"] and "没填港口名" in r["message"], r["message"])

e = fresh()
r = e.start(module="trip", trip_stops=[{"stage": "buy", "port": "汉堡", "goods": ["啤酒"]},
                                       {"stage": "sell", "port": "汉堡", "goods": []}],
            current_port="汉堡")
check("相邻两站同港 -> 拒绝（出港花真金币却回到原地）",
      not r["ok"] and "连着两站同港" in r["message"], r["message"])

e = fresh()
r = e.start(module="trip", trip_stops=[STOPS[0], STOPS[1],
                                       {"idx": 2, "stage": "buy", "port": "东京", "goods": ["瓷器"]}],
            current_port="汉堡")
check("第 3 站买不了也要在启动时就拦住（逐站预检，不走到才说）",
      not r["ok"] and "第 3 站" in r["message"] and "东京" in r["message"], r["message"])

e = fresh()
r = e.start(module="trip", trip_stops=[{"stage": "buy", "port": "汉堡", "goods": []}])
check("买货站一件货都没勾 -> 拒绝，文字和单模块那条一模一样",
      not r["ok"] and "没勾任何货物" in r["message"], r["message"])

e, r = started(stops=[{"idx": 0, "stage": "sell", "port": "北京", "goods": [], "note": ""}],
               flag=False, current_port="北京")
check("起点就是出货站、待卖账是 0 -> 拒绝（别白花那张船票）",
      not r["ok"] and "待卖账是 0" in r["message"], r["message"])

e, r = started(stops=[STOPS[2]], flag=True)
check("同样这一站、账上是 1 -> 放过去", r["ok"], r["message"])
check("起点落在卖货链头", e.current_state == "sell_in_port", str(e.current_state))
check("出货站吃 sell_port、不吃购物表格",
      e.run_port_field == "sell_port" and e.run_rows == [], f"{e.run_port_field} / {e.run_rows}")
e.stop()

real_load = state_machine.load_states
try:
    def mk(sid, module):
        return {"id": sid, "name": sid, "module": module, "entry": True,
                "condition": {"type": "template", "name": "UI-海上", "threshold": 0.8},
                "actions": [{"type": "stop", "reason": "测试"}], "next": None}

    state_machine.load_states = lambda: {
        "config": dict(DATA["config"], trip={"modules": ["buy", "sail"], "max_rounds": 400}),
        "states": DATA["states"]}
    e, r = started()
    check("配置里只列买货 + 移动：卖货那几条被裁掉，整趟接不进出货站 -> 拒绝",
          not r["ok"] and "trip_entry: sell" in r["message"], r["message"])

    state_machine.load_states = lambda: {
        "config": dict(DATA["config"], trip={"modules": ["buy", "sail"], "max_rounds": 400}),
        "states": DATA["states"]}
    e, r = started(stops=[{"idx": 0, "stage": "buy", "port": "汉堡",
                           "goods": ["啤酒"], "note": ""}])
    check("同一份配置，这趟只排买货一站就走得起来（拦的是站次要用的那类站）",
          r["ok"] and e.trip["entries"] == {"buy": "in_port", "transit": "sail_from_port"},
          r["message"] + " " + str(e.trip.get("entries")))
    e.stop()

    state_machine.load_states = lambda: {
        "config": DATA["config"],
        "states": [mk("p", "buy"), mk("q", "sail")]}
    e, r = started(stops=[STOPS[0], STOPS[1]])
    check("配置要接的链在 states.json 里一条状态都没有 -> 拒绝并点名是哪条",
          not r["ok"] and "没有状态" in r["message"] and "sell" in r["message"], r["message"])

    state_machine.load_states = lambda: {
        "config": dict(DATA["config"], trip={"modules": [], "max_rounds": 400}),
        "states": DATA["states"]}
    e, r = started()
    check("config.trip.modules 是空的 -> 拒绝并指出去哪配",
          not r["ok"] and "config.trip.modules" in r["message"], r["message"])

    state_machine.load_states = lambda: {
        # 这组假 states 只测三条业务链的 trip_entry；同步缩小 modules，避免真实配置里新增的
        # switch_config 先触发「缺模块」而遮住本断言真正要测的错误。
        "config": dict(DATA["config"], trip={"modules": ["buy", "sail", "sell"], "max_rounds": 400}),
        "states": [mk("p", "buy"), mk("q", "sail"), mk("z", "sell")]}
    e, r = started()
    check("状态里没人用 trip_entry 认领自己是谁的链头 -> 拒绝（引擎不猜）",
          not r["ok"] and "trip_entry" in r["message"], r["message"])
finally:
    state_machine.load_states = real_load

e, r = started()
again = e.start(module="trip", trip_stops=[one_stop(2)[0]], current_port="北京")
check("已经在跑就不要再起一趟（会把上一趟的绑定冲掉）",
      not again["ok"] and "已在运行" in again["message"], again["message"])
check("拒绝第二次启动时，第一趟的站次和绑定一点没动",
      e.trip["pos"] == 0 and e.run_port == "汉堡" and len(e.trip["stops"]) == 3,
      f"{e.trip['pos']} / {e.run_port} / {len(e.trip['stops'])}")
e.stop()

print("=" * 72)
print("C) 按段推进：拿 states.json 里那三份真 trip_next 动作把整趟走完")
W_ACT, A_ACT, S_ACT = trip_next_actions()
check("买货链收尾是 work（正事做完，要开去下一站）",
      W_ACT.get("after") == "work", str(W_ACT.get("after")))
check("移动链收尾是 arrive（船刚靠港，认一认这是第几站）",
      A_ACT.get("after") == "arrive", str(A_ACT.get("after")))

ctl = FakeCtl()
e, r = started()
msg = e._do_action(W_ACT, ctl, SCR)
check("第 1 步 买货做完 -> 先退回码头（在交易所里按 1 是没用的）",
      e._exit_calls == [1], msg)
check("退出去点的是画面，不是停止：没置停止位", e._stop_event.is_set() is False)
check("绑定改成「开去下一站」：本次港口 = 第 2 站（中转港）",
      e.run_port == "南安普顿" and e.run_port_field == "sail_port", f"{e.run_port}/{e.run_port_field}")
check("清单清空（移动链不吃购物表格）", e.run_rows == [], str(e.run_rows))
check("起点 = 移动链链头（人和船都在码头上按 1）",
      e.current_state == "sail_from_port" and e._pending_goto == "sail_from_port",
      str(e.current_state))
check("站次计数还停在第 1 站（work 不加，靠港才加）", e.trip["pos"] == 0, str(e.trip["pos"]))
e._pending_goto = None

msg = e._do_action(A_ACT, ctl, SCR)
check("第 2 步 靠上中转港 -> 认成第 2 站", e.trip["pos"] == 1, str(e.trip["pos"]) + " " + msg)
check("中转站只做「再出一次港」：不进买货链、不进交易所",
      e.current_state == "sail_from_port", str(e.current_state))
check("这一段的本次港口 = 第 3 站（要打进地图搜索框的目的港）",
      e.run_port == "北京" and e.run_port_field == "sail_port", f"{e.run_port}/{e.run_port_field}")
check("日志念得出「这一站是中转」", "中转" in log_text(e)[-120:], log_text(e)[-120:])
e._pending_goto = None

msg = e._do_action(A_ACT, ctl, SCR)
check("第 3 步 靠上出货港 -> 认成第 3 站", e.trip["pos"] == 2, str(e.trip["pos"]) + " " + msg)
check("起点 = 卖货链头", e.current_state == "sell_in_port" and e._pending_goto == "sell_in_port",
      str(e.current_state))
check("出货港吃 sell_port", e.run_port_field == "sell_port" and e.run_port == "北京",
      f"{e.run_port}/{e.run_port_field}")
check("全程只由配置决定去哪，没多点任何一下", ctl.clicks == [], str(ctl.clicks))
e._pending_goto = None

msg = e._do_action(S_ACT, ctl, SCR)
check("第 4 步 卖完、后面没站了 -> 整趟收尾停止", e._stop_event.is_set() and e.running is False, msg)
check("收尾文字用的是配置里那句 done_reason",
      e.stop_reason == S_ACT.get("done_reason"), str(e.stop_reason))
check("收尾这一步不再退回码头（已经没下一趟船要开了）", e._exit_calls == [1], str(e._exit_calls))
check("收尾不挂弹窗（这是好消息，不是出事了）", e._alert_request is None)
check("退回码头整趟只调用了一次（每换一段点一次）", e._exit_calls == [1], str(e._exit_calls))

print("=" * 72)
print("C2) 三时点切换配置挂钩：买货前 / 出港前 / 卖货前，完成后回原链且不重复")
e, r = started(options=SWITCH_OPTIONS)
check("启用三时点后仍能启动，第一站先进入切换配置链",
      r["ok"] and e.current_state == state_machine.SWITCH_CONFIG_ENTRY,
      str((r, e.current_state)))
check("before_buy 带着目标配置名，并记住要回买货链头",
      e.trip["switch_request"] == {"key": "0:before_buy", "point": "before_buy",
                                    "config_name": "买货配置", "resume_state": "in_port"},
      str(e.trip["switch_request"]))
msg = e._do_switch_config_resume()
check("before_buy 完成后回原买货链，并登记完成键",
      msg == "goto in_port" and e._pending_goto == "in_port"
      and e.trip["switch_completed"] == ["0:before_buy"]
      and e.trip["switch_request"] is None, str((msg, e.trip)))
e._pending_goto = None
err = e._bind_leg(0)
check("同一站同一时点重新绑定不会重复切换",
      err is None and e.current_state == "in_port" and e.trip["switch_request"] is None
      and e.trip["switch_completed"] == ["0:before_buy"], str(e.trip))

msg = e._do_action(W_ACT, ctl, SCR)
check("买货做完准备开船时触发 before_sail，原链记为移动链头",
      e.current_state == state_machine.SWITCH_CONFIG_ENTRY
      and e.trip["switch_request"] == {"key": "0:before_sail", "point": "before_sail",
                                       "config_name": "航速配置", "resume_state": "sail_from_port"},
      str((msg, e.trip["switch_request"])))
msg = e._do_switch_config_resume()
check("before_sail 完成后回移动链头",
      msg == "goto sail_from_port" and e._pending_goto == "sail_from_port"
      and "0:before_sail" in e.trip["switch_completed"], str((msg, e.trip["switch_completed"])))
e._pending_goto = None

# 到达中转港后还要从该港继续开船；这是新的一段 before_sail，按站号 1 单独记账。
e._do_action(A_ACT, ctl, SCR)
check("中转港再次出港是另一个 before_sail（同一时点可在不同港各执行一次）",
      e.trip["pos"] == 1 and e.trip["switch_request"]["key"] == "1:before_sail"
      and e.trip["switch_request"]["resume_state"] == "sail_from_port",
      str(e.trip["switch_request"]))
e._do_switch_config_resume()
e._pending_goto = None
e._do_action(A_ACT, ctl, SCR)
check("到达出货港触发 before_sell，并记住回卖货链头",
      e.trip["pos"] == 2 and e.current_state == state_machine.SWITCH_CONFIG_ENTRY
      and e.trip["switch_request"] == {"key": "2:before_sell", "point": "before_sell",
                                       "config_name": "卖货配置", "resume_state": "sell_in_port"},
      str(e.trip["switch_request"]))
msg = e._do_switch_config_resume()
check("before_sell 完成后回卖货链头；三个时点的完成键都保留",
      msg == "goto sell_in_port" and e._pending_goto == "sell_in_port"
      and e.trip["switch_completed"]
          == ["0:before_buy", "0:before_sail", "1:before_sail", "2:before_sell"],
      str((msg, e.trip["switch_completed"])))
e.stop()

print("=" * 72)
print("D) 边角：单模块那一份行为、到账卖不成、写错的 after、表格中途被改")
e = fresh()
r = e.start(module="sail", sail_port="北京", current_port="汉堡")
check("单模块（移动）启动成功", r["ok"], r["message"])
check("单模块没有整趟运行态", e.trip is None)
msg = e._do_action(A_ACT, ctl, SCR)
check("没在跑整趟时 trip_next 就是原来那条 stop（连返回文字都照旧）",
      e.stop_reason == A_ACT["reason"] and msg == "主动停止（" + A_ACT["reason"] + "）", msg)
check("单模块那份停止不挂弹窗（和以前一样只在日志里收尾）", e._alert_request is None)
check("单模块那份不退回码头", e._exit_calls == [], str(e._exit_calls))

e, r = started(stops=[STOPS[0], STOPS[1]])
check("把中转站排在最后一站 -> 拒绝：它的全部意义就是「到了再开去下一站」，后面没站就别排",
      not r["ok"] and "后面没有站了" in r["message"], r["message"])
check("这一条在启动时就拦，不等船开出去了才发现", e.running is False and e.trip is None)

e, r = started(stops=[STOPS[0], STOPS[2]], flag=True)
check("买货 + 出货 两站能启动", r["ok"], r["message"])
with_flag(False)      # 人到现场清过账 / 前面那一站其实没买成
msg = e._do_action(A_ACT, ctl, SCR)
check("到了出货港却发现待卖账是 0 -> 停住，不白进一次交易所",
      e._stop_event.is_set() and "待卖账是 0" in (e.stop_reason or ""), msg)
check("这一种必须喊人（弹窗标题）",
      (e._alert_request or ["", ""])[0] == "UWO 已停止：到了出货港却没货可卖",
      str((e._alert_request or [None])[0]))
check("弹窗正文里说清去哪个文件看哪个字段", "sell_pending" in (e._alert_request or ["", ""])[1],
      str((e._alert_request or ["", ""])[1])[:80])
check("没跳到卖货链头（停在原地等人）", e._pending_goto is None and e.trip["pos"] == 0,
      f"{e._pending_goto}/{e.trip['pos']}")

try:
    e._do_action({"type": "trip_next", "after": "wok"}, ctl, SCR)
    check("after 写错要当场响，不能闷着走下去", False)
except ValueError as err:
    check("after 写错要当场响，不能闷着走下去", True, str(err))

e, r = started(stops=[STOPS[0], {"idx": 1, "stage": "buy", "port": "上海", "goods": ["瓷器"]}])
check("两站都要买货、目录里都查得到 -> 启动成功", r["ok"], r["message"])
PLAN_ROWS[:] = [x for x in PLAN_ROWS if x["port"] != "上海"]   # 人在中途改了购物表格
msg = e._do_action(A_ACT, ctl, SCR)
check("走到下一站才发现表格对不上 -> 停住 + 喊人",
      e._stop_event.is_set() and "上海" in (e.stop_reason or ""), msg)
check("喊人的标题说清是换站卡住",
      (e._alert_request or ["", ""])[0] == "UWO 已停止：换下一站时卡住了",
      str((e._alert_request or [None])[0]))
check("不带着空清单硬开下一站（跳转标记是空的）", e._pending_goto is None, str(e._pending_goto))
PLAN_ROWS[:] = [{"port": "汉堡", "goods_name": "啤酒", "cargo_type": "货物-啤酒"},
                {"port": "汉堡", "goods_name": "铜版画", "cargo_type": "货物-铜版画"},
                {"port": "汉堡", "goods_name": "油画", "cargo_type": "货物-油画"},
                {"port": "上海", "goods_name": "瓷器", "cargo_type": "货物-啤酒"}]

print("=" * 72)
print("E) 换回单模块跑法：上一次整趟的东西必须清干净")
e = fresh()
r = e.start(module="trip", trip_stops=[dict(s) for s in STOPS], current_port="汉堡")
check("先跑一趟整趟", r["ok"], r["message"])
e.stop()
r = e.start(module="buy", buy_port="汉堡", buy_goods=["油画"], current_port="汉堡")
check("同一个引擎接着按单模块启动", r["ok"], r["message"])
check("整趟运行态复位（不清的话 trip_next 会去找早该丢掉的站次）", e.trip is None, str(e.trip))
check("status() 里的进度也一起没了", e.status().get("trip") is None)
check("清单换成了单模块那一份", [x["goods_name"] for x in e.run_rows] == ["油画"], str(e.run_rows))
check("起点回到买货链头", e.current_state == "in_port", str(e.current_state))
msg = e._do_action(W_ACT, ctl, SCR)
check("这时候链尾就是单模块那条 stop", e.stop_reason == W_ACT["reason"], msg)
e.stop()
r = e.start(module="trip", trip_stops=[dict(s) for s in STOPS], current_port="汉堡")
check("再切回整趟也起得来（复位是对称的）", r["ok"] and e.trip is not None, r["message"])
e.stop()

print("=" * 72)
print("F) 轮次上限是从配置读的（整趟 400 轮，单模块 60 轮）")
mods, cap = StateMachineEngine._trip_conf(DATA["config"])
check("config.trip.modules 读出来是三条业务链 + 完整切换配置链",
      mods == ["buy", "sail", "sell", "switch_config"], str(mods))
check("config.trip.max_rounds 读出来是 400（单模块 60 轮跑不完一趟）",
      cap == 400, str(cap))
check("没配 trip 时也有兜底（不让整趟一跑到第 60 轮自己判死）",
      StateMachineEngine._trip_conf({})[1] == 400, str(StateMachineEngine._trip_conf({})))
check("单模块那份轮次上限没被顺手改掉",
      int(DATA["config"].get("max_rounds", 0)) == 60, str(DATA["config"].get("max_rounds")))
check("不认识的站别写句人话，而不是崩",
      StateMachineEngine._trip_leg_text({"stage": "dock", "port": "伦敦"}) == "dock伦敦",
      StateMachineEngine._trip_leg_text({"stage": "dock", "port": "伦敦"}))
check("站次里没填港的那一行，日志里显示成「(没填港)」而不是空白",
      "(没填港)" in StateMachineEngine._trip_leg_text({"stage": "buy"}),
      StateMachineEngine._trip_leg_text({"stage": "buy"}))

print("=" * 72)
print("G) 点启动那一下（/api/state/start）：界面那份不算数，港名留空才自己读一次")
# 上面几节验的是「引擎拿到站次之后怎么走」，这一节验它前面那一半：本次在哪个港下单、
# 买哪几件、整趟从第几站走起，全在这个接口里从 route_plan.json 现算。界面（app.js）传来的
# buy_port / buy_goods 只是它当时看到的样子 —— 接过来当真就等于「谁改了下拉框就能换掉
# 花金币的港」。所以这里钉三件事：引擎收到的每个字段都来自规划文件、规划文件读不进就 400、
# current_port 留空才 OCR 一次（读不出/认不出 = 拒绝，一次引擎都不调）。
import app as appmod  # noqa: E402

TMP_ROUTE = os.path.join(TMP, "uwo_trip_selfcheck_route_plan.json")
REAL_ROUTE = route_plan.ROUTE_JSON
real_grab = appmod.grab_screen
real_ocr_region = appmod.ocr_text_by_region
real_engine_obj = appmod.engine

TRIP_ROUTE = {"run_module": "trip", "current_port": "",
              "stops": [{"stage": "buy", "port": "汉堡", "goods": ["铜版画", "啤酒"]},
                        {"stage": "transit", "port": "南安普顿"},
                        {"stage": "sell", "port": "北京"}]}

CAP = []
OCR = {"text": "汉堡", "calls": 0}


def write_route(rec):
    with open(TMP_ROUTE, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False)


def call(rec):
    """真调接口；只换掉三样 —— 规划文件、ADB 截图、OCR 那一步，其余走真代码。"""
    CAP.clear()
    OCR["calls"] = 0
    return appmod.state_start(rec)


def call_err(rec):
    try:
        r = call(rec)
    except appmod.HTTPException as e:
        return e.status_code, str(e.detail)
    return None, f"没拒绝，正常返回了 {r}"


route_plan.ROUTE_JSON = TMP_ROUTE
appmod.grab_screen = lambda p: None      # 一次都不碰 ADB
appmod.ocr_text_by_region = lambda path, region, **kw: (
    OCR.__setitem__("calls", OCR["calls"] + 1), OCR["text"])[1]
appmod.engine = types.SimpleNamespace(start=lambda **kw: (CAP.append(kw),
                                                          {"ok": True, "message": "桩"})[1])
try:
    write_route(TRIP_ROUTE)
    r = call({"module": "trip", "current_port": "汉堡",
              "buy_port": "东京", "buy_goods": ["违禁货"],
              "sail_dest_port": "开罗", "sell_port": "孟买"})
    kw = CAP[0]
    check("本次港 / 本次清单 / 下一港 / 出货港全从规划现算（不是界面那四个）",
          kw["buy_port"] == "汉堡" and kw["buy_goods"] == ["铜版画", "啤酒"]
          and kw["sail_port"] == "南安普顿" and kw["sell_port"] == "", str(kw))
    check("界面那份「东京 / 开罗 / 孟买 / 违禁货」在交给引擎的东西里一个字都找不到",
          not any(s in json.dumps(kw, ensure_ascii=False)
                  for s in ["东京", "开罗", "孟买", "违禁货"]), json.dumps(kw, ensure_ascii=False)[:120])
    check("整趟交给引擎的是 3 站、从第 1 站汉堡走起",
          len(kw["trip_stops"] or []) == 3 and kw["trip_stops"][0]["port"] == "汉堡",
          str([(s["stage"], s["port"]) for s in kw["trip_stops"] or []]))
    check("手填了「当前所在港」就不 OCR（不浪费一次截图）", OCR["calls"] == 0, str(OCR["calls"]))
    check("没读就不谎报：返回里没有 read_port", "read_port" not in r, str(r))
    # 这一条看着没必要，其实最值钱：桩函数收 **kwargs，名字打错了这里照样「通过」，
    # 换成真引擎就是点启动当场 TypeError —— 而接口会把那个异常原样抛给界面。
    check("接口交出去的那几个名字都是 start() 的真形参（打错一个字母 = 启动当场崩溃）",
          set(CAP[0]) <= set(inspect.signature(StateMachineEngine.start).parameters) - {"self"},
          str(sorted(set(CAP[0]) - (set(inspect.signature(StateMachineEngine.start).parameters) - {"self"}))))

    write_route(TRIP_ROUTE)
    OCR["text"] = "汉堡_丽"    # 那块 roi 真会读到两行、拼成一串（第 18 节学到的那课）
    r = call({"module": "trip"})
    check("港名留空 -> 自己读一次（一次截图、一次 OCR）", OCR["calls"] == 1, str(OCR["calls"]))
    check("读到「汉堡_丽」认的是站次里那份『汉堡』（交出去的不是 OCR 那串，否则核对画面永远对不上）",
          CAP[0]["current_port"] == "汉堡" and r["read_port"]["port"] == "汉堡"
          and r["read_port"]["screen_text"] == "汉堡_丽", str((CAP[0]["current_port"], r.get("read_port"))))
    check("read_port 里带「这是第几站」，日志和界面都说得清起步点", r["read_port"]["stop_index"] == 0,
          str(r.get("read_port")))
    on_disk = json.load(open(TMP_ROUTE, encoding="utf-8")).get("current_port")
    check("读到的港名回写进规划文件（界面下一轮刷新看到的是同一个说法）", on_disk == "汉堡", repr(on_disk))

    write_route(TRIP_ROUTE)
    OCR["text"] = "汊堡"       # 汉堡被读成「汊堡」= 第 18 节没加 otsu 时的真实读法
    code, msg = call_err({"module": "trip"})
    check("OCR 认错字 -> 拒绝启动，不猜一站起步", code == 400 and "汊堡" in msg, msg[:90])
    check("认不出站次时引擎一次都没被调到（金币一分没花）", not CAP, str(CAP))
    check("拒绝理由里带站次清单（人要能自己看出差在哪一站）",
          "不在这一趟的站次里" in msg and "北京" in msg, msg[:90])

    write_route(TRIP_ROUTE)
    OCR["text"] = ""
    code, msg = call_err({"module": "trip"})
    check("读不出港口名（船不在港口画面上）-> 拒绝", code == 400 and "读不出港口名" in msg, msg[:90])
    check("理由里给了下一步（开进港口，或者手填一次）", "手填" in msg, msg[:90])

    write_route({"run_module": "trip", "current_port": "", "stops": []})
    code, msg = call_err({"module": "trip"})
    check("整趟一站都没排 -> 在规划校验那一关就拒，连图都不截",
          code == 400 and "一站都没排" in msg and OCR["calls"] == 0, f"{msg[:60]} calls={OCR['calls']}")

    write_route({"run_module": "buy", "current_port": "", "stops": []})
    code, msg = call_err({"module": "buy"})
    check("单模块没站次也不让 OCR 瞎认：站次是空的，读到的港名无处可归",
          code == 400 and OCR["calls"] == 0 and "一站都没排" in msg, f"{msg[:60]} calls={OCR['calls']}")

    write_route({"run_module": "buy", "current_port": "汉堡",
                 "stops": [{"stage": "sell", "port": "北京"},
                           {"stage": "buy", "port": "汉堡", "goods": ["啤酒"]}]})
    r = call({"module": "trip"})
    legs = CAP[0]["trip_stops"] or []
    check("文件里存的是单模块、这次点的是整趟：先后**按整趟排好再交出去**"
          "（2026-09-29 你选的 A —— 原来这一条是「排法不行就 400」，现在不拒了）",
          [(s["stage"], s["port"]) for s in legs] == [("buy", "汉堡"), ("sell", "北京")],
          str([(s["stage"], s["port"]) for s in legs]))
    check("   排完第一站就是船现在这个港（在汉堡下单买货），出货站挪到最后",
          legs[0]["idx"] == 0 and CAP[0]["current_port"] == "汉堡", str(legs)[:70])
    check("   本次清单跟着自己那一站挪（重排只换先后，不会把货挪到别的港去）",
          CAP[0]["buy_port"] == "汉堡" and CAP[0]["buy_goods"] == ["啤酒"],
          str((CAP[0]["buy_port"], CAP[0]["buy_goods"])))
    check("   点启动不改盘上那份：文件里还是单模块时的先后（只有 OCR 读到港名才回写）",
          [s["stage"] for s in json.load(open(TMP_ROUTE, encoding="utf-8"))["stops"]]
          == ["sell", "buy"], str(json.load(open(TMP_ROUTE, encoding="utf-8"))["stops"])[:60])

    write_route({"buy_port": "汉堡", "stops": []})    # 改格式之前那份老文件
    code, msg = call_err({"module": "trip"})
    check("规划文件本身读不进（老格式）-> 说清是计划有问题，不是界面坏了",
          code == 400 and "计划本身有问题" in msg, msg[:90])

    write_route(dict(TRIP_ROUTE, current_port="北京"))
    r = call({"module": "trip", "current_port": "汉堡"})
    check("界面这次填的港名优先于文件里那份（文件只是上一次的存档）",
          CAP[0]["current_port"] == "汉堡" and OCR["calls"] == 0,
          str((CAP[0]["current_port"], OCR["calls"], r.get("read_port"))))
    check("清单也按这个港现算：文件里那份『北京』不能拖着本次清单跑（港名一个说法、清单另一个说法）",
          CAP[0]["buy_port"] == "汉堡" and CAP[0]["sell_port"] == ""
          and CAP[0]["trip_stops"][0]["port"] == "汉堡",
          str((CAP[0]["buy_port"], CAP[0]["sell_port"], CAP[0]["trip_stops"][0]["port"])))

    write_route(dict(TRIP_ROUTE, current_port=""))
    OCR["text"] = "北京"
    call({"module": "sell"})
    check("单模块跑法共用这一份 OCR，但不交整趟站次（trip_stops=None）",
          CAP[0]["module"] == "sell" and CAP[0]["trip_stops"] is None
          and CAP[0]["current_port"] == "北京", str(CAP[0])[:120])
    check("单模块的本次出货港也是从「当前所在港 + 出货站」算出来的",
          CAP[0]["sell_port"] == "北京", str(CAP[0]["sell_port"]))

    appmod.engine.start = lambda **kw: (CAP.append(kw),
                                        {"ok": False, "message": "引擎说：这一站的货在目录里查不到"})[1]
    write_route(TRIP_ROUTE)
    code, msg = call_err({"module": "trip", "current_port": "汉堡"})
    check("引擎自己拒绝时转成 400、原因原样念给用户", code == 400 and "目录里查不到" in msg, msg[:90])
finally:
    route_plan.ROUTE_JSON = REAL_ROUTE
    appmod.grab_screen = real_grab
    appmod.ocr_text_by_region = real_ocr_region
    appmod.engine = real_engine_obj
    if os.path.exists(TMP_ROUTE):
        os.remove(TMP_ROUTE)
check("自检跑完，项目里那份真的 route_plan.json 路径已经换回来（没被临时文件顶掉）",
      route_plan.ROUTE_JSON == REAL_ROUTE and not os.path.exists(TMP_ROUTE), route_plan.ROUTE_JSON)

print("=" * 72)
print("H) 前端那份：整趟这一档在界面上选得到、启动只交两件、进度念引擎的")
# 为什么盯着 js 文本验：这一栏是原生 JS、没有构建步骤，改名、漏登记都悄无声息 ——
# 上次「states.json 用了 trip_next 但动作下拉里没有」就是这套自检抓出来的（T-054 同类）。
# 这里只盯「和后端必须一字不差的那几样」，不盯样式和措辞。
js = open(os.path.join(BASE, "frontend", "app.js"), encoding="utf-8").read()
html = open(os.path.join(BASE, "frontend", "index.html"), encoding="utf-8").read()


def js_fn(name):
    m = re.search(r"function %s\([^)]*\)\s*\{(.*?)\n\}" % re.escape(name), js, re.S)
    return m.group(1) if m else ""


m_trip = re.search(r'const TRIP_MODULE = "([^"]+)"', js)
check("前端有 TRIP_MODULE 常量，值和后端 route_plan.TRIP_MODULE 一字不差",
      m_trip is not None and m_trip.group(1) == route_plan.TRIP_MODULE,
      m_trip.group(1) if m_trip else "没找到常量")
check("中文标签登记了「完整一趟」（不登记界面就念成 trip）",
      re.search(r'MODULE_LABELS = \{[^}]*trip:\s*"完整一趟"', js) is not None)
m_stages = re.search(r"const TRIP_STAGES = \[(.*?)\];", js, re.S)
pairs = sorted(re.findall(r'\["(\w+)",\s*"([^"]+)"\]', m_stages.group(1) if m_stages else ""))
check("前端的段名表和后端 STAGE_LABELS 一致（买货 / 中转 / 出货）",
      pairs == sorted(route_plan.STAGE_LABELS.items()), str(pairs))
check("跑商设置不再有「本次模块」下拉和「当前所在港」（方案一律 trip、当前港由队列 OCR 读）",
      "route-module" not in html and "route-current-port" not in html
      and "route-warnings" not in html,
      str([k for k in ["route-module", "route-current-port", "route-warnings"] if k in html]))
check("跑商设置那一栏写明「只编方案、真正跑哪几个去运行栏排队列」",
      "只编方案" in html and "排队列" in html)
sm = re.search(r"const STEP_MODULES = \[(.*?)\];", js, re.S)
check("单步调试能选的模块和后端 app.STEP_MODULES 一字不差，且不含 trip（整趟不在调试范围）",
      sm is not None
      and tuple(re.findall(r'"([^"]+)"', sm.group(1))) == tuple(appmod.STEP_MODULES),
      sm.group(1).strip() if sm else "没找到常量")
check("调试栏确实挂上了单步这一档（tab + 面板都在 index.html 里）",
      'data-dtab="step"' in html and 'id="panel-step"' in html)
sq = js_fn("startQueue")
check("启动队列只交三件：items + mode + repeat（港名和本次清单由队列线程每趟现算）",
      "items" in sq and "mode" in sq and "repeat" in sq, sq.strip().replace("\n", " ")[:160])
check("界面不再交那四个港名 / 清单（后端从方案现算，两份说法迟早不一致）",
      not any(k in sq for k in ["buy_port", "buy_goods", "sail_dest_port", "sell_port"]),
      str([k for k in ["buy_port", "buy_goods", "sail_dest_port", "sell_port"] if k in sq]))
check("「每趟启动前会 OCR 读一次」在点启动前就告诉人（不让人以为按钮卡住）",
      "OCR" in sq, "OCR" in sq)
sd = js_fn("runDebugStep")
check("单步调试交四件：module + port + current_port + goods（不写 route_plan.json）",
      all(k in sd for k in ["module:", "port", "current_port:", "goods"]),
      sd.strip().replace("\n", " ")[:160])
check("单步调试的港口必填、买货必须勾货（花真金币的入口都先拦一道）",
      "先填「本次港口」" in sd and "买货要勾至少一件货" in sd)
check("运行那一行的整趟进度念的是引擎 s.trip（前端不自己数站）",
      "s.trip" in js_fn("renderRunInfo") and "stop_reason" in js_fn("renderRunInfo"))
check("说明文字里那句「一次只跑一个模块」已经改掉（现在方案都是完整流程，留着会骗人）",
      "一次只跑一个模块" not in html, "还在 index.html 里" if "一次只跑一个模块" in html else "")
check("「当前所在港」那一栏写明「留空 = 点运行时 OCR 读一次」（在单步调试那一栏）",
      "OCR 读一次" in html)
rn = js_fn("reorderNote")
check("存方案会念「买货 → 中转 → 卖货」自动重排 + 同类内部保持你排的先后（挪了不吭声才是问题）",
      "买货 → 中转 → 卖货" in rn and "同类内部" in rn and "先后" in rn,
      rn.strip().replace("\n", " ")[:90])
check("重排明细只在「保存方案」这一处挂（保存规划 / 读方案那两个入口已经不再走）",
      js.count("reorderNote(out);") == 1, "调用 %d 次" % js.count("reorderNote(out);"))
check("index.html 的中转卡片不再说「未进流程 / 填了不会真的走」",
      "未进流程" not in html and "填了不会真的走" not in html,
      "还有旧话" if ("未进流程" in html or "填了不会真的走" in html) else "")
check("   中转卡片改口成「整趟会走这一站」，并写明它只做进出港、不进交易所",
      "整趟会走这一站" in html and "不进交易所" in html)
css = open(os.path.join(BASE, "frontend", "style.css"), encoding="utf-8").read()
check("「没接后端的卡片」那套虚线样式已经跟着删掉（全界面只剩中转一张用过它，留着会骗后来的人）",
      ".route-demo {" not in css and "route-demo" not in html)

# ---------- H2 表格「货物名称」的匹配搜索（三级界面：一级港口清单 → 二级该港货物清单 → 三级单件编辑） ----------
# 为什么单独盯这块：货物名称只能来自模板库的 商品-*（买货是拿模板去找货）。
# 换成搜索框之后，「打进去的字」和「选中的货」必须是两件事 ——
# 一旦哪天有人把 oninput 直接接到 goods_name 上，手打错字就会静默写进表格，
# 界面看着正常、点启动才买不到货。所以这里既验文本契约，也把匹配函数真跑一遍。
# 2026-09-30 按用户要求改成三级（一级只列港口名 + 可搜 + 按拼音排、二级看这个港有哪些货、三级才编辑），
# 顺带盯着：一级不许藏编辑器 / 不许铺开按钮、二级港口名改一次整组生效、落盘字段一个不多。
check("旧的那套「一行一张卡」已经没了（goodsRowHtml / .plan-item 都不再用）",
      "goodsRowHtml" not in js and "plan-item" not in js and "plan-item" not in css)
check("一级只列港口名：有搜索框 + 按拼音排，点一行就是 openPlanPort(港口名)（一级里没有货物编辑器）",
      'id="plan-port-search"' in js_fn("renderPortList")
      and "planSortedGroups()" in js_fn("renderPortList")
      and "openPlanPort(" in js_fn("renderPortList")
      and "plan-goods-search" not in js_fn("renderPortList"),
      js_fn("renderPortList").strip().replace("\n", " ")[:100])
check("   搜索框只筛显示（打几个字就筛港口名），不动数据；重画后光标还给搜索框",
      "setPortQuery(this.value)" in js_fn("renderPortList")
      and "planPortQuery" in js_fn("renderPortList") and "planPortQuery" in js_fn("setPortQuery")
      and "plan-port-search" in js_fn("setPortQuery"),
      js_fn("setPortQuery").strip().replace("\n", " ")[:100])
check("   一级排序按拼音（localeCompare 带 zh-Hans-CN），没填港口名的那组永远垫底",
      'localeCompare(b.label, "zh-Hans-CN")' in js_fn("planSortedGroups")
      and "!g.port" in js_fn("planSortedGroups"),
      js_fn("planSortedGroups").strip().replace("\n", " ")[:100])
check("一级那一行点下去就是 openPlanPort(港口名)，整行里没有一个编辑控件",
      re.search(r'onclick="openPlanPort\(this\.dataset\.port\)"', js) is not None
      and 'data-port="${escapeHtml(g.label)}"' in js,
      js_fn("renderPortList").strip().replace("\n", " ")[:120])
check("二级：点开这个港看它的货物清单，每件货一行、只列货名（点一行才进三级）",
      "planLineHtml(i)" in js_fn("renderPortGoods")
      and "<input" not in js_fn("planLineHtml")
      and re.search(r'onclick="openPlanRow\(\$\{i\}\)"', js) is not None,
      js_fn("planLineHtml").strip().replace("\n", " ")[:120])
check("二级有「返回港口列表」+「加一种货」+「删整组」（不在一级铺开按钮）",
      "backToPortList()" in js_fn("renderPortGoods")
      and "addPlanGoods(${anchor})" in js_fn("renderPortGoods")
      and "deletePlanGroup(${anchor})" in js_fn("renderPortGoods"))
check("三级有「返回货物清单」，也有删除本行",
      "closePlanRow()" in js_fn("renderPlanDetail")
      and "deletePlanRow(${i})" in js_fn("renderPlanDetail"))
check("   返回 / 打开某一行都只动 planPort·planEdit，不碰 planData",
      "planPort = null" in js_fn("backToPortList") and "planEdit = null" in js_fn("backToPortList")
      and "planEdit = i" in js_fn("openPlanRow") and "planEdit = null" in js_fn("closePlanRow")
      and "planData" not in js_fn("openPlanRow") + js_fn("closePlanRow") + js_fn("backToPortList"),
      (js_fn("openPlanRow") + " | " + js_fn("closePlanRow")).strip().replace("\n", " ")[:120])
check("港口名只在二级组头改（整组生效），三级里不给港口输入框",
      re.search(r'oninput="setGroupPort\(\$\{anchor\}, this\.value\)"', js) is not None
      and "g.indices[0]" in js_fn("renderPortGoods")
      and "plan-port" not in js_fn("renderPlanDetail"))
check("表格里那个「货物名称」下拉已经换成搜索框（select.plan-goods-name 不再存在）",
      'class="plan-goods-name"' not in js and 'class="plan-goods-search"' in js)
check("搜索框的 oninput 只重画候选，不写 goods_name（打字永远不会变成货名）",
      re.search(r'class="plan-goods-search"[^>]*oninput="refreshGoodsPick\(\$\{i\}\)"', js) is not None,
      js_fn("renderPlanDetail")[:120].replace("\n", " "))
check("全界面没有任何输入事件直接写 goods_name（打字 / 改备注都不算选中货）",
      re.search(r'on(?:input|change)="setPlanField\([^,]+,\s*["\']goods_name', js) is None,
      "有输入事件直接写货名" if re.search(r'on(?:input|change)="setPlanField\([^,]+,\s*["\']goods_name', js) else "")
check("写 goods_name 的只有两处：点中候选（pickGoods）和清空（clearGoodsPick）",
      js.count('setPlanField(i, "goods_name"') == 2
      and "setPlanField(i, \"goods_name\"" in js_fn("pickGoods")
      and "setPlanField(i, \"goods_name\", \"\")" in js_fn("clearGoodsPick"),
      "出现 %d 次" % js.count('setPlanField(i, "goods_name"'))
check("   pickGoods 取的是 goods_options 里按下标的那件（不是自己拼名字）",
      "goods_options" in js_fn("pickGoods") and "[idx]" in js_fn("pickGoods"),
      js_fn("pickGoods").strip().replace("\n", " ")[:120])
check("候选的 onclick 传的是下标不是中文名字（T-065 那条老规矩）",
      re.search(r'onclick="pickGoods\(\$\{i\}, \$\{idx\}\)"', js) is not None
      and "pickGoods(${i}, ${idx})" in js)
check("候选是纯文字，不放缩略图（2026-09-29 你点名「不要显示图片」）",
      "<img" not in js_fn("goodsPickHtml") and "thumb" not in js_fn("goodsPickHtml")
      and "templateOfGoods" not in js and ".pgoods-item img" not in css,
      js_fn("goodsPickHtml").strip().replace("\n", " ")[:100])
check("下拉默认收起，点进输入框才展开；点候选前用 mousedown 保住焦点（不然 blur 先把下拉收走）",
      'class="pgoods-drop hidden"' in js and ".pgoods-drop.hidden" in css
      and 'onmousedown="event.preventDefault()"' in js)
check("已存的值在模板库里查不到时仍然说人话（缺模板的提醒没被搜索框弄丢）",
      "goodsWarn(value)" in js_fn("setPlanField") and "pgoods-current" in js_fn("setPlanField"))
check("   二级那件缺模板的货也用得上这条信息（那一行右边点一个红点，不铺长文字）",
      "goodsWarn(row.goods_name)" in js_fn("planLineHtml")
      and '.plan-line.warn .plan-line-goods::after' in css)
check("样式用的是 pgoods- 前缀，没把跑商设置那套 .goods-* 覆盖掉",
      ".pgoods-item" in css and ".goods-row {" in css)
check("三级界面没多出落盘字段：保存还是 port / goods_name / cargo_type / note 那四个",
      all(k in js_fn("savePlan") for k in ["port:", "goods_name:", "cargo_type:", "note:"])
      and "cabin_name" not in js_fn("savePlan"),
      js_fn("savePlan").strip().replace("\n", " ")[:120])
check("   planEdit / planPort 只是前端「现在看哪一层」，重新读取和删除都会退回上层",
      "planEdit = null" in js_fn("loadPlan") and js.count("planEdit = null") >= 5,
      "出现 %d 次" % js.count("planEdit = null"))
check("index.html 的表格说明改成了「打几个字 + 点一下才算选中」，不再说下拉",
      "点一下才算选中" in html and "挑的下拉" not in html)
check("   长说明收进折叠的「说明」块，界面上只留两行短的（你要的「显示简洁」）",
      '<details class="plan-help">' in html and "plan-short" in html
      and "点一个港口进它的货物清单" in html)
check("   短说明写明一级只有港口名 + 可搜索 + 按拼音排（你要的三件事在界面上说得出口）",
      "只有港口名" in html and "可搜索" in html and "按拼音排" in html)
check("   说明里不再宣称候选带缩略图", "带缩略图" not in html)

# 匹配函数本身：把 app.js 里那份 matchGoods 抠出来交给 node 真跑，不在 python 里重写一遍
# （重写的那份永远是对的，测不到东西）。
m_mg = re.search(r"function matchGoods\(([^)]*)\)\s*\{(.*?)\n\}", js, re.S)
check("app.js 里能抠出完整的 matchGoods 函数", m_mg is not None,
      "没找到这个函数" if m_mg is None else "")
if m_mg:
    cases = [["铜版画", "版", True], ["铜版画", "版画", True], ["油画", "画", True],
             ["中国画", "中国", True], ["上等腌制鲱鱼", "鲱鱼", True],
             ["上等腌制鲱鱼", "腌鲱", False],          # 不连着就不算：这条是刻意的口径
             ["油画", "", True], ["油画", "   ", True],  # 空查询 = 全部列出来
             ["油画", "YH", False], ["", "版", False],
             ["ABC", "abc", True], ["ABC", "Ab", True]]  # 拉丁字母不分大小写
    harness = "const matchGoods = (%s) => {%s};\n" % (m_mg.group(1), m_mg.group(2))
    harness += "const C = %s;\n" % json.dumps(cases, ensure_ascii=False)
    harness += ("let bad = C.filter(c => matchGoods(c[0], c[1]) !== c[2])"
                ".map(c => c[0] + '|' + c[1] + ' 期望 ' + c[2]);\n")
    harness += "console.log(bad.length ? 'BAD ' + JSON.stringify(bad) : 'OK ' + C.length);\n"
    hj = os.path.join(TMP, "uwo_matchgoods_selfcheck.js")
    with open(hj, "w", encoding="utf-8") as f:
        f.write(harness)
    node = shutil.which("node")
    check("   node 在（匹配用例要真跑，不真跑的断言不算验过）", node is not None,
          "PATH 里找不到 node" if node is None else "")
    if node:
        r = subprocess.run([node, hj], capture_output=True, text=True, encoding="utf-8")
        out = (r.stdout or "").strip()
        check("   matchGoods 真跑 %d 条用例全对（打「版」中「铜版画」，不连着的不算）" % len(cases),
              r.returncode == 0 and out.startswith("OK"),
              out or (r.stderr or "").strip()[:160])
    if os.path.exists(hj):
        os.remove(hj)

print("=" * 72)
print("I) 前端第二级：跑商设置栏内分「方案设置 / 购买前改舱 / 切换配置」")
import route_presets as rps  # noqa: E402  (界面上能选谁，必须和后端肯存谁一模一样)

# 后面有好几条断言要比对「去掉强调标签和书名号之后的界面原话」—— 那些标记只是排版用的，
# 词本身是产品对用户说的话，断言按整句写才不会一改文案就假失败。
plain = html.replace("<b>", "").replace("</b>", "").replace("「", "").replace("」", "")


def js_list(name):
    """把 app.js 里那行 `const X = ["…", "…"];` 读成 python 列表（读不出来给 None，让断言当场报）。"""
    m = re.search(r"const %s = \[([^\]]*)\];" % name, js)
    if not m:
        return None
    return [s.strip().strip('"') for s in m.group(1).split(",") if s.strip()]


check("二级那一排存在：三块面板 + 三个 data-rtab（方案设置 / refit / cfg）",
      'id="route-subtabs"' in html and html.count('data-rtab=') == 3
      and 'data-rtab="plan"' in html and 'data-rtab="refit"' in html and 'data-rtab="cfg"' in html
      and 'id="route-sub-plan"' in html and 'id="route-sub-refit"' in html
      and 'id="route-sub-cfg"' in html,
      str(re.findall(r'data-rtab="(\w+)"', html)))
check("二级用的是 div 不是 button：<input> 套进 <button> 是非法 HTML，点一次会变两次",
      '<div class="subtab' in html and '<button class="subtab' not in html)
check("原来那一整套内容整块搬进「方案设置」，一个字没删（方案库卡片 + 站次三张卡 + datalist 都在里面）",
      html.index('<div id="route-sub-plan" hidden>') < html.index('id="preset-list"')
      and html.index('<div id="route-sub-plan" hidden>') < html.index('id="route-stops-sell"')
      and html.index('<div id="route-sub-plan" hidden>') < html.index('id="route-port-options"')
      and html.index('id="route-port-options"') < html.index("</div><!-- /route-sub-plan -->"),
      "顺序对不上")
# 2026-10-01 你又拍了一条：进栏时**三块都收起**，点哪一个才展开哪一个（默认谁都不摊开）
check("三块面板默认**全部收起**（方案设置也不抢那个「一进来就摊开」的位置）",
      'id="route-sub-plan" hidden' in html and 'id="route-sub-refit" hidden' in html
      and 'id="route-sub-cfg" hidden' in html,
      str([i for i in ["plan", "refit", "cfg"] if 'id="route-sub-%s" hidden' % i not in html]))
check("html 里没有任何一个二级标签写着默认高亮（active 只能由点击产生）",
      '<div class="subtab active"' not in html,
      str(re.findall(r'<div class="subtab[^"]*"', html))[:120])
check("进栏走 switchRouteTab(null) = 三块都收起；旧的「记住上次开的是哪块」已经删掉"
      "（记了就会自己弹开一块，正好和你要的相反）",
      "switchRouteTab(null);" in js_fn("loadPresetEditor")
      and "uwo.routeTab" not in js and "routeTab = null" in js,
      js_fn("loadPresetEditor").strip().replace("\n", " ")[-90:])
check("点左侧导航进这一栏时**先当场收起**再去拉数据（不然上一块会摊着两百毫秒，像没收）",
      'if (page === "route") {' in js_fn("switchPage")
      and js_fn("switchPage").index("switchRouteTab(null);")
          < js_fn("switchPage").index("loadPresetEditor();"),
      js_fn("switchPage")[-200:].replace("\n", " "))
check("switchRouteTab 认 null（收起全部），非 null 才高亮那一块",
      "if (tab !== null && !ROUTE_TABS.includes(tab))" in js_fn("switchRouteTab")
      and 'b.dataset.rtab === tab' in js_fn("switchRouteTab")
      and "el.hidden = k !== tab" in js_fn("switchRouteTab"),
      js_fn("switchRouteTab").strip().replace("\n", " ")[:110])
check("点「编辑」/「另存为新方案」会顺手展开方案设置（收起着点它俩会像没反应）",
      'switchRouteTab("plan");' in js_fn("presetEditByIdx")
      and 'switchRouteTab("plan");' in js_fn("presetSaveAsNew"),
      "presetEditByIdx / presetSaveAsNew 里没找到展开调用")
check("界面上把「三块默认收起、点哪一个展开哪一个」写出来了（不能让人自己猜）",
      "默认都是收起的" in plain and "点上面哪一个" in plain, "")
check("改舱总开关留在二级标签；切换配置标签改为显示三时点启用数",
      'id="flag-refit"' in html and 'id="flag-cfg"' not in html
      and 'id="flag-refit-state"' in html and 'id="flag-cfg-state"' in html
      and "已启用 ${enabledConfigs.length}/3" in js_fn("renderRouteOptions"))
check("改舱开关走 onOptionFlag；三个配置时点都走 onConfigField，写回同一份 options 草稿",
      html.count('onchange="onOptionFlag(') == 1
      and js_fn("renderRefitBody").count('onchange="onOptionFlag(') == 1
      and "o.switch_config[point]" in js_fn("onConfigField")
      and js_fn("renderCfgBody").count("onConfigField(") == 2,
      "html %d / refit %d / cfg %d" % (
          html.count('onchange="onOptionFlag('),
          js_fn("renderRefitBody").count('onchange="onOptionFlag('),
          js_fn("renderCfgBody").count("onConfigField(")))
check("切换面板只换这一栏内部的三块，左侧一级导航一个字不动（没多出一个 data-page）",
      'data-page="refit"' not in html and 'data-page="cfg"' not in html
      and "route-sub-plan" in js_fn("switchRouteTab") and "PAGES" not in js_fn("switchRouteTab"),
      js_fn("switchRouteTab").strip().replace("\n", " ")[:120])
# 改舱那一页：参数是真的（船种 / 哪几格 / 改成什么舱），且和后端同一份清单
check("改舱那一页给三个真参数：船种下拉、格子勾框、类别下拉 + 只读船舱名",
      'id="refit-ship"' in js_fn("renderRefitBody")
      and 'id="refit-cargo"' in js_fn("renderRefitBody")
      and "data-slot=" in js_fn("renderRefitBody")
      and 'class="opt-cabin"' in js_fn("renderRefitBody"),
      js_fn("renderRefitBody").strip().replace("\n", " ")[:120])
check("船舱名按 大型XX管理室 现算（和表格栏 / purchase_plan.cabin_name 同一条规则，不另存一份）",
      "`大型${o.cargo_type}管理室`" in js_fn("renderRefitBody"))
check("界面上能选的船种 == 后端肯存的船种（一边多一个名字就会出现「选了存不进」）",
      js_list("REFIT_SHIPS") == rps.REFIT_SHIPS, str(js_list("REFIT_SHIPS")))
check("界面上能勾的格子 == 后端肯存的格子，而且全是「可搭乘」（花蓝钻的那一栏根本不在候选里）",
      js_list("REFIT_SLOTS") == rps.REFIT_SLOTS
      and not any("无法搭乘" in s for s in (js_list("REFIT_SLOTS") or [])),
      str(js_list("REFIT_SLOTS")))
check("类别下拉的候选从 /api/plan 取（那 17 种只有表格栏一份，不在前端抄写一遍）",
      "routeCargoTypes = Array.isArray(plan.cargo_types)" in js_fn("loadPresetEditor"))
check("没录判据的船种在界面上是**勾不上的占位项**（disabled，不是能选但存不进的选项）",
      '<option value="" disabled>（占位' in js_fn("renderRefitBody"))
# 切换配置那一页：三个时点分别配开关和游戏内名称，已经接入队列与状态机
cfg_body = js_fn("renderCfgBody")
check("切换配置页按 CONFIG_POINTS 生成三个独立时点，每项都有开关和可编辑配置名",
      "CONFIG_POINTS.map" in cfg_body and "item.enabled" in cfg_body
      and "item.config_name" in cfg_body and "disabled" not in cfg_body
      and "maxlength=\"40\"" in cfg_body, cfg_body.strip().replace("\n", " ")[:180])
check("pickOptions 把三时点的 enabled / config_name 全部规整后交给后端",
      "CONFIG_POINTS.forEach" in js_fn("pickOptions")
      and "enabled: item.enabled === true" in js_fn("pickOptions")
      and "config_name: String(item.config_name || \"\").trim()" in js_fn("pickOptions")
      and "switch_config: switchConfig" in js_fn("pickOptions"),
      js_fn("pickOptions").strip().replace("\n", " ")[-180:])
check("界面在栏口说清三个独立时点，并明确切换配置会自动执行",
      "买货前、出港前、卖货前" in plain and "三个独立开关" in plain
      and "切换配置会在启用的时点自动进入“分配设置”" in plain)
check("蓝钻那条规矩在界面上说得出（改舱只碰金币那一栏），并且写了实测那次的单价",
      "蓝色钻石" in html and "5,943,000" in html)
check("切换配置页写清分配设置、右半屏模板、OCR 列表和找不到即停",
      "分配设置" in html and "屏幕右半区域" in html and "OCR 配置名" in html
      and "停止喊人" in cfg_body)
check("旧的占位口径已删掉，不再写『尚未接队列 / 先搁置 / 前两步已录』",
      all(old not in html + js for old in ["尚未接入队列", "先搁置", "前两步已录"]))
# 数据线：勾了要存得进去、脏检查要管得到、老方案要读得动
check("保存方案真的把 options 交出去（不然这一页白填）",
      "options: core.options" in js_fn("saveRoute"))
check("「有改动未保存」的比对覆盖 options（勾框一改就提醒，切走会拦住问一句）",
      "options: pickOptions(preset.options)" in js_fn("coreOf"))
check("读回来的勾框认的是 === true（老方案里没这一项 = 没勾，绝不当成勾上了）",
      js_fn("pickOptions").count("=== true") == 2, str(js_fn("pickOptions").count("=== true")))
check("格子按固定顺序存（不是点击顺序），后端同一条规矩 —— 两边存出来必须一模一样",
      "o.refit.slots = REFIT_SLOTS.filter(s => set.has(s))" in js_fn("onRefitSlot")
      and "[s for s in REFIT_SLOTS if s in slots]" in open(
          os.path.join(BASE, "route_presets.py"), encoding="utf-8").read())
check("每次重画这两块面板都排队到下一个任务（勾框自己在自己那块里，同步换掉会一点一删）",
      "setTimeout(() => { renderRouteOptions()" in js_fn("refreshRouteOptions"))
check("站次那头的每一处改动都顺手刷这两块（「这一趟要跑哪几类货」那一行不会停在旧的）",
      js.count("refreshRouteOptions()") >= 6, "出现 %d 次" % js.count("refreshRouteOptions()"))
check("整片刷新走 renderRoute → renderRouteOptions：换方案时站次和勾框一起换，不会半新半旧",
      "renderRouteOptions();" in js_fn("renderRoute")
      and "renderRoute();" in js_fn("loadPresetEditor"),
      js_fn("renderRoute").strip().replace("\n", " ")[:100])
check("方案列表那一行念得出勾了什么（options_text 由后端算，前端不自己拼第二套话）",
      "p.options_text" in js_fn("renderPresets") and "out.preset.options_text" in js_fn("saveRoute"))
check("样式齐了：二级标签 / 勾框状态徽 / 只读船舱名 / 列表那一行的附加步骤",
      all(s in css for s in [".subtabs {", ".subtab.active", ".subtab-state.on",
                             "input.opt-cabin", ".preset-extra"]),
      str([s for s in [".subtabs {", ".subtab.active", ".subtab-state.on",
                       "input.opt-cabin", ".preset-extra"] if s not in css]))
check("勾了改舱却没填格子/类别时界面上有黄字提醒（后端会拒，先在界面上说明白）",
      "还得说清" in js_fn("renderRefitBody")
      and "改完退不回去" in js_fn("renderRefitBody")
      and "o.enabled && miss.length" in js_fn("renderRefitBody")
      and "route-warn-item" in js_fn("renderRefitBody")
      and len(re.findall(r"miss\.push", js_fn("renderRefitBody"))) == 2,
      js_fn("renderRefitBody").count("route-warn-item"))
check("界面上不写死船舱判据模板名以外的东西：格子的模板名按 UI-船舱-可搭乘-格序拼（命名口径统一）",
      "`UI-船舱-可搭乘-${s}-…`" in js_fn("renderRefitBody"))

if os.path.exists(FLAGFILE):
    os.remove(FLAGFILE)
state_machine.purchase_plan = real_plan
print("=" * 72)
print("失败 %d 项" % len(FAILS))
for f in FAILS:
    print("   FAIL:", f)
sys.exit(1 if FAILS else 0)
