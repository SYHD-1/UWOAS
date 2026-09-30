"""运行态账本层离线自检：run_state.json 落盘 + 引擎的 flag 条件 / set_flag 动作 + 按港口记的补货时刻 + 前端登记。

为什么单独一份自检：这一层是「引擎自己记的账」，既不看画面也不碰 ADB，
买货链末尾置 1、卖货链链头认它、卖完置 0；买货前查哪个港几点补货也靠它 —— 三层全靠它串起来。
它的失败方式很安静（名字打错、条件写了没人读、界面下拉里没登记），
所以这份自检专门盯「安静失败」。

全程只读写 %TEMP% 下的假 run_state.json，项目里那份真文件一次都不碰（最后一条断言核对字节）。
"""
import json
import os
import re
import sys
import threading

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 项目根 = selfcheck/ 的上一级（换目录/换机器都不用改）
sys.path.insert(0, BASE)

import run_state  # noqa: E402
import state_machine  # noqa: E402
from state_machine import StateMachineEngine  # noqa: E402

TMP = os.environ.get("TEMP", "/tmp")   # 换机器也不用改（和 route_format 那份同款写法）
FAKE = os.path.join(TMP, "uwo_runstate_selfcheck.json")
UNUSED_SHOT = os.path.join(BASE, "selfcheck", "fixtures", "state_screen_buy.png")   # 这一层的判据不看画面，传了也不会读

# 第一件事就把落盘路径改到临时目录：后面所有用例都往这里写，绝不碰项目里那份
REAL_LEDGER = run_state.RUN_STATE_JSON
REAL_BYTES = open(REAL_LEDGER, "rb").read() if os.path.exists(REAL_LEDGER) else None
run_state.RUN_STATE_JSON = FAKE

FAILS = []


def check(label, cond, detail=""):
    print("  %-4s %s %s" % ("PASS" if cond else "FAIL", label, detail))
    if not cond:
        FAILS.append(label)


def reset_file(text=None):
    """放一份指定内容的假文件（text=None 就删掉，模拟「第一次跑还没写过」）。"""
    if os.path.exists(FAKE):
        os.remove(FAKE)
    if text is not None:
        with open(FAKE, "w", encoding="utf-8") as f:
            f.write(text)


def raises(fn):
    """跑一下，返回 (是不是 ValueError, 异常对象)。"""
    try:
        fn()
    except ValueError as e:
        return True, e
    except Exception as e:            # noqa: BLE001 - 自检里要区分「错得太离谱」
        return False, e
    return False, None


class FakeCtl:
    def __init__(self):
        self.clicks = []
        self.keys = []

    def click(self, x, y):
        self.clicks.append((x, y))

    def send_key(self, code, hold_ms=80):
        self.keys.append(code)
        return True, "fake"


def make_engine():
    data = state_machine.load_states()
    eng = StateMachineEngine("fake-adb", 16384, UNUSED_SHOT)
    eng.config = data["config"]
    eng.states = data["states"]
    return eng, FakeCtl()


eng, ctl = make_engine()

print("=" * 72)
print("A) as_bool：字符串 'false' 绝不能变成 True")
for val in [True, "true", "TRUE", " true ", "1", "yes", "on", 1]:
    got = run_state.as_bool(val)
    check(f"{val!r} -> True", got is True, f"实得 {got!r}")
for val in [False, "false", "False", "0", "no", "off", "", "   ", 0]:
    got = run_state.as_bool(val)
    check(f"{val!r} -> False", got is False, f"实得 {got!r}")
for val in ["maybe", "y", "1.0", 2, 3, None, [], {}, True and 1.0]:
    bad, e = raises(lambda v=val: run_state.as_bool(v))
    check(f"{val!r} 必须报错（不猜）", bad, f"实得 {type(e).__name__}: {e}")
check("报错话说清了是布尔的问题",
      "布尔" in str(raises(lambda: run_state.as_bool("maybe"))[1]))

