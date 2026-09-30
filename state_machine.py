"""状态机引擎。

后台线程按固定间隔循环执行：
    截图 -> 匹配当前状态的识别条件 -> 执行动作序列 -> 跳转下一个状态。

- states.json 存状态机配置（config + states）
- 模块（module）：跑商流程切成「买货 / 港口间移动 …」后，同一张港口画面可以同时是两个模块的
  起点，看画面分不出该走哪条 —— 所以启动时必须指定本次跑哪个模块，引擎只把该模块的状态
  （加 global 状态）当候选。非全局状态的 module 漏填直接拒绝启动。单模块跑法模块之间不自动串。
- 完整一趟（module="trip"）：一次点启动把「进货 →（可选）中转 → 卖货」按站次顺序走完。
  它不是第四条链，而是把 buy / sail / sell 三条已有链**依次接起来**：每条链跑到底的那个动作
  写成 trip_next，单模块时它就是以前的 stop，跑整趟时它决定下一段跳去哪个状态。
  港口名和本次清单每换一段重新绑一次（见 _bind_leg），所以三条链里的状态一个字都不用改。
- 内置「协商」动作：OCR 固定区域「协商按钮区」，按钮坐标从 config.negotiation.buttons 读
- 启动前一次性把画面退回港口界面（config.exit_to_port，只在 _loop 开头跑一次）
- retry_watch：航行中断看门狗，认出「重启自动移动」就点它，连续点满上限仍不恢复就停止 + 弹窗喊人
- wait_arrival：在海上阻塞等到港（灯塔判据出现），期间顺手照看航行中断 —— 整趟要在到港那一刻接下一段
- goto：按画面决定跳到哪个状态（状态上的 next 是写死的一个，表达不了「等到某画面出现才换」）
- 安全机制：单动作超时、连续无匹配停止、轮次上限、随时手动停止
- 日志：最近 100 条（时间 / 事件 / 消息）
"""

import ctypes
import json
import os
import random
import threading
import time

import purchase_plan
import restock
import route_plan
import run_state
from mumu_controller import MuMuController, capture
from vision import (OCR_REGIONS_JSON, find_in_list, find_template, ocr_find,
                    ocr_find_in_region, ocr_text_by_region, scroll_list_to_top)

# 本文件所在目录：所有数据文件路径都基于它拼绝对路径，不受启动目录影响
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATES_JSON = os.path.join(BASE_DIR, "states.json")
TEMPLATES_JSON = os.path.join(BASE_DIR, "templates.json")

# 启动参数里可以当「本次港口」的字段，就这三个 —— plan_port 条件的 from 只能填这里的名字。
# 为什么要卡死：填错一个字段名的话核对会拿一个空串去比，看起来「一直没匹配上」，
# 实际是启动时少传了个值，这种错最难查；宁可启动前就拒绝。
PORT_FIELD_LABELS = {
    "buy_port": "本次买货港口",
    "sell_port": "本次出货港口",
    "sail_port": "目的港（只打进搜索框）",
}

DEFAULT_CONFIG = {
    "interval_ms": 500,        # 每轮间隔（毫秒）
    "max_rounds": 1000,        # 循环轮次上限
    "max_no_match": 5,         # 连续无匹配轮次上限，达到即停止
    "action_timeout_seconds": 10,  # 单个动作超时（秒）
    "negotiation": {
        "region": "协商按钮区",   # 协商弹窗的 OCR 区域名（固定）
        "max_clicks": 1,           # 点几次「进行1次」
        "click_interval_ms": 1500,  # 每次点击后等待/重读的间隔
        "buttons": {
            "no": [1276, 585],     # 不
            "once": [1283, 669],   # 进行1次
            "all": [1283, 754],    # 进行所有
        },
    },
    # 启动前一次性把画面退回港口界面（详见 _exit_to_port）
    "exit_to_port": {
        "enabled": True,
        # 右上角图标槽里的「退出」按钮，两个共用同一个 ROI，只会显示一个
        "buttons": ["图标-房子-退出", "图标-关闭-X"],
        "max_clicks": 5,      # 最多点几次，防止反复进出去没完
        "wait_ms": 1200,      # 每次点击后等页面切换
        "port_marker": "UI-港口标志",  # 到港判据：≡ 菜单
    },
    # 人手随机点击：落点在匹配到的矩形里随机，点击前随机等一下（详见 _tap）
    "human_click": {
        "enabled": True,
        "delay_min_ms": 120,   # 点击前最短等待
        "delay_max_ms": 450,   # 点击前最长等待
        "inset_ratio": 0.18,   # 矩形四周各留 18% 再取随机点，避免贴边
    },
    # 完整一趟（module="trip"）：这一次要把哪几条链接起来、整趟最多允许跑多少轮。
    # 为什么不写死在 Python 里：以后加一段（比如「港口作业」）只改这一行；
    # max_rounds 另给一份是因为单模块那 60 轮只够跑一条链，整趟要走 3~5 条链会撞「轮次上限」。
    "trip": {
        "modules": ["buy", "sail", "sell"],
        "max_rounds": 400,
    },
}


