"""模块机制 + 港口间移动链 离线自检：真引擎代码 + 真截图 + 假控制器（一次都不真点 ADB）。

覆盖：
  1 正常航行画面 -> voyage 命中，retry_watch 判「未中断」，一次点击都不发
  2 到港画面     -> voyage 不命中（不该在港内盯航行）
  3 出港所画面   -> sail_office 命中，且 in_port / voyage 都不命中
  4 世界地图画面 -> sail_pick_city 命中
  5 合成中断画面 -> retry_watch 点满 max_attempts 后停止 + 挂出弹窗请求（不真弹）
  6 goto         -> 只设跳转标记，不碰画面
  7 start() 按模块裁 self.states：sail 跑不进 buy 的状态，反之亦然
  8 「本次港口」取哪个字段由模块自己定（买货吃 buy_port，移动吃 sail_port）
  9 各种该拒绝启动的组合：没填 module / 模块名不存在 / 模块没入口 / 目的港空 /
    表格没这个港 / **买货没选港** / **移动目的地就是当前所在港（原地打转）**
 10 移动链头 sail_from_port 从港内码头起步；到港收尾是 stop，不自动接买货
 11 _classify_page：每张真图认得出是什么页面
 12 补货判据：按港口记的「下次补货时刻」决定等不等（假时钟 + 假读数 + 假账本）
 13 states.json 接线：补货倒计时那一格的位置与参数
 14 卖货链结构：链头 = 出货港核对 + 待卖账=1；两张新模板的阈值；不吃购物表格
 15 卖货收尾：看到结算窗才点确定 + 清账 + 停止；买货链结算后把账置 1
 16 卖货模块启动路径：只认 sell_port，空/拼错/和 buy_port 混用都拒绝
 17 真图判据：买卖两条链不抢画面、灰「出售」点不下去、没账链头不走
 18 港口名 OCR 的预处理（otsu）：汉堡/北京读得对、别的区域没被顺手改、花画面不脑补出港名
 19 结算窗收尾：锚点定窗 + 窗内匹配「确定」，落点用实机那帧验（不再靠人量像素）
 20 点启动时「船现在停在哪个港」：OCR 读到的港名认成第几站
 21 白栏判据的解析：dumpsys input_method 那两行字段（真字段样例，不碰 ADB）
 22 ensure_input：点框 → 认栏 → 打字 → OCR 读回核对，不通就重来（重试环）
 23 confirm_city_move：先核对选中的城市 = 本次港口，再等按钮真出现才点
 24 接线：sail_pick_city 的走位、两块新 OCR 区域、前端认这两种新动作
"""
import json
import os
import shutil
import sys
import types

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 项目根 = selfcheck/ 的上一级（换目录/换机器都不用改）
FIX = os.path.join(BASE, "selfcheck", "fixtures")   # 自检要用的真图一律钉在这里（T-060）：不读 %TEMP%、不读实跑留下的调试图
sys.path.insert(0, BASE)
import cv2  # noqa: E402

import state_machine  # noqa: E402
from state_machine import StateMachineEngine  # noqa: E402

TMP = os.environ.get("TEMP", "/tmp")   # 换机器也不用改（和 route_format 那份同款写法）
SYN = os.path.join(TMP, "uwo_synth_interrupted.png")

FAILS = []


def check(label, cond, detail=""):
    print("  %-4s %s %s" % ("PASS" if cond else "FAIL", label, detail))
    if not cond:
        FAILS.append(label)


class FakeCtl:
    def __init__(self):
        self.clicks = []
        self.keys = []
        # 白栏判据的假答案：ensure_input 每轮都会问一次（真实实现走 dumpsys，这里不碰 ADB）。
        # bar_calls 记下问了几次 —— 「每轮都真去认过栏」正是这次修复的核心，不能只靠代码里有一行。
        self.bar = (True, "假：可见位 V / y=806")
        self.bar_calls = 0

    def click(self, x, y):
        self.clicks.append((x, y))

    def send_key(self, code, hold_ms=80):
        self.keys.append(code)
        return True, "fake"

    def input_text(self, text):
        self.clicks.append(("input", text))

    def set_adbkeyboard(self):
        self.clicks.append(("ime",))

    def clear_text(self):
        self.clicks.append(("clear",))

    def input_bar_state(self):
        self.bar_calls += 1
        return self.bar


def make_engine(next_shot):
    """造一个引擎：截图全部退回 next_shot 那张，点击记在 ctl.clicks 里。"""
    data = state_machine.load_states()
    eng = StateMachineEngine("fake-adb", 16384, next_shot)
    eng.config = data["config"]
    eng.states = data["states"]
    eng.run_port = "汉堡"
    ctl = FakeCtl()

    def fake_snap(_ctl, path, what="截图"):
        shutil.copyfile(next_shot, path)
        return True, "fake"
    eng._snap = fake_snap
    return eng, ctl


def cond_of(sid):
    return next(s for s in eng.states if s["id"] == sid)["condition"]


def actions_of(states, sid):
    return next(s for s in states if s["id"] == sid)["actions"]


# ---- 合成一张「自动航行中断」的画面：把中断模板贴回它自己的 roi 位置 ----
tpl = next(t for t in json.load(open(os.path.join(BASE, "templates.json"), encoding="utf-8"))
           if t["name"] == "UI-海上-重启自动移动")
img = cv2.imread(os.path.join(FIX, "uwo_172000_sailing.png"))
patch = cv2.imread(os.path.join(BASE, tpl["image"]))
tx, ty, tw, th = tpl["roi"]
img[ty:ty + patch.shape[0], tx:tx + patch.shape[1]] = patch
cv2.imwrite(SYN, img)

SAIL = os.path.join(FIX, "uwo_172000_sailing.png")
ARR = os.path.join(FIX, "uwo_arr_1755.png")
OFF = os.path.join(FIX, "uwo_move_02_after_key1.png")
MAP = os.path.join(FIX, "uwo_160909_01_before_row.png")

eng = None
print("=" * 72)
print("1) 正常航行画面：voyage 命中，「等到港」这一步只看不点，等不到就明确停 + 喊人")
eng, ctl = make_engine(SAIL)
check("voyage 条件命中", eng._check_condition(cond_of("voyage"), SAIL))
VOY_ACTS = actions_of(eng.states, "voyage")
waits = [a for a in VOY_ACTS if a.get("type") == "wait_arrival"]
check("到港改由 wait_arrival 当场认（不再是「连续无匹配」撞墙收尾），且它是第一步",
      len(waits) == 1 and VOY_ACTS[0]["type"] == "wait_arrival",
      str([a.get("type") for a in VOY_ACTS]))
w = waits[0]
VOY_WA = w   # 后面 5c 还要拿这份「等到港」动作验中断，别重写一份（两处说法迟早对不上）
check("判据用灯塔（和三条链头同一张判据）", w.get("name") == "UI-港口标志", str(w.get("name")))
check("等待上限 40 分钟，和补货倒计时同一口径",
      w.get("max_wait_seconds") == 2400, str(w.get("max_wait_seconds")))
check("动作外壳的超时比等待上限宽（否则每趟都误报一句「动作超时」）",
      int(w.get("timeout_seconds", 0)) > int(w["max_wait_seconds"]), str(w.get("timeout_seconds")))
check("阻塞期间带了一份完整的中断看门狗（retry_watch）照看航行",
      (w.get("interrupt") or {}).get("type") == "retry_watch",
      str((w.get("interrupt") or {}).get("type")))
# 真等 40 分钟自检就废了：把上限压到 1 秒，验的是「等不到时怎么办」这条路
fast = dict(w, max_wait_seconds=1, poll_interval_ms=0)
msg = eng._do_action(fast, ctl, SAIL)
check("一直在海上：返回里说清是没等到", "超时" in msg, msg[:60])
check("一次点击都没发（等待本身不动画面）", ctl.clicks == [], str(ctl.clicks))
check("等不到 = 明确停止，写清为什么", "没到港" in (eng.stop_reason or ""), str(eng.stop_reason))
check("等不到要喊人（弹窗挂在 _loop 退出时弹，不卡线程）", eng._alert_request is not None)
check("没设跳转（停住就行，不该跳到下一段）", eng._pending_goto is None)

print("=" * 72)
print("1b) 到港画面：wait_arrival 当场认出「到了」，一次点击都不发")
eng, ctl = make_engine(ARR)
r = eng._do_action(dict(w, max_wait_seconds=1, poll_interval_ms=0), ctl, ARR)
check("一眼认出已到港", "已到港" in r, r[:70])
check("认出来没点任何东西", ctl.clicks == [], str(ctl.clicks))
check("这是好消息，不是失败：没置停止位", eng._stop_event.is_set() is False)
check("没挂弹窗请求", eng._alert_request is None)

print("=" * 72)
print("2) 到港画面：voyage 不该命中，sail_office 也不该命中")
eng, ctl = make_engine(ARR)
check("voyage 不命中", not eng._check_condition(cond_of("voyage"), ARR))
check("sail_office 不命中", not eng._check_condition(cond_of("sail_office"), ARR))
check("in_port 的灯塔判据命中（该由港口链接手）",
      eng._check_condition({"type": "template", "name": "UI-港口标志"}, ARR))

print("=" * 72)
print("3) 出港所画面：只有 sail_office 命中")
eng, ctl = make_engine(OFF)
check("sail_office 命中", eng._check_condition(cond_of("sail_office"), OFF))
check("voyage 不命中", not eng._check_condition(cond_of("voyage"), OFF))
check("in_port 灯塔判据不命中",
      not eng._check_condition({"type": "template", "name": "UI-港口标志"}, OFF))
r = eng._do_action({"type": "click_template", "name": "UI-补给后出航"}, ctl, OFF)
# 随机落点不能拿「离中心多远」来断言（引擎本来就在模板矩形内随机取点）。
# 正确的断言是：每次落点都必须还在「补给后出航」这个按钮的矩形里。
BTN = (1388, 738, 188, 90)   # 该按钮 roi：x0,y0,w,h
bad = []
for i in range(20):
    ctl.clicks.clear()
    eng._do_action({"type": "click_template", "name": "UI-补给后出航"}, ctl, OFF)
    if len(ctl.clicks) != 1:
        bad.append(f"第{i}次点了{len(ctl.clicks)}下")
        continue
    px, py = ctl.clicks[0]
    if not (BTN[0] <= px < BTN[0] + BTN[2] and BTN[1] <= py < BTN[1] + BTN[3]):
        bad.append(f"第{i}次落点 ({px},{py}) 出了按钮矩形 {BTN}")
check("连点 20 次：每次都只点 1 下、且落点在按钮矩形内", not bad, "; ".join(bad) or r)

print("=" * 72)
print("4) 世界地图画面：sail_pick_city 命中")
eng, ctl = make_engine(MAP)
check("sail_pick_city 命中", eng._check_condition(cond_of("sail_pick_city"), MAP))
check("出港所不命中", not eng._check_condition(cond_of("sail_office"), MAP))

print("=" * 72)
print("5) 合成中断画面：点满 2 次后停止 + 挂弹窗请求")
eng, ctl = make_engine(SYN)
check("voyage 命中（贴了模板也还在海上）", eng._check_condition(cond_of("voyage"), SYN))
act = {"type": "retry_watch", "name": "UI-海上-重启自动移动",
       "click": [802, 811], "click_rect": [692, 782, 220, 57],
       "interval_ms": 0, "max_attempts": 2, "fail_reason": "测试用失败原因"}
msg = eng._do_action(act, ctl, SYN)
check("点了 2 次", len(ctl.clicks) == 2, str(ctl.clicks))
check("落点都在横幅矩形内",
      all(692 <= x <= 912 and 782 <= y <= 839 for x, y in ctl.clicks), str(ctl.clicks))
check("置了停止位", eng._stop_event.is_set())
check("stop_reason = fail_reason", eng.stop_reason == "测试用失败原因", str(eng.stop_reason))
check("挂出了弹窗请求", eng._alert_request is not None, str(eng._alert_request and eng._alert_request[0]))
check("没有真的弹窗（_alert 未被调用）", getattr(eng, "_alert_called", None) is None)
print("     返回消息:", msg)

print("=" * 72)
print("5b) 中断一次就恢复：点 1 次后画面恢复正常，不该停止")
eng, ctl = make_engine(SYN)


def capture_then_normal(_ctl, prefix):
    # 第一次点完之后的重认就当横幅回到了「预计到达时间」
    return SAIL


eng._capture = capture_then_normal
msg = eng._do_action(dict(act, max_attempts=5), ctl, SYN)
check("只点了 1 次", len(ctl.clicks) == 1, str(ctl.clicks))
check("没停止", eng._stop_event.is_set() is False)
check("没挂弹窗", eng._alert_request is None)
print("     返回消息:", msg)