print("-" * 72)
print("B) 白名单 + 落盘")
check("目前只有一个开关 sell_pending", run_state.FLAGS == ("sell_pending",), str(run_state.FLAGS))
reset_file()
check("文件不存在 -> 默认值全 False", run_state.load_state() == {"sell_pending": False})
check("读默认值不会顺手把文件建出来", not os.path.exists(FAKE))
check("get_flag 默认 False", run_state.get_flag("sell_pending") is False)
check("set_flag True 返回 True", run_state.set_flag("sell_pending", True) is True)
raw = json.load(open(FAKE, encoding="utf-8"))
check("落盘落的是真布尔 true，不是字符串", raw == {"sell_pending": True}, str(raw))
check("写完 get_flag 读到 True", run_state.get_flag("sell_pending") is True)
run_state.set_flag("sell_pending", "false")
check("写字符串 'false' 收成 False（人手工改文件最容易踩这个）",
      json.load(open(FAKE, encoding="utf-8")) == {"sell_pending": False})
reset_file('{"sell_pending": false, "手工备注": "别删我"}')
run_state.set_flag("sell_pending", True)
after = json.load(open(FAKE, encoding="utf-8"))
check("保存时不替人删文件里多出来的字段", after.get("手工备注") == "别删我", str(after))
check("但 load_state 只认白名单里的键", set(run_state.load_state()) == {"sell_pending"})
bad, e = raises(lambda: run_state.get_flag("sell_pendingg"))
check("读没登记过的开关名 -> 当场报错", bad and "sell_pendingg" in str(e), str(e))
before = open(FAKE, encoding="utf-8").read()
bad, e = raises(lambda: run_state.set_flag("nope", True))
check("写没登记过的开关名 -> 报错", bad, str(e))
check("报错时一个字都没往文件里写", open(FAKE, encoding="utf-8").read() == before)
check("报错里把可选名字念出来了（好修）", "sell_pending" in str(e))
reset_file("{不是 JSON")
bad, e = raises(lambda: run_state.get_flag("sell_pending"))
check("文件被写坏 -> 抛的还是 ValueError（引擎那层只接得住这个）",
      bad, f"{type(e).__name__}: {e}")
check("坏文件让条件判 False，不会把引擎崩掉",
      eng._check_condition({"type": "flag", "name": "sell_pending"}, UNUSED_SHOT) is False)
reset_file('{"sell_pending": true}')
errs = []


def hammer(n):
    try:
        for _ in range(30):
            run_state.set_flag("sell_pending", n % 2 == 0)
    except Exception as e:                # noqa: BLE001
        errs.append(repr(e))


threads = [threading.Thread(target=hammer, args=(i,)) for i in range(8)]
[t.start() for t in threads]
[t.join() for t in threads]
check("8 个线程一起写：一次异常都没有", errs == [], str(errs[:2]))
check("8 个线程一起写：文件还是合法 JSON 且读得回布尔",
      isinstance(run_state.load_state()["sell_pending"], bool),
      open(FAKE, encoding="utf-8").read().replace("\n", ""))

print("-" * 72)
print("C) flag 条件：主循环和「测试条件」按钮必须给同一个答案（T-033 的老坑）")
cases = [
    ({"type": "flag", "name": "sell_pending", "equals": True}, True, True),
    ({"type": "flag", "name": "sell_pending", "equals": False}, True, False),
    ({"type": "flag", "name": "sell_pending"}, True, True),          # equals 省略 = 要求 1
    ({"type": "flag", "name": "sell_pending", "equals": True}, False, False),
    ({"type": "flag", "name": "sell_pending", "equals": 1}, False, False),
    ({"type": "flag", "name": "sell_pending", "equals": "false"}, False, True),
    ({"type": "flag", "name": "sell_pendin", "equals": True}, True, False),   # 名字打错
]
for cond, disk, want in cases:
    reset_file(json.dumps({"sell_pending": disk}))
    loop = eng._check_condition(cond, UNUSED_SHOT)
    ui = eng._eval_condition_detail(cond, UNUSED_SHOT)
    check(f"{cond} 盘上 {int(disk)} -> {int(want)}（两处一致）",
          loop is want and ui["matched"] is want,
          f"主循环 {loop} / 按钮 {ui['matched']}｜{ui['detail']}")
reset_file(json.dumps({"sell_pending": True}))
check("不认识的开关名：主循环不抛，只判 False",
      eng._check_condition({"type": "flag", "name": "sell_pendin"}, UNUSED_SHOT) is False)
