"""运行态开关：引擎自己记账「这趟买过货没有、该不该去卖、每个港几点补货」。

数据文件 run_state.json 与 states.json / route_plan.json 同级，但**不由界面填写**：
它是引擎在跑的过程中自己写的，人只在出错时看一眼。

    {
      "sell_pending": true,
      "restock_at": {
        "汉堡": 1790000000
      }
    }

为什么不塞进 route_plan.json：那份文件回答的是「你规划这一趟怎么走」，是人手填的；
把引擎跑出来的状态混进去，以后看到 `sell_pending: true` 就分不清是你标的还是引擎标的。
为什么不塞进 states.json 的 config：那份是配置，改一次要重启才生效的是代码、不用重启的是数据，
运行态既不是配置也不是规划，所以单独一份。

为什么用开关而不是去画面里读货舱：2026-09-28 实测「出售」按钮**亮和灰分不出来** ——
同一块裁图在能成交的金色按钮上 1.0000，在不能成交的灰色按钮上 0.9039，三张灰图全是这个数；
换四块「只含金色底、不含文字」的裁法也不行，因为购买页同一个槽位也是金色、照样 1.0000。
所以「有没有货要卖」不看画面猜，按你定的规则记：**每次买完货置 1，卖出完成置 0**。

开关名只有白名单里这几个（FLAGS）。不在名单里的一律拒绝，理由是：这些值会落盘，
打错一个字母就会写出一个引擎永远不会读的文件字段，看起来存成功了，实际没人认。

`restock_at` 是同一本账上的第二样东西，但形状不一样：开关是布尔，它是一张**按港口分开的
时刻表**（港口名 -> 下次补货的 epoch 秒），所以单独走一对读/写口（get_restock_at /
set_restock_at），不塞进 FLAGS。为什么要它：画面上那串倒计时是「下一次刷新」，不是
「货架空了」，每天第一次买其实早就刷过了 —— 盯着它等纯属浪费时间（用户 2026-09-30 说的）。
按港口分开记是因为每个港的刷新点不同，热那亚等到的点不能拿去卡汉堡。
"""

import json
import os
import threading

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RUN_STATE_JSON = os.path.join(BASE_DIR, "run_state.json")

# 目前只有一个开关：买过货、还没卖 —— 卖货模块的链头认它。
FLAGS = ("sell_pending",)

DEFAULTS = {"sell_pending": False}

# 同一本账上的第二样东西：按港口记的「下次补货时刻」，字段名固定这一个。
RESTOCK_FIELD = "restock_at"

# 引擎的写和界面的读可能同时发生，落盘要一把锁；这把锁只在这一个进程内有效（同 T-022 的口径）。
_LOCK = threading.Lock()


def as_bool(value):
    """把读到的值收成真正的布尔。

    不直接 bool(value)：那样字符串 "false" 会变成 True，人手工改文件时最容易踩。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "1", "yes", "on"):
            return True
        if text in ("false", "0", "no", "off", ""):
            return False
        raise ValueError(f"开关值不是能认的布尔: {value!r}")
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise ValueError(f"开关值不是能认的布尔: {value!r}")


def load_state():
    """读 run_state.json；文件不存在就返回默认值（第一次跑还没写过）。

    只挑白名单里的键，文件里多出来的东西原样留在盘上不动 —— 那可能是别人手工加的备注，
    我们不该在下次保存时替人删掉。
    """
    with _LOCK:
        data = {}
        if os.path.exists(RUN_STATE_JSON):
            with open(RUN_STATE_JSON, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
        state = dict(DEFAULTS)
        for name in FLAGS:
            if name in data:
                state[name] = as_bool(data[name])
        return state


def get_flag(name):
    """读一个开关。名字不在白名单里直接抛错，不返回 None —— 拼错要当场响。"""
    if name not in FLAGS:
        raise ValueError(f"没有这个运行态开关: {name}（可选: {'、'.join(FLAGS)}）")
    return load_state()[name]


def set_flag(name, value):
    """写一个开关并落盘，返回写进去的布尔值。"""
    if name not in FLAGS:
        raise ValueError(f"没有这个运行态开关: {name}（可选: {'、'.join(FLAGS)}）")
    flag = as_bool(value)
    with _LOCK:
        data = {}
        if os.path.exists(RUN_STATE_JSON):
            try:
                with open(RUN_STATE_JSON, "r", encoding="utf-8") as f:
                    data = json.load(f) or {}
            except ValueError:
                data = {}
        data[name] = flag
        with open(RUN_STATE_JSON, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    return flag


def load_restock():
    """读「每个港下次补货的时刻」那张表 -> {港口名: 整数秒}；没记过就是空表。

    只认非负整数，别的形状一律当场报错（和开关那边一个口径）：布尔、字符串、浮点、
    负数、不是字典的整个字段 —— 拿到一个来路不明的时刻，要么白等半小时、要么早买，
    两种都不敢悄悄收下。报错之后怎么办由引擎决定（它按「没记过」处理并留一条警告），
    这一层只负责别把脏东西当成数。
    """
    with _LOCK:
        data = {}
        if os.path.exists(RUN_STATE_JSON):
            with open(RUN_STATE_JSON, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
        raw = data.get(RESTOCK_FIELD)
        if raw is None:
            return {}
        if not isinstance(raw, dict):
            raise ValueError(f"{RESTOCK_FIELD} 不是一张表: {raw!r}")
        out = {}
        for port, when in raw.items():
            if not str(port).strip():
                raise ValueError(f"{RESTOCK_FIELD} 里有一个空港口名")
            if isinstance(when, bool) or not isinstance(when, int) or when < 0:
                raise ValueError(f"港口『{port}』的补货时刻不是非负整数: {when!r}")
            out[str(port).strip()] = when
        return out


def port_name(port):
    """收一个港口名：必须是没带空格的文字，别的形状一律抛错。

    为什么不顺手 str(123)：JSON 的键只能是字符串，收下方数字会静默写进一个「123」格，
    永远匹配不上任何真港口 —— 那种格子没人读得到，正是这本账最安静的坏法。
    """
    if not isinstance(port, str):
        raise ValueError(f"港口名要写成文字，不是 {port!r}")
    name = port.strip()
    if not name:
        raise ValueError("港口名是空的（这一趟没选到港口？）")
    return name


def get_restock_at(port):
    """读一个港口记下的下次补货时刻；没记过返回 None。

    港口名空着直接抛错，不返回 None：那和「真的没记过」是两回事，混在一起会把
    「这一趟压根没选港」读成「今天第一次买」，然后一路不等直接买。
    """
    return load_restock().get(port_name(port))


def set_restock_at(port, when):
    """写一个港口的下次补货时刻（epoch 秒）并落盘，返回写进去的整数。

    只覆盖这个港口那一格，别人记的原样留着 —— 一张表按港口分开记（用户 2026-09-30 拍的粒度）。
    """
    name = port_name(port)
    if isinstance(when, bool) or not isinstance(when, int) or when < 0:
        raise ValueError(f"补货时刻要写成非负整数秒，不是 {when!r}")
    with _LOCK:
        data = {}
        if os.path.exists(RUN_STATE_JSON):
            try:
                with open(RUN_STATE_JSON, "r", encoding="utf-8") as f:
                    data = json.load(f) or {}
            except ValueError:
                data = {}
        table = data.get(RESTOCK_FIELD)
        if not isinstance(table, dict):
            table = {}
        table[name] = when
        data[RESTOCK_FIELD] = table
        with open(RUN_STATE_JSON, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    return when