print("=" * 72)
print("5c) wait_arrival 阻塞期间也照看得见中断（不能一进等待就变成聋子）")
eng, ctl = make_engine(SYN)
w2 = dict(VOY_WA, max_wait_seconds=5, poll_interval_ms=0,
          interrupt=dict(VOY_WA["interrupt"], interval_ms=0, max_attempts=1))
msg = eng._do_action(w2, ctl, SYN)
check("中断时确实点了「重启自动移动」（点完重认还是中断 -> 1 次）",
      len(ctl.clicks) == 1, str(ctl.clicks))
check("点不回来就中止等待，不白等 40 分钟", "中断没救回来" in msg, msg[:60])
check("停止原因来自看门狗（人看得懂是航行中断）",
      "重启自动移动" in (eng.stop_reason or ""), str(eng.stop_reason))
check("弹窗请求挂上了", eng._alert_request is not None)

print("=" * 72)
print("6) goto：只设标记，不碰画面")
eng, ctl = make_engine(SAIL)
r = eng._do_action({"type": "goto", "state": "in_port"}, ctl, SAIL)
check("_pending_goto = in_port", eng._pending_goto == "in_port", r)
check("没有点击", ctl.clicks == [])
try:
    eng._do_action({"type": "goto", "state": "no_such_state"}, ctl, SAIL)
    check("目标不存在要报错", False)
except ValueError as e:
    check("目标不存在要报错", True, str(e))

# =====================  模块这一层  =====================
SAIL_IDS = {"sail_from_port", "sail_office", "sail_open_map", "sail_pick_city", "voyage"}
BUY_IDS = {"in_port", "enter_exchange", "check_restock", "buy_at_exchange"}


def fresh(shot=SAIL):
    """能跑 start() 的引擎，但把后台循环换成空函数：只看 start() 往引擎里装了什么。"""
    e = StateMachineEngine("fake-adb", 16384, shot)
    e._loop = lambda: None
    return e


print("=" * 72)
print("7) start(module) 把 self.states 裁成本模块那几份")
e = fresh()
r = e.start(module="sail", buy_port="汉堡", sail_port="北京", current_port="汉堡")
check("sail 模块启动成功", r["ok"], r["message"])
got = {s["id"] for s in e.states}
check("装进来的正好是移动链 5 个状态", got == SAIL_IDS, str(sorted(got)))
check("买货状态一条都没进来（跨模块从结构上就过不去）", not (got & BUY_IDS))
check("run_module = sail", e.run_module == "sail", str(e.run_module))
check("status() 里能看到本次模块", e.status().get("module") == "sail")
check("current_state 落在移动链头", e.current_state == "sail_from_port", str(e.current_state))
e.stop()

print("=" * 72)
print("8) 「本次港口」取哪个字段由模块自己定，界面不替它猜")
check("sail 模块吃 sail_port", e.run_port == "北京", str(e.run_port))
check("sail 不套购物表格", e.run_rows == [], str(e.run_rows))
logs = " | ".join(x["message"] for x in e._logs)
check("启动日志说清了当前所在港（和目的地不是同一个）",
      "当前所在港『汉堡』" in logs and "已确认与目的港不是同一个" in logs, logs)

FAKE_ROWS = [
    {"port": "汉堡", "goods_name": "啤酒", "cargo_type": "货物-啤酒"},
    {"port": "汉堡", "goods_name": "油画", "cargo_type": "货物-油画"},
    {"port": "汉堡", "goods_name": "铜版画", "cargo_type": "货物-铜版画"},
    {"port": "北京", "goods_name": "中国画", "cargo_type": "货物-中国画"},
    {"port": "汉堡", "goods_name": "走私货", "cargo_type": "没这个类别"},
]
fake_plan = types.SimpleNamespace(
    load_plan=lambda: {"rows": FAKE_ROWS},
    rows_for_port=lambda plan, port: [x for x in plan["rows"] if x["port"] == port],
    row_key=lambda row: row["goods_name"],
    CARGO_TYPES={"货物-啤酒", "货物-油画", "货物-铜版画", "货物-中国画"},
)
real_plan = state_machine.purchase_plan
state_machine.purchase_plan = fake_plan
try:
    e = fresh()
    # 2026-09-27 分工改了：表格只是目录，本次买哪几件、什么顺序，来自跑商设置当前那一站的 goods
    r = e.start(module="buy", buy_port="汉堡", buy_goods=["铜版画", "啤酒"], sail_port="北京")
    check("buy 模块启动成功", r["ok"], r["message"])
    got = {s["id"] for s in e.states}
    check("装进来的正好是买货链 4 个状态（2026-09-28 多了 check_restock）", got == BUY_IDS, str(sorted(got)))
    check("buy 模块吃 buy_port（sail_port 给了也不算）", e.run_port == "汉堡", str(e.run_port))
    check("本次只有勾的 2 件，不是该港全部 3 行", len(e.run_rows) == 2, str(e.run_rows))
    check("买货顺序 = 跑商设置里勾选的顺序，不是表格里的先后",
          [x["goods_name"] for x in e.run_rows] == ["铜版画", "啤酒"],
          str([x["goods_name"] for x in e.run_rows]))
    check("类别仍然从目录那行取（run_rows 带着 cargo_type）",
          e.run_rows[0]["cargo_type"] == "货物-铜版画", str(e.run_rows[0]))
    check("current_state 落在买货链头", e.current_state == "in_port", str(e.current_state))
    logs = " | ".join(x["message"] for x in e._logs)
    check("启动日志写明清单来自当前这一站、挂几件",
          "本次买货港『汉堡』" in logs and "挂 2 件" in logs, logs)
    e.stop()

    print("=" * 72)
    print("9) 该拒绝启动的组合，一个都不能放过去")
    e = fresh()
    r = e.start(module="buy", buy_port="上海", buy_goods=["啤酒"])
    check("购物表格里没这个港口 -> 拒绝", not r["ok"], r["message"])

    e = fresh()
    r = e.start(module="buy", buy_port="汉堡", buy_goods=[])
    check("当前这一站一件货都没勾 -> 拒绝（表格有货也不算）",
          not r["ok"] and "没勾任何货物" in r["message"], r["message"])

    e = fresh()
    r = e.start(module="buy", buy_port="汉堡", buy_goods=["   "])
    check("只交一串空格过去 = 当成没勾，拒绝", not r["ok"] and "没勾任何货物" in r["message"],
          r["message"])

    e = fresh()
    r = e.start(module="buy", buy_port="汉堡", buy_goods=["啤酒", "不存在的东西"])
    check("挂的货在该港目录里查不到 -> 拒绝并说清该港有什么",
          not r["ok"] and "不存在的东西" in r["message"] and "油画" in r["message"], r["message"])

    e = fresh()
    r = e.start(module="buy", buy_port="汉堡", buy_goods=["中国画"])
    check("北京才有的画不能在汉堡下单（按港查目录，跨港不算）",
          not r["ok"] and "查不到" in r["message"], r["message"])

    e = fresh()
    r = e.start(module="buy", buy_port="汉堡", buy_goods=["走私货"])
    check("目录里那一行的类别非法 -> 拒绝", not r["ok"] and "类别非法" in r["message"],
          r["message"])

    # 2026-09-27 你拍板：买货模块没选港口 = 拒绝启动，不留着空跑
    e = fresh()
    r = e.start(module="buy", buy_port="")
    check("买货模块没选买货港 -> 拒绝（不许空着起来一步货都不买）",
          not r["ok"] and "buy_port 是空的" in r["message"], r["message"])
    check("拒绝时没有把状态机装成运行中", e.running is False and e.states == [])

    e = fresh()
    r = e.start(module="buy", buy_port="   ")
    check("买货港只有一串空格 -> 一样拒绝（空格不算选了港）", not r["ok"], r["message"])
finally:
    state_machine.purchase_plan = real_plan

e = fresh()
r = e.start(module="sail", buy_port="汉堡", sail_port="")
check("移动模块要把港名打进搜索框，目的港空 -> 拒绝", not r["ok"], r["message"])

# 2026-09-27 你拍板加的校验：出港要花真金币，目的地就是当前所在港 = 原地打转
e = fresh()
r = e.start(module="sail", sail_port="北京", current_port="北京")
check("移动目的地 == 当前所在港 -> 拒绝（同港不叫移动）",
      not r["ok"] and "同港不叫移动" in r["message"], r["message"])

e = fresh()
r = e.start(module="sail", sail_port="北京", current_port=" 北京 ")
check("当前所在港两边带空格也认得是同一个港 -> 拒绝", not r["ok"], r["message"])

e = fresh()
r = e.start(module="sail", sail_port="北京", current_port="")
check("没填当前所在港 -> 不拦（只是少了这一条核对），日志里要写明",
      r["ok"] and any("没法核对是否原地打转" in x["message"] for x in e._logs),
      r["message"])
e.stop()

e = fresh()
r = e.start(module="nope", sail_port="北京")
check("模块名不存在 -> 拒绝", not r["ok"], r["message"])

real_load = state_machine.load_states


def mk(sid, module=None, entry=False, glob=False, acts=()):
    st = {"id": sid, "name": sid,
          "condition": {"type": "template", "name": "UI-海上", "threshold": 0.8},
          "actions": list(acts), "next": None}
    if module:
        st["module"] = module
    if entry:
        st["entry"] = True
    if glob:
        st["global"] = True
    return st


def use(states):
    state_machine.load_states = lambda: {"config": {}, "states": states}


try:
    use([mk("a", entry=True), mk("b", module="buy", entry=True)])
    e = fresh()
    r = e.start(module="buy", buy_port="汉堡")
    check("有状态没填 module -> 拒绝启动", not r["ok"], r["message"])

    use([mk("a", module="buy"), mk("b", module="buy")])
    e = fresh()
    r = e.start(module="buy")
    check("模块里没有入口状态 -> 拒绝", not r["ok"], r["message"])

    use([mk("a", module="solo", entry=True)])
    e = fresh()
    r = e.start(buy_port=None)
    check("只有一个模块时不用再选一遍 -> 自动选中并启动", r["ok"], r["message"])
    e.stop()

    use([mk("a", module="solo", entry=True), mk("g", glob=True, entry=False)])
    e = fresh()
    r = e.start()
    check("global 状态跟进来（本模块 1 个 + 全局 1 个）",
          r["ok"] and {s["id"] for s in e.states} == {"a", "g"},
          str(sorted({s["id"] for s in e.states})))
    e.stop()

    use([mk("m", module="solo", entry=True,
            acts=[{"type": "goto", "state": "x"}]), mk("x", module="other", entry=True)])
    e = fresh()
    r = e.start(module="solo")
    check("两个模块时按指定的那个裁（另一模块的状态进不来）",
          r["ok"] and {s["id"] for s in e.states} == {"m"},
          r["message"] + " 实际: " + str(sorted({s["id"] for s in e.states})))
    try:
        e._do_action({"type": "goto", "state": "x"}, FakeCtl(), SAIL)
        check("goto 目标在别的模块里 -> 拦下（self.states 已被裁，跨不过去）", False)
    except ValueError as err:
        check("goto 目标在别的模块里 -> 拦下（self.states 已被裁，跨不过去）", True, str(err))
    e.stop()
finally:
    state_machine.load_states = real_load

print("=" * 72)
print("10) 移动链头在港内起步；到港就停，不自动接买货")
eng, ctl = make_engine(ARR)
check("到港画面命中移动链头 sail_from_port",
      eng._check_condition(cond_of("sail_from_port"), ARR))
check("出港所画面不命中链头（不该在出港所再按 1）",
      not eng._check_condition(cond_of("sail_from_port"), OFF))
check("交易所画面不命中链头",
      not eng._check_condition(cond_of("sail_from_port"), MAP))
ctl2 = FakeCtl()
for a in actions_of(eng.states, "sail_from_port"):
    eng._do_action(a, ctl2, ARR)
check("链头只按一次数字 1（sendevent 码 2）", ctl2.keys == [2] and ctl2.clicks == [],
      f"keys={ctl2.keys} clicks={ctl2.clicks}")

data = real_load()
voy = next(s for s in data["states"] if s["id"] == "voyage")
tails = [a for a in voy["actions"] if a.get("type") == "trip_next"]
check("到港那一步交给 trip_next 分流（单模块=停止，整趟=接下一段），不是写死的 goto",
      len(tails) == 1 and tails[0].get("after") == "arrive",
      str([b.get("type") for b in voy["actions"]]))
check("单模块那一份停止文字还在（到港就停、要不要接着买货由人启动）",
      "由人再启动" in (tails[0].get("reason") or ""), str(tails[0].get("reason")))
check("整趟那一份收尾文字也备着（走完最后一站才用得上）",
      bool((tails[0].get("done_reason") or "").strip()), str(tails[0].get("done_reason")))
sail_states = [s for s in data["states"] if s.get("module") == "sail"]
gotos = {n["state"] for s in sail_states for n in state_machine.StateMachineEngine._walk(s["actions"])
         if isinstance(n, dict) and n.get("type") == "goto"}