ui = eng._eval_condition_detail({"type": "flag", "name": "sell_pendin"}, UNUSED_SHOT)
check("按钮那边说的是人话（含「没有这个运行态开关」），不是「未知条件类型」",
      "没有这个运行态开关" in ui["detail"] and "未知条件类型" not in ui["detail"], ui["detail"])
ui = eng._eval_condition_detail({"type": "flag", "name": "sell_pending", "equals": False}, UNUSED_SHOT)
check("详情里说清了当前值和要求值", "当前 1" in ui["detail"] and "要求 0" in ui["detail"], ui["detail"])
# 顺手把另一种「不看画面」的内存判据也钉在一起：将来再加第三种时，这条会提醒两处都要写
eng.run_rows = [{"goods_name": "布"}]
eng.run_row_index = 0
for cond in [{"type": "plan_pending"}, {"type": "flag", "name": "sell_pending"}]:
    for idx in (0, 1):
        eng.run_row_index = idx
        reset_file(json.dumps({"sell_pending": idx == 1}))
        check(f"{cond['type']} 第 {idx} 轮两处一致",
              eng._check_condition(cond, UNUSED_SHOT)
              is eng._eval_condition_detail(cond, UNUSED_SHOT)["matched"])

print("-" * 72)
print("D) set_flag 动作：只写文件，不碰画面")
reset_file()
ctl = FakeCtl()
msg = eng._do_action({"type": "set_flag", "name": "sell_pending", "value": True}, ctl, UNUSED_SHOT)
check("置 1 之后盘上真是 True", run_state.get_flag("sell_pending") is True, msg)
check("返回值里写了「置 1」", "置 1" in msg, msg)
check("日志里能看到是哪个开关", any("sell_pending" in l["message"] for l in eng._logs),
      str([l["message"] for l in eng._logs][-1:]))
check("一步画面都没点", ctl.clicks == [] and ctl.keys == [], str((ctl.clicks, ctl.keys)))
eng._do_action({"type": "set_flag", "name": "sell_pending", "value": False}, ctl, UNUSED_SHOT)
check("置 0 之后盘上是 False", run_state.get_flag("sell_pending") is False)
eng._do_action({"type": "set_flag", "name": "sell_pending"}, ctl, UNUSED_SHOT)
check("省略 value 就是置 1", run_state.get_flag("sell_pending") is True)
before = open(FAKE, encoding="utf-8").read()
msg = eng._do_action({"type": "set_flag", "name": "sell_pendingy", "value": True}, ctl, UNUSED_SHOT)
check("名字打错：返回「设置失败」而不是抛出去把整轮弄崩", "设置失败" in msg, msg)
check("名字打错：文件没被动过", open(FAKE, encoding="utf-8").read() == before)
check("名字打错：日志里留了 error 说清是哪个动作",
      any(l["event"] == "error" and "set_flag" in l["message"] for l in eng._logs),
      str([l["message"] for l in eng._logs if l["event"] == "error"][-1:]))
reset_file()
eng._do_action({"type": "set_flag", "name": "sell_pending", "value": True}, ctl, UNUSED_SHOT)
check("买完货置 1：卖货链头的 flag 条件成立",
      eng._check_condition({"type": "flag", "name": "sell_pending", "equals": True}, UNUSED_SHOT) is True)
eng._do_action({"type": "set_flag", "name": "sell_pending", "value": False}, ctl, UNUSED_SHOT)
check("卖出完成置 0：同一条条件立刻不成立（不会反复去卖）",
      eng._check_condition({"type": "flag", "name": "sell_pending", "equals": True}, UNUSED_SHOT) is False)
check("重启进程也还在（读的就是那份文件）", run_state.load_state() == {"sell_pending": False})

print("-" * 72)
print("E) 前端登记：后端加了的类型，界面下拉里必须有（否则只能手改 JSON）")
js = open(os.path.join(BASE, "frontend", "app.js"), encoding="utf-8").read()


def js_array(name):
    m = re.search(r"const %s = \[(.*?)\];" % name, js, re.S)
    return re.findall(r'"([^"]+)"', m.group(1)) if m else []