def load_states():
    """读取 states.json，返回完整配置 dict（config 已与默认值浅合并）。"""
    if not os.path.exists(STATES_JSON):
        return {"config": dict(DEFAULT_CONFIG), "states": []}
    with open(STATES_JSON, "r", encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("config", {})
    for k, v in DEFAULT_CONFIG.items():
        data["config"].setdefault(k, v)
    data.setdefault("states", [])
    return data


def save_states(data):
    """写入 states.json。"""
    with open(STATES_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


class StateMachineEngine:
    """状态机引擎：start() 起后台线程，stop() 停止，status() 查询。

    运行模式为「当前状态驱动」：每轮只匹配 current_state 的条件，命中则执行
    动作并跳转到其 next；不命中则累加连续无匹配计数，超过 max_no_match 停止。
    """

    def __init__(self, adb_path, port, screen_path):
        self.adb_path = adb_path
        self.port = port
        self.screen_path = screen_path

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = None

        self.config = dict(DEFAULT_CONFIG)
        self.states = []
        # 本次只跑「一个模块」：self.states 在 start() 里就被裁成该模块的状态 + 全局状态，
        # 之后所有匹配、goto 目标校验、入口检测都只看这份裁过的列表。
        self.run_module = None
        self.running = False
        self.current_state = None
        self.round = 0
        self.no_match_count = 0
        self.stop_reason = None
        # 机器可读的停止类别（人读 stop_reason，程序读这个）：
        #   "done"        整趟/链条正常走到头（队列靠它判断「这一趟跑完了，接下一个」）
        #   "manual"      人手点停止
        #   "stop_action" states.json 里的 stop 动作主动停（可能是正常收尾，也可能是错误路径，
        #                 所以队列不把它当「可继续」——见 run_queue 的口径）
        #   "alert"       需要人工（停在弹窗上等人处理）
        #   "error"       引擎异常 / 轮次上限 / 连续无匹配 等
        # 为什么不用 stop_reason 的中文串去判断：那是给人看的，改一个字判断就错，而且错得无声无息。
        self.stop_kind = None
        # goto 动作写在「本轮动作跑完之后要跳去哪个状态」，由 _loop 在跳转那一步消费。
        # 需要它的原因见 _do_goto：状态中只有一个静态 next，表达不了「等到某个画面出现才换」。
        self._pending_goto = None
        # 待弹的人工提醒 (标题, 正文)。动作只往这里放，真正弹窗在 _loop 退出时做，
        # 原因见 _do_retry_watch 末尾的说明：弹窗是阻塞的，不能卡在动作线程里。
        self._alert_request = None
        # 本次运行锁定购物表格里「某一个港口」名下的全部行（启动前人工选港口）。
        # run_row_index 是游标：买完一行由 plan_advance 动作推进一格，
        # 游标越界就说明本港口的货买完了，状态机自己停，不用人回来点。
        self.run_port = None
        # run_port 出自启动参数的哪个字段（buy_port / sell_port / sail_port）。
        # 界面「测试条件」的详情要把它说清：买货链比的是本次买货港，卖货链比的是本次出货港，
        # 都叫「本次港口」但不是一回事，看日志的人必须能一眼分开。
        self.run_port_field = None
        self.run_rows = []
        self.run_row_index = 0
        # 完整一趟的运行态：None 就是单模块跑法（行为和以前一个字都不差）。
        # 结构 {"stops": [{stage, port, goods, note}, ...], "pos": 现在站在第几站}，
        # stops 是「从当前所在港那一站起、到这一趟结尾」的有序切片（route_plan.derive 算好的），
        # 换段时只重新绑 run_port / run_rows（见 _bind_leg），三条链自己不想知道在跑第几趟。
        self.trip = None
        # 补货判据的最新结论（给界面只读显示用）：等待期间每轮刷新，不等的那一路也写一次。
        # 结构见 _do_wait_restock；没跑过这一格就是 None。
        self.restock = None
        self._logs = []  # 环形缓冲，最多 100 条

    @property
    def run_row(self):
        """游标当前指向的表格行；没选港口、或本港口的行已买完时为 None。"""
        if 0 <= self.run_row_index < len(self.run_rows):
            return self.run_rows[self.run_row_index]
        return None

    # ---------- 模块 ----------
    @staticmethod
    def _module_of(state):
        return (state.get("module") or "").strip()

    @classmethod
    def _walk(cls, node):
        """把一份 condition / actions 里所有 dict 和 list 元素逐个交出来。

        条件与动作能嵌套（all / any / if / run_actions），不递归就会漏判。
        """
        yield node
        if isinstance(node, dict):
            for v in node.values():
                yield from cls._walk(v)
        elif isinstance(node, list):
            for v in node:
                yield from cls._walk(v)

    @classmethod
    def _state_touches(cls, state, pred):
        """这个状态的判据或动作里，有没有一处满足 pred。
        _walk 连字符串、数字这些叶子值也会吐出来，pred 只看带 type 的那种节点，
        所以这里先把非 dict 的滤掉。"""
        for node in (state.get("condition"), state.get("actions")):
            if node is not None and any(pred(n) for n in cls._walk(node)
                                        if isinstance(n, dict)):
                return True
        return False

    @classmethod
    def _plan_port_sources(cls, states):
        """这些状态里的港口名核对（plan_port）各自声明「要比的港口名来自启动参数的哪个字段」。

        默认 buy_port —— 买货链本来就是这样。卖货链把它写成 sell_port：那张画面要核对的是
        「我确实站在这一趟安排的出货港」，而买货港在卖货这一趟里可能压根是另一个港。
        返回值去重排序，好让 start() 一眼看出「这个模块只认一个本次港口」还是「写乱了」。
        """
        out = set()
        for s in states:
            for node in (s.get("condition"), s.get("actions")):
                for n in cls._walk(node):
                    if isinstance(n, dict) and n.get("type") == "plan_port":
                        out.add(n.get("from") or "buy_port")
        return sorted(out)

    # ---------- 完整一趟（trip） ----------
    @staticmethod
    def _trip_conf(config):
        """整趟要跨哪几个模块、允许跑多少轮 —— 写在 states.json 的 config.trip 里，
        不在 Python 里认模块名：以后加一段（比如「港口作业」）只改配置。"""
        cfg = (config or {}).get("trip") or {}
        return [m for m in (cfg.get("modules") or []) if m], int(cfg.get("max_rounds", 400))

    @staticmethod
    def _trip_state_for(states, stage):
        """这一趟走到「stage 这一类站」时要进哪个状态 —— 由那个状态自己用 trip_entry 声明。

        为什么不让引擎写死（buy→in_port 那张表）：状态 id 是人在 states.json 里改的，
        写死在 Python 里就等于两处说法，改了那处不会报错、只会跑到一半才不对。
        """
        for s in states:
            if s.get("trip_entry") == stage:
                return s.get("id")
        return None

    @staticmethod
    def _trip_leg_text(stop):
        """第几站说什么样子的一行字：『买货 汉堡』。给日志和界面共用，别说两套。"""
        stage = (stop or {}).get("stage") or ""
        return f"{route_plan.STAGE_LABELS.get(stage, stage)}{(stop or {}).get('port') or '(没填港)'}"

    def _trip_snapshot(self):
        """给界面念的整趟进度：现在第几站、这一站做什么、后面还有哪些站。单模块跑法给 None。"""
        if not self.trip:
            return None
        stops = self.trip.get("stops") or []
        pos = self.trip.get("pos", -1)
        return {
            "pos": pos,
            "total": len(stops),
            "stage": (stops[pos] or {}).get("stage") if 0 <= pos < len(stops) else None,
            "port": (stops[pos] or {}).get("port") if 0 <= pos < len(stops) else None,
            "legs": [{"stage": s.get("stage"), "port": s.get("port"),
                      "text": self._trip_leg_text(s)} for s in stops],
        }

    @staticmethod
    def _plan_rows_for(plan, port, want):
        """按「港口 + 本次要买的货」从购物表格里取这一站的行。返回 (行, None) 或 (None, 人话原因)。

        这套校验以前长在 start() 里面；跑整趟时每个买货站都要照同一口径核一遍，
        所以挪出来给两处共用 —— 两处说法必须一模一样，不然人在日志里看到两种措辞，
        会以为是两套规则，不知道到底哪一条拦住了自己。
        """
        rows_here = purchase_plan.rows_for_port(plan, port)
        if not rows_here:
            ports = sorted({(r.get("port") or "").strip() for r in plan.get("rows", [])})
            return None, (f"港口『{port}』在购物表格里没有行，表格里现有: "
                          f"{ports or '（表格是空的）'}")
        # 表格只是**目录**：本次买哪几件由「跑商设置」这一站勾的货说了算，顺序也照它。
        by_name = {}
        for r in rows_here:
            name = (r.get("goods_name") or "").strip()
            if name and name not in by_name:
                by_name[name] = r
        want = [g for g in (want or []) if (g or "").strip()]
        if not want:
            return None, (f"港口『{port}』这一站没勾任何货物 —— 空着起来会一步货都不买、"
                          f"却照样点购买/确定，去「跑商设置」在这一站勾上要买的货")
        missing = [g for g in want if g not in by_name]
        if missing:
            return None, (f"港口『{port}』这些货在购物表格里查不到（表格改过了？）: "
                          f"{missing}；该港现在买得到: {sorted(by_name)}")
        rows = [by_name[(g or "").strip()] for g in want]
        bad = [purchase_plan.row_key(r) for r in rows
               if r.get("cargo_type") not in purchase_plan.CARGO_TYPES]
        if bad:
            return None, f"港口『{port}』这些行的类别非法（必须先改表格）: {bad}"
        return rows, None

    def _leg_binding(self, stops, entries, pos, plan=None):
        """第 pos 站「现在该把 run_port / run_rows / 起点状态 绑成什么样」。
        返回 (绑定 + note, None)，或 (None, 这一趟走不下去的人话原因)。

        为什么每换一站都要重绑：三条链读的一直是 run_port / run_rows 这两个字段
        （买货链拿它核对港名、取本次要买的货；移动链拿它打进地图搜索框；卖货链拿它核对出货港）。
        整趟里唯一变的就是「这一站该填哪一个港」—— 所以链上的状态一个字都不用改。
        做成不碰 self 的纯函数，是因为启动时要把**每一站**都先算一遍：走到一半才发现
        某一站买不了，钱和船已经花在前几站上了（2026-09-27 你定的口径：花钱的事启动前拦）。
        """
        stop = stops[pos]
        stage = (stop.get("stage") or "").strip()
        label = self._trip_leg_text(stop)
        entry = entries.get(stage)
        if not entry:
            return None, (f"第 {pos + 1} 站 {label}：states.json 里没有哪个状态声明自己是"
                          f"这类站的入口（trip_entry: {stage}）")

        if stage == "buy":
            if plan is None:
                try:
                    plan = purchase_plan.load_plan()
                except Exception as e:
                    return None, f"读取 purchase_plan.json 失败: {e}"
            rows, err = self._plan_rows_for(plan, stop.get("port"), stop.get("goods"))
            if err:
                return None, f"第 {pos + 1} 站 {label}买不了：{err}"
            names = "、".join(f"{i + 1}. {r['goods_name']}" for i, r in enumerate(rows))
            return {"run_port": (stop.get("port") or "").strip(), "run_port_field": "buy_port",
                    "run_rows": rows, "current_state": entry,
                    "note": f"挂 {len(rows)} 件，按「跑商设置」里的先后买完就接下一段：{names}"}, None

        if stage == "sell":
            # 只核对港名、不吃货物清单（2026-09-28 你拍的「货舱里有什么卖什么」，这一站不挂货）
            return {"run_port": (stop.get("port") or "").strip(), "run_port_field": "sell_port",
                    "run_rows": [], "current_state": entry,
                    "note": "就在这一港把舱里的货全卖掉"}, None

        # 中转：这一站要做的只有「再出一次港」，所以绑的是**下一站**的港名 —— 移动链要把
        # 它打进地图搜索框；进出港这一下本身就够补上水粮了（2026-09-29 你拍的口径）。
        b, err = self._depart_binding(stops, entries, pos, "只进出一次港补个水粮")
        if err:
            return None, err
        b["note"] = f"{b['note']}（这一站是中转）"
        return b, None

    def _depart_binding(self, stops, entries, pos, what):
        """「要开船了」这一种绑法：本次港口 = 下一站的港名，起点 = 移动链链头。

        两处要用它：这一站是中转站（到了就走）、以及这一站的正事做完要开去下一站。
        写成一份是因为两者的画面完全一样（人都站在码头上按 1），两份说法迟早会对不上。
        """
        entry = entries.get("transit")
        if entry is None:
            return None, "要开去下一站，但没有状态声明 trip_entry: transit（移动链的链头）"
        if pos + 1 >= len(stops):
            return None, (f"第 {pos + 1} 站 {self._trip_leg_text(stops[pos])} 后面没有站了 —— "
                          f"{what}，但没有下一港可去就别排这一段")
        dest = (stops[pos + 1].get("port") or "").strip()
        return {"run_port": dest or None, "run_port_field": "sail_port", "run_rows": [],
                "current_state": entry, "note": f"{what}，下一港是『{dest}』"}, None

    def _apply_binding(self, pos, b):
        """把一份绑定落到 self 上（换段的唯一落点），并给日志念一句这一站要做什么。"""
        stops = self.trip["stops"]
        self.trip["pos"] = pos
        self.run_port = b["run_port"]
        self.run_port_field = b["run_port_field"]
        self.run_rows = b["run_rows"]
        self.run_row_index = 0
        self.current_state = b["current_state"]
        self._log("trip", f"第 {pos + 1}/{len(stops)} 站 "
                          f"{self._trip_leg_text(stops[pos])}：{b['note']}")

    def _bind_leg(self, pos):
        """按 _leg_binding 算出来的样子落到 self 上。返回 None 或「走不下去」的原因。"""
        b, err = self._leg_binding(self.trip["stops"], self.trip["entries"], pos)
        if err:
            return err
        self._apply_binding(pos, b)
        return None

    def _mark_stop(self, kind, reason):
        """记下停止类别和原因。kind 是给程序读的（见 __init__ 里的取值），reason 是给人读的。

        单独一个函数是为了让「running=False + stop_reason + stop_kind」这三件事永远一起写：
        分散写的话，新增一个停止分支时很容易只补了 stop_reason，队列那边就会拿着上一次的
        stop_kind 去判断「这一趟跑完了没有」。
        """
        with self._lock:
            self.running = False
            self.stop_reason = reason
            self.stop_kind = kind

    def _trip_stop(self, reason, alert=None):
        """整趟走不下去就明确停下：文字进停止原因，需要时挂一个人工弹窗。

        为什么不复用 stop 动作：那条路只写停止原因；中途卡住（比如到了出货港却发现自己
        没货可卖）是人回来必须看一眼的，光在日志里躺着一行字容易被忽略。
        """
        self._log("stop", f"整趟停住：{reason}")
        self._stop_event.set()
        # 没有 alert = 这就是整趟正常的收尾（_do_trip_next 的 done 分支）；带 alert = 要人来看
        self._mark_stop("alert" if alert else "done", reason)
        if alert:
            self._alert_request = alert
        return f"停止（{reason}）"

    @staticmethod
    def run_state_snapshot():
        """给界面念的只读账本：这趟买过货没有（sell_pending）+ 每个港下次几点补货。

        读失败不抛：这一栏和「运行状态 + 日志」同一条 status 请求，账本读不出
        不该把整栏变成 500 —— 退回来给 {"error": "..."}，界面那行显示「读不到」。
        """
        try:
            snap = dict(run_state.load_state())
            snap["restock_at"] = run_state.load_restock()
            return snap
        except Exception as e:
            return {"error": str(e)}

    # ---------- 日志 ----------
    def _log(self, event, message):
        with self._lock:
            self._logs.append({
                "time": time.strftime("%H:%M:%S"),
                "event": event,
                "message": message,
            })
            if len(self._logs) > 100:
                self._logs = self._logs[-100:]

    # ---------- 对外控制 ----------
    def start(self, buy_port=None, buy_goods=None, sail_port=None, module=None,
              current_port=None, sell_port=None, trip_stops=None):
        """加载配置并启动后台线程。返回 {"ok": bool, "message": str}。

        module：本次跑**哪一个模块**。跑商流程被切成模块（买货 / 港口间移动 …）之后，
        同一张港口画面既可能是买货模块的起点、也可能是移动模块的起点，光看画面分不出
        该走哪条 —— 所以这一件必须由人在启动前指定，不能让引擎猜。
        启动时就把 self.states 裁成「本模块的状态 + global 状态」，之后入口检测、每轮匹配、
        goto 目标校验都只看得见这一份，跨不过模块边界。
        唯一例外是 module="trip"（完整一趟）：那次裁的是 config.trip.modules 里**几条链**的
        状态，跑到底由 trip_next 决定接哪一段（见 _start_trip）。

        current_port：船**现在停在哪个港**。两件事靠它：
        ① 移动模块拦「原地打转」（目的地就是这个港 → 出港要花真金币却哪儿也不去，直接拒）；
        ② 买货 / 卖货模块的「本次这一站」就是它（2026-09-27 你拍「认当前所在港那一站」，
          卖货那条照同一条走），对不上时界面那侧的 derived.buy_port / derived.sell_port
          会是空的 → 走到下面「启动时 xx_port 是空的」那条拒绝。

        buy_port / buy_goods / sail_port / sell_port：港口名和本次清单一起交进来，
        **哪一些算数由本模块自己决定**（见下面 uses_plan / port_field 那段），界面不必替模块猜：
        - 吃购物表格的模块（买货）→ run_port = buy_port，本次清单 = buy_goods 里那几件、
          **就按 buy_goods 的顺序**一件一件买，买完自己停。三件都由「跑商设置」当前那一站定：
          没选港拒、一件货都没勾拒、挂的货在该港目录里查不到也拒（2026-09-27 你拍板要拦；
          买货花真金币、不可逆，免得跑到一半才发现选错）。
          注意表格只是**目录**：不再自动拿「该港全部行」当本次清单。
        - 只拿港口名去搜索框打字的模块（港口间移动）→ run_port = sail_port：
          这是「船要开去的目的港」，**不要求**它出现在购物表格里，也不看 buy_goods。
        - 只核对港口名、不吃货物清单的模块（卖货）→ run_port = sell_port：
          2026-09-28 你拍的「货舱里有什么卖什么（全部添加）」，所以卖货这一站不挂 goods，
          只需要一个港名去核对「我确实站在这一趟安排的出货港」。要核对哪个字段写在
          plan_port 条件自己的 from 上（见 _plan_port_sources），不在代码里认模块名。

        trip_stops：只给 module="trip" 用 —— 「从当前所在港这一站起、到这一趟结尾」的有序站次
        （route_plan.derive 算出来的那份切片，每站 {stage, port, goods, note}）。
        整趟**没有**「一个本次港口」这回事：每一站各绑各的港名和清单（见 _bind_leg），
        所以上面那套单模块推断对 trip 完全不适用，走 _start_trip 那条路。
        """
        try:
            data = load_states()
        except Exception as e:
            return {"ok": False, "message": f"读取 states.json 失败: {e}"}

        run_port = None
        run_rows = []

        states = data.get("states", [])
        if not states:
            return {"ok": False, "message": "states.json 中没有状态，请先配置"}

        # 校验 id 非空且唯一
        ids = [s.get("id") for s in states]
        if any(not (i and str(i).strip()) for i in ids):
            return {"ok": False, "message": "存在空 id 的状态"}
        if len(set(ids)) != len(ids):
            return {"ok": False, "message": f"状态 id 重复: {ids}"}

        # 非全局状态必须标明自己属于哪个模块：漏标就等于「哪个模块都能进」，
        # 那正是这次要消灭的歧义，所以宁可拒绝启动，也不悄悄当成默认模块。
        untitled = [s.get("id") for s in states
                    if not s.get("global") and not self._module_of(s)]
        if untitled:
            return {"ok": False,
                    "message": f"这些状态没填 module（它属于哪个模块）: {untitled}"}
        modules = sorted({self._module_of(s) for s in states if not s.get("global")})
        run_module = (module or "").strip()
        if not run_module and len(modules) == 1:
            run_module = modules[0]          # 只有一个模块时不必再让人选一遍
        if not run_module:
            return {"ok": False, "message": f"没指定本次模块，可选: {'、'.join(modules) or '（states.json 里一个模块都没有）'}"}
        # 完整一趟裁的是**几条链**的状态；单模块照旧只裁那一条。
        trip_mode = run_module == route_plan.TRIP_MODULE
        if trip_mode:
            trip_modules, _ = self._trip_conf(data.get("config"))
            if not trip_modules:
                return {"ok": False,
                        "message": "完整一趟要接哪几条链没配：去 states.json 的 config.trip.modules 里列出来"}
            no_states = [m for m in trip_modules if m not in modules]
            if no_states:
                return {"ok": False,
                        "message": f"完整一趟要接的这几条链在 states.json 里没有状态: {no_states}；"
                                   f"现有模块: {'、'.join(modules)}"}
            mod_set = set(trip_modules)
            module_states = [s for s in states
                             if s.get("global") or self._module_of(s) in mod_set]
        elif run_module not in modules:
            return {"ok": False,
                    "message": f"模块『{run_module}』下没有任何状态，现有模块: {'、'.join(modules)}"}
        else:
            module_states = [s for s in states
                             if s.get("global") or self._module_of(s) == run_module]
        if not any(s.get("entry") for s in module_states):
            return {"ok": False,
                    "message": f"模块『{run_module}』里没有 entry:true 的入口状态，不知道从哪儿开始"}

        if trip_mode:
            # 整趟**没有**「一个本次港口」：下面那一整段单模块推断对它不适用，各站各绑（见 _start_trip）
            return self._start_trip(data, module_states, trip_stops, current_port)

        # 这个模块到底碰不碰购物表格、缺不缺港口名，从它自己的状态里读出来，
        # 不在代码里写死「买货才校验」—— 以后加卖货模块不用回来改这里。
        # uses_plan 现在只管「吃不吃法拿那份表格行」；核对港口名的来源另说（见 port_field）。
        uses_plan = any(self._state_touches(s, lambda n:
                            n.get("type") in ("plan_pending", "plan_advance")
                            or "templates_from_plan" in n)
                        for s in module_states)
        needs_port = any(self._state_touches(s, lambda n: n.get("text_from") == "run_port")
                         for s in module_states)
        port_fields = self._plan_port_sources(module_states)
        if len(port_fields) > 1:
            return {"ok": False,
                    "message": f"模块『{run_module}』里有多处港口名核对（plan_port），要比的港名"
                               f"却来自不同启动字段: {port_fields} —— 一个模块只认一个「本次港口」，"
                               f"要么把它们写成同一个，要么把核对拆开"}
        port_field = port_fields[0] if port_fields else None
        if port_field and port_field not in PORT_FIELD_LABELS:
            return {"ok": False,
                    "message": f"模块『{run_module}』的 plan_port 写了 from=『{port_field}』，"
                               f"启动参数里没有这个字段（可选: "
                               f"{'、'.join(sorted(PORT_FIELD_LABELS))}）"}
        if uses_plan and port_field and port_field != "buy_port":
            # 表格行是按买货港取的那一批，货在 A 港、核对却按 B 港，两边永远对不上。
            return {"ok": False,
                    "message": f"模块『{run_module}』既吃购物表格（本次买哪几件货由它定、"
                               f"表格行按买货港取），又把港口名核对写成 from=『{port_field}』—— "
                               f"吃表格的模块只能核对 buy_port"}

        # 「本次港口」取启动参数里的哪一个，仍然由模块自己的状态说了算（不写死模块名）：
        # 吃购物表格的 → buy_port（表格行就是按它取的）；只做港口名核对的（卖货）→ 它自己声明的字段；
        # 只把港口名打进搜索框的（移动）→ sail_port；三者都不碰 → run_port 留 None。
        port_values = {"buy_port": buy_port, "sell_port": sell_port, "sail_port": sail_port}
        field = port_field or ("buy_port" if uses_plan else None)
        if port_field and needs_port:
            return {"ok": False,
                    "message": f"模块『{run_module}』既要核对港口名（{port_field}）、"
                               f"又要拿港口名打进搜索框（text_from: run_port）—— 这两个用的不是同一个港，"
                               f"一个模块只认一个「本次港口」，请把买货 / 移动 / 卖货分成不同模块跑"}
        if field:
            run_port = (port_values[field] or "").strip() or None
            port_from = f"{field}（{PORT_FIELD_LABELS[field]}）"
        elif needs_port:
            run_port = (sail_port or "").strip() or None
            port_from = "sail_port（目的港，只打进搜索框）"
        else:
            port_from = None
        cur = (current_port or "").strip()

        # 需要港口名却没给 → 两种都拦（2026-09-27 你拍板：买货那边也要拦）。
        # 为什么不留着让它空跑：买货模块空着起来会一批货都不买、却照样点购买/确定，
        # 白转一圈还花钱；移动模块空着更是不知道该往搜索框里打什么字；
        # 卖货模块空着则是要在「不知道是哪个港」的画面里真把货卖掉，同样不可逆。
        if needs_port and not field and not run_port:
            return {"ok": False,
                    "message": f"模块『{run_module}』要把港口名打进搜索框，但启动时 sail_port 是空的"
                               f"（去「跑商设置」填「港口间移动的目的港」）"}
        if field and not run_port:
            why = {
                "buy_port": "（买货模块认「当前所在港」那个买货站 —— 去「跑商设置」把当前所在港"
                            "填成这一趟里某个买货港口；空着起来会一步货都不买）",
                "sell_port": "（卖货模块认「当前所在港」那个出货站 —— 去「跑商设置」把当前所在港"
                             "填成这一趟里那个出货港口；空着起来不知道要在哪个港把货卖掉）",
            }.get(field, f"（这个模块要用到 {field}）")
            if uses_plan:
                why = "；本次买哪几件货也按这个港口去购物表格取行" + why
            return {"ok": False,
                    "message": f"模块『{run_module}』要比的港口名取自启动参数 {field}，"
                               f"但启动时 {field} 是空的{why}"}

        # 原地打转：目的地就是船现在这个港。出港那一下花真金币（实测 699，每次数额会变），
        # 船却还在原地，所以直接拒绝，不弹确认让人自己判断。
        if needs_port and not field and run_port and cur and run_port == cur:
            return {"ok": False,
                    "message": f"模块『{run_module}』的目的地『{run_port}』就是当前所在港 —— "
                               f"出港要花真金币，同港不叫移动"}

        if uses_plan:
            try:
                plan = purchase_plan.load_plan()
            except Exception as e:
                return {"ok": False, "message": f"读取 purchase_plan.json 失败: {e}"}
            # 本次买哪几件、按什么顺序买，来自「跑商设置」里当前这一站勾的货（2026-09-27 你改的分工：
            # 表格只是目录，不再自动等于「该港全部行」）。类别仍然从目录那行取 —— 只信一个来源。
            want = [g for g in (buy_goods or []) if (g or "").strip()]
            if not want:
                return {"ok": False,
                        "message": f"模块『{run_module}』本次在『{run_port}』没勾任何货物 —— "
                                   f"空着起来会一步货都不买、却照样点购买/确定，"
                                   f"去「跑商设置」在当前所在港那一站勾上要买的货"}
            # 剩下的核对（这个港在表格里有没有行、这几件查不查得到、类别合不合法）
            # 和整趟每一站用的是同一份代码，见 _plan_rows_for
            rows, err = self._plan_rows_for(plan, run_port, want)
            if err:
                return {"ok": False, "message": err}
            run_rows = rows

        started = self._launch(data, module_states, run_module,
                               run_port=run_port, run_port_field=field,
                               run_rows=run_rows)
        if not started["ok"]:
            return started

        entry_ids = [s["id"] for s in module_states if s.get("entry")]
        desc = ", ".join(entry_ids) if entry_ids else f"无 entry，默认 {self.current_state}"
        self._log("start", f"本次模块『{run_module}』：{len(module_states)} 个状态"
                           f"（全局 {sum(1 for s in module_states if s.get('global'))} 个）")
        if run_rows:
            names = "、".join(f"{i + 1}. {r['goods_name']}" for i, r in enumerate(run_rows))
            self._log("start", f"本次买货港『{run_port}』（= 当前所在港那一站）挂 {len(run_rows)} 件，"
                               f"按「跑商设置」里的先后买完就停：{names}")
        elif run_port:
            # 不吃表格的模块（卖货核对港名 / 移动把港名打进搜索框）：说清这个港名是哪来的，
            # 不然日志里「本次港口」到底是买货港、出货港还是目的港，看的人分不出。
            self._log("start", f"本次港口『{run_port}』取自 {port_from}，不套用购物表格")
            if needs_port:
                self._log("start", f"当前所在港『{cur or "(没填)"}』"
                                   + ("已确认与目的港不是同一个（同港不出航）" if cur
                                      else "没填，没法核对是否原地打转"))
        else:
            self._log("start", "未选择港口（本次运行不套用购物表格）")
        self._log("start", f"状态机启动，候选入口({len(entry_ids)}): {desc}")
        return {"ok": True,
                "message": f"已启动（模块 {run_module}），首轮自动检测入口（候选: {desc}）"}

    # ---------- 起线程 ----------
    def _launch(self, data, module_states, run_module, *, run_port=None,
                run_port_field=None, run_rows=None, trip=None, current_state=None):
        """把运行态一次性摆正，然后起后台线程。单模块和整趟共用这一处。

        为什么抽出来：两处各写一遍「复位清单」，早晚会有一份漏掉一个字段（比如新加的
        self.trip 只在一条路里清），下一次启动就留着上一次的东西 —— 这种 bug 最难查。
        所有字段都在 self.running 置 True 之前摆好，后台线程不可能读到半个初始化的状态。
        """
        with self._lock:
            if self.running:
                return {"ok": False, "message": "状态机已在运行"}
            self.config = data.get("config", dict(DEFAULT_CONFIG))
            self.states = module_states
            self.run_module = run_module
            self.run_port = run_port
            # run_port 出自哪个字段（buy_port / sell_port / sail_port）：日志和详情靠它把
            # 「本次买货港 / 本次出货港 / 目的港」分开说清，都叫「本次港口」会骗人。
            self.run_port_field = run_port_field
            self.run_rows = run_rows or []
            self.trip = trip
            # 多入口：单模块这里先放第一个 entry 作为兜底，真正的入口由后台线程截图检测后
            # 决定（检测要截图 + 匹配，放在 start() 里会阻塞 HTTP 请求）；整趟则由当前所在港
            # 直接定死起点（current_state），不必猜。
            self.current_state = current_state or next(
                (s["id"] for s in module_states if s.get("entry")), module_states[0]["id"])
            self.round = 0
            self.no_match_count = 0
            self.stop_reason = None
            self.stop_kind = None
            self.run_row_index = 0
            self.restock = None
            self._logs = []
            self._stop_event.clear()
            self._alert_request = None
            self._pending_goto = None
            self.running = True
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
        return {"ok": True, "message": "已启动"}

    def _start_trip(self, data, module_states, trip_stops, current_port):
        """完整一趟（module="trip"）的启动：先把**每一站**都算一遍，全算得通才起线程。

        为什么要预先逐站核对：整趟一口气要花掉好几次真金币（出港一次实测 699、买货另算），
        走到第 3 站才发现那一站的货在表格里查不到，前两站的船票和货钱就白扔了。
        单模块那套「一个本次港口」的推断对整趟不适用 —— 各站各绑，见 _leg_binding。
        """
        raw = trip_stops if isinstance(trip_stops, list) else []
        stops = [s for s in raw if isinstance(s, dict)]
        cur = (current_port or "").strip()
        if not stops:
            return {"ok": False,
                    "message": "完整一趟没收到站次（启动时 trip_stops 是空的）—— "
                               "先去「跑商设置」把进货（可选中转）和卖货排好"}
        if len(stops) != len(raw):
            return {"ok": False,
                    "message": f"完整一趟的站次里有 {len(raw) - len(stops)} 条不是一站该有的样子"
                               f"（要 {{stage, port, goods, note}} 那种对象）"}
        for i, st in enumerate(stops):
            if not (st.get("port") or "").strip():
                return {"ok": False,
                        "message": f"完整一趟第 {i + 1} 站没填港口名，不知道要把船开去哪"}
        # 连着两站同港 = 花真金币出一趟港却回到原地（和单模块那条「原地打转」拒绝同一条理由）
        for i in range(len(stops) - 1):
            a = (stops[i].get("port") or "").strip()
            if a == (stops[i + 1].get("port") or "").strip():
                return {"ok": False,
                        "message": f"完整一趟第 {i + 1}、{i + 2} 站都是『{a}』—— 出港要花真金币，"
                                   f"连着两站同港不叫移动"}

        # 每一类站都要有状态用 trip_entry 认领；「再出一次港」那条（transit）不管这趟排没排
        # 中转站都用得到 —— 每一站做完接下一站，靠的就是它。
        entries = {}
        for stage in sorted({(s.get("stage") or "").strip() for s in stops} | {"transit"}):
            sid = self._trip_state_for(module_states, stage)
            if not sid:
                return {"ok": False,
                        "message": f"这一趟要用『{route_plan.STAGE_LABELS.get(stage, stage)}』这类站，"
                                   f"但 states.json 里没哪个状态声明 trip_entry: {stage}"
                                   f"（可选状态: {', '.join(s.get('id') for s in module_states if s.get('entry'))}）"}
            entries[stage] = sid

        try:
            plan = purchase_plan.load_plan()
        except Exception as e:
            return {"ok": False, "message": f"读取 purchase_plan.json 失败: {e}"}
        for i in range(len(stops)):
            _, err = self._leg_binding(stops, entries, i, plan=plan)
            if err:
                return {"ok": False, "message": f"这一趟走不完：{err}"}

        # 起点正好是出货站：先看「待卖账」，是 0 就说明舱里没货要卖，别花那张船票钱
        if (stops[0].get("stage") or "").strip() == "sell" and not run_state.get_flag("sell_pending"):
            return {"ok": False,
                    "message": f"这一趟从出货站『{(stops[0].get('port') or '').strip()}』开始，"
                               f"但待卖账是 0（这之前没买过货）—— 舱里没有要卖的货，"
                               f"先跑进货那一段，或直接选一个买货站起步"}

        b, err = self._leg_binding(stops, entries, 0, plan=plan)   # 上面已逐站验过，这里必定成功
        if err:
            return {"ok": False, "message": f"这一趟走不完：{err}"}
        started = self._launch(data, module_states, route_plan.TRIP_MODULE,
                               run_port=b["run_port"], run_port_field=b["run_port_field"],
                               run_rows=b["run_rows"], current_state=b["current_state"],
                               trip={"stops": stops, "entries": entries, "pos": 0})
        if not started["ok"]:
            return started

        self._log("start", f"本次模块『{route_plan.TRIP_MODULE}』（完整一趟）："
                           f"{len(module_states)} 个状态，"
                           f"全局 {sum(1 for s in module_states if s.get('global'))} 个")
        self._log("start", "整趟 " + str(len(stops)) + " 站：" +
                  " → ".join(self._trip_leg_text(s) for s in stops))
        self._log("start", f"当前所在港『{cur or '(没读到)'}』，从第 1 站 "
                           f"{self._trip_leg_text(stops[0])} 走起（到港那一刻由 trip_next 接下一站）")
        self._log("trip", f"第 1/{len(stops)} 站 {self._trip_leg_text(stops[0])}：{b['note']}")
        return {"ok": True,
                "message": f"已启动（完整一趟 {len(stops)} 站），从 "
                           f"{self._trip_leg_text(stops[0])} 走起，全程自动接下一站"}

    def stop(self):
        """手动停止。返回 {"ok": bool, "message": str}。"""
        with self._lock:
            if not self.running:
                return {"ok": False, "message": "状态机未运行"}
            self._stop_event.set()
        # _mark_stop 自己会拿锁，所以上面那段必须先出锁再调它（Lock 不可重入）
        self._mark_stop("manual", "手动停止")
        self._log("stop", "手动停止")
        return {"ok": True, "message": "已停止"}

    def is_running(self):
        with self._lock:
            return self.running

    def status(self):
        """返回运行状态 + 最近日志。"""
        with self._lock:
            return {
                "running": self.running,
                "module": self.run_module,
                "current_state": self.current_state,
                "round": self.round,
                "no_match_count": self.no_match_count,
                "stop_reason": self.stop_reason,
                "stop_kind": self.stop_kind,
                "run_port": self.run_port,
                "run_port_field": self.run_port_field,
                "run_state": self.run_state_snapshot(),
                "run_row": purchase_plan.decorate(self.run_row) if self.run_row else None,
                "run_rows": [purchase_plan.decorate(r) for r in self.run_rows],
                "run_row_index": self.run_row_index,
                "run_pending": self.run_row_index < len(self.run_rows),
                "restock": self.restock,
                "trip": self._trip_snapshot(),
                "state_ids": [s.get("id") for s in self.states],
                "logs": list(self._logs),
            }

    # ---------- 条件测试（给前端编辑器用） ----------
    def _test_screen_path(self):
        """条件测试用的独立截图文件，避免和运行中的引擎、其它接口抢同一个文件。"""
        root, ext = os.path.splitext(self.screen_path)
        return f"{root}_test{ext}"

    def test_condition(self, state_id):
        """对指定状态实时评估一次 condition，返回 {ok, matched, confidence, detail}。"""
        try:
            data = load_states()
        except Exception as e:
            return {"ok": False, "message": f"读取 states.json 失败: {e}"}

        st = next((s for s in data.get("states", []) if s.get("id") == state_id), None)
        if st is None:
            return {"ok": False, "message": f"状态不存在: {state_id}"}
        cond = st.get("condition")
        if not cond:
            return {"ok": False, "message": f"状态『{state_id}』没有配置 condition"}

        ctl = MuMuController(port=self.port, adb_path=self.adb_path)
        try:
            ctl._run("-s", ctl.addr, "connect", ctl.addr)
        except FileNotFoundError:
            return {"ok": False, "message": f"找不到 adb: {self.adb_path}"}
        if self._device_state(ctl) != "device":
            return {"ok": False, "message": "ADB 未连接，请确认模拟器已启动"}

        path = self._test_screen_path()
        snap_ok, snap_info = self._snap(ctl, path, "条件测试截图")
        if not snap_ok:
            return {"ok": False, "message": snap_info}

        try:
            info = self._eval_condition_detail(cond, path)
        except Exception as e:
            return {"ok": False, "message": f"条件评估异常: {type(e).__name__}: {e}"}
        info["ok"] = True
        return info

    def _eval_condition_detail(self, cond, screen_path):
        """评估条件并给出可读细节，返回 {matched, confidence, detail}（供编辑器显示）。

        与 _check_condition 的区别：模板条件用 threshold=0 取「原始最高置信度」，
        这样未达标时也能看到具体差多少，而不是只得到 False。
        """
        if not cond:
            return {"matched": False, "confidence": None, "detail": "条件为空（永不匹配）"}
        t = cond.get("type")

        if t == "template":
            name = cond.get("name")
            path, roi, thr = self._resolve_template(name)
            if not path:
                return {"matched": False, "confidence": None, "detail": f"模板不存在: {name}"}
            threshold = float(cond.get("threshold", thr))
            raw = find_template(screen_path, path, roi=roi, threshold=0.0)
            conf = round(float(raw["confidence"]), 4) if raw else 0.0
            matched = conf >= threshold
            pos = f"，位置 ({raw['cx']},{raw['cy']})" if matched else ""
            return {"matched": matched, "confidence": conf,
                    "detail": f"模板『{name}』最高置信度 {conf}（阈值 {threshold}）{pos}"}

        if t == "ocr":
            region, kw = cond.get("region"), cond.get("contains")
            try:
                res = ocr_find_in_region(screen_path, kw, region)
            except ValueError as e:
                return {"matched": False, "confidence": None, "detail": str(e)}
            text = (res.get("text") or "")[:60]
            return {"matched": res["found"], "confidence": None,
                    "detail": f"区域『{region}』OCR 文字: {text!r}，"
                              f"关键词『{kw}』{'命中' if res['found'] else '未命中'}"}

        # 下面两个是「不看画面」的内存判据，主循环的 _check_condition 里也有同名分支。
        # 这里必须跟着一起加，否则网页的「测试条件」按钮会把它们报成未知类型，
        # 启动前就没法确认港口核对写得对不对。
        if t == "plan_pending":
            left = len(self.run_rows) - self.run_row_index
            return {"matched": left > 0, "confidence": None,
                    "detail": (f"购物表格『{self.run_port or '(未选港口)'}』还有 {left} 行没买"
                               f"（第 {min(self.run_row_index + 1, len(self.run_rows))}/{len(self.run_rows)} 行起）"
                               if left > 0 else
                               f"购物表格『{self.run_port or '(未选港口)'}』{len(self.run_rows)} 行已全部买完")}

        if t == "flag":
            name = cond.get("name")
            want = cond.get("equals", True)
            try:
                now = run_state.get_flag(name)
                need = run_state.as_bool(want)
            except ValueError as e:
                return {"matched": False, "confidence": None, "detail": str(e)}
            return {"matched": now == need, "confidence": None,
                    "detail": f"运行态开关『{name}』当前 {int(now)}，要求 {int(need)}"
                              f"（值存在 {os.path.basename(run_state.RUN_STATE_JSON)}，"
                              f"买完货置 1、卖出完成置 0）"}

        if t == "plan_port":
            port_what = PORT_FIELD_LABELS.get(self.run_port_field) or "本次港口"
            if not self.run_port:
                return {"matched": True, "confidence": None,
                        "detail": f"港口核对：启动时没选「{port_what}」，无据可比，按通过处理"
                                  f"（真启动会被拒绝，这条只在没跑起来时点得动）"}
            region = cond.get("region") or "港口名字"
            try:
                res = ocr_find_in_region(screen_path, self.run_port, region)
            except ValueError as e:
                return {"matched": False, "confidence": None, "detail": str(e)}
            text = (res.get("text") or "")[:60]
            return {"matched": res["found"], "confidence": None,
                    "detail": f"区域『{region}』OCR 文字: {text!r}，本次「{port_what}」"
                              f"（启动参数 {self.run_port_field}）是『{self.run_port}』"
                              f"{'命中' if res['found'] else '未命中'}"}

        if t in ("any", "all"):
            subs = cond.get("conditions", [])
            infos = [self._eval_condition_detail(c, screen_path) for c in subs]
            if t == "any":
                matched = any(i["matched"] for i in infos)
            else:
                matched = bool(infos) and all(i["matched"] for i in infos)
            confs = [i["confidence"] for i in infos if i["confidence"] is not None]
            conf = (max(confs) if t == "any" else min(confs)) if confs else None
            hits = sum(1 for i in infos if i["matched"])
            detail = (f"{t}（{len(subs)} 个子条件，{hits} 个命中）: "
                      + "；".join(f"[{'✓' if i['matched'] else '×'}] {i['detail']}" for i in infos))
            return {"matched": matched, "confidence": conf, "detail": detail}

        return {"matched": False, "confidence": None, "detail": f"未知条件类型: {t}"}

    # ---------- 启动前退回港口界面 ----------
    def _match_exit_button(self, screen_path, names):
        """在右上角图标槽里找退出按钮（房子 / X）。命中返回结果 dict（带 name），否则 None。

        两个图标共用同一个 ROI，理论上只会命中一个；万一同时命中就取置信度高的，
        不做任何模糊匹配。
        """
        best = None
        for name in names:
            path, roi, thr = self._resolve_template(name)
            if not path:
                self._log("error", f"退出按钮模板不存在: {name}")
                continue
            try:
                res = find_template(screen_path, path, roi=roi, threshold=thr)
            except Exception as e:
                self._log("error", f"模板『{name}』匹配异常: {type(e).__name__}: {e}")
                continue
            if res and (best is None or res["confidence"] > best["confidence"]):
                res["name"] = name
                best = res
        return best

    def _exit_to_port(self, ctl):
        """启动前把画面退回港口界面：看到房子（建筑内部页）或 X（菜单页）就点掉。

        右上角 [1500,6,86,70] 这个槽位互斥显示 ≡ / 房子 / X，所以匹配到这个位置
        有什么图标，就知道当前在哪一层；两个退出图标都不在，说明已经回到港口界面。

        只在启动时调用一次，绝不能做成每轮检查：买货本身就在交易所页面里进行，
        每轮检查会让引擎一边买一边把自己退出去。

        任何一步失败（ADB 没就绪 / 截图失败 / 模板缺失 / 点了上限还没退出去）
        都只记日志，不拦启动 —— 退不出去最多让后面的入口检测自己再判断。
        """
        cfg = self.config.get("exit_to_port") or {}
        if not cfg.get("enabled", True):
            self._log("exit_port", "启动前退出港口界面：配置里已关闭，跳过")
            return

        names = cfg.get("buttons") or []
        max_clicks = int(cfg.get("max_clicks", 5))
        wait = int(cfg.get("wait_ms", 1200)) / 1000
        port_marker = cfg.get("port_marker")
        # 独立截图文件：不和主循环那张 state_screen.png 抢
        screen = self._cap_path("exit")

        clicks = 0
        for _ in range(max_clicks):
            if self._stop_event.is_set():
                return
            if self._device_state(ctl) != "device":
                self._log("warn", "退出港口界面：ADB 未就绪，跳过退出检查继续启动")
                return
            ok, info = self._snap(ctl, screen, "退出港口界面截图")
            if not ok:
                self._log("warn", f"退出港口界面：{info}，跳过退出检查继续启动")
                return

            hit = self._match_exit_button(screen, names)
            if not hit:
                # 两个退出图标都不在 = 已经不在建筑内部页 / 菜单页，停手
                break

            # 只有匹配到图标才点：这个位置在港口界面是 ≡ 菜单，盲点会把菜单打开
            px, py, note = self._tap(ctl, hit)
            clicks += 1
            self._log("exit_port",
                      f"第 {clicks}/{max_clicks} 次：匹配到『{hit['name']}』"
                      f"{note} 置信度{hit['confidence']}，点击退出")
            self._sleep_interruptible(wait)
        else:
            self._log("warn", f"点了 {max_clicks} 次仍未退出到港口界面，停止点击")

        if not port_marker:
            return
        m_path, m_roi, m_thr = self._resolve_template(port_marker)
        if not m_path:
            self._log("error", f"到港判据模板不存在: {port_marker}，无法确认")
            return
        ok, info = self._snap(ctl, screen, "到港确认截图")
        if not ok:
            self._log("warn", f"到港确认：{info}")
            return
        try:
            res = find_template(screen, m_path, roi=m_roi, threshold=m_thr)
        except Exception as e:
            self._log("error", f"到港确认匹配异常: {type(e).__name__}: {e}")
            return
        if res:
            self._log("exit_port",
                      f"已在港口界面（{port_marker} 置信度{res['confidence']}），"
                      f"本次共点退出 {clicks} 次")
        else:
            self._log("warn",
                      f"未能确认在港口界面（{port_marker} 未命中），仍继续启动")

    # ---------- 入口检测 ----------
    def _detect_entry(self, ctl):
        """启动时按 states 顺序检测本模块所有 entry:true 的状态，第一个匹配的作为起点。

        都不匹配时记日志并退回第一个 entry（正常循环继续跑，不阻塞启动）。
        只看本模块的入口 —— 别的模块的起点画面长得可能一模一样，猜不得。
        """
        entries = [s for s in self.states if s.get("entry")]
        if not entries:
            self.current_state = self.states[0]["id"]
            self._log("start", f"没有状态标记 entry，默认从第一个状态开始: {self.current_state}")
            return

        if self._stop_event.is_set():
            return

        if self._device_state(ctl) != "device":
            self.current_state = entries[0]["id"]
            self._log("warn", f"入口检测时 ADB 未就绪，暂用第一个入口: {self.current_state}")
            return

        snap_ok, snap_info = self._snap(ctl, self.screen_path, "入口检测截图")
        if not snap_ok:
            self.current_state = entries[0]["id"]
            self._log("warn", f"{snap_info}，暂用第一个入口: {self.current_state}")
            return

        self._log("start", f"入口检测：按顺序检查 {len(entries)} 个入口状态 "
                           f"({', '.join(s['id'] for s in entries)})")
        for s in entries:
            if self._stop_event.is_set():
                return
            try:
                if self._check_condition(s.get("condition"), self.screen_path):
                    self.current_state = s["id"]
                    label = s.get("name") or s["id"]
                    self._log("start", f"入口检测命中『{label}』，从状态 {s['id']} 开始")
                    return
            except Exception as e:
                self._log("error", f"入口状态『{s['id']}』条件判断异常: {type(e).__name__}: {e}")

        self.current_state = entries[0]["id"]
        self._log("warn", f"启动时未检测到任何入口状态，暂用第一个入口: {self.current_state}")

    # ---------- 主循环 ----------
    def _loop(self):
        ctl = MuMuController(port=self.port, adb_path=self.adb_path)
        ctl._run("-s", ctl.addr, "connect", ctl.addr)

        # 启动前先把画面退回港口界面，再选入口
        self._exit_to_port(ctl)

        if self.trip:
            # 整趟不做入口猜测：起点是「当前所在港是第几站」算出来的，已经定死在 current_state。
            # 万一画面和它不符（比如人其实站在另一个港），下一步的港口名核对会拦下来 ——
            # 比让引擎看图猜「这画面像是买货的起点」安全得多。
            self._log("start", f"完整一趟：起点已定『{self.current_state}』，不做入口检测")
        else:
            # 启动时先选入口（多入口自动检测），再进入正常循环
            self._detect_entry(ctl)

        try:
            while not self._stop_event.is_set():
                # 整趟要走 3~5 条链，轮数天然比单模块多得多，所以另配一份上限
                # （见 config.trip.max_rounds；单模块那份 60 轮对整趟来说不够）
                cap = (self._trip_conf(self.config)[1] if self.trip
                       else int(self.config.get("max_rounds", 1000)))
                if self.round >= cap:
                    self._finish("达到轮次上限")
                    break

                self.round += 1
                max_no_match = int(self.config.get("max_no_match", 5))
                interval = int(self.config.get("interval_ms", 500)) / 1000

                # 1. 截图（主循环固定用 state_screen.png，其它用途各有各的文件）
                if self._device_state(ctl) != "device":
                    self._log("error", f"第 {self.round} 轮：ADB 未就绪，跳过本轮")
                    self._sleep_interruptible(interval)
                    continue
                snap_ok, snap_info = self._snap(ctl, self.screen_path, f"第 {self.round} 轮截图")
                if not snap_ok:
                    self._log("error", snap_info)
                    self._sleep_interruptible(interval)
                    continue

                # 2. 组装本轮候选：先所有 global 状态（按 states 顺序），再当前状态
                current = self._find_state(self.current_state)
                if current is None:
                    self._finish(f"下一个状态不存在: {self.current_state}")
                    break
                globals_ = [s for s in self.states if s.get("global")]
                candidates = globals_ + (
                    [current] if current.get("id") not in {s.get("id") for s in globals_} else []
                )

                # 3. 按顺序匹配，命中第一个即停止检查
                matched_state = None
                for cand in candidates:
                    if self._stop_event.is_set():
                        break
                    try:
                        if self._check_condition(cand.get("condition"), self.screen_path):
                            matched_state = cand
                            break
                    except Exception as e:
                        self._log("error", f"条件判断异常: {type(e).__name__}: {e}")

                # 4. 全部未命中
                if matched_state is None:
                    self.no_match_count += 1
                    cname = current.get("name") or current.get("id")
                    self._log("no_match", f"状态『{cname}』未匹配（{self.no_match_count}/{max_no_match}）")
                    if self.no_match_count >= max_no_match:
                        self._finish(f"连续 {max_no_match} 轮无匹配")
                        break
                    self._sleep_interruptible(interval)
                    continue

                # 5. 命中：重置无匹配计数，按顺序执行动作
                self.no_match_count = 0
                self._pending_goto = None   # 本轮动作里没人说要去哪，默认走状态上写死的 next
                name = matched_state.get("name") or matched_state.get("id")
                self._log("match", f"匹配到状态『{name}』{'（全局）' if matched_state.get('global') else ''}")
                for action in matched_state.get("actions", []):
                    if self._stop_event.is_set() or self._pending_goto:
                        break  # 手动停止、或动作里已经 goto，后面的不再执行
                    ok, msg = self._run_action_with_timeout(action, ctl, self.screen_path)
                    self._log("action" if ok else "error", f"[{action.get('type')}] {msg}")
                if self._stop_event.is_set():
                    break  # 手动停止，直接退出

                # 6. 跳转下一状态：动作里的 goto 优先于状态上写死的 next
                if self._pending_goto:
                    nxt = self._pending_goto
                    self._pending_goto = None
                    self._log("transition", f"状态切换（goto）: {matched_state.get('id')} -> {nxt}")
                    self.current_state = nxt
                else:
                    nxt = matched_state.get("next")
                    self._log("transition", f"状态切换: {matched_state.get('id')} -> {nxt or '(终点)'}")
                    self.current_state = nxt
                    if not nxt:
                        self._finish("到达终点", kind="done")
                        break

                self._sleep_interruptible(interval)
        except Exception as e:
            self._finish(f"引擎异常: {type(e).__name__}: {e}")
        finally:
            # 人工提醒放在这里弹，而不是在动作里弹：MessageBoxW 会一直卡住调用它的线程，
            # 如果卡在第 5 步的动作循环里，人就再也点不动「启动」之外的任何收尾；
            # 更要紧的是旧循环还挂在弹窗上时，人如果重新点了启动，会同时跑两个 _loop。
            # 退出时弹一次，弹完这个线程就真的结束了。
            req, self._alert_request = self._alert_request, None
            if req:
                self._alert(req[0], req[1])

    def _finish(self, reason, kind="error"):
        """标记停止（自动停止 / 异常）。只在需要时写入，避免覆盖手动停止。

        kind 默认 "error"：这里绝大多数调用是「出事了才停」（轮次上限、状态不存在、
        连续无匹配、引擎异常）。只有「到达终点」是整趟正常走到头，调用处显式传 "done"。
        """
        with self._lock:
            if self.running:
                self.running = False
                self.stop_reason = reason
                self.stop_kind = kind
        self._log("stop", f"停止: {reason}")

    def _sleep_interruptible(self, seconds):
        """可被 stop() 打断的睡眠。"""
        end = time.time() + seconds
        while time.time() < end and not self._stop_event.is_set():
            time.sleep(0.05)

    # ---------- 人工提醒 ----------
    def _alert(self, title, message):
        """Windows 阻塞式置顶弹窗：人回来点掉之前不会往下走。

        为什么用「阻塞弹窗」而不是日志或声音（用户 2026-09-27 拍板：提醒只要弹窗）：
        会走到这一步，说明状态机自己已经没有别的办法往下推进了，弹窗本身就是那道闸 ——
        不点掉就不继续。日志要人主动去翻，提示音一次就过、人不在跟前等于没响。
        弹窗发不出去（非 Windows、没有桌面会话）只记一条 warn，绝不影响停止本身生效。
        """
        self._log("error", f"[需要人工] {title}：{message}")
        try:
            MB_OK = 0x0
            MB_ICONERROR = 0x10     # 红色叉图标
            MB_TOPMOST = 0x40000    # 置顶：其它窗口盖着也能被看见
            ctypes.windll.user32.MessageBoxW(0, str(message), str(title),
                                             MB_OK | MB_ICONERROR | MB_TOPMOST)
        except Exception as e:
            self._log("warn", f"弹窗提醒发不出去（{type(e).__name__}: {e}），只留日志")

    # ---------- 点击 ----------
    def _tap(self, ctl, res, label=""):
        """点击一个匹配结果，返回 (实际落点x, 落点y, 写日志用的说明)。

        config.human_click.enabled 为真、且匹配结果带矩形（模板匹配都有）时：
        先在矩形四周各留 inset_ratio 的边距，再在剩下的范围里随机取点，
        点击前随机等 delay_min_ms ~ delay_max_ms —— 落点和节奏都像人手，不总点同一个像素。
        没有矩形（OCR 结果）、或功能关掉，就照原样点中心。
        矩形太小（留边后可用宽度不足 1 像素）也退回中心：随机反而可能点到框外。
        """
        cx, cy = int(res["cx"]), int(res["cy"])
        cfg = self.config.get("human_click") or {}
        rect = res.get("rect")
        if not (cfg.get("enabled", True) and rect):
            ctl.click(cx, cy)
            return cx, cy, f"({cx},{cy})"

        x, y, w, h = rect
        ratio = float(cfg.get("inset_ratio", 0.18))
        ix, iy = w * ratio, h * ratio
        rw, rh = w - 2 * ix, h - 2 * iy
        if rw < 1 or rh < 1:
            ctl.click(cx, cy)
            return cx, cy, f"({cx},{cy})（矩形 {w}x{h} 太小，退回中心）"

        delay = random.uniform(float(cfg.get("delay_min_ms", 120)),
                               float(cfg.get("delay_max_ms", 450))) / 1000
        self._sleep_interruptible(delay)
        px = int(round(x + ix + random.uniform(0, rw)))
        py = int(round(y + iy + random.uniform(0, rh)))
        ctl.click(px, py)
        return px, py, f"({px},{py})[随机，中心 {cx},{cy}]"

    # ---------- 辅助 ----------
    def _device_state(self, ctl):
        """执行 adb devices，返回目标设备状态，未列出返回 None。"""
        proc = ctl._run("devices")
        if proc.returncode != 0:
            return None
        for line in proc.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] == ctl.addr:
                return parts[1]
        return None

    def _find_state(self, sid):
        for s in self.states:
            if s.get("id") == sid:
                return s
        return None

    def _load_templates(self):
        if not os.path.exists(TEMPLATES_JSON):
            return []
        with open(TEMPLATES_JSON, "r", encoding="utf-8") as f:
            return json.load(f)

    def _resolve_template(self, name):
        """按名称查模板，返回 (绝对路径, roi, threshold)，找不到返回 (None, None, None)。"""
        for t in self._load_templates():
            if t.get("name") == name:
                rel = t.get("image")
                if not rel:
                    return None, None, None
                path = os.path.join(BASE_DIR, rel)
                roi = tuple(t["roi"]) if t.get("roi") else None
                threshold = t.get("threshold", 0.8)
                return path, roi, threshold
        return None, None, None

    def _cap_path(self, prefix):
        """给某一类用途分配独立的截图文件（不同前缀 = 不同文件，互不覆盖）。

        主循环用 state_screen.png；动作内部各用各的：
        optional（可选点击）/ if（分支判断）/ anchor（锚点后点击）/ buy（批量买）/ nego（协商）。
        以前全都共用一个 state_screen.png，一旦有线程在写、另一个在读，
        就会 WinError 32（文件被占用）或 cv2 读到半截空文件。
        """
        return os.path.join(BASE_DIR, f"state_screen_{prefix}.png")

    def _snap(self, ctl, path, what="截图"):
        """ADB 截图的唯一入口，实际串行化在 mumu_controller.capture。

        返回 (是否成功, 说明或失败原因)。锁是模块级共用的：调试台点「刷新截图」
        和引擎每轮截图排在同一把锁上，不会并发抢 ADB / 抢同一个 png。
        """
        return capture(ctl, path, what)

    def _capture(self, ctl, prefix):
        """重新截一张最新画面到该用途的独立文件，返回可用于读取的文件路径。

        轮次开始时那张截图在点击之后可能已经过期（弹窗出现/消失），
        所以 click_template_optional 和 if 判断前都要刷新。
        截图失败不抛异常：退回本轮主循环那张画面，避免一次抖动就中断流程。
        """
        path = self._cap_path(prefix)
        ok, info = self._snap(ctl, path, "刷新截图")
        if not ok:
            self._log("warn", f"{info}，沿用上一张画面继续判断")
            if os.path.exists(self.screen_path):
                return self.screen_path
        return path

    def _find_ocr_region_roi(self, name):
        """按名称查 ocr_regions.json，返回区域 roi (x,y,w,h)，找不到或无 roi 返回 None。"""
        if not os.path.exists(OCR_REGIONS_JSON):
            return None
        with open(OCR_REGIONS_JSON, "r", encoding="utf-8") as f:
            regions = json.load(f)
        for r in regions:
            if r.get("name") == name:
                roi = r.get("roi")
                return tuple(roi) if roi else None
        return None

    # ---------- 条件判断 ----------
    def _check_condition(self, cond, screen_path):
        """判断单个条件是否成立。cond 为 None 视为永不匹配。"""
        if not cond:
            return False
        t = cond.get("type")

        if t == "template":
            path, roi, thr = self._resolve_template(cond["name"])
            if not path:
                self._log("error", f"条件引用的模板不存在: {cond.get('name')}")
                return False
            threshold = cond.get("threshold", thr)
            return find_template(screen_path, path, roi=roi, threshold=threshold) is not None

        if t == "ocr":
            try:
                return ocr_find_in_region(screen_path, cond["contains"], cond["region"])["found"]
            except ValueError as e:
                self._log("error", str(e))
                return False

        if t == "any":
            return any(self._check_condition(c, screen_path) for c in cond.get("conditions", []))
        if t == "all":
            return all(self._check_condition(c, screen_path) for c in cond.get("conditions", []))

        if t == "plan_pending":
            # 不看画面，只看内存里的表格游标：本港口还有没买的行就返回 True。
            # 这是「买完就停」的判据 —— 不需要在游戏里认字。
            return self.run_row_index < len(self.run_rows)

        if t == "flag":
            # 运行态开关（落盘在 run_state.json），同样不看画面。
            # 卖货链的链头靠它：2026-09-28 实测「出售」按钮亮/灰分不出来（金色 1.0000、
            # 灰色 0.9039，换只含底色的裁法又会被购买页那个同槽位的金色按钮 1.0000 命中），
            # 所以「有没有货要卖」不看画面猜，按你定的规则记：买完货置 1、卖出完成置 0。
            name = cond.get("name")
            want = cond.get("equals", True)
            try:
                return run_state.get_flag(name) == run_state.as_bool(want)
            except ValueError as e:
                self._log("error", f"flag 条件用不了: {e}")
                return False

        if t == "plan_port":
            # 到港口后先核对港口名：画面那块区域 OCR 出来的字必须包含本次表格里安排的港口名。
            # 港口名取自启动前人选的港口（self.run_port），不写死在配置里；只做「包含」判断，不用正则。
            # 对不上就是不匹配 —— 状态机不会按 3 进交易所，更买不到货，宁可停。
            if not self.run_port:
                self._log("info", "港口核对：本次没选港口，无据可比，跳过")
                return True
            region = cond.get("region") or "港口名字"
            try:
                res = ocr_find_in_region(screen_path, self.run_port, region)
            except ValueError as e:
                self._log("error", f"港口核对失败：{e}")
                return False
            read = (res.get("text") or "").strip()
            # 日志别说「要买的港」：卖货模块核对的是出货港，说反了会把人带偏（见 PORT_FIELD_LABELS）
            port_what = PORT_FIELD_LABELS.get(self.run_port_field) or "本次港口"
            if res["found"]:
                self._log("match", f"港口核对通过：区域『{region}』读到『{read}』，"
                                   f"{port_what}就是『{self.run_port}』")
            else:
                self._log("error", f"港口核对不通过：区域『{region}』读到『{read or '(空)'}』，"
                                   f"{port_what}安排的是『{self.run_port}』 —— 不是这个港，一步都不会走")
            return res["found"]

        self._log("error", f"未知条件类型: {t}")
        return False

    # ---------- 动作执行 ----------
    def _run_action_with_timeout(self, action, ctl, screen_path):
        """执行单个动作，返回 (成功?, 消息)。返回前保证动作已「真正结束」。

        串行保证：无论动作是否超时，都必须等它收尾才返回。
        以前超时就立刻返回，但线程杀不掉、动作还在后台继续截图点击，
        于是它和下一轮同时操作 —— 这就是 WinError 32 / 读到空文件的根源。
        所以超时只当警告：记一条日志，然后继续等它结束。
        """
        # 动作自己写了 timeout_seconds 就用它的：等待类动作（wait_restock）天生要跑几十分钟，
        # 套全局那 120 秒每轮都会误报一句「动作超时」，看日志的人反而以为出了事。
        timeout = int(action.get("timeout_seconds")
                      or self.config.get("action_timeout_seconds", 10))
        out = {}

        def worker():
            try:
                out["msg"] = self._do_action(action, ctl, screen_path)
                out["ok"] = True
            except Exception as e:
                out["ok"] = False
                out["msg"] = f"{type(e).__name__}: {e}"

        th = threading.Thread(target=worker, daemon=True)
        th.start()
        th.join(timeout)
        if th.is_alive():
            self._log("warn", f"[{action.get('type')}] 动作超过 {timeout}s 未完成，"
                              f"等它结束后再进入下一轮（避免和下一轮抢截图）")
            th.join()  # 必须等它真的结束，否则会并发截图
            return out.get("ok", False), f"超时（>{timeout}s）｜{out.get('msg', '')}"
        return out.get("ok", False), out.get("msg", "")

    def _resolve_input_text(self, action):
        """input / ensure_input 共用的取字规则：写死在 text 里，或 text_from="run_port" 取本次港口名。"""
        text = action.get("text")
        if action.get("text_from") == "run_port":
            if not self.run_port:
                raise ValueError("text_from=run_port，但本次启动没选港口，不知道该搜哪个港")
            text = self.run_port
        if not text:
            raise ValueError("input 既没有 text，也没有可用的 text_from")
        return str(text)

    def _stop_for_human(self, title, reason, detail):
        """动作自己实在走不下去时的统一收尾：记错误 + 停引擎 + 挂一条人工提醒。

        形状照 _do_retry_watch 的失败分支：弹窗真正弹出是在 _loop 退出的 finally 里，
        在动作线程里直接弹会把收尾卡死（人想停都停不动）。
        """
        self._log("error", f"{reason}｜{detail.splitlines()[0]}")
        self._stop_event.set()
        self._mark_stop("alert", reason)
        self._alert_request = (title, f"{reason}\n\n{detail}")
        return f"停止：{reason}"

    def _do_action(self, action, ctl, screen_path):
        """真正执行单个动作，返回人类可读结果；失败抛异常。"""
        t = action.get("type")

        if t == "click":
            x, y = int(action["x"]), int(action["y"])
            ctl.click(x, y)
            return f"点击 ({x},{y})"

        if t == "click_template":
            path, roi, thr = self._resolve_template(action.get("name"))
            if not path:
                raise ValueError(f"模板不存在: {action.get('name')}")
            threshold = action.get("threshold", thr)
            res = find_template(screen_path, path, roi=roi, threshold=threshold)
            if not res:
                raise ValueError(f"未找到模板: {action.get('name')}")
            px, py, note = self._tap(ctl, res)
            return f"点击模板 {action.get('name')} {note} 置信度{res['confidence']}"

        if t == "click_template_optional":
            return self._do_click_template_optional(action, ctl, screen_path)

        if t == "click_ocr":
            roi = self._find_ocr_region_roi(action["region"])
            if roi is None:
                self._log("warn", f"OCR 区域不存在或无 roi: {action.get('region')}，跳过点击")
                return f"OCR 区域不存在: {action.get('region')}，跳过点击"
            res = ocr_find(screen_path, action["keyword"], roi=roi)
            if not res["found"]:
                self._log("warn", f"OCR 未找到关键词『{action.get('keyword')}』"
                                  f"(区域 {action.get('region')})，跳过点击")
                return f"OCR 未找到『{action.get('keyword')}』，跳过点击"
            ctl.click(res["cx"], res["cy"])
            return f"点击 OCR『{action.get('keyword')}』({res['cx']},{res['cy']})"

        if t == "click_after_template":
            return self._do_click_after_template(action, ctl, screen_path)

        if t == "swipe":
            ctl.swipe(action["x1"], action["y1"], action["x2"], action["y2"],
                      duration=int(action.get("duration", 300)))
            return f"滑动 ({action['x1']},{action['y1']}) -> ({action['x2']},{action['y2']})"

        if t == "input":
            # 打字内容可以写死在 text 里，也可以 text_from="run_port" 取本次启动人选的港口名 ——
            # 港口名是启动时才定的，写死等于每换一个港就要改一次配置。
            text = self._resolve_input_text(action)
            # 中文只能走 ADBKeyboard 广播；每次先确保输入法是它，
            # 因为人可能在自己手里切回了搜狗（切过去很便宜，两次 adb shell）。
            if not text.isascii() and action.get("ensure_adbkeyboard", True):
                ctl.set_adbkeyboard()
            if action.get("clear_first"):
                ctl.clear_text()   # 搜索框会残留上一次的港名，不清空就筛出 0 行
            ctl.input_text(text)
            return f"输入: {text}" + ("（先清空）" if action.get("clear_first") else "")

        if t == "key":
            # 游戏的自带 PC 快捷键（1=出港所 2=造船所 3=交易所）只认原始输入事件，
            # 所以这里走 sendevent 而不是 adb input keyevent，见 mumu_controller.send_key。
            # 它是「导航键」：角色先走到建筑门口才进页面，实测 3~12 秒，
            # 因此 wait_ms 不能当同步手段——目标页面要交给下一个状态的 condition 判定。
            digit = int(action["digit"])
            if not 0 <= digit <= 9:
                raise ValueError(f"digit 只支持 0-9: {digit}")
            code = digit + 1        # Linux 输入码 KEY_0=1、KEY_1=2 …… KEY_9=10
            ok, info = ctl.send_key(code, int(action.get("hold_ms", 80)))
            if not ok:
                raise RuntimeError(f"按数字 {digit} 失败: {info}")
            wait_ms = int(action.get("wait_ms", 0))
            if wait_ms:
                self._sleep_interruptible(wait_ms / 1000)
            return f"按数字 {digit}（Linux code {code}，设备 {info}）"

        if t == "wait":
            ms = int(action.get("ms", 0))
            time.sleep(ms / 1000)
            return f"等待 {ms}ms"

        if t == "stop":
            # 主动停止：复用 _stop_event，主循环每轮和动作列表每次迭代前都会检查它，
            # 所以 stop 之后的动作不会执行，状态跳转也不会发生。
            reason = (action.get("reason") or "").strip() or "手动停止"
            self._stop_event.set()
            # 交给 _mark_stop 记类别（它自己拿锁，所以这里不能包在 with self._lock 里，
            # Lock 不可重入）。队列不把 stop_action 当「这一趟正常跑完」，原因见 __init__ 的注释。
            self._mark_stop("stop_action", reason)
            self._log("stop", f"状态机停止：{reason}")
            return f"主动停止（{reason}）"

        if t == "retry_watch":
            return self._do_retry_watch(action, ctl, screen_path)

        if t == "wait_arrival":
            return self._do_wait_arrival(action, ctl, screen_path)

        if t == "wait_restock":
            return self._do_wait_restock(action, ctl, screen_path)

        if t == "ensure_input":
            return self._do_ensure_input(action, ctl, screen_path)

        if t == "confirm_city_move":
            return self._do_confirm_city_move(action, ctl, screen_path)

        if t == "goto":
            return self._do_goto(action)

        if t == "trip_next":
            return self._do_trip_next(action, ctl, screen_path)

        if t == "negotiation":
            return self._do_negotiation(action, ctl, screen_path)

        if t == "buy_commodities":
            return self._do_buy_commodities(action, ctl, screen_path)

        if t == "plan_advance":
            # 购物表格游标前进一格：本港口这一行已经买完了。
            # 要不要继续由 plan_pending 条件判断，动作本身不碰画面。
            total = len(self.run_rows)
            if not total:
                self._log("warn", "没有套用购物表格（启动时没选港口），plan_advance 无事可做")
                return "未套用表格，跳过推进"
            self.run_row_index += 1
            cur = self.run_row
            if cur:
                self._log("action", f"『{cur.get('goods_name')}』之前的行已买完，"
                                    f"下一行『{cur['goods_name']}』"
                                    f"（第 {self.run_row_index + 1}/{total} 行）")
                return f"表格推进到第 {self.run_row_index + 1}/{total} 行『{cur['goods_name']}』"
            self._log("action", f"港口『{self.run_port}』的 {total} 行已全部买完")
            return f"表格已买完（{total} 行）"

        if t == "set_flag":
            # 运行态开关落盘（run_state.json）：买完货置 sell_pending=1、卖出完成置 0。
            # 不碰画面，也不管置完之后要不要往下走 —— 那是条件的事。
            name = action.get("name")
            try:
                val = run_state.set_flag(name, action.get("value", True))
            except ValueError as e:
                # 名字打错时不静默：写了没人读的文件字段比报错更难查（同 get_flag 的口径）。
                self._log("error", f"set_flag 动作用不了: {e}")
                return f"设置失败: {e}"
            self._log("action", f"运行态开关『{name}』置 {int(val)}"
                               f"（已写入 {os.path.basename(run_state.RUN_STATE_JSON)}）")
            return f"开关『{name}』置 {int(val)}"

        if t == "run_actions":
            results = []
            for sub in action.get("actions", []):
                if self._stop_event.is_set():
                    break  # 子动作里有 stop，后面的不再执行
                results.append(self._do_action(sub, ctl, screen_path))
            return "复合动作: " + " | ".join(results)

        if t == "if":
            cond = action.get("condition") or {}
            if cond.get("type") == "plan_pending":
                # 纯内存判据（只看购物表格游标），不重新截图：省掉一次 ~2.8 秒的 ADB 截图
                screen = screen_path
                matched = self._check_condition(cond, screen)
            else:
                # 分支判断必须基于「点击之后」的最新画面。
                # condition_timeout_ms > 0 时，在窗口内轮询等待条件成立
                # （弹窗有渲染延迟，立即判断会把「有协商」误判成「无协商」）
                cond_wait = max(int(action.get("condition_timeout_ms", 0)), 0) / 1000
                deadline = time.time() + cond_wait
                screen = screen_path
                while True:
                    screen = self._capture(ctl, "if")   # 独立文件，不覆盖主循环画面
                    matched = self._check_condition(cond, screen)
                    if matched or time.time() >= deadline or self._stop_event.is_set():
                        break
                    time.sleep(0.3)
            branch = action.get("then") if matched else action.get("else")
            label = "then" if matched else "else"
            # 子动作也读这张刚截的新图（比本轮主循环那张更贴近当前界面）
            results = []
            for sub in (branch or []):
                if self._stop_event.is_set():
                    break  # 分支里有 stop，后面的不再执行
                results.append(self._do_action(sub, ctl, screen))
            return f"条件分支[{label}]: " + (" | ".join(results) if results else "无动作")

        raise ValueError(f"未知动作类型: {t}")

    def _do_click_template_optional(self, action, ctl, screen_path):
        """可选点击：在 timeout_ms 内轮询等待模板出现，出现就点，最多点 max_clicks 次。

        专门用于「可能出现的确认弹窗」：弹窗有动画/加载延迟，单次查找会漏点；
        连续两次确认（购买确认 -> 结果确认）也能靠 max_clicks 覆盖。

        - timeout_ms       : 每轮等待模板出现的时长，默认 1500（0 = 只查一次）
        - poll_interval_ms : 轮询间隔，默认 300
        - max_clicks       : 最多点几次，点完继续等下一次出现，默认 1
        - wait_before_ms   : 点击前先等（给动画时间）
        - wait_ms          : 每次点击后等

        一直没出现 -> info 日志 + 跳过，不抛异常，不影响后续动作。
        """
        name = action.get("name")
        path, roi, thr = self._resolve_template(name)
        if not path:
            raise ValueError(f"模板不存在: {name}")
        threshold = action.get("threshold", thr)
        timeout = max(int(action.get("timeout_ms", 1500)), 0) / 1000
        poll = max(int(action.get("poll_interval_ms", 300)), 50) / 1000
        max_clicks = max(int(action.get("max_clicks", 1)), 1)
        wait_before_ms = int(action.get("wait_before_ms", 0))
        wait_ms = int(action.get("wait_ms", 0))

        if wait_before_ms > 0:
            time.sleep(wait_before_ms / 1000)

        clicks = 0
        for _ in range(max_clicks):
            deadline = time.time() + timeout
            res = None
            while not self._stop_event.is_set():
                # 每次轮询截到本动作专用的文件，不覆盖主循环 / 其它动作的画面
                screen = self._capture(ctl, "optional")
                res = find_template(screen, path, roi=roi, threshold=threshold)
                if res or time.time() >= deadline:
                    break
                time.sleep(poll)
            if not res:
                break  # 这一轮没等到，说明弹窗已经不再出现
            px, py, note = self._tap(ctl, res)
            clicks += 1
            self._log("action", f"点击可选模板 {name} {note} 置信度{res['confidence']}")
            if wait_ms > 0:
                time.sleep(wait_ms / 1000)

        if clicks == 0:
            self._log("info", f"等待 {int(timeout * 1000)}ms 未出现，跳过点击: {name}")
            return f"可选点击跳过（未出现 {name}）"
        return f"可选点击 {name} ×{clicks} 次"

    def _do_click_after_template(self, action, ctl, screen_path):
        """先全图认出「锚点」（某扇窗独有的标题/图案），再点这扇窗里的按钮。

        为什么不干脆全图匹配按钮本身：金色「确定」这同一个控件长相，在
        购买面板(901,851)、结算结果、改船舱(895,689)、退出游戏(893,745) 几扇窗里都出现，
        全图取最高分就可能点到最不该点的那一个（后两个一个花真资源、一个直接退出游戏）。
        而窗口的标题是全图唯一的，先证明「这是哪扇窗」，
        才允许点窗里的按钮 —— 这就是这个动作存在的全部理由。

        窗里的按钮怎么定位，两种点法二选一：

        - name + search_box（新的一种，卖货收尾用它）：认出锚点后，只在
          「锚点中心 + search_box」这块矩形里匹配 name 那张模板，再按人手随机点在它身上
          （和 click_template 走同一个 _tap）。
          按钮到底在哪由画面说了算，不用人量像素。
          为什么临时加这一种：2026-09-29 实机卖货第一次跑通，结算窗比买货那扇多出
          「出售价格/关税/溢价/利益/总额/拥有金额」六行、整窗往上顶，照买货量的固定
          偏移 [0,318] 点到了「总额」那行字上，窗没关。固定偏移换个窗型就废。
        - offset（老的一种）：点「锚点中心 + [dx, dy]」，不认按钮长什么样。

        - anchor          : 锚点模板名（必填）。**固定全图匹配**，模板自带的 ROI 在这里忽略
        - name            : 窗里要点的那张模板；给了它就必须在下面给 search_box
        - search_box      : [dx, dy, w, h]，相对锚点中心的矩形，name 只在这里面找。
                            同样**忽略 name 模板自带的 ROI** —— UI-确定 那个 ROI 是购买面板
                            那一格 (811,827)，拿它去框结算窗的确定 (712,689) 会直接找不到
        - offset          : [dx, dy]，没给 name 时才用
        - threshold       : 锚点置信度阈值，默认取模板自己的（name 那张用它自己在
                            templates.json 里的阈值，这里不开第二个旋钮）
        - wait_before_ms  : 找锚点前先等一会儿（给弹窗动画时间），默认 0

        锚点没出现 = 那扇窗根本没弹，记 warn 后跳过，不抛异常（要无条件点某个模板用 click_template）。
        锚点在、矩形里没找到 name 也记 warn 跳过：宁可把那扇窗留给人关，也不凭坐标乱点一下。
        """
        name = action.get("anchor")
        if not name:
            raise ValueError("click_after_template 缺少 anchor（锚点模板名）")
        path, _roi, thr = self._resolve_template(name)
        if not path:
            raise ValueError(f"锚点模板不存在: {name}")
        target = action.get("name")
        box = action.get("search_box")
        offset = action.get("offset")
        if target and offset:
            raise ValueError("click_after_template：name（窗内匹配）和 offset（固定偏移）只能给一个")
        if target:
            if not (isinstance(box, (list, tuple)) and len(box) == 4):
                raise ValueError(f"给了 name 就得给 search_box [dx, dy, w, h]，现在是: {box!r}")
            tpath, _troi, tthr = self._resolve_template(target)
            if not tpath:
                raise ValueError(f"窗内要点的模板不存在: {target}")
        else:
            if not (isinstance(offset, (list, tuple)) and len(offset) == 2):
                raise ValueError(f"offset 要写成 [dx, dy] 两个数，现在是: {offset!r}")
            dx, dy = int(offset[0]), int(offset[1])

        wait_before_ms = int(action.get("wait_before_ms", 0))
        if wait_before_ms > 0:
            time.sleep(wait_before_ms / 1000)

        threshold = action.get("threshold", thr)
        # 重新截一张：弹窗是上一个动作点完之后才冒出来的，本轮开头那张画面里没有它
        screen = self._capture(ctl, "anchor")
        res = find_template(screen, path, roi=None, threshold=threshold)
        if not res:
            where = f"它窗里的『{target}』" if target else f"它旁边的 ({dx},{dy})"
            self._log("warn", f"锚点『{name}』未出现（阈值 {threshold}），没有点{where}")
            return f"锚点未出现，跳过点击: {name}"

        if target:
            bx, by, bw, bh = (int(v) for v in box)
            if bw <= 0 or bh <= 0:
                raise ValueError(f"search_box 的宽高必须是正数，现在是: {box!r}")
            rect = (int(res["cx"]) + bx, int(res["cy"]) + by, bw, bh)
            hit = find_template(screen, tpath, roi=rect, threshold=tthr)
            if not hit:
                self._log("warn", f"锚点『{name}』在 ({res['cx']},{res['cy']})，"
                                  f"但矩形 {rect} 里没找到『{target}』（阈值 {tthr}）—— 没点，窗留着")
                return f"锚点在、窗里没找到 {target}，跳过点击"
            px, py, note = self._tap(ctl, hit)
            self._log("action", f"锚点『{name}』({res['cx']},{res['cy']}) 置信度{res['confidence']} "
                                f"→ 窗内矩形 {rect} 找到『{target}』{note} "
                                f"置信度{hit['confidence']}，点击")
            return f"窗内匹配 {target} → ({px},{py}) 置信度{hit['confidence']}"

        tx, ty = int(res["cx"]) + dx, int(res["cy"]) + dy
        ctl.click(tx, ty)
        self._log("action", f"锚点『{name}』({res['cx']},{res['cy']}) 置信度{res['confidence']} "
                            f"+ 偏移 ({dx},{dy}) → 点击 ({tx},{ty})")
        return f"点击 {name}+偏移({dx},{dy}) → ({tx},{ty}) 置信度{res['confidence']}"

    def _do_goto(self, action):
        """本轮动作跑完后跳到指定状态，跳过状态上写死的那个 next。只改内存，不碰画面。

        为什么需要它：一个状态只有一个**写死**的 next，可「在海上」这一格真正要等的是
        「左上角什么时候从帆船换成灯塔」—— 那是按画面决定的跳转，不是固定跳转。
        没有 goto 就只能让状态机在海上一直「未匹配」，而 max_no_match 一共 20 轮
        （约两分钟），航行动辄十几分钟，会被自己判成卡死停止。
        """
        state = (action.get("state") or "").strip()
        if not state:
            raise ValueError("goto 缺少 state（要跳去的状态 id）")
        if not self._find_state(state):
            # self.states 在 start() 里就裁好了：单模块只剩那一条链 + 全局，所以想跳去
            # 别的模块的状态会走到这一句被拒；完整一趟（trip）那份裁的是几条链，
            # 于是 trip_next 能跨链接力（用户 2026-09-29 改成「一次点启动跑完整趟」）。
            raise ValueError(f"goto 目标『{state}』不在本次模块『{self.run_module}』里（不存在或属于别的模块）")
        self._pending_goto = state
        self._log("action", f"标记跳转 → {state}（本轮剩余动作和写死的 next 都不走）")
        return f"goto {state}"

    def _do_retry_watch(self, action, ctl, screen_path):
        """航行中断看门狗：认出「重启自动移动」就点它重新发起；点满上限还没恢复就报错停止 + 喊人。

        为什么不复用 click_template_optional：那个动作只有「等模板出现、点几次」，
        没有「点完之后到底恢复了没有」这个概念，也就数不出用户要的「连续 5 次」。
        这里的计数是**连续**的：只要有一次认不到中断（横幅回到「预计到达时间」），
        就算恢复，直接返回、计数作废。
        没中断时只做一次模板匹配就返回 —— 不额外截图、不点击，交给主循环下一轮再看。

        - name         : 中断判据模板（必填），如 UI-海上-重启自动移动
        - click        : [cx, cy] 落点（必填），用户实测的横幅中心 (802,811)
        - click_rect   : [x,y,w,h] 给了就走 _tap 的矩形内随机落点（人手随机），没给就点中心
        - interval_ms  : 每次点完之后的等待，默认 5000
        - max_attempts : 最多点几次，默认 5
        - threshold    : 覆盖模板自带阈值
        - fail_reason  : 失败时写进停止原因和弹窗的文字

        失败时只做三件事：记 error 日志、按 stop 动作的方式置停止位、把弹窗挂到
        self._alert_request（真正弹它在 _loop 的 finally，理由见那里）。
        """
        name = action.get("name")
        path, roi, thr = self._resolve_template(name)
        if not path:
            raise ValueError(f"模板不存在: {name}")
        click = action.get("click")
        if not (isinstance(click, (list, tuple)) and len(click) == 2):
            raise ValueError(f"click 要写成 [cx, cy] 两个数，现在是: {click!r}")
        threshold = action.get("threshold", thr)
        interval = max(int(action.get("interval_ms", 5000)), 0) / 1000
        max_attempts = max(int(action.get("max_attempts", 5)), 1)
        fail_reason = (action.get("fail_reason") or "").strip() or \
            f"连续 {max_attempts} 次点击『{name}』仍未恢复自动航行"

        rect = action.get("click_rect")
        if rect:
            x, y, w, h = (int(v) for v in rect)
            target = {"cx": int(click[0]), "cy": int(click[1]), "rect": [x, y, w, h]}
        else:
            target = {"cx": int(click[0]), "cy": int(click[1])}

        def _interrupted(screen):
            return find_template(screen, path, roi=roi, threshold=threshold)

        # 先看本轮主循环那张：绝大多数轮次船跑得好好的，不必为此起一次 ~2.8 秒的截图
        res = _interrupted(screen_path)
        if not res:
            return f"航行未中断（未出现 {name}）"

        attempts = 0
        while res and attempts < max_attempts:
            px, py, note = self._tap(ctl, target)
            attempts += 1
            self._log("action", f"检测到自动航行中断：第 {attempts}/{max_attempts} 次点击 "
                                f"{note} 重新发起自动移动（置信度{res['confidence']}）")
            self._sleep_interruptible(interval)
            if self._stop_event.is_set():
                return f"已停止，中断重启停在第 {attempts} 次"
            res = _interrupted(self._capture(ctl, "retry"))

        if not res:
            return f"自动航行已恢复（点了 {attempts} 次）"

        self._log("error", f"连续 {attempts} 次点击『{name}』后，画面仍是自动航行中断，不再重试")
        self._stop_event.set()
        self._mark_stop("alert", fail_reason)   # 停在弹窗上等人处理
        self._alert_request = (
            "UWO 已停止：需要人工处理",
            f"{fail_reason}\n\n已点击『{name}』{attempts} 次，每次间隔 {interval:g} 秒，"
            f"画面没有恢复。请人工看一下模拟器（可能是断线、船被击沉、或弹了全屏活动页）。",
        )
        return f"重启失败（{attempts} 次）: {fail_reason}"

    def _do_ensure_input(self, action, ctl, screen_path):
        """把「本次港口名」真的打进输入框：点框 → 认白栏 → 打字 → OCR 读回来核对，不通就重来。

        2026-09-30 实机暴露：老写法是点一次搜索框 → 广播打字 → 直接往下走，
        全程没问过「字到底进去了没有」。那次一个字都没落地（截图上搜索框还是占位符『搜索』），
        于是列表没筛出目的港、第一行还是默认头一座城，下一步 UI-城市移动 又找不到，船在海上干等。
        用户拍板的判据（原话「首先按照原来的接口检测，然后ocr搜索框里的字」）分两层：
          ① 原来的接口检测 = dumpsys input_method 认游戏底部那条白色输入栏开着没有
             （栏收着时打字/清空广播都静默失效，见 mumu_controller.input_bar_state）；
          ② OCR 读输入框那一行，读到要打的字才算真输进去了 —— 这条是最终裁决。
        白栏读不出时不拦路（记一条 warn），照样按 ② 判：字在框里就是成了，别的都是猜测。
        两层不过就重新点框、重新清、重新打，最多 max_attempts 轮；用尽 → 停下喊人，
        绝不带着空搜索框去点列表第一行 —— 那会把船开去别人的港，一个来回几十分钟真时间。

        - click            : [x, y] 输入框落点（必填）。实测只能点**左端** (125,124)：
                             点框中心 (267,124) 正落在已有文字上，只是移光标、不弹栏。
        - click_rect       : [x,y,w,h] 给了就走 _tap 的矩形内随机落点（人手随机）
        - text / text_from : 要打的内容；text_from="run_port" 取本次启动人选的港口名
        - region           : OCR 区域名（必填），打完字读这一行来核对
        - clear_first      : 默认 True —— 搜索框会残留上一次的港名，不清空就筛出 0 行
        - settle_ms        : 点完框到打字之间的等待，默认 900（栏弹出来要时间）
        - read_ms          : 打完字到截图去读之间的等待，默认 800
        - max_attempts     : 最多几轮，默认 5
        """
        click = action.get("click")
        if not (isinstance(click, (list, tuple)) and len(click) == 2):
            raise ValueError(f"click 要写成 [cx, cy] 两个数，现在是: {click!r}")
        region = action.get("region")
        if not region:
            raise ValueError("ensure_input 必须给 region：没有它就判不出字到底进没进框")
        want = self._resolve_input_text(action)
        target = {"cx": int(click[0]), "cy": int(click[1])}
        if action.get("click_rect"):
            target["rect"] = [int(v) for v in action["click_rect"]]
        settle = max(int(action.get("settle_ms", 900)), 0) / 1000
        read_wait = max(int(action.get("read_ms", 800)), 0) / 1000
        cap = max(int(action.get("max_attempts", 5)), 1)
        clear_first = action.get("clear_first", True)

        last = ""
        for attempt in range(1, cap + 1):
            if self._stop_event.is_set():
                return f"已停止，输入没再重试（第 {attempt} 轮之前）"
            px, py, note = self._tap(ctl, target)
            self._log("action", f"第 {attempt}/{cap} 轮：点输入框 {note}，"
                               f"{'先清空再' if clear_first else ''}打『{want}』")
            self._sleep_interruptible(settle)
            bar, bar_note = ctl.input_bar_state()
            if bar is None:
                self._log("warn", f"白栏判据读不出（{bar_note}），这一轮只按 OCR 读到的字判")
            elif not bar:
                self._log("warn", f"白栏没开（{bar_note}）—— 打字广播会静默失效，这一轮大概率要重来")
            self._do_action({"type": "input", "text": want, "clear_first": clear_first},
                            ctl, screen_path)
            self._sleep_interruptible(read_wait)
            screen = self._capture(ctl, "input")
            try:
                read = ocr_text_by_region(screen, region)
            except ValueError as e:
                raise ValueError(f"ensure_input 的 OCR 区域用不了：{e}")
            if want in read:
                return (f"输入已确认（第 {attempt}/{cap} 轮）：OCR『{region}』读到『{read}』"
                        f"，白栏 {bar_note}")
            last = f"OCR『{region}』读到『{read or '(空)'}』，里面没有『{want}』"
            self._log("warn", f"第 {attempt}/{cap} 轮字没进框：{last}（白栏 {bar_note}）")

        return self._stop_for_human(
            "UWO 已停止：港口名打不进搜索框",
            f"点输入框 + 打字重试 {cap} 轮，搜索框里始终读不到『{want}』",
            f"最后一次：{last}。\n\n可能原因：白色输入栏没弹出来（落点要偏到搜索框左端）、"
            f"模拟器输入法不是 ADBKeyboard、或者画面根本不在世界地图。\n"
            f"本次要去『{want}』，但不能瞎点列表第一行 —— 那会把船开去别的港。")

    def _do_confirm_city_move(self, action, ctl, screen_path):
        """点「城市移动」之前先核对：选中的城市就是本次港口，按钮也确实在屏上；不过就停下喊人。

        为什么这一步必须有判据（2026-09-30 用户拍板「停下来喊人，不点城市移动」）：
        列表第一行是谁，完全取决于搜索框里那几个字。框是空的 → 第一行就是默认列表头一座城
        （实机那次是『马赛』），按固定坐标点下去就把船开去别人的港；
        而点「城市移动」没有二次确认（录制 2.7 已确认），一下去就是几十分钟真航程。
        所以先读右边「城市信息」面板那行城市名（区域 地图选中城市，实测 OCR 干净到只出城名），
        跟本次港口名对上之后，再等 UI-城市移动 这张模板真出现才点。
        老写法是匹配不上就抛 ValueError —— 引擎记下错误照样跳到 voyage，
        等于带着一个没点成的按钮往下跑，只在海上干等到「无匹配」停止。

        - name / threshold   : 城市移动按钮模板，默认 UI-城市移动
        - region             : 城市名 OCR 区域，默认 地图选中城市
        - timeout_ms         : 等按钮出现的最长毫秒，默认 6000
                               （点列表那一行会让白栏收起、地图平移、面板弹出，这些都要时间）
        - poll_interval_ms   : 轮询间隔，默认 800
        """
        port = (self.run_port or "").strip()
        if not port:
            return self._stop_for_human(
                "UWO 已停止：不知道该核对哪个港",
                "本次启动没带港口名，没法核对选中的城市是不是要去的港",
                "点「城市移动」会把船开到一个没人确认过的城市，所以这一步没点。")
        region = action.get("region") or "地图选中城市"
        name = action.get("name") or "UI-城市移动"
        path, roi, thr = self._resolve_template(name)
        if not path:
            raise ValueError(f"模板不存在: {name}")
        threshold = action.get("threshold", thr)
        timeout = max(int(action.get("timeout_ms", 6000)), 0) / 1000
        poll = max(int(action.get("poll_interval_ms", 800)), 200) / 1000

        screen = self._capture(ctl, "citymove")
        try:
            read = ocr_text_by_region(screen, region)
        except ValueError as e:
            raise ValueError(f"confirm_city_move 的 OCR 区域用不了：{e}")
        if port not in read:
            return self._stop_for_human(
                "UWO 已停止：选中的城市不是本次港口",
                f"『{region}』读到『{read or '(空)'}』，跟本次港口『{port}』对不上",
                f"列表第一行不是要去的港，硬点会把船开去别处，所以「城市移动」没有点。\n"
                f"多半是上一步搜索框没进字 —— 人工看一眼世界地图：现在选中的是哪座城。")

        deadline = time.time() + timeout
        while True:
            res = find_template(screen, path, roi=roi, threshold=threshold)
            if res:
                px, py, note = self._tap(ctl, res)
                return (f"已核对：『{region}』读到『{read}』= 本次港口『{port}』；"
                        f"点击 {name} {note} 置信度{res['confidence']}")
            if time.time() >= deadline or self._stop_event.is_set():
                break
            self._sleep_interruptible(poll)
            screen = self._capture(ctl, "citymove")

        return self._stop_for_human(
            "UWO 已停止：没找到「城市移动」按钮",
            f"城市名核对通过（『{region}』读到『{read}』），但 {timeout:g} 秒内没认出 {name}",
            "按钮要在白色输入栏收起之后才露得出来（它就在栏盖住的那一条 y 810~900 上）。\n"
            "人工看一眼：栏是不是还开着、或者画面被别的弹窗盖住了。")

    # ---------- 画面判据 + 补货倒计时 ----------
    # 「这一屏是什么页面」用的判据：全是 states.json 里已经在用的那几张标志模板。
    # 一个都不命中 = 未知画面，多半是充值礼包弹窗、全屏宣传页这类盖住整屏的东西。
    PAGE_MARKERS = (
        ("购买页", "UI-购买页-标题"),
        ("交易所", "UI-交易所标志"),
        ("港口", "UI-港口标志"),
        ("海上", "UI-海上"),
        ("世界地图", "UI-地图-城市列表"),
        ("出港所", "UI-出港所-标题"),
        ("结算窗", "UI-结算结果-标题"),
        ("协商窗", "UI-协商-标题"),
    )

    def _do_wait_arrival(self, action, ctl, screen_path):
        """在海上阻塞等到港：反复截图，直到出现到港判据（左上角那座灯塔）才返回。

        为什么需要它：voyage 这个状态的判据是 UI-海上，船一靠港它就再也不匹配，
        于是引擎靠「连续 N 轮无匹配」停下 —— 那是撞墙停的，不是「到港了」这件事被认出来。
        完整一趟要在靠港那一刻接上下一段，所以到港必须由这一步当场认出来。

        - name / threshold   : 到港判据模板，默认 UI-港口标志（阈值取模板自带的）
        - poll_interval_ms   : 两次截图之间的等待，默认 5000（截图本身就要 ~2.8 秒，不必更密）
        - max_wait_seconds   : 最多等多久，默认 2400（40 分钟，和补货倒计时同一口径）
        - interrupt          : 一份完整的 retry_watch 动作。给了就在这段等待里每轮顺手照看
                               航行中断 —— 阻塞期间回不到主循环，不照看的话船被打断就一直干等
        - timeout_reason     : 超时时的停止文字

        超时不抛异常：抛出去日志里只多一行 ValueError，人不知道为什么。
        照 retry_watch 的做法置停止位 + 挂弹窗（真正弹在 _loop 的 finally）。
        """
        name = action.get("name") or "UI-港口标志"
        path, roi, thr = self._resolve_template(name)
        if not path:
            raise ValueError(f"到港判据模板不存在: {name}")
        threshold = action.get("threshold", thr)
        interval = max(int(action.get("poll_interval_ms", 5000)), 0) / 1000
        limit = max(int(action.get("max_wait_seconds", 2400)), 1)
        interrupt = action.get("interrupt")
        started = time.time()
        polls = 0
        screen = screen_path   # 先看主循环那张：船还没开出去时一眼就能否掉，不必再起一次截图
        while True:
            if self._stop_event.is_set():
                return f"已停止，等待到港中退出（等了 {time.time() - started:.0f} 秒）"
            polls += 1
            res = find_template(screen, path, roi=roi, threshold=threshold)
            if res:
                return (f"已到港（{name} 置信度{res['confidence']}，"
                        f"等了 {time.time() - started:.0f} 秒 / 看了 {polls} 次）")
            waited = time.time() - started
            if waited > limit:
                reason = (action.get("timeout_reason") or "").strip() or \
                    f"等了 {int(limit)} 秒还没到港（{name} 一直没出现）"
                self._log("error", f"等待到港超时：{reason}")
                self._stop_event.set()
                self._mark_stop("alert", reason)   # 停在弹窗上等人处理
                self._alert_request = (
                    "UWO 已停止：等不到港",
                    f"{reason}\n\n请人工看一下模拟器：船可能被打沉、被拉进战斗，"
                    f"或者弹了全屏活动页把画面挡住了。",
                )
                return f"等待到港超时: {reason}"
            if interrupt:
                # 看门狗自己会点「重启自动移动」；点不回来时它就把停止位置上、挂好弹窗
                self._do_action(dict(interrupt), ctl, screen)
                if self._stop_event.is_set():
                    return "等待到港中止：航行中断没救回来"
            self._sleep_interruptible(interval)
            if self._stop_event.is_set():
                return f"已停止，等待到港中退出（等了 {time.time() - started:.0f} 秒）"
            screen = self._capture(ctl, "arrival")

    def _do_trip_next(self, action, ctl, screen_path):
        """三条链跑到底那一步的「分流」：单模块照旧停止，跑整趟则决定下一段去哪。

        为什么要一个动作兼两件事，而不是给整趟复制一份 states.json：
        没在跑整趟时它原样就是那条 stop（连停止原因的文字都和以前一样），
        于是买货 / 移动 / 卖货三条链的结尾只写一处，两种跑法共用同一份配置。

        - after       : "work"  = 这一站的正事（买完 / 卖完）做完了，要开去下一站
                        "arrive" = 船刚靠港，认一认这是第几站、进门该做什么
        - reason      : 单模块跑法（没在跑整趟）的停止文字
        - done_reason : 整趟走到「没有下一站」时的停止文字

        跳转只走 _do_goto 标记（goto 优先于状态里写死的 next），所以三条链各自的
        链尾 next 不必为整趟改；单模块那边也照旧：买货链跑完自己回到 buy_at_exchange 等下一轮。
        """
        if not self.trip:
            reason = (action.get("reason") or "").strip() or "本次运行结束"
            return self._do_action({"type": "stop", "reason": reason}, ctl, screen_path)

        stops = self.trip["stops"]
        entries = self.trip["entries"]
        pos = self.trip["pos"]
        total = len(stops)
        after = (action.get("after") or "").strip()
        here = stops[pos] if 0 <= pos < total else {}
        done = (action.get("done_reason") or "").strip() or "这一趟的站次已经全部走完"

        if after == "work":
            if pos + 1 >= total:
                self._log("trip", f"第 {pos + 1}/{total} 站 {self._trip_leg_text(here)} 的正事做完，"
                                  f"后面没有站了")
                return self._trip_stop(done)
            # 买货 / 卖货跑完时画面还在交易所里，而出港那一下要在码头上按 1：先退回港口界面
            self._exit_to_port(ctl)
            b, err = self._depart_binding(stops, entries, pos, "这一站的正事已经做完")
            if err:
                return self._trip_stop(f"整趟走不下去：{err}")
            self._apply_binding(pos, b)
            return self._do_goto({"state": b["current_state"]})

        if after != "arrive":
            raise ValueError(f'trip_next 的 after 只认 "work" / "arrive"，现在是: {after!r}')

        # 船刚靠港：靠的就是上一段绑定的那个目的港，也就是站次表里的下一站
        nxt = pos + 1
        if nxt >= total:
            return self._trip_stop(done)
        stop = stops[nxt]
        stage = (stop.get("stage") or "").strip()
        port = (stop.get("port") or "").strip()
        if stage == "sell" and not run_state.get_flag("sell_pending"):
            return self._trip_stop(
                f"到了『{port}』但待卖账是 0（这之前没买过货）—— 舱里没有要卖的货，"
                f"整趟停在这里，别白进一次交易所",
                ("UWO 已停止：到了出货港却没货可卖",
                 f"这一趟的下一站是出货站『{port}』，但待卖账"
                 f"（{os.path.basename(run_state.RUN_STATE_JSON)} 的 sell_pending）是 0。\n\n"
                 f"可能是前面进货那一站没真正买成，也可能有人手动清过账。请人工看一下货舱。"))
        err = self._bind_leg(nxt)
        if err:
            return self._trip_stop(f"整趟走不下去：{err}",
                                   ("UWO 已停止：换下一站时卡住了",
                                    f"{err}\n\n船已经靠港了，但这一站接不上。\n"
                                    f"最常见的原因是购物表格在那之后被改过（货名或类别对不上）。"))
        return self._do_goto({"state": self.current_state})

    def _classify_page(self, screen_path):
        """判断当前这一屏是什么页面，返回命中的页面名列表（可能多个）。

        空列表 = 一个判据都不命中 = 未知页面。用户 2026-09-28 的要求：
        「加入一个函数对当前页面是什么进行判断，如果是未知页面持续超过两分钟就报告」。
        只做模板匹配、不做 OCR —— 等待环里每一轮都要跑，OCR 一次好几秒不划算。
        模板被删掉不算未知（跳过它继续判别的），否则素材一改就把等待环弄崩。
        """
        hits = []
        for label, name in self.PAGE_MARKERS:
            path, roi, thr = self._resolve_template(name)
            if not path:
                continue
            if find_template(screen_path, path, roi=roi, threshold=thr) is not None:
                hits.append(label)
        return hits

    def _restock_stop(self, reason, detail):
        """等待补货中途判定「等不下去」：记错误、停引擎、挂一条人工提醒。

        形状照 _do_retry_watch 的失败分支：弹窗真正弹出是在 _loop 退出的 finally 里，
        在动作线程里直接弹会把收尾卡死（人想停都停不动）。
        """
        self._log("error", f"补货倒计时：{reason}｜{detail}")
        self._stop_event.set()
        self._mark_stop("alert", reason)   # 停在弹窗上等人处理
        self._alert_request = ("UWO 已停止：补货倒计时没等到", f"{reason}\n\n{detail}")
        if self.restock is not None:
            self.restock = dict(self.restock, phase="failed", reason=reason)
        return f"等待失败：{reason}"

    def _record_restock(self, ctl, port, region):
        """买货这一刻读一次倒计时，把「这个港下次几点补货」记进账本。

        读不出不拦买货：resolve_remaining 会按 30 分钟兜底，于是时刻被标晚、下次来多等一阵 ——
        宁可多等，不可早买（这条口径从 2026-09-28 沿用）。
        没有港口名（单模块跑法没选港）就只读不记：记到哪个港去都不敢猜。
        """
        screen = self._capture(ctl, "restock")
        labels = self._classify_page(screen)
        try:
            text = ocr_text_by_region(screen, region)
        except ValueError as e:
            self._log("error", f"倒计时区域读不了：{e}")
            text = ""
        read = restock.resolve_remaining(text)
        when = restock.next_restock_at(read, time.time())
        if not labels:
            self._log("warn", f"记补货时刻这一刻画面认不出任何已知页面（弹窗可能盖进来了），"
                              f"读数按『{read['read_text'] or '(空)'}』算")
        saved = False
        if port:
            try:
                run_state.set_restock_at(port, when)
                saved = True
            except ValueError as e:
                self._log("error", f"补货时刻没能记进账本：{e}")
        else:
            self._log("warn", "这一趟没有港口名，读到的补货时刻没记（下次来还是按「没记过」直接买）")
        return read, when, saved, labels

    def _do_wait_restock(self, action, ctl, screen_path):
        """买货前先决定「这个港到底要不要等补货」，然后把这一刻读到的倒计时记进账本。

        2026-09-30 用户指出的错：画面上那串倒计时是「下一次刷新」，不是「货架空了」。
        每天首次购买默认已经刷新（服务器时间在推进，一夜离线必定超过 30 分钟），
        所以每次进购买页都死等那串数字是纯浪费时间。改成按港口自己记一次时刻，五条规则：
        ① 要不要等不看画面，看 run_state.json 里这个港记的「下次补货时刻」：
           没记过（今天第一次来买）或时刻已过 -> 直接买，一秒都不等；
           时刻还在将来 -> 本地等到那个点，没等完不买；
        ② 每次买货这一刻用 OCR 读一次倒计时，换算成时刻记下来；读不出按 30 分钟记
           （宁可下次多等，不可早买）；
        ③ 等到点之后游戏不会自己刷列表，要重新点一次「购买」标签；
        ④ 等待期间画面连续 120 秒认不出任何已知页面就报告（这半小时里充值弹窗会盖进来）；
        ⑤ 单次等待总上限 40 分钟 —— 记错时刻、电脑改过系统时间、OCR 读出一串离谱数字，
           都不该让人干等，到上限就停下来喊人。

        为什么做成一个动作而不是一个新状态：主循环每轮都要过 max_rounds / max_no_match，
        半小时的等待会把这两个计数直接撑爆（现配置 max_rounds=60），引擎会把自己判成卡死。
        等待放在动作里，每轮自己截图、自己打点，_sleep_interruptible 让「停止」随时能打断。

        - region             : 倒计时 OCR 区域名，默认『补货倒计时』
        - poll_interval_ms   : 等待期间隔多久醒来看一次表，默认 15000
        - max_wait_seconds   : 单次等待上限，默认 restock.MAX_WAIT_SECONDS（40 分钟）
        - unknown_seconds    : 未知画面报告阈值，默认 restock.UNKNOWN_PAGE_SECONDS（120 秒）
        - tab_region/tab_keyword : 到点重进的标签，默认「购买出售标签」里的「购买」
        """
        region = action.get("region") or restock.COUNTDOWN_REGION
        poll = max(int(action.get("poll_interval_ms", 15000)), 1000) / 1000
        cap = max(int(action.get("max_wait_seconds", restock.MAX_WAIT_SECONDS)), 60)
        unknown_cap = max(int(action.get("unknown_seconds", restock.UNKNOWN_PAGE_SECONDS)), 10)
        tab_region = action.get("tab_region") or "购买出售标签"
        tab_keyword = action.get("tab_keyword") or "购买"
        port = (self.run_port or "").strip()

        began = time.time()
        try:
            stored = run_state.get_restock_at(port) if port else None
        except ValueError as e:
            # 账本被手工改坏不能把买货顶死：按「没记过」处理，先买，留一条看得见的警告。
            self._log("warn", f"补货账本读不出（{e}），港口『{port}』这一趟按没记过处理")
            stored = None
        should_wait, secs, why = restock.decide(stored, began)
        why_text = (
            "账本里没记过这个港 —— 今天第一次来买，隔夜怎么都刷过一轮了" if why == "no_record"
            else f"账本里记的 {restock.moment_text(stored)} 已经到了" if why == "passed"
            else f"账本里记的下次补货在 {restock.moment_text(stored)}，还有 {restock.remaining_text(secs)}")

        def snapshot(phase, read=None, when=None, saved=False, remaining=0,
                     waited=0, polls=0, labels=None):
            """给界面那一行用的事实快照：只把已经发生的事念出来，前端不自己算。"""
            snap = {
                "phase": phase,
                "port": port,
                "decision": why,
                "decision_text": why_text,
                "stored_at": stored,
                "stored_text": restock.moment_text(stored),
                "remaining_seconds": int(remaining),
                "remaining_text": restock.remaining_text(remaining),
                "polls": polls,
                "waited_seconds": int(waited),
                "max_wait_seconds": cap,
                "region": region,
                "page": labels or [],
                "updated_at": time.strftime("%H:%M:%S"),
            }
            if read is not None:
                snap["source"] = read["source"]
                snap["read_text"] = read["read_text"]
            if when is not None:
                snap["next_at"] = when
                snap["next_text"] = restock.moment_text(when)
                snap["saved"] = saved
            return snap

        if not should_wait:
            if self._stop_event.is_set():
                # 人要停就不该再去截图、读 OCR、改账本 —— 这一支本来就是「不花时间去等」，
                # 更没理由在按了停止之后还去碰画面。
                return f"已手动停止（这一港不用等补货：{why}，账本没动）"
            self._log("action", f"不用等补货：{why_text}。读一次倒计时记下『{port or '?'}』下次几点补货，直接买")
            read, when, saved, labels = self._record_restock(ctl, port, region)
            self.restock = snapshot("direct", read=read, when=when, saved=saved,
                                    waited=time.time() - began, polls=1, labels=labels)
            return (f"没等补货（{why}），已记下下次补货时刻 {restock.moment_text(when)}"
                    f"（读数『{read['read_text'] or '(空)'}』/ {read['source']}）")

        self._log("action", f"要等补货：{why_text}，等到点前不看画面上的那串数字")
        target = began + secs
        deadline = began + cap
        unknown_since = None
        last_report = 0.0
        polls = 0
        while True:
            if self._stop_event.is_set():
                return "已手动停止，补货没等到点"
            now = time.time()
            remaining = target - now
            if remaining <= 0:
                break
            if now >= deadline:
                return self._restock_stop(
                    f"等补货超过上限 {cap // 60} 分钟还没到点",
                    f"港口『{port}』记的下次补货时刻是 {restock.moment_text(stored)}，"
                    f"已醒来看过 {polls} 次表。游戏里最多 30 分钟刷一次，等这么久说明账本里的时刻"
                    f"或电脑的系统时间有问题，请人去看一眼 run_state.json。")

            screen = self._capture(ctl, "restock")
            labels = self._classify_page(screen)
            polls += 1
            self.restock = snapshot("waiting", remaining=remaining,
                                    waited=now - began, polls=polls, labels=labels)

            if labels:
                unknown_since = None
            elif unknown_since is None:
                unknown_since = now
                self._log("warn", "画面认不出任何已知页面（先继续等，连续 120 秒才报告）")
            elif now - unknown_since >= unknown_cap:
                return self._restock_stop(
                    f"连续 {int(now - unknown_since)} 秒认不出画面",
                    "多半是充值弹窗或全屏宣传页盖住了购买页（这两处项目里还没做自动关闭）。"
                    "请人到模拟器前把弹窗点掉，再重新启动。")

            if now - last_report >= 60:
                last_report = now
                self._log("action", f"等补货中：还剩 {restock.remaining_text(remaining)}"
                                    f"（等到 {restock.moment_text(stored)}），"
                                    f"画面 {('、'.join(labels)) or '认不出'}")
            self._sleep_interruptible(max(min(remaining, poll, deadline - now), 0))

        self._log("action", f"补货时刻到了（等了 {restock.remaining_text(time.time() - began)}），"
                            f"重新点一次『{tab_keyword}』标签让货物列表刷新")
        self._do_action({"type": "click_ocr", "region": tab_region,
                         "keyword": tab_keyword}, ctl, self._capture(ctl, "restock"))
        self._sleep_interruptible(1.2)   # 标签点完页面要切换，给一秒多
        waited = time.time() - began
        read, when, saved, labels = self._record_restock(ctl, port, region)
        self.restock = snapshot("ready", read=read, when=when, saved=saved,
                                waited=waited, polls=polls + 1, labels=labels)
        return (f"已等到补货（{polls} 轮 / {restock.remaining_text(waited)}），"
                f"重进『{tab_keyword}』标签，下次补货时刻记为 {restock.moment_text(when)}")

    def _do_buy_commodities(self, action, ctl, screen_path):
        """批量买商品：对每个模板名做「列表定位 -> 点击 -> 等待 ->（可选）协商」。

        列表查找支持双向：先向下翻 max_swipes 次，再向上翻 max_swipes_up 次。
        reset_to_top 为 true 时，整批买完后把列表滑回顶部，
        保证状态机下一轮（next 指回本状态）从同一个画面开始。

        templates_from_plan 为 true 时，要买哪件货取自购物表格游标当前指向的那一行
        （`商品-<goods_name>`），此时 action.templates 不再参与 —— 来源只留一个，
        免得太板上写死一份、表格里又写一份，两边对不上还看不出来。
        """
        names = list(action.get("templates") or [])
        if action.get("templates_from_plan"):
            goods = ((self.run_row or {}).get("goods_name") or "").strip()
            if not goods:
                self._log("error", "动作配了 templates_from_plan，但当前没有待买的表格行"
                                   f"（启动时没选港口，或港口『{self.run_port}』的 "
                                   f"{len(self.run_rows)} 行已买完）—— 这批一件都不买，跳过")
                return "无待买货物行，跳过批量购买"
            names = [purchase_plan.goods_template_name(goods)]
            seq = (f"（第 {self.run_row_index + 1}/{len(self.run_rows)} 行）"
                   if len(self.run_rows) > 1 else "")
            self._log("action", f"按购物表格买『{goods}』{seq}（模板 {names[0]}）")
        list_roi = tuple(action["list_roi"]) if action.get("list_roi") else None
        swipe_range = tuple(action["swipe_range"]) if action.get("swipe_range") else None
        max_swipes = int(action.get("max_swipes", 3))
        max_swipes_up = int(action.get("max_swipes_up", 0))
        swipe_pause_ms = int(action.get("swipe_pause_ms", 800))
        reset_to_top = bool(action.get("reset_to_top", False))
        click_wait_ms = int(action.get("click_wait_ms", 1000))
        negotiation = action.get("negotiation", True)
        threshold = float(action.get("threshold", 0.8))

        # 商品列表查找用独立截图文件，不覆盖主循环 / 协商 / 分支判断的画面
        buy_screen = self._cap_path("buy")

        def snap():
            """每次滑动后重新截一张（_snap 走全局那把锁）。
            截图失败只记警告、拿上一张继续找：翻页途中一次抖动不值得中断整批购买。"""
            ok, info = self._snap(ctl, buy_screen, "商品列表截图")
            if not ok:
                self._log("warn", f"{info}，用上一张画面继续查找")
            return buy_screen

        def swipe_func(x1, y1, x2, y2):
            ctl.swipe(x1, y1, x2, y2)

        ok_count = 0
        fail_count = 0
        for name in names:
            path, _, _ = self._resolve_template(name)
            if not path:
                self._log("warn", f"商品模板不存在: {name}，跳过")
                fail_count += 1
                continue

            # 每次买前重新截图，确保在最新画面上查找
            snap()
            try:
                res = find_in_list(
                    buy_screen, path, list_roi,
                    swipe_range=swipe_range, max_swipes=max_swipes,
                    capture=snap, swipe_func=swipe_func, threshold=threshold,
                    max_swipes_up=max_swipes_up, swipe_pause_ms=swipe_pause_ms,
                )
            except Exception as e:
                self._log("warn", f"商品『{name}』查找异常: {e}")
                fail_count += 1
                continue

            if not res:
                where = f"向下 {max_swipes} 次 + 向上 {max_swipes_up} 次"
                self._log("warn", f"商品『{name}』在列表中未找到（已滑动{where}），跳过")
                fail_count += 1
                continue

            direction_text = {"none": "首屏", "down": f"向下 {res['swipes_used']} 次",
                              "up": f"向下 {max_swipes} + 向上 {res['swipes_used']} 次"}
            px, py, note = self._tap(ctl, res)
            self._log("action", f"找到并点击商品『{name}』（{direction_text[res['swipe_direction']]}）"
                                f"{note} 置信度{res['confidence']}")
            if click_wait_ms > 0:
                time.sleep(click_wait_ms / 1000)
            if negotiation:
                try:
                    self._do_negotiation(action, ctl, screen_path)
                except Exception as e:
                    self._log("error", f"商品『{name}』协商处理异常: {e}")
            ok_count += 1

        if reset_to_top and names:
            if not swipe_range and not list_roi:
                self._log("warn", "未配置 swipe_range / list_roi，无法确定回位手势，跳过滑回顶部")
            else:
                # 次数取向下查找上限 + 1：到顶后再滑就停住不动，不会滑过头
                n = scroll_list_to_top(max_swipes + 1, swipe_func,
                                       swipe_range=swipe_range, list_roi=list_roi,
                                       capture=snap, swipe_pause_ms=swipe_pause_ms)
                self._log("action", f"商品列表已滑回顶部（反向滑动 {n} 次）")

        return f"批量购买完成：成功 {ok_count} 个，失败 {fail_count} 个"

    def _do_negotiation(self, action, ctl, screen_path):
        """内置「协商」处理（次数与按钮坐标都从 config.negotiation 读）。

        流程：
        1. 重新截图并 OCR config.negotiation.region 得到 text
        2. 「进行1次」「进行所有剩余机会」都不在 text -> 无弹窗，直接返回
        3. 「进行1次」在 text（练度未满，还有剩余次数）：
              循环 max_clicks 次：点「进行一次」-> 等 click_interval_ms ->
              重新截图 OCR；「进行1次」消失则 break。
              点完后若按钮仍在（进行1次/进行所有剩余机会）-> 点「不」结算。
        4. 只有「进行所有剩余机会」（练度已满）：点该按钮，等 interval，返回。
        """
        cfg = self.config.get("negotiation", {})
        region = cfg.get("region", "协商按钮区")
        max_clicks = int(cfg.get("max_clicks", 1))
        interval = int(cfg.get("click_interval_ms", 1500)) / 1000
        buttons = cfg.get("buttons", {})

        KEY_ONCE = "进行1次"
        KEY_ALL = "进行所有剩余机会"

        # 协商 OCR 用独立截图文件，不覆盖主循环 / 分支判断 / 商品查找的画面
        nego_screen = self._cap_path("nego")

        def read_text():
            """重新截一张最新画面，再 OCR 协商按钮区（读到点击后的实时状态）。"""
            ok, info = self._snap(ctl, nego_screen, "协商截图")
            if not ok:
                self._log("warn", f"{info}，按当前画面继续判定")
            return ocr_text_by_region(nego_screen, region)

        # 步骤 1
        text = read_text()

        # 步骤 2：没有弹窗
        if KEY_ONCE not in text and KEY_ALL not in text:
            return f"协商未出现（区域文本: {text[:30]}），跳过"

        # 步骤 3：练度未满，还有「进行1次」
        if KEY_ONCE in text:
            once = buttons.get("once")
            no = buttons.get("no")
            if not once or not no:
                raise ValueError("config.negotiation.buttons.once / no 未配置")
            for _ in range(max_clicks):
                ctl.click(*once)
                time.sleep(interval)
                text = read_text()
                if KEY_ONCE not in text:
                    break  # 按钮消失，可能失败或次数用完，停止点击
            # 点完 N 次后按钮还在 -> 点「不」结算
            if KEY_ONCE in text or KEY_ALL in text:
                ctl.click(*no)
                time.sleep(1.0)
                return "点『进行一次』后仍可继续，点『不』结算"
            return f"点『进行一次』{max_clicks} 次后按钮消失，结束"

        # 步骤 4：练度已满，只有「进行所有剩余机会」
        all_btn = buttons.get("all")
        if not all_btn:
            raise ValueError("config.negotiation.buttons.all 未配置")
        ctl.click(*all_btn)
        time.sleep(interval)
        return "练度已满：点『进行所有剩余机会』"