check("移动模块里的 goto 目标全在自己模块内", gotos <= SAIL_IDS, str(gotos))
check("buy 模块里也没有偷偷 goto 到移动状态",
      {n["state"] for s in data["states"] if s.get("module") == "buy"
       for n in state_machine.StateMachineEngine._walk(s["actions"])
       if isinstance(n, dict) and n.get("type") == "goto"} <= BUY_IDS)

print("=" * 72)
print("11) 画面判据 _classify_page：每张真图认得出是什么页面")
import numpy as np  # noqa: E402
import restock as rs  # noqa: E402

BUY = os.path.join(FIX, "state_screen_buy.png")      # 9-24 实跑留下的购买页真图（已钉进 fixtures）
BLACK = os.path.join(TMP, "uwo_classify_black.png")   # 合成一张什么都没有的画面 = 未知页面
cv2.imwrite(BLACK, np.zeros((900, 1600, 3), np.uint8))

check("购买页真图认出「购买页」（新模板 UI-购买页-标题）",
      "购买页" in eng._classify_page(BUY), "、".join(eng._classify_page(BUY)) or "(未知)")
for path, want in [(SAIL, "海上"), (ARR, "港口"), (OFF, "出港所"), (MAP, "世界地图")]:
    hits = eng._classify_page(path)
    check(f"{os.path.basename(path)} 认出『{want}』", want in hits, "、".join(hits) or "(未知)")
for path, label in [(SAIL, "海上"), (ARR, "港口"), (OFF, "出港所"), (MAP, "世界地图"), (BLACK, "纯黑")]:
    check(f"『{label}』画面不会被误认成购买页（等待环不能认错地方就开买）",
          "购买页" not in eng._classify_page(path), "、".join(eng._classify_page(path)))
check("纯黑合成图 = 未知页面（一个判据都不命中）", eng._classify_page(BLACK) == [],
      str(eng._classify_page(BLACK)))
check("判据表里每张模板都在模板库内（缺模板会被静默跳过，等于少一路判据）",
      all(eng._resolve_template(name)[0] for _, name in eng.PAGE_MARKERS),
      str([n for _, n in eng.PAGE_MARKERS if not eng._resolve_template(n)[0]]))

print("=" * 72)
print("12) 补货判据：先查这个港记的「下次补货时刻」再决定等不等（假时钟 + 假读数 + 假账本）")
import time as _time  # noqa: E402


class FakeClock:
    """假时钟：只有 _sleep_interruptible 会推进它，所以「等 30 分钟」的用例也几毫秒跑完。"""

    def __init__(self, start=1000000.0):
        self.now = start

    def time(self):
        return self.now

    def strftime(self, fmt, *a):
        return _time.strftime(fmt, _time.localtime(self.now))

    def sleep(self, secs):
        self.now += secs


START = 1000000.0                                            # 假时钟的「现在」，下面的时刻都拿它算
TMP_LEDGER = os.path.join(TMP, "uwo_module_selfcheck_restock_ledger.json")


def restock_case(reads, action=None, shot=BUY, classify=None, ledger=None,
                 pre_stop=False, port="汉堡"):
    """跑一次买货前的补货判断。

    reads  : 每次 OCR 读到的那串（用完就一直给最后一条）
    ledger : 假 run_state.json 的内容（字典）；None = 文件还不存在
    返回值里带 ocr_calls，好核对「等待期间一次都不读画面」这条新规则。
    """
    e, ctl = make_engine(shot)
    e.run_port = port
    clock = FakeClock(START)
    real = (state_machine.time, state_machine.ocr_text_by_region, state_machine.ocr_find,
            state_machine.run_state.RUN_STATE_JSON)
    slept, ocr_calls = [], []
    seq = list(reads)

    def fake_ocr(path, region):
        ocr_calls.append(region)
        return seq.pop(0) if len(seq) > 1 else (seq[0] if seq else "")

    def fake_find(path, keyword, roi=None, **kw):
        return {"found": True, "text": keyword, "cx": 60, "cy": 150}

    state_machine.time = clock
    state_machine.ocr_text_by_region = fake_ocr
    state_machine.ocr_find = fake_find
    # 落盘路径改到临时文件：这一格动作会写账本，绝不能碰项目里那份真 run_state.json
    state_machine.run_state.RUN_STATE_JSON = TMP_LEDGER
    if os.path.exists(TMP_LEDGER):
        os.remove(TMP_LEDGER)
    if ledger is not None:
        with open(TMP_LEDGER, "w", encoding="utf-8") as f:
            json.dump(ledger, f, ensure_ascii=False)
    if classify is not None:
        e._classify_page = lambda p: classify
    if pre_stop:
        e._stop_event.set()
    e._sleep_interruptible = lambda s: (slept.append(s), setattr(clock, "now", clock.now + s))
    try:
        msg = e._do_wait_restock(action or {}, ctl, shot)
    finally:
        (state_machine.time, state_machine.ocr_text_by_region, state_machine.ocr_find,
         state_machine.run_state.RUN_STATE_JSON) = real
    return e, ctl, msg, clock, slept, ocr_calls


def ledger_now():
    """把假账本读回来，看动作到底往文件里写了什么。"""
    with open(TMP_LEDGER, encoding="utf-8") as f:
        return json.load(f)


e, ctl, msg, clock, slept, calls = restock_case(["00:09:22"])
check("账本没记过（今天第一次来买）-> 一秒都不等，直接放行",
      "没等补货" in msg and slept == [], msg)
check("判据说清了为什么不等（用户报的那个错：每天首次购买本来就已经刷过）",
      e.restock["decision"] == "no_record" and "今天第一次来买" in e.restock["decision_text"],
      e.restock.get("decision_text", ""))
check("不点「购买」标签（没等到点，列表不用刷）", ctl.clicks == [], str(ctl.clicks))
check("买货这一刻只读一次倒计时", calls == [rs.COUNTDOWN_REGION], str(calls))
check("读到的 562 秒换算成时刻、按港口记进账本",
      ledger_now()["restock_at"] == {"汉堡": int(START) + 562}, str(ledger_now()))
check("状态里带上新旧两个时刻和写盘成功标记",
      e.restock["phase"] == "direct" and e.restock["next_at"] == int(START) + 562
      and e.restock["saved"] is True, str(e.restock))
check("没停止、没挂弹窗", e._stop_event.is_set() is False and e._alert_request is None)

e, ctl, msg, clock, slept, calls = restock_case(
    ["00:00:30"], ledger={"sell_pending": True, "restock_at": {"热那亚": int(START) + 9999}})
check("别的港记的时刻卡不住这个港（粒度 = 按港口分别记）", "没等补货" in msg and slept == [], msg)
after = ledger_now()
check("两个港各记各的，谁也不覆盖谁",
      after["restock_at"] == {"热那亚": int(START) + 9999, "汉堡": int(START) + 30}, str(after))
check("写补货时刻不替人删开关（sell_pending 还在）", after["sell_pending"] is True, str(after))

e, ctl, msg, clock, slept, calls = restock_case(
    ["00:29:59"], ledger={"restock_at": {"汉堡": int(START) - 8 * 3600}})
check("记的时刻是 8 小时前（隔夜离线）-> 已经过了，直接买、不等",
      "没等补货" in msg and slept == [] and e.restock["decision"] == "passed", msg)
check("界面那句话点了「已经到了」和当时的钟点",
      "已经到了" in e.restock["decision_text"] and e.restock["stored_text"].count(":") == 2,
      f"{e.restock['decision_text']}／{e.restock['stored_text']}")
check("买完这一次照样把时刻往前推（下一次来才会等）",
      ledger_now()["restock_at"] == {"汉堡": int(START) + 1799}, str(ledger_now()))

e, ctl, msg, clock, slept, calls = restock_case(
    ["00:29:59"], {"poll_interval_ms": 20000}, ledger={"restock_at": {"汉堡": int(START) + 90}})
check("记的时刻还在将来 -> 本地等到那个点才买", "已等到补货" in msg, msg)
check("等待期间一次都不读画面倒计时（新口径：信账本，不信那串数字）",
      calls == [rs.COUNTDOWN_REGION], str(calls))
check("睡的是「还差多少」，不多睡一秒（90 秒 = 20+20+20+20+10，再等页面切换 1.2）",
      slept == [20.0, 20.0, 20.0, 20.0, 10.0, 1.2], str(slept))
check("到点后恰好点一次「购买」标签（游戏不会自己刷列表）", ctl.clicks == [(60, 150)], str(ctl.clicks))
check("等完才读那一次，记的是「读的那一刻 + 29 分 59 秒」",
      ledger_now()["restock_at"] == {"汉堡": int(START) + 91 + 1799}, str(ledger_now()))
check("状态：phase=ready、decision=pending、等了 91 秒",
      e.restock["phase"] == "ready" and e.restock["decision"] == "pending"
      and e.restock["waited_seconds"] == 91, str(e.restock))

e, ctl, msg, clock, slept, calls = restock_case(
    [""], ledger={"restock_at": {"汉堡": int(START) - 5}})
check("买货那一刻读不出数字 -> 不拦买货，按 30 分钟把时刻记晚（宁可多等，不可早买）",
      "没等补货" in msg and e.restock["source"] == "fallback"
      and ledger_now()["restock_at"] == {"汉堡": int(START) + 1800}, str(ledger_now()))

e, ctl, msg, clock, slept, calls = restock_case(
    ["00:00:10"], ledger={"sell_pending": True, "restock_at": "明天下午"})
check("账本被手工改坏 -> 按「没记过」处理，先买、不干等", "没等补货" in msg and slept == [], msg)
check("改坏这件事留了一条 warn，不是悄悄吞掉",
      any(l["event"] == "warn" and "补货账本读不出" in l["message"] for l in e._logs),
      str([l["message"] for l in e._logs if l["event"] == "warn"]))
check("脏字段被这一次写盘换成合法的一张表，开关没被牵连",
      ledger_now() == {"sell_pending": True, "restock_at": {"汉堡": int(START) + 10}}, str(ledger_now()))

e, ctl, msg, clock, slept, calls = restock_case(
    ["00:00:05"], {"poll_interval_ms": 20000}, classify=[],
    ledger={"restock_at": {"汉堡": int(START) + 1200}})
check("等待期间画面连续 120 秒认不出 -> 报告并停止",
      "认不出画面" in msg and e._stop_event.is_set(), msg)
check("报告时挂了人工提醒，正文点明可能是弹窗盖住",
      e._alert_request is not None and "弹窗" in e._alert_request[1], str(e._alert_request))
check("这种情况一件货都不该买：不点标签、也不动账本里记好的时刻",
      ctl.clicks == [] and e.restock["phase"] == "failed"
      and ledger_now()["restock_at"] == {"汉堡": int(START) + 1200}, str(e.restock))
check("是连续累计：认不出 20 秒时只记一条 warn，不当场报告",
      sum(1 for l in e._logs if l["event"] == "warn" and "认不出任何已知页面" in l["message"]) == 1,
      str([l["message"] for l in e._logs if l["event"] == "warn"]))

e, ctl, msg, clock, slept, calls = restock_case(
    ["00:00:05"], {"poll_interval_ms": 20000, "max_wait_seconds": 60},
    ledger={"restock_at": {"汉堡": int(START) + 1200}})
check("账本里的时刻远到超过等待上限 -> 不闷头等，报告并停止",
      "等待失败" in msg and e._stop_event.is_set() and ctl.clicks == [], msg)
check("报告正文指着账本和系统时间让人去看（不是只说「超时」）",
      e._alert_request is not None and "run_state.json" in e._alert_request[1], str(e._alert_request))
check("上限写进状态，界面能显示「最多等多久」", e.restock["max_wait_seconds"] == 60, str(e.restock))

e, ctl, msg, clock, slept, calls = restock_case(
    ["00:00:05"], pre_stop=True, ledger={"restock_at": {"汉堡": int(START) + 90}})
check("人已经按下停止 -> 第一轮就退出等待：不截图、不点标签、不报错",
      "已手动停止" in msg and ctl.clicks == [] and e.restock is None
      and slept == [] and calls == [], msg)
check("停止这一路不挂弹窗（人就在跟前，不用喊）", e._alert_request is None)

e, ctl, msg, clock, slept, calls = restock_case(["00:00:05"], pre_stop=True)
check("停止之后连「本来不用等」这条路也不去读画面、不改账本",
      "已手动停止" in msg and calls == [] and not os.path.exists(TMP_LEDGER), msg)

e, ctl, msg, clock, slept, calls = restock_case(["00:00:20"], port="")
check("没有港口名（单模块跑法没选港）-> 只读不记，照样放行买货",
      "没等补货" in msg and calls == [rs.COUNTDOWN_REGION] and not os.path.exists(TMP_LEDGER), msg)
check("「没记上」这件事说在日志里了（不是悄悄丢）",
      any(l["event"] == "warn" and "没有港口名" in l["message"] for l in e._logs),
      str([l["message"] for l in e._logs if l["event"] == "warn"]))