conds = set(js_array("CONDITION_TYPES"))
acts = set(js_array("ACTION_TYPES"))
flags = set(js_array("RUN_FLAGS"))
check("条件下拉里有 flag", "flag" in conds, str(sorted(conds)))
check("动作下拉里有 set_flag", "set_flag" in acts, str(sorted(acts)))
check("动作下拉里有 wait_restock（T-054 漏过一次）", "wait_restock" in acts)
check("前端开关名单和后端 FLAGS 一字不差", flags == set(run_state.FLAGS), f"{flags} vs {set(run_state.FLAGS)}")
check("defaultCondition 给 flag 建了默认值", re.search(r'type:\s*"flag"', js) is not None)
check("defaultAction 给 set_flag / wait_restock 建了默认值",
      'case "set_flag"' in js and 'case "wait_restock"' in js)


def walk_actions(node, out):
    if isinstance(node, list):
        for x in node:
            walk_actions(x, out)
        return
    if not isinstance(node, dict):
        return
    t = node.get("type")
    if t:
        out.add(t)
    if t == "if":
        walk_condition(node.get("condition"))
        walk_actions(node.get("then"), out)
        walk_actions(node.get("else"), out)
    elif t == "run_actions":
        walk_actions(node.get("actions"), out)


out_conds = set()


def walk_condition(node):
    if isinstance(node, list):
        for x in node:
            walk_condition(x)
        return
    if not isinstance(node, dict):
        return
    if node.get("type"):
        out_conds.add(node["type"])
    walk_condition(node.get("conditions"))


used_acts = set()
data = state_machine.load_states()
for st in data["states"]:
    walk_condition(st.get("condition"))
    walk_actions(st.get("actions"), used_acts)
check(f"states.json 里用到的动作类型全在下拉里 {sorted(used_acts)}", used_acts <= acts,
      f"缺: {sorted(used_acts - acts)}")
check(f"states.json 里用到的条件类型全在下拉里 {sorted(out_conds)}", out_conds <= conds,
      f"缺: {sorted(out_conds - conds)}")

print("-" * 72)
print("F) restock_at：按港口分开记的「下次补货时刻」（2026-09-30 加，代替「每次进购买页盯画面倒计时」）")
reset_file()
check("没文件 -> 空表（今天一个港都没买过）", run_state.load_restock() == {})
check("没记过的港口读到 None", run_state.get_restock_at("汉堡") is None)
for bad_port in ["", "   ", None, 123]:
    bad, e = raises(lambda p=bad_port: run_state.get_restock_at(p))
    check(f"港口名 {bad_port!r} 读 -> 当场报错，不拿 None 蒙过去", bad, str(e))
    bad, e = raises(lambda p=bad_port: run_state.set_restock_at(p, 1000))
    check(f"港口名 {bad_port!r} 写 -> 报错", bad, str(e))
check("这一路报错没把文件建出来", not os.path.exists(FAKE))
check("写一个港：返回写进去的整数", run_state.set_restock_at("汉堡", 1000562) == 1000562)
check("落盘形状就是 {港口名: 整数秒}",
      json.load(open(FAKE, encoding="utf-8")) == {"restock_at": {"汉堡": 1000562}},
      open(FAKE, encoding="utf-8").read().replace("\n", ""))
check("读回来是同一个整数", run_state.get_restock_at("汉堡") == 1000562)
run_state.set_restock_at("热那亚", 1000999)
check("第二个港各记各的，不覆盖第一个（粒度 = 按港口）",
      run_state.load_restock() == {"汉堡": 1000562, "热那亚": 1000999}, str(run_state.load_restock()))
run_state.set_restock_at("汉堡", 1001000)
check("同一个港再写一次 = 只换那一格", run_state.get_restock_at("汉堡") == 1001000)
check("另一个港没被牵连", run_state.get_restock_at("热那亚") == 1000999)
run_state.set_restock_at(" 北京 ", 1002000)
check("港口名两边空格去掉再存（OCR 读出来常带空格）",
      run_state.get_restock_at("北京") == 1002000
      and " 北京 " not in json.load(open(FAKE, encoding="utf-8"))["restock_at"],
      str(run_state.load_restock()))