check("状态里 saved=False，界面会显示「没写进文件」", e.restock["saved"] is False, str(e.restock))
check("状态字段齐了（界面只念这些，不自己算）",
      {"phase", "port", "decision", "decision_text", "stored_at", "stored_text",
       "remaining_seconds", "remaining_text", "source", "read_text", "next_at", "next_text",
       "saved", "region", "page", "polls", "waited_seconds", "max_wait_seconds", "updated_at"}
      <= set(e.restock), str(sorted(set(e.restock))))

if os.path.exists(TMP_LEDGER):
    os.remove(TMP_LEDGER)

print("=" * 72)
print("13) states.json 接线：倒计时只读一次、位置对、参数不跟常量漂移")
real = state_machine.load_states()
by_id = {s["id"]: s for s in real["states"]}
order = [s["id"] for s in real["states"]]
check("多了 check_restock 这个状态，属于 buy 模块、是入口",
      "check_restock" in by_id and by_id["check_restock"].get("module") == "buy"
      and by_id["check_restock"].get("entry") is True, str(by_id.get("check_restock", {}))[:80])
check("进交易所之后先进倒计时这一格，不是直接开买",
      by_id["enter_exchange"]["next"] == "check_restock", by_id["enter_exchange"]["next"])
check("等完倒计时才进 buy_at_exchange",
      by_id["check_restock"]["next"] == "buy_at_exchange", by_id["check_restock"]["next"])
check("buy_at_exchange 还是自己循环（所以一趟运行只在开头读一次倒计时，"
      "不会每买一行就再等半小时）",
      by_id["buy_at_exchange"]["next"] == "buy_at_exchange", by_id["buy_at_exchange"]["next"])
check("入口检测按 states 顺序取第一个命中：check_restock 排在 buy_at_exchange 之前",
      order.index("check_restock") < order.index("buy_at_exchange"), str(order))
act = by_id["check_restock"]["actions"][0]
check("这一格只有一个动作，就是 wait_restock（不许顺手点别的）",
      len(by_id["check_restock"]["actions"]) == 1 and act["type"] == "wait_restock",
      str([a["type"] for a in by_id["check_restock"]["actions"]]))
check("动作自带的超时 > 它的最长等待（否则每轮都会误报一句「动作超时」）",
      act["timeout_seconds"] > act["max_wait_seconds"],
      f"{act['timeout_seconds']} vs {act['max_wait_seconds']}")
check("配置里的上限和模块里的常量是一个数（别两处各写一份）",
      act["max_wait_seconds"] == rs.MAX_WAIT_SECONDS and act["unknown_seconds"] == rs.UNKNOWN_PAGE_SECONDS,
      f"{act['max_wait_seconds']}/{rs.MAX_WAIT_SECONDS}  {act['unknown_seconds']}/{rs.UNKNOWN_PAGE_SECONDS}")
check("倒计时判据用的是新模板 + 0.9 阈值（只裁文字，防串档）",
      by_id["check_restock"]["condition"]["name"] == "UI-购买页-标题"
      and by_id["check_restock"]["condition"]["threshold"] == 0.9,
      str(by_id["check_restock"]["condition"]))
check("加了这个状态之后，买货模块的入口从 3 个变成 4 个",
      sum(1 for s in real["states"] if s.get("module") == "buy" and s.get("entry")) == 4)
check("移动模块一个都没被牵连（sail 还是 5 个状态）",
      sum(1 for s in real["states"] if s.get("module") == "sail") == 5)

print("=" * 72)
print("14) 卖货链结构：链头认「出货港 + 账上有货」，收尾只在真看到结算窗时清账")
SELL_IDS = {"sell_in_port", "sell_open_tab", "sell_add_all"}
real = state_machine.load_states()
by_id = {s["id"]: s for s in real["states"]}
sell = [s for s in real["states"] if s.get("module") == "sell"]
check("sell 模块正好 3 个状态", {s["id"] for s in sell} == SELL_IDS,
      str(sorted(s["id"] for s in sell)))
check("3 个都是入口（人站在哪张画面上都能起）", all(s.get("entry") for s in sell))
head = by_id["sell_in_port"]["condition"]
subs = {n.get("type"): n for n in head.get("conditions", []) if isinstance(n, dict)}
check("链头是三样一起：港口标志 + 港名核对 + 待卖账=1",
      head.get("type") == "all" and set(subs) == {"template", "plan_port", "flag"},
      str([n.get("type") for n in head.get("conditions", [])]))
check("链头核对的是出货港（from=sell_port），不是买货港",
      subs.get("plan_port", {}).get("from") == "sell_port"
      and subs.get("plan_port", {}).get("region") == "港口名字", str(subs.get("plan_port")))
check("链头等的是 sell_pending=1（买完货才算有货要卖）",
      subs.get("flag", {}).get("name") == "sell_pending"
      and subs.get("flag", {}).get("equals") is True, str(subs.get("flag")))
check("链头只按一次数字 3 进交易所",
      [a.get("type") for a in by_id["sell_in_port"]["actions"]] == ["key"]
      and by_id["sell_in_port"]["actions"][0]["digit"] == 3,
      str(by_id["sell_in_port"]["actions"]))
check("顺序是 港内 → 点出售标签 → 出售页操作（下一站写死，不靠画面猜）",
      by_id["sell_in_port"]["next"] == "sell_open_tab"
      and by_id["sell_open_tab"]["next"] == "sell_add_all"
      and by_id["sell_add_all"]["next"] == "sell_add_all",
      f"{by_id['sell_in_port']['next']}/{by_id['sell_open_tab']['next']}")
tab = by_id["sell_open_tab"]["actions"][0]
check("点的是「出售」标签、区域是那条左侧标签栏（不是买货那个「购买」）",
      tab.get("type") == "click_ocr" and tab.get("keyword") == "出售"
      and tab.get("region") == "购买出售标签", str(tab))
adds = {a.get("name"): a for a in by_id["sell_add_all"]["actions"]
        if a.get("type") in ("click_template", "click_template_optional")
        and a.get("name") in ("UI-全部添加", "UI-出售")}
check("出售页点两张新模板：全部添加 + 出售",
      set(adds) == {"UI-全部添加", "UI-出售"}, str(sorted(adds)))
check("UI-出售 阈值 0.95：灰按钮实测 0.9039，压不住就会点下去（点了就是真卖）",
      adds.get("UI-出售", {}).get("threshold") == 0.95, str(adds.get("UI-出售")))
check("UI-全部添加 阈值 0.9（委托交易品那扇窗的「一键添加」实测 0.6408，不能串）",
      adds.get("UI-全部添加", {}).get("threshold") == 0.9, str(adds.get("UI-全部添加")))
# 2026-09-29 实机第一次栽在这里：点完「全部添加」只等 800ms，按钮还在灰态，
# click_template 看一眼就抛「未找到模板」。这张按钮必须改成会重截屏轮询的动作。
btn = adds.get("UI-出售", {})
check("UI-出售 用的是会重截屏轮询的动作（不是看一眼就走）",
      btn.get("type") == "click_template_optional", str(btn))
check("至少轮 8 秒、每 500ms 内重看一次（变金是动画，不是瞬间）",
      int(btn.get("timeout_ms", 0)) >= 8000 and 0 < int(btn.get("poll_interval_ms", 9999)) <= 500,
      str(btn))
check("等不到金按钮是「跳过」不是「抛异常」：后面还有结算窗守卫兜着",
      btn.get("type") == "click_template_optional", str(btn))
# 卖货链里出现的模板名必须真在模板库里：名字打错 = find_template 直接抛，等于这一步没做
tpl_names = {t["name"] for t in json.load(open(os.path.join(BASE, "templates.json"),
                                               encoding="utf-8"))}
used = sorted({n.get("name") or n.get("anchor")
               for s in sell for n in state_machine.StateMachineEngine._walk(s["actions"])
               if isinstance(n, dict) and (n.get("name") or n.get("anchor"))
               and n.get("type") in ("click_template", "click_template_optional",
                                     "click_after_template", "template")})
check("卖货链引用的模板全在模板库里（缺一个就会静默不点）",
      all(eng._resolve_template(n)[0] and n in tpl_names for n in used),
      f"用到: {used}")
no_plan = [n.get("type") for s in sell
           for n in state_machine.StateMachineEngine._walk(s["actions"] + [s["condition"]])
           if isinstance(n, dict) and n.get("type") in
           ("plan_advance", "plan_pending", "templates_from_plan")]
check("卖货链不吃购物表格（货舱里有什么卖什么，没有清单这回事）",
      not no_plan and not any("templates_from_plan" in json.dumps(s) for s in sell),
      str(no_plan))
sells = {s["id"] for s in sell}
sg = {n["state"] for s in sell for n in state_machine.StateMachineEngine._walk(s["actions"])
      if isinstance(n, dict) and n.get("type") == "goto"}
check("卖货链没有 goto 到别的模块（这里干脆一条 goto 都不用，靠 next 串）",
      sg <= sells, str(sg))

print("=" * 72)
print("15) 卖货收尾：结算窗那一步 = 点确定 → 清账 → 收尾分流；没看到窗就不清账")
tail = [a for a in by_id["sell_add_all"]["actions"] if a.get("type") == "if"]
settle = next((a for a in tail if (a.get("condition") or {}).get("name") == "UI-结算结果-标题"), None)
check("最后有一个「看到结算结果窗才动手」的分支（锚点判据，不是全图找金色确定）",
      settle is not None and settle["condition"]["threshold"] == 0.9, str(tail[-1])[:120])
then = settle.get("then") or []
els = settle.get("else") or []
flags_in_then = [a for a in then if a.get("type") == "set_flag"]
check("这一支按顺序是：锚点定窗+窗内匹配确定 → 待卖账置 0 → 分流收尾（trip_next）",
      [a.get("type") for a in then] == ["click_after_template", "set_flag", "trip_next"],
      str([a.get("type") for a in then]))
check("清完账之后单模块照旧停止（文字里还得是那句「卖货这一趟结束」）",
      then[-1].get("after") == "work" and "卖货这一趟结束" in (then[-1].get("reason") or ""),
      str(then[-1])[:120])
check("置的是 sell_pending=0（不是 1）",
      len(flags_in_then) == 1 and flags_in_then[0].get("name") == "sell_pending"
      and flags_in_then[0].get("value") is False, str(flags_in_then))
check("没看到结算窗 -> 只停止，绝不清账（账还是 1，下次还能接着卖）",
      any(a.get("type") == "stop" for a in els)
      and not any(a.get("type") == "set_flag" for a in els), str(els))
check("结算窗这一步是「锚点定窗 + 窗内匹配 UI-确定」，不是固定偏移（2026-09-29 实机：照买货量的 "
      "[0,318] 在卖货那扇高窗上点到「总额」那行字，窗没关）",
      then[0].get("anchor") == "UI-结算结果-标题" and then[0].get("name") == "UI-确定"
      and "offset" not in then[0], str(then[0]))
nego = [a for a in by_id["sell_add_all"]["actions"]
        if a.get("type") == "if"
        and "协商按钮区" in json.dumps(a.get("condition") or {}, ensure_ascii=False)]
check("协商这一段照买货那套搬过来了（有协商就谈，没有就直接确定）",
      len(nego) == 1 and any(b.get("type") == "negotiation" for b in nego[0].get("then") or []),
      str([a.get("type") for a in nego]))
buy_acts = by_id["buy_at_exchange"]["actions"]
kinds = [a.get("type") for a in buy_acts]
check("买货链在关掉结算窗之后、推进表格游标之前把待卖账置 1",
      kinds.index("set_flag") == kinds.index("click_after_template") + 1
      and kinds.index("set_flag") < kinds.index("plan_advance"), str(kinds))
flag_act = buy_acts[kinds.index("set_flag")]
check("置的是 sell_pending=1", flag_act.get("name") == "sell_pending"
      and flag_act.get("value") is True, str(flag_act))

print("=" * 72)
print("16) 卖货模块的启动路径：只认 sell_port，缺了就拒绝")
e = fresh()
r = e.start(module="sell", sell_port="汉堡", buy_port="北京", current_port="汉堡")
check("sell 模块启动成功", r["ok"], r["message"])
check("装进来的正好是卖货链 3 个状态", {s["id"] for s in e.states} == SELL_IDS,
      str(sorted(s["id"] for s in e.states)))
check("买货 / 移动状态一条都没进来", not ({s["id"] for s in e.states} & (BUY_IDS | SAIL_IDS)))
check("本次港口取的是 sell_port（给了 buy_port 也不算）",
      e.run_port == "汉堡" and e.run_port_field == "sell_port",
      f"{e.run_port} / {e.run_port_field}")
check("不套购物表格（run_rows 是空的）", e.run_rows == [], str(e.run_rows))
check("current_state 落在卖货链头", e.current_state == "sell_in_port", str(e.current_state))
check("status() 里看得到本次港口字段和待卖账",
      e.status().get("run_port_field") == "sell_port" and "run_state" in e.status(),
      str({k: e.status().get(k) for k in ("run_port_field", "run_state")}))
logs = " | ".join(x["message"] for x in e._logs)
check("启动日志说清本次港口来自哪个字段（出货港，不是买货港）",
      "sell_port" in logs and "本次出货港口" in logs, logs)
check("不套用购物表格这句话也写进日志了", "不套用购物表格" in logs, logs)
e.stop()

e = fresh()
r = e.start(module="sell", sell_port="", current_port="北京")
check("卖货模块没选出货港 -> 拒绝（不知道要在哪个港把货卖掉）",
      not r["ok"] and "sell_port 是空的" in r["message"], r["message"])
check("拒绝时没有把引擎装成运行中", e.running is False and e.states == [])

e = fresh()
r = e.start(module="sell", sell_port="   ")
check("出货港只有一串空格 -> 一样拒绝", not r["ok"] and "sell_port 是空的" in r["message"],
      r["message"])

try:
    def pp(frm):
        return {"type": "plan_port", "region": "港口名字", "from": frm}

    use([mk("a", module="sell", entry=True, acts=[pp("sell_port")]),
         mk("b", module="sell", entry=False, acts=[pp("buy_port")])])
    e = fresh()
    r = e.start(module="sell", sell_port="汉堡", buy_port="北京")
    check("同一模块里两处核对认了不同字段 -> 拒绝（一个模块只认一个本次港口）",
          not r["ok"] and "sell_port" in r["message"], r["message"])

    use([mk("a", module="sell", entry=True, acts=[pp("sell_porr")])])
    e = fresh()
    r = e.start(module="sell", sell_port="汉堡")
    check("from 拼错（sell_porr）-> 拒绝并列出可选字段",
          not r["ok"] and "buy_port" in r["message"], r["message"])

    use([mk("a", module="sell", entry=True,
            acts=[pp("sell_port"), {"type": "buy_commodities", "templates_from_plan": True}])])
    e = fresh()
    r = e.start(module="sell", sell_port="汉堡", buy_port="北京", buy_goods=["啤酒"])
    check("既吃表格又按 sell_port 核对 -> 拒绝（货在 A 港、核对在 B 港，永远对不上）",
          not r["ok"] and "buy_port" in r["message"], r["message"])

    use([mk("a", module="sell", entry=True,
            acts=[pp("sell_port"), {"type": "input_text", "text_from": "run_port"}])])
    e = fresh()
    r = e.start(module="sell", sell_port="汉堡", current_port="汉堡")
    check("既核对港名又要把港名打进搜索框 -> 拒绝（这两个用的不是一个港）",
          not r["ok"] and "一个模块只认一个" in r["message"], r["message"])
finally:
    state_machine.load_states = real_load

print("=" * 72)
print("17) 真图判据：买货页 / 卖货页互不抢画面；灰「出售」点不下去；链头没账就不走")
import run_state as rs_mod  # noqa: E402   和引擎里是同一个模块对象，改这一个就够

SELL_DIR = os.path.join(FIX, "sell")
EXCH = os.path.join(SELL_DIR, "02_exchange_default.png")     # 交易所默认页（委托交易品）
SELL_TAB = os.path.join(SELL_DIR, "03_sell_tab.png")         # 刚点进「出售」标签，待售列表是空的
SELL_ADDED = os.path.join(SELL_DIR, "04_sell_all_added.png")  # 全部添加后：出售按钮变金
SELL_CLEAR = os.path.join(SELL_DIR, "05_sell_list_cleared.png")  # 手动清空列表：又变回灰
for p in (EXCH, SELL_TAB, SELL_ADDED, SELL_CLEAR, BUY):
    if not os.path.exists(p):
        check(f"缺真图 {os.path.basename(p)}", False, p)

eng, ctl = make_engine(SELL_TAB)
check("出售页命中 sell_add_all（右侧面板标题写着「出售」）",
      eng._check_condition(cond_of("sell_add_all"), SELL_TAB))
check("同一张出售页不会命中买货那一格（右侧标题不是「购买」）",
      not eng._check_condition(cond_of("buy_at_exchange"), SELL_TAB))
eng, ctl = make_engine(BUY)
check("购买页不会命中 sell_add_all（两条链不抢同一张画面）",
      not eng._check_condition(cond_of("sell_add_all"), BUY))
eng, ctl = make_engine(EXCH)
check("交易所默认页命中 sell_open_tab（该点「出售」标签了）",
      eng._check_condition(cond_of("sell_open_tab"), EXCH))
eng, ctl = make_engine(SELL_TAB)
check("已经进了出售页就不该再点标签（sell_open_tab 在出售页不命中，否则会来回切）",
      not eng._check_condition(cond_of("sell_open_tab"), SELL_TAB))

# 「出售」按钮：只有变金（真的能卖）才点得下去。灰按钮实测 0.9039 < 0.95。
for path, label in [(SELL_TAB, "列表空着（灰）"), (SELL_CLEAR, "列表清空后（灰）"),
                    (BUY, "购买页同槽位的金按钮"), (ARR, "港内画面")]:
    eng, ctl = make_engine(path)
    try:
        eng._do_action({"type": "click_template", "name": "UI-出售"}, ctl, path)
        check(f"{label} 不该点到「出售」", False, f"实际点了 {ctl.clicks}")
    except ValueError as err:
        check(f"{label} 点不到「出售」（阈值拦住）", "未找到模板" in str(err), str(err))
eng, ctl = make_engine(SELL_ADDED)
r = eng._do_action({"type": "click_template", "name": "UI-出售"}, ctl, SELL_ADDED)
check("全部添加之后点得到「出售」（阈值 0.95 不挡真按钮）", len(ctl.clicks) == 1, r)
check("落点还在按钮矩形 [1400,800,100,55] 里",
      all(1400 <= x < 1500 and 800 <= y < 855 for x, y in ctl.clicks), str(ctl.clicks))

# 上面几条是手写动作字典（只验阈值分得开）。这里拿 states.json 里真配的那一条再跑一遍：
# 类型换了、轮询换了，判据就得按配置的那份验，不然改了配置自检还是绿的。
real_btn = next((a for a in by_id["sell_add_all"]["actions"]
                 if a.get("name") == "UI-出售"), None)
fast = dict(real_btn, timeout_ms=300, poll_interval_ms=100)  # 只缩短等待，判据一字不动
eng, ctl = make_engine(SELL_TAB)
r = eng._do_action(fast, ctl, SELL_TAB)
check("配置那条真动作·灰按钮画面：一次都不点，也不抛异常（等不到就走跳过）",
      ctl.clicks == [] and "跳过" in r, str((ctl.clicks, r)))
eng, ctl = make_engine(SELL_ADDED)
r = eng._do_action(fast, ctl, SELL_ADDED)
check("配置那条真动作·全部添加后：点得到且只点一次，落点在按钮矩形里",
      len(ctl.clicks) == 1 and all(1400 <= x < 1500 and 800 <= y < 855 for x, y in ctl.clicks),
      str((ctl.clicks, r)))
eng, ctl = make_engine(SELL_TAB)
r = eng._do_action({"type": "click_template", "name": "UI-全部添加"}, ctl, SELL_TAB)
check("「全部添加」在出售页点得到", len(ctl.clicks) == 1, r)
eng, ctl = make_engine(EXCH)
try:
    eng._do_action({"type": "click_template", "name": "UI-全部添加"}, ctl, EXCH)
    check("委托交易品那扇窗的「一键添加」不能被误当成「全部添加」", False, str(ctl.clicks))
except ValueError as err:
    check("委托交易品窗不会误点（实测 0.6408 < 0.9）", True, str(err))

# 链头那张 all 条件：拿真到港画面 + 临时账本跑一遍（绝不碰项目里的 run_state.json）
TMP_FLAGS = os.path.join(TMP, "uwo_module_selfcheck_run_state.json")
real_flag_path = rs_mod.RUN_STATE_JSON
rs_mod.RUN_STATE_JSON = TMP_FLAGS
try:
    if os.path.exists(TMP_FLAGS):
        os.remove(TMP_FLAGS)
    from vision import ocr_text_by_region  # noqa: E402
    read = (ocr_text_by_region(ARR, "港口名字") or "").strip()
    check("到港画面那块区域 OCR 得到字（拿它当本次港名，才有「对得上」这一半）",
          bool(read), repr(read))
    eng, ctl = make_engine(ARR)
    eng.run_port = read
    eng.run_port_field = "sell_port"
    head_cond = cond_of("sell_in_port")
    rs_mod.set_flag("sell_pending", False)
    check("账上没记着有货要卖 -> 链头不走（哪怕港名对得上）",
          not eng._check_condition(head_cond, ARR))
    rs_mod.set_flag("sell_pending", True)
    check("账上有货 + 港名对得上 -> 链头命中", eng._check_condition(head_cond, ARR))
    eng.run_port = read + "不存在的港"
    check("港名对不上 -> 链头不走（卖货要花掉真货，宁可停）",
          not eng._check_condition(head_cond, ARR))
    eng.run_port = read
    rs_mod.set_flag("sell_pending", False)
    check("换成买货链头：没有待卖账这一条，照样只看港名（flag 条件没混进 buy 模块）",
          "flag" not in json.dumps(cond_of("in_port")), str(cond_of("in_port"))[:80])
finally:
    rs_mod.RUN_STATE_JSON = real_flag_path
    if os.path.exists(TMP_FLAGS):
        os.remove(TMP_FLAGS)

print("=" * 72)
print("18) 港口名 OCR 的预处理（otsu）：链头 plan_port 全靠这一块区域，读错字 = 买货卖货都开不了工")
# 为什么单独一节：2026-09-28 实测『港口名字』不二值化时把 汉堡 读成「汊堡」、北京 读成「兆京」，
# 而 plan_port 是「读到的字要被本次港名包含才算过」—— 读错字时链头会一声不响地拒绝启动，
# 看起来像「画面不对」，其实是预处理在骗人。改法是给这一条区域加 binary_method:"otsu"（数据文件，不用重启）。
from vision import ocr_find_in_region, ocr_text_by_region  # noqa: E402
OCR_REGIONS = json.load(open(os.path.join(BASE, "ocr_regions.json"), encoding="utf-8"))
by_name = {r.get("name"): r for r in OCR_REGIONS}
bm = lambda n: (by_name.get(n, {}).get("preprocess") or {}).get("binary_method")  # noqa: E731
check("『港口名字』是 otsu（改回 null 就等于把两条链的链头关掉）", bm("港口名字") == "otsu", repr(bm("港口名字")))
for other in ["协商按钮区", "购买出售标签", "右侧面板标题", "商品列表", "补货倒计时"]:
    check(f"{other} 没被顺手一起改（那些判据是在不二值化下验的）", bm(other) is None, repr(bm(other)))
HAMBURG_PORT = os.path.join(SELL_DIR, "01_port.png")
check("卖货录制·汉堡港内：找『汉堡』命中（改之前这里读到「汊堡」= False）",
      ocr_find_in_region(HAMBURG_PORT, "汉堡", "港口名字")["found"],
      repr((ocr_find_in_region(HAMBURG_PORT, "汉堡", "港口名字") or {}).get("text")))
check("同一张图拿别的港名去比不能命中（包含判断不是万能钥匙）",
      not ocr_find_in_region(HAMBURG_PORT, "北京", "港口名字")["found"])
if os.path.exists(ARR):
    check("到港真图（汉堡）也命中", ocr_find_in_region(ARR, "汉堡", "港口名字")["found"])
# 港内快照：这份文件每次实跑都会被引擎覆盖（今天就被覆盖成了热那亚那一屏），所以不断言具体是哪个港，
# 只断言「读得出字」。
# 2026-09-29 学到的一课（别再写回去）：这块区域可能不止一行 —— 热那亚那帧读到 ['热那亚', '_丽'] 两行，
# ocr_text_by_region 把它们**拼成一串**返回「热那亚_丽」，而 plan_port 的「包含」是**按单行**判的。
# 所以「拿读到的整串当本次港名去命中」必然 False：那是断言写错，不是链头坏（引擎喂的是计划里的港名，
# 单行『热那亚』命中，实测 True）。要验「包含」这一半就得用行数和字都钉得住的录制素材。
BJ = os.path.join(FIX, "state_screen_exit.png")
read_exit = (ocr_text_by_region(BJ, "港口名字") or "").strip()
check("港内快照 OCR 读得出字（读空 = 区域跑偏或预处理坏掉）", bool(read_exit), repr(read_exit))
read_hamburg = (ocr_text_by_region(HAMBURG_PORT, "港口名字") or "").strip()
check("汉堡录制素材读到的就是『汉堡』（单行，能拿来当自指的本次港名）",
      read_hamburg == "汉堡", repr(read_hamburg))