for bad_when in ["1000562", 1000562.0, True, False, None, -1, {}, []]:
    before = open(FAKE, encoding="utf-8").read()
    bad, e = raises(lambda w=bad_when: run_state.set_restock_at("汉堡", w))
    check(f"时刻 {bad_when!r} -> 拒绝写入且不落盘", bad, str(e))
    check(f"时刻 {bad_when!r} 被拒时文件一个字节没动", open(FAKE, encoding="utf-8").read() == before)
reset_file(json.dumps({"sell_pending": False, "手工备注": "别删我", "restock_at": {"汉堡": 1}},
                      ensure_ascii=False))
run_state.set_restock_at("热那亚", 2)
after = json.load(open(FAKE, encoding="utf-8"))
check("写时刻不替人删开关和手工备注",
      after.get("sell_pending") is False and after.get("手工备注") == "别删我", str(after))
reset_file(json.dumps({"sell_pending": True, "restock_at": {"汉堡": 123}}, ensure_ascii=False))
run_state.set_flag("sell_pending", False)
check("写开关也不替人删补货表（两样东西同住一个文件，互不清场）",
      json.load(open(FAKE, encoding="utf-8")).get("restock_at") == {"汉堡": 123},
      open(FAKE, encoding="utf-8").read().replace("\n", ""))
check("load_state 还是只认开关，restock_at 不混进布尔那本账",
      run_state.load_state() == {"sell_pending": False}, str(run_state.load_state()))
for junk, want_kw in [({"restock_at": "明天下午"}, "不是一张表"),
                      ({"restock_at": {"汉堡": "123"}}, "不是非负整数"),
                      ({"restock_at": {"汉堡": True}}, "不是非负整数"),
                      ({"restock_at": {"汉堡": -5}}, "不是非负整数"),
                      ({"restock_at": {"汉堡": 1.5}}, "不是非负整数"),
                      ({"restock_at": {"": 5}}, "空港口名")]:
    reset_file(json.dumps(junk, ensure_ascii=False))
    bad, e = raises(lambda: run_state.load_restock())
    check(f"账本上写着 {junk} -> 读的时候就报错（要说清「{want_kw}」）",
          bad and want_kw in str(e), f"{type(e).__name__}: {e}")
    bad, e = raises(lambda: run_state.get_restock_at("汉堡"))
    check("同一个脏值走 get_restock_at 也报错，不会悄悄给个 None 让引擎当成没记过", bad, str(e))
reset_file('{"restock_at": {不是 JSON')
bad, e = raises(lambda: run_state.load_restock())
check("文件被写坏 -> 抛的还是 ValueError（引擎那层只接得住这个）",
      bad, f"{type(e).__name__}: {e}")

reset_file(json.dumps({"sell_pending": True, "restock_at": {"汉堡": 123}}, ensure_ascii=False))
snap = StateMachineEngine.run_state_snapshot()
check("给界面念的账本两样都带上（只读，界面没有改它的入口）",
      snap.get("sell_pending") is True and snap.get("restock_at") == {"汉堡": 123}, str(snap))
reset_file(json.dumps({"restock_at": "一串字"}, ensure_ascii=False))
snap = StateMachineEngine.run_state_snapshot()
check("账本读不出时整栏退成 error，不把 status 那一条请求弄 500", "error" in snap, str(snap))

reset_file()
errs = []


def hammer_port(n):
    try:
        for _ in range(30):
            run_state.set_restock_at(f"港{n}", 1000 + n)
    except Exception as e:                # noqa: BLE001
        errs.append(repr(e))


threads = [threading.Thread(target=hammer_port, args=(i,)) for i in range(8)]
[t.start() for t in threads]
[t.join() for t in threads]
check("8 个线程各写各的港：一次异常都没有", errs == [], str(errs[:2]))
check("8 个港全在表里（读-改-写没互相吃掉别人的格）", len(run_state.load_restock()) == 8,
      str(run_state.load_restock()))

reset_file()
check("自检用的是临时账本，项目里那份真 run_state.json 一个字节都没被写过",
      (open(REAL_LEDGER, "rb").read() if os.path.exists(REAL_LEDGER) else None) == REAL_BYTES,
      REAL_LEDGER)

print("=" * 72)
if FAILS:
    print(f"RESULT: {len(FAILS)} FAIL -> {FAILS}")
    sys.exit(1)
print("RESULT: ALL PASS (RUNSTATE_OK)")