check("拿读到的字当本次港名：命中（这条测「包含」这一半，素材固定所以换港不会假失败）",
      ocr_find_in_region(HAMBURG_PORT, read_hamburg, "港口名字")["found"], repr(read_hamburg))
for path, label in [(BUY, "购买页"), (EXCH, "交易所默认页"), (SELL_TAB, "出售页")]:
    hit = ocr_find_in_region(path, "汉堡", "港口名字")["found"] or \
          ocr_find_in_region(path, "北京", "港口名字")["found"]
    check(f"{label} 不会脑补出港名（otsu 最坏的就是把花画面认成字）", not hit)

print("=" * 72)
print("19) 结算窗收尾：锚点定窗 + 窗内匹配「确定」，用实机那帧验落点（不再靠人量像素）")
# 为什么改：2026-09-29 实机卖货第一次跑通，结算窗比买货那扇多出六行、整窗往上顶，
# 照买货量的固定偏移 [0,318] 点到了「总额」那行字上 —— 钱已到账但窗没关。
# 固定偏移是魔法数字，换个窗型就废；新点法先证明「这是哪扇窗」，再只在这扇窗的框里找按钮。
from vision import find_template  # noqa: E402
TPL_ENTRY = {t["name"]: t for t in json.load(open(os.path.join(BASE, "templates.json"),
                                                  encoding="utf-8"))}
settle_act = then[0]
SELL_SETTLE = os.path.join(SELL_DIR, "06_settlement_window.png")


def confirm_rect(frame):
    """这张图上的「确定」到底在哪（全图匹配，只当真值用，不是流程走的那条路）。"""
    t = TPL_ENTRY["UI-确定"]
    return find_template(frame, os.path.join(BASE, t["image"]), roi=None, threshold=t["threshold"])


def inside(pt, rect):
    x, y, w, h = rect
    return x <= pt[0] < x + w and y <= pt[1] < y + h


for frame, label in [(SELL_SETTLE, "卖货结算窗（热那亚实机那帧）")]:
    if not os.path.exists(frame):
        check(f"{label} 素材在不在", False, frame)
        continue
    rect = confirm_rect(frame)
    check(f"{label}：真值「确定」认得出（不然这条断言没意义）", rect is not None, str(rect))
    if not rect:
        continue
    eng, ctl = make_engine(frame)
    r = eng._do_action(dict(settle_act, wait_before_ms=0), ctl, frame)
    check(f"{label}：窗内匹配这条动作点下去了，就一次", len(ctl.clicks) == 1, str((ctl.clicks, r)))
    check(f"{label}：落点落在「确定」那颗按钮自己的矩形 {rect['rect']} 里",
          bool(ctl.clicks) and inside(ctl.clicks[0], rect["rect"]), str((ctl.clicks, rect["rect"])))

# 老写法（固定偏移）这条代码路不能因为加了新模式就坏掉 —— 买货链的结算步现在还是老写法。
# 注意这里只验「锚点在 → 点且只点一次 → 落点正好是锚点中心+[0,318]」，
# **不**验「买货窗的确定在不在 [0,318]」：那张素材没了。state_screen_optional.png 是引擎按
# 状态 id 落盘的调试图，2026-09-29 12:08 卖货实跑把它覆盖成了出售页那一帧，买货结算窗跟着丢了。
# 买货链那条落点目前只有历史实测撑着，要重新钉住得等下次买货实跑把素材存进 recordings/（见 待办事项.md）。
if os.path.exists(SELL_SETTLE):
    eng, ctl = make_engine(SELL_SETTLE)
    anchor = find_template(SELL_SETTLE,
                           os.path.join(BASE, TPL_ENTRY["UI-结算结果-标题"]["image"]),
                           roi=None, threshold=0.9)
    old = {"type": "click_after_template", "anchor": "UI-结算结果-标题",
           "offset": [0, 318], "threshold": 0.9}
    eng._do_action(old, ctl, SELL_SETTLE)
    check("老写法（固定偏移）代码路没坏：点一次，落点 = 锚点中心 + [0,318]",
          anchor and ctl.clicks == [(anchor["cx"], anchor["cy"] + 318)],
          str((ctl.clicks, anchor and (anchor["cx"], anchor["cy"]))))
buy_act = None
for st in data["states"]:
    if st.get("module") != "buy":
        continue
    for act in st.get("actions") or []:
        for c in [act] + (act.get("then") or []) + (act.get("else") or []):
            if c.get("type") == "click_after_template":
                buy_act = c
check("买货链的结算步这次没被顺手改（仍是 anchor+offset，没有 name）",
      buy_act and buy_act.get("anchor") == "UI-结算结果-标题"
      and buy_act.get("offset") == [0, 318] and "name" not in buy_act, str(buy_act))

# 存盘这条路也要真走一遍：界面上点一次「保存」是走 app.normalize_state 落盘的，
# 它在 actions 上是原样透传（app.py:833），但「原样透传」是读代码读出来的，得实测。
# 万一以后有人给 actions 加字段白名单，search_box 会被这一保存悄悄吃掉 —— 卖货收尾当场失效。
import app as app_mod  # noqa: E402
saved = app_mod.normalize_state({"id": "sell_add_all", "module": "sell",
                                 "actions": [dict(settle_act)], "condition": None,
                                 "next": None})
check("界面保存这条路过一遍：normalize_state 没把 name/search_box 吃掉",
      saved["actions"][0].get("name") == "UI-确定"
      and saved["actions"][0].get("search_box") == list(settle_act["search_box"]),
      str(saved["actions"]))

# 框不能画太大：金色「确定」在购买面板那一格是 (811,827,180,48)，框进去就又变成两个候选取最高分了
eng, ctl = make_engine(SELL_SETTLE)
a = find_template(SELL_SETTLE, os.path.join(BASE, TPL_ENTRY["UI-结算结果-标题"]["image"]),
                  roi=None, threshold=0.9)
dx, dy, bw, bh = settle_act["search_box"]
bottom = a["cy"] + dy + bh
check(f"search_box 的下边界 {bottom} 没够到购买面板那格确定的顶 (827)（一框只罩一个候选）",
      bottom < 827, f"锚点中心 {a['cx']},{a['cy']} 框 {settle_act['search_box']}")

# 窗根本没弹：只记 warn 跳过，不抛异常也不许点任何东西（抛了会连带后面的清账步骤一起走）
eng, ctl = make_engine(SELL_ADDED)
r = eng._do_action(dict(settle_act, wait_before_ms=0), ctl, SELL_ADDED)
check("结算窗没出现时：一下都不点、也不抛异常（交给后面的守卫去停）",
      ctl.clicks == [] and "跳过" in r, str((ctl.clicks, r)))

for bad, why in [
    ({"anchor": "UI-结算结果-标题", "name": "UI-确定", "offset": [0, 318]}, "name 和 offset 同时给"),
    ({"anchor": "UI-结算结果-标题", "name": "UI-确定"}, "给了 name 没给 search_box"),
    ({"anchor": "UI-结算结果-标题", "name": "UI-确定", "search_box": [0, 0, -5, 600]}, "宽是负数"),
    ({"anchor": "UI-结算结果-标题"}, "两个都不给（老写法漏了 offset）"),
]:
    act = {"type": "click_after_template"}
    act.update(bad)
    eng, ctl = make_engine(SELL_SETTLE)
    try:
        eng._do_action(dict(act, wait_before_ms=0), ctl, SELL_SETTLE)
        check(f"配错要当场响：{why}", False, f"居然跑通了，还点了 {ctl.clicks}")
    except ValueError as err:
        check(f"配错要当场响：{why}", True, str(err))

print("=" * 72)
print("20) 点启动时「船现在停在哪个港」：OCR 读到的港名认成第几站（纯函数 + 真图）")
# 为什么单独一节：用户 2026-09-29 拍「当前港口不需要手填，OCR 读一次即可」。
# 读到的字必须落到「这一趟的第几站」上才知道从哪起步 —— 这一步认错，整趟就从错的港开始花钱；
# 读不出来则必须拒绝启动，不能拿上一个港的名字顶（那是闭眼开船）。
import route_plan  # noqa: E402

STOPS20 = [{"stage": "buy", "port": "汉堡", "goods": ["啤酒"], "note": ""},
           {"stage": "transit", "port": "热那亚", "goods": [], "note": ""},
           {"stage": "sell", "port": "北京", "goods": [], "note": ""}]
find = route_plan.find_stop_by_name
check("港名一模一样 -> 认成第 1 站", find(STOPS20, "汉堡") == (0, [0]), str(find(STOPS20, "汉堡")))
check("读到的串里含这个港名就算（那块 roi 会有两行、拼成一串）",
      find(STOPS20, "热那亚_丽")[0] == 1, str(find(STOPS20, "热那亚_丽")))
check("认成站之后交出去的是站次里那份写法，不是 OCR 那串",
      STOPS20[find(STOPS20, "热那亚_丽")[0]]["port"] == "热那亚")
check("读成别的字（汊堡）-> 认不出，不猜", find(STOPS20, "汊堡")[0] == -1, str(find(STOPS20, "汊堡")))
check("一个字都没读到 -> 认不出", find(STOPS20, "")[0] == -1 and find(STOPS20, None)[0] == -1)
check("一站都没排 -> 认不出（不拿空表去比）", find([], "汉堡")[0] == -1)
TWICE = [{"stage": "buy", "port": "汉堡", "goods": ["啤酒"], "note": ""},
         {"stage": "transit", "port": "热那亚", "goods": [], "note": ""},
         {"stage": "sell", "port": "汉堡", "goods": [], "note": ""}]
idx, hits = find(TWICE, "汉堡")
check("同一个港在一趟里出现两次（汉堡买 → 汉堡卖）-> 认第一次，但把两次都交出来好说清",
      idx == 0 and hits == [0, 2], f"{idx} {hits}")
NEAR = [{"stage": "buy", "port": "伦敦", "goods": ["啤酒"], "note": ""},
        {"stage": "sell", "port": "东伦敦", "goods": [], "note": ""}]
check("读到「东伦敦」时两个港名都在串里 -> 取名字长的那个（认成短的会把船叫去别的港）",
      find(NEAR, "东伦敦") == (1, [0, 1]), str(find(NEAR, "东伦敦")))
check("只读到「伦敦」时不会被后面的「东伦敦」抢走", find(NEAR, "伦敦")[0] == 0,
      str(find(NEAR, "伦敦")))
check("启动时读港名用的是链头同一块区域（两处说法必须一样）",
      app_mod.PORT_REGION == "港口名字", repr(app_mod.PORT_REGION))

# 拿真图当屏幕读一次：传了 screen_path 就不碰 ADB，也不动项目里的规划文件
port, raw, at = app_mod.read_current_port_by_ocr(STOPS20, screen_path=HAMBURG_PORT)
check("汉堡港内真图：认出是这一趟的第 1 站", (port, at) == ("汉堡", 0), f"{port} / {raw!r} / {at}")
if os.path.exists(ARR):
    port2 = app_mod.read_current_port_by_ocr(STOPS20, screen_path=ARR)[0]
    check("到港真图（也是汉堡）：读出同一个港，不管那块区域有几行", port2 == "汉堡", repr(port2))
for path, label in [(BUY, "购买页"), (EXCH, "交易所默认页"), (SELL_TAB, "出售页")]:
    try:
        got = app_mod.read_current_port_by_ocr(STOPS20, screen_path=path)
        check(f"{label}：不能从这种画面脑补出一个站", False, str(got))
    except app_mod.HTTPException as e:
        check(f"{label}：明确拒绝启动（400 + 说清为什么）", e.status_code == 400,
              str(e.detail)[:70])
# 这趟没排到的港（热那亚那帧的汉堡换成北京来看）：读得出字但不在这趟里，一样得拒绝
ONLY_BJ = [{"stage": "buy", "port": "北京", "goods": ["中国画"], "note": ""}]
try:
    got = app_mod.read_current_port_by_ocr(ONLY_BJ, screen_path=HAMBURG_PORT)
    check("读得出「汉堡」但这趟只排了北京 -> 也要拒绝", False, str(got))
except app_mod.HTTPException as e:
    check("读得出「汉堡」但这趟只排了北京 -> 也要拒绝（不许拿别的站起步）",
          e.status_code == 400 and "不在这一趟的站次里" in str(e.detail), str(e.detail)[:80])

print("=" * 72)
print("21) 白栏判据的解析：dumpsys input_method 那段字段（真字段样例，一次都不碰 ADB）")
# 为什么单独一节：2026-09-30 实机那次「打字广播回 result=0、字却一个字没进框」，
# 骗就在「广播的返回值」上。唯一讲得清「栏到底开着没有」的是 dumpsys 里那两行字段，
# 所以解析它的这段代码必须钉死：字段名、取哪一位、V/G 与 y 怎么合成结论、读不到时返回 None。
from mumu_controller import MuMuController  # noqa: E402

# 两行字段按 2026-09-27 录制（港口间移动-流程记录.md 2.4 / 2.4b）里实测到的形状写：
# 可见性位在花括号里第二个字段（第一个是 hash），pos 行的 y 开着时是正数、收着时 -1000。
DUMP_OPEN = (
    "  mServedView=com.epicgames.ue4.GameActivity$VirtualKeyboardInput"
    "{7c1b2a3 VFED..CL. 0,806-1600,900 #121}\n"
    "  mServedView pos: x=0 y=806 w=1600 h=94\n"
    "  mInputShown=true\n")
# 同一扇栏换了输入法之后的 y（16:01 搜狗量到 835）—— 判据不能写死 806
DUMP_OPEN_SOGOU = DUMP_OPEN.replace("y=806", "y=835").replace("0,806-1600", "0,835-1600")
DUMP_CLOSED = (
    "  mServedView=com.epicgames.ue4.GameActivity$VirtualKeyboardInput"
    "{7c1b2a3 GFED..CL. 0,-1000-1600,-906 #121}\n"
    "  mServedView pos: x=0 y=-1000 w=1600 h=94\n"
    "  mInputShown=true\n"
    "  mHaveConnection=true\n")


def fake_dumpsys(text, rc=0):
    """造一个只会背 dumpsys 的控制器：_run 换成假返回值，其余代码全是真的。"""
    c = MuMuController()
    c._run = lambda *a: types.SimpleNamespace(
        returncode=rc, stdout=text, stderr="adb: device offline")
    return c


check("栏开着（V + y=806）：判成开着", fake_dumpsys(DUMP_OPEN).input_bar_state()[0] is True,
      str(fake_dumpsys(DUMP_OPEN).input_bar_state()))
check("换输入法后 y 变成 835 照样判开着（判据里没有写死的 y）",
      fake_dumpsys(DUMP_OPEN_SOGOU).input_bar_state()[0] is True)
check("栏收着（G + y=-1000）：判成没开 —— 哪怕 mHaveConnection 也是 true",
      fake_dumpsys(DUMP_CLOSED).input_bar_state()[0] is False,
      str(fake_dumpsys(DUMP_CLOSED).input_bar_state()))
check("说明里带上两个信号（人看日志时能自己核）",
      "V" in fake_dumpsys(DUMP_OPEN).input_bar_state()[1]
      and "806" in fake_dumpsys(DUMP_OPEN).input_bar_state()[1],
      fake_dumpsys(DUMP_OPEN).input_bar_state()[1])
# 两个信号不一致时按「没开」：误判成开着 = 白打一次字还当成功了，多点一次框只是慢
check("V 但 y=-1000（不同步）：取「与」，判成没开",
      fake_dumpsys(DUMP_OPEN.replace("y=806 w", "y=-1000 w")
                   .replace("0,806-1600", "0,-1000-1600")).input_bar_state()[0] is False)
check("G 但 y 是正数（不同步）：判成没开",
      fake_dumpsys(DUMP_CLOSED.replace("GFED", "GFED").replace("y=-1000 w", "y=806 w")
                   ).input_bar_state()[0] is False)
none_cases = [
    ("dumpsys 里根本没这行：返回 None（不许当成没开）",
     fake_dumpsys("  mCurFocus=none\n").input_bar_state()),
    ("只有可见位、没有 pos 行：返回 None",
     fake_dumpsys(DUMP_OPEN.splitlines()[0] + "\n").input_bar_state()),
    ("adb 命令本身失败：返回 None，并把 stderr 带进说明",
     fake_dumpsys("", rc=1).input_bar_state()),
    # dumpsys 里别处会把 mServedView= 夹在一行中间（嵌套的 wrapper），那不是正在服务的 view
    ("字段出现在行中间（嵌套 wrapper）：不当判据，返回 None",
     fake_dumpsys("  mServedInputConnection={mServedView=com.a.b{1 VFED..CL. 0,806-1,1}\n"
                  + "  mServedView pos: x=0 y=806 w=1 h=1\n").input_bar_state()),
]
for label, got in none_cases:
    check(label, got[0] is None, str(got[1])[:70])
check("读不出时的说明是人话（会原样进日志，不能只写个 null）",
      all(len(g[1]) > 8 for _, g in none_cases))

print("=" * 72)
print("22) ensure_input：点框 → 认栏 → 打字 → OCR 读回来核对，不通就重来（重试环）")
# 这一节验的是用户 2026-09-30 拍的那句「不断点击直到检测到输入接口」+
# 「首先按照原来的接口检测，然后ocr搜索框里的字」。OCR 那段换掉：真引擎一次十几秒，
# 而且「字有没有进框」这件事离线压根没素材（实机那次框是空的），验的是走位不是认字能力。
_real_ocr_text = state_machine.ocr_text_by_region


def stub_ocr(*answers):
    """按调用顺序把 OCR 读数吐回去；用完必须 restore_ocr() 还回去（后面还要读真图）。"""
    seq = list(answers)
    seen = []

    def fake(_screen, region, **_kw):
        seen.append(region)
        return seq.pop(0) if seq else ""
    state_machine.ocr_text_by_region = fake
    return seen


def restore_ocr():
    state_machine.ocr_text_by_region = _real_ocr_text


def taps(ctl):
    """只数真点击的坐标。FakeCtl 把打字/清空/切输入法记在同一条流水里，
    其中 ("input", "汉堡") 也是两个元素 —— 所以必须按「两个都是整数」筛，
    不然下面所有「点了几下 / 落点在不在矩形里」都会把打字算成点击。"""
    return [c for c in ctl.clicks
            if isinstance(c, tuple) and len(c) == 2 and all(isinstance(v, int) for v in c)]


ENS = {"type": "ensure_input", "click": [125, 124], "click_rect": [90, 105, 345, 45],
       "text_from": "run_port", "region": "地图搜索框",
       "settle_ms": 0, "read_ms": 0, "max_attempts": 3}

# a. 一次就成：第一轮点框 → 认栏 → 打字 → 读到「汉堡」= 要打的字 → 立刻返回，不再重试
seen = stub_ocr("汉堡")
eng, ctl = make_engine(MAP)
r = eng._do_action(ENS, ctl, MAP)
check("一次就成：返回里写明第 1 轮 + 读到的字", "第 1/3 轮" in r and "汉堡" in r, r[:80])
check("读的是指定区域（不是随便挑一行）", seen == ["地图搜索框"], str(seen))
check("落点在搜索框矩形里、且不在框中心（中心 (267,124) 点了不弹栏，实测）",
      taps(ctl) and all(90 <= x <= 435 and 105 <= y <= 150 for x, y in taps(ctl))
      and not any((x, y) == (267, 124) for x, y in taps(ctl)), str(taps(ctl)))
check("打字前先把输入法切成 ADBKeyboard、再清空（搜索框会残留上一个港名）",
      ("ime",) in ctl.clicks and ("clear",) in ctl.clicks
      and ctl.clicks.index(("clear",)) < ctl.clicks.index(("input", "汉堡")), str(ctl.clicks))
check("每轮都真去认过一次栏（不是只在代码里写了一行）", ctl.bar_calls == 1, str(ctl.bar_calls))
check("成了就不停止、不喊人", eng._stop_event.is_set() is False and eng._alert_request is None)
restore_ocr()

# b. 第二轮才成：第一轮读到的是占位符「搜索」—— 这就是实机那次的样子
seen = stub_ocr("搜索", "汉堡")
eng, ctl = make_engine(MAP)
r = eng._do_action(ENS, ctl, MAP)
check("第一轮字没进框 → 自动重来，第二轮才算成", "第 2/3 轮" in r, r[:80])
check("重来的动作是「再点框 + 再清 + 再打」（不是只补一次广播）",
      len(taps(ctl)) == 2 and ctl.clicks.count(("input", "汉堡")) == 2
      and ctl.clicks.count(("clear",)) == 2, str(ctl.clicks))
check("两轮各认一次栏", ctl.bar_calls == 2, str(ctl.bar_calls))
check("中途的失败只记 warn，不算停止", eng._stop_event.is_set() is False)
restore_ocr()

# c. 栏一直收着 + 广播照样回 result=0：只信 OCR，三轮用尽就停下喊人
eng, ctl = make_engine(MAP)
eng._log_lines = []
eng._log = lambda ev, m: eng._log_lines.append((ev, m))
ctl.bar = (False, "假：可见位 G / y=-1000")
seen = stub_ocr("搜索", "搜索", "搜索")
r = eng._do_action(ENS, ctl, MAP)
check("栏没开也照样问了一句（判据不能省，开了就好打字）", ctl.bar_calls == 3, str(ctl.bar_calls))
check("栏关着时讲明了「广播会静默失效」",
      any("静默失效" in m for ev, m in eng._log_lines if ev == "warn"),
      str([m for ev, m in eng._log_lines if ev == "warn"])[:90])
check("三轮都读不到字 → 停下（返回以「停止」开头）", r.startswith("停止"), r[:70])
check("置了停止位 + stop_kind=alert（队列按这个判断这趟没跑完）",
      eng._stop_event.is_set() and eng.stop_kind == "alert", str(eng.stop_kind))
check("挂出人工提醒，标题说的是「打不进搜索框」这件事",
      eng._alert_request and eng._alert_request[0] == "UWO 已停止：港口名打不进搜索框",
      str(eng._alert_request and eng._alert_request[0]))
check("提醒正文带了下一步能查的线索（栏/输入法/画面）",
      eng._alert_request and "白色输入栏" in eng._alert_request[1],
      str(eng._alert_request and eng._alert_request[1])[:60])
check("重试次数就是配的上限，不多点", len(taps(ctl)) == 3 and ctl.clicks.count(("input", "汉堡")) == 3,
      str(ctl.clicks))
restore_ocr()

# d. 白栏读不出（dumpsys 拿不到字段）：不拦路，只按 OCR 判 —— 字在框里就是成了
eng, ctl = make_engine(MAP)
eng._log_lines = []
eng._log = lambda ev, m: eng._log_lines.append((ev, m))
ctl.bar = (None, "假：dumpsys 里没读到 mServedView")
seen = stub_ocr("汉堡")
r = eng._do_action(ENS, ctl, MAP)
check("栏读不出 + OCR 读到字 = 判成（不因为读不到就白试）", "第 1/3 轮" in r, r[:70])
check("读不出记一条 warn，讲的是「只按 OCR 判」",
      any("只按 OCR" in m for ev, m in eng._log_lines), str(eng._log_lines)[:100])
check("没停止", eng._stop_event.is_set() is False)
restore_ocr()

# e. 字没进框时，后面「点列表第一行」那一下绝不能发生（照 _loop 的 stop_event 语义复刻一遍）
picked = actions_of(data["states"], "sail_pick_city")
eng, ctl = make_engine(MAP)
stub_ocr("搜索")
r = eng._do_action(picked[2], ctl, MAP)      # 真 states.json 那一格（上限 5 轮）
for a in picked[3:]:                        # _loop 第 5 步：每格动作前都查 stop_event
    if eng._stop_event.is_set() or eng._pending_goto:
        break
    eng._do_action(a, ctl, MAP)
check("用尽 5 轮 = 停止，之后的点 (265,173) 与城市移动都没发生",
      len(taps(ctl)) == 5 and (265, 173) not in taps(ctl), str(taps(ctl)))
check("也没有跳转（停在原地等人，不该跳到 voyage）", eng._pending_goto is None)
restore_ocr()

# f. 配错要当场响（这些字段错了就等于没判据，不能静默往下走）
for bad, why in [
    ({"region": "地图搜索框", "text": "汉堡"}, "没给 click 落点"),
    ({"click": [125], "region": "地图搜索框", "text": "汉堡"}, "click 只有一个数"),
    ({"click": [125, 124], "text": "汉堡"}, "没给 region（没有它就判不出字进没进框）"),
    ({"click": [125, 124], "region": "地图搜索框"}, "既没 text 也没 text_from"),
]:
    act = dict({"type": "ensure_input"}, **bad)
    eng, ctl = make_engine(MAP)
    try:
        eng._do_action(act, ctl, MAP)
        check(f"配错要当场响：{why}", False, f"居然跑通了，还点了 {taps(ctl)}")
    except ValueError as err:
        check(f"配错要当场响：{why}", True, str(err)[:70])
# text_from=run_port 但本次没带港名：不能拿空串去搜（空串会筛出整个列表，第一行就是别人的城）
eng, ctl = make_engine(MAP)
eng.run_port = ""
try:
    eng._do_action(dict(ENS, text=""), ctl, MAP)
    check("text_from=run_port 但本次没带港名：当场拒绝", False, "居然跑通了")
except ValueError as err:
    check("text_from=run_port 但本次没带港名：当场拒绝（不拿空串搜出全列表）",
          "没选港口" in str(err), str(err)[:70])
# region 名字打错：真 OCR 会抛 ValueError，这里换成抛异常验「原样响、不静默重试」
eng, ctl = make_engine(MAP)


def boom(_s, region, **_kw):
    raise ValueError(f"OCR 区域不存在: {region}")


state_machine.ocr_text_by_region = boom
try:
    eng._do_action(dict(ENS, region="地图搜所框"), ctl, MAP)
    check("区域名字打错：不当成「字没进框」白重试 5 轮，直接响", False, "居然跑通了")
except ValueError as err:
    check("区域名字打错：当场响，且说清是 OCR 区域的问题",
          "OCR 区域用不了" in str(err), str(err)[:80])
restore_ocr()

# g. 人在界面上点了停止：循环每轮开头都要看一眼，不能继续白点
eng, ctl = make_engine(MAP)
seen = stub_ocr("搜索")
eng._stop_event.set()
r = eng._do_action(ENS, ctl, MAP)
check("已停止时一轮都不试：不点框、不打字", ctl.clicks == [] and "已停止" in r, str(ctl.clicks))
restore_ocr()

# h. clear_first=False：不许偷偷清空（留着给「框里已有字、只想补几个」的场景）
eng, ctl = make_engine(MAP)
stub_ocr("汉堡")
eng._do_action(dict(ENS, clear_first=False), ctl, MAP)
check("clear_first=False 时没发 ADB_CLEAR_TEXT", ("clear",) not in ctl.clicks, str(ctl.clicks))
restore_ocr()

print("=" * 72)
print("23) confirm_city_move：先核对选中的城市 = 本次港口，再等按钮真出现才点")
# 用户拍板：「停下来喊人，不点城市移动」。理由是点「城市移动」没有二次确认（录制 2.7），
# 一下去就是几十分钟真航程；而列表第一行是谁完全取决于搜索框里那几个字。
CM = {"type": "confirm_city_move", "name": "UI-城市移动", "region": "地图选中城市",
      "timeout_ms": 0, "poll_interval_ms": 200}
AFTER_ROW = os.path.join(TMP, "uwo_160909_02_after_row.png")   # 16:09 选中汉堡后那一帧
if os.path.exists(AFTER_ROW):
    eng, ctl = make_engine(AFTER_ROW)
    seen = stub_ocr("汉堡")
    r = eng._do_action(CM, ctl, AFTER_ROW)
    check("城市名对上 + 按钮在屏：点下去了，就一次", len(taps(ctl)) == 1, str(taps(ctl)))
    check("落点落在「城市移动」按钮的 roi [722,800,154,77] 里",
          taps(ctl) and all(722 <= x <= 876 and 800 <= y <= 877 for x, y in taps(ctl)),
          str(taps(ctl)))
    check("返回里先讲核对、再讲点击（人看日志能确认「点的是汉堡」）",
          "已核对" in r and "汉堡" in r, r[:90])
    check("读的是那块 1187,163,255,44（用户给的区域）", seen == ["地图选中城市"], str(seen))
    restore_ocr()

    # 那块 roi 实测会读到两行拼串（「汉堡_自由港」这种），包含判据要认
    eng, ctl = make_engine(AFTER_ROW)
    stub_ocr("汉堡_自由港")
    r = eng._do_action(CM, ctl, AFTER_ROW)
    check("读到两行拼串也认得出本次港（和链头 plan_port 同一口径）",
          len(taps(ctl)) == 1 and "已核对" in r, str((taps(ctl), r[:50])))
    restore_ocr()

    # 实机那次：框是空的 → 第一行是默认列表的头一座城 → 选中的是「马赛」，不是汉堡
    eng, ctl = make_engine(AFTER_ROW)
    stub_ocr("马赛")
    r = eng._do_action(CM, ctl, AFTER_ROW)
    check("城市名对不上：一下都不点（几十分钟真航程不能赌）", ctl.clicks == [], str(ctl.clicks))
    check("对不上 = 停止 + 喊人，标题说清「选中的城市不是本次港口」",
          eng._stop_event.is_set() and eng._alert_request
          and eng._alert_request[0] == "UWO 已停止：选中的城市不是本次港口",
          str(eng._alert_request and eng._alert_request[0]))
    check("提醒正文指向上一步（搜索框没进字）",
          eng._alert_request and "搜索框" in eng._alert_request[1],
          str(eng._alert_request and eng._alert_request[1])[:70])
    restore_ocr()
else:
    check("16:09 录制素材在（选城市那一帧）", False, AFTER_ROW)

# 一个字都没读到：和读错一样处理 —— 不能因为「读不出」就当成没意见点下去
eng, ctl = make_engine(AFTER_ROW if os.path.exists(AFTER_ROW) else MAP)
stub_ocr("")
r = eng._do_action(CM, ctl, eng.screen_path)
check("读空也拒绝点（读不出不等于没意见）",
      ctl.clicks == [] and "对不上" in eng.stop_reason, str((ctl.clicks, eng.stop_reason))[:90])
restore_ocr()

# 本次没带港名：核对不了就不点
eng, ctl = make_engine(AFTER_ROW if os.path.exists(AFTER_ROW) else MAP)
eng.run_port = ""
stub_ocr("汉堡")
r = eng._do_action(CM, ctl, eng.screen_path)
check("不知道该核对哪个港：不点 + 喊人",
      ctl.clicks == [] and eng._alert_request
      and eng._alert_request[0] == "UWO 已停止：不知道该核对哪个港", str(ctl.clicks))
restore_ocr()

# 2026-09-30 18:55 那一幕：白栏还盖着按钮（城市移动在 y 810~900 那一条上），模板认不出。
# 老写法抛 ValueError 之后引擎照样跳到 voyage，只在海上干等；现在改成明确停止 + 喊人。
if os.path.exists(MAP):
    eng, ctl = make_engine(MAP)
    stub_ocr("汉堡")
    r = eng._do_action(CM, ctl, MAP)
    check("按钮认不出（栏还盖着它）：点满 0 下，不抛异常也不往下走",
          ctl.clicks == [] and r.startswith("停止"), str((ctl.clicks, r[:60])))
    check("标题说清「没找到城市移动按钮」",
          eng._alert_request and eng._alert_request[0] == "UWO 已停止：没找到「城市移动」按钮",
          str(eng._alert_request and eng._alert_request[0]))
    check("提醒正文讲的是那一条 y（人一看就知道是栏盖住了）",
          eng._alert_request and "810~900" in eng._alert_request[1],
          str(eng._alert_request and eng._alert_request[1])[:70])
    restore_ocr()
# 模板名不存在 / 区域用不了：配错当场响，不能拿「找不到」当「没出现」混过去
eng, ctl = make_engine(AFTER_ROW if os.path.exists(AFTER_ROW) else MAP)
try:
    eng._do_action(dict(CM, name="UI-城市移动移动"), ctl, eng.screen_path)
    check("模板名打错：当场响", False, "居然跑通了")
except ValueError as err:
    check("模板名打错：当场响（不是等 6 秒再报「没找到按钮」）", "模板不存在" in str(err), str(err)[:60])
eng, ctl = make_engine(AFTER_ROW if os.path.exists(AFTER_ROW) else MAP)


def boom2(_s, region, **_kw):
    raise ValueError(f"OCR 区域不存在: {region}")


state_machine.ocr_text_by_region = boom2
try:
    eng._do_action(CM, ctl, eng.screen_path)
    check("核对用的区域打错：当场响", False, "居然跑通了")
except ValueError as err:
    check("核对用的区域打错：当场响（不能因为读不出就当没意见）",
          "confirm_city_move 的 OCR 区域用不了" in str(err), str(err)[:80])
restore_ocr()

print("-" * 72)
print("23b) 启动路径：这两种动作要走主循环用的那一层（带超时外壳、动作跑在 worker 线程里）")
# 为什么单独验：上面全是直接调 _do_action，那只证明「函数写对了」，
# 没证明 dispatch 里那两行接上了。少接一行 = 函数在但没人调，
# 走到未知动作类型就抛异常、被记成一条 error 跳过，搜索框照旧一个字没打 —— 和实机那次一模一样。
eng, ctl = make_engine(MAP)
seen = stub_ocr("汉堡")
ok, msg = eng._run_action_with_timeout(ENS, ctl, MAP)
check("ensure_input 接通了主循环那一层（ok=True，不是「未知动作类型」）",
      ok is True and "第 1/3 轮" in msg, str((ok, msg[:60])))
restore_ocr()
eng, ctl = make_engine(AFTER_ROW if os.path.exists(AFTER_ROW) else MAP)
stub_ocr("汉堡")
ok, msg = eng._run_action_with_timeout(dict(CM, timeout_ms=200), ctl, eng.screen_path)
check("confirm_city_move 接通了主循环那一层", ok is True and "已核对" in msg, str((ok, msg[:60])))
restore_ocr()
eng, ctl = make_engine(MAP)
ok, msg = eng._run_action_with_timeout({"type": "ensure_inpu"}, ctl, MAP)
check("反面：名字少写一个字（没接进 dispatch 的样子）不会静默算成功",
      ok is False and "未知动作类型" in msg, str((ok, msg[:60])))

print("=" * 72)
print("24) 接线：sail_pick_city 的走位、两张新区域、界面认识这两种动作")
wired = next(s for s in data["states"] if s["id"] == "sail_pick_city")
wa = wired["actions"]
check("整格走位是：开列表 → 等 → 搜港并确认字进了 → 点第一行 → 等 → 核对城市才点移动",
      [a["type"] for a in wa] == ["click_template", "wait", "ensure_input", "click",
                                  "wait", "confirm_city_move"],
      str([a["type"] for a in wa]))
check("「先确认字进了再点行」的顺序没写反（反了就是拿空框点别人的城）",
      [a["type"] for a in wa].index("ensure_input") < [a["type"] for a in wa].index("click"))
check("老的裸 input 不再出现在这一格（它一次都不回头验）",
      "input" not in [a["type"] for a in wa], str([a["type"] for a in wa]))
ens = wa[2]
check("点的是搜索框左端 (125,124)：中心 (267,124) 实测不弹栏",
      ens.get("click") == [125, 124] and 267 not in ens.get("click", []), str(ens.get("click")))
check("带 click_rect：落点在框内随机（人手），不是每轮死点同一个像素",
      ens.get("click_rect") == [90, 105, 345, 45], str(ens.get("click_rect")))
check("搜的是本次港口（text_from=run_port），不是写死的港名",
      ens.get("text_from") == "run_port" and not ens.get("text"), str(ens.get("text_from")))
check("核对用的区域就是那块搜索框（和 click_rect 同一个 roi，两处说法要一样）",
      ens.get("region") == "地图搜索框"
      and next(x for x in json.load(open(os.path.join(BASE, "ocr_regions.json"),
                                        encoding="utf-8"))
               if x["name"] == "地图搜索框")["roi"] == ens["click_rect"],
      str(ens.get("region")))
check("最多 5 轮；动作外壳的超时比最坏耗时宽（5 轮各等 900+800ms，还要加打字与 OCR）",
      ens.get("max_attempts") == 5
      and int(ens.get("timeout_seconds", 0)) > 5 * ((ens["settle_ms"] + ens["read_ms"]) / 1000),
      f"{ens.get('timeout_seconds')}s vs 最坏 {5 * ((ens['settle_ms'] + ens['read_ms']) / 1000):g}s 纯等待")
cm = wa[5]
check("核对读的是用户给的那块 1187,163,255,44（中心 1315,185）",
      cm.get("region") == "地图选中城市"
      and next(x for x in json.load(open(os.path.join(BASE, "ocr_regions.json"),
                                        encoding="utf-8"))
               if x["name"] == "地图选中城市")["roi"] == [1187, 163, 255, 44], str(cm.get("region")))
check("按的是 UI-城市移动；等按钮出现给了 6 秒（白栏收起 + 地图平移 + 面板弹出都要时间）",
      cm.get("name") == "UI-城市移动" and int(cm.get("timeout_ms", 0)) >= 6000, str(cm))
check("点完第一行到核对之间等 1500ms（栏收起前读右边面板会读到旧画面）",
      wa[4].get("ms", 0) >= 1000, str(wa[4]))
check("这一格还是靠 next 串到 voyage（没塞 goto，模块边界不破）",
      wired.get("next") == "voyage"
      and not any(a.get("type") == "goto" for a in wa), str(wired.get("next")))
# 界面得认这两种新动作，不然调试台里这一格只能看不能改（保存时会被当成未知类型）
js = open(os.path.join(BASE, "frontend", "app.js"), encoding="utf-8").read()
check("前端 ACTION_TYPES 认这两种动作（列表里一处 + 默认参数两处）",
      js.count("ensure_input") >= 2 and js.count("confirm_city_move") >= 2,
      f"{js.count('ensure_input')}/{js.count('confirm_city_move')}")
check("前端不许在状态里裸用 input 而丢掉验字（老的 input 编辑项还在，但注明了只适合手动单步）",
      "手动单步" in js)

print("=" * 72)
print("结果:", "全部通过" if not FAILS else "失败项 = %s" % FAILS)
sys.exit(1 if FAILS else 0)
