"""跑商规划：这一趟「跑哪个模块、船现在停在哪个港、按什么顺序走哪几站、每站买哪几件货」。

数据文件 route_plan.json 与 purchase_plan.json 同级，只在界面的「跑商设置」栏改：

    {
      "run_module": "buy",
      "current_port": "北京",
      "stops": [
        {"stage": "buy", "port": "北京", "goods": ["中国画", "油画"], "note": ""},
        {"stage": "buy", "port": "汉堡", "goods": ["啤酒"], "note": "补给"},
        {"stage": "transit", "port": "希洪", "note": "卸货腾舱"},
        {"stage": "sell", "port": "北京", "note": "卖艺术品"}
      ]
    }

格式是 2026-09-27 按你拍的顺序改的：**进货港口（可以多个）→ 中转港口（可以多个）→ 出货港口，
每一项内部也按顺序**。以前是 `buy_port` / `transit_port` / `sell_port` 三个单值字段，
装不下「可以多个」，所以整份换成一个**有序站次列表** `stops`：数组顺序就是走港口的顺序，
`stage` 只说明这一站是买货、中转还是出货。同一个港口在一趟里出现两次是允许的
（例如 北京 买货 → 希洪 中转 → 北京 出货）。

和购物表格的分工（2026-09-27 你纠正过一次，以这版为准）：
- **purchase_plan.json 只是目录**：回答「哪个港口买得到哪些货」（附类别，供改船舱用），
  一次填好长期用，里面**不含任何「这一趟」的信息**；
- **本文件回答「这一趟怎么走、每站买什么」**：`goods` 就是这一站本次要买的货，
  只能从该港在目录里已有的货里挑，数组顺序 = 先买哪件后买哪件（你要求「每一个项目内也按照顺序」）。
  以前是引擎拿「该港全部行」当本次清单，那份权力现在收回到跑商设置里了。

只有 `stage=buy` 的站能挂 `goods`（2026-09-28 卖货模块定了，规则照旧：
**卖货是「货舱里有什么卖什么」**，出货站挂购物清单没有意义，所以中转 / 出货两段都不挂）。
别的段挂了就拒绝保存 —— 存下一份引擎永远不会读的清单，比报错更容易骗人。

**出货段整段只能有一站**（2026-09-29 你指出「出货港口实际不可以多个」）：`normalize_route` 见到
两个 `stage=sell` 就拒绝保存。买货、中转两段仍然可以各排多站。

2026-09-27 加了模块这一层（你要求「跑商流程 = 买货 + 移动，每次只执行一个模块内的状态机判断」）：
- `run_module` 决定这一次启动跑哪套状态；引擎不看画面猜，只认这里填的模块名。
- `current_port` 是**船现在停在哪个港**。它现在担着三件事，不再只是装饰：
  ① 移动模块靠它算「下一站」，也算得出「下一站就是当前这个港 = 原地打转」（引擎会拒，出港花真金币）；
  ② **买货模块的「本次这一站」就是它**（你 2026-09-27 拍的「认当前所在港那一站」）——
  当前所在港在站次里对不到一个买货站，买货模块直接拒绝启动。
  ③ **卖货模块同理认「当前所在港 + 类型是出货」那一站**（2026-09-28 你拍的：要核对，按出货站核对），
  算出来的 `sell_port` 就是启动参数里那个 `sell_port`。

`derive()` 是唯一一处算「买货有哪几个港 / 本次这一站是哪站、挂哪几件货 / 本次出货港是谁 / 下一站是谁」的地方，
界面只把它显示出来，不另算一遍 —— 两处算就会有两处说法。
`stage=sell` 从 2026-09-28 起**会真跑**了（states.json 里的 `sell` 模块，把「本次运行模块」切成 sell）。
`stage=transit` 从 2026-09-29 起在**完整一趟**里会真走：它不是「跑一条链」，而是**途经这个港**
（你原话：「中转只是为了补充水粮，执行一次进出港的程序自然就实现了我的目的」）—— 引擎开到那儿、
补一次水粮、再开去下一站，不进交易所、不买也不卖。单模块（buy / sail / sell）还是不看中转段。
`run_module` 因此多了一个值：`"trip"` = 一次点启动按站次顺序把 买货 → 中转 → 卖货 整趟走完
（见 `TRIP_MODULE` 和 `_sort_trip_stages`）。整趟的**起点就是船实际站的那个港**：
`current_port` 可以留空，引擎在点启动时自己 OCR 读一次港名，用 `find_stop_by_name()` 认「这是第几站」。

**整趟的顺序由后端排，不靠人排对**（2026-09-29 你拍的 A：「后端保存时自动按规则重排」）：
`run_module=trip` 时保存会按 `买货 → 中转 → 卖货` 自动重排站次，**同类内部保持你排的先后**
（买货段的先后就是买货顺序，不会按港口名打乱），排完把「哪一站从第几挪到第几」回给界面显示。
以前排错顺序是当场拒绝保存，那条已经作废。仍然拒的两件事：一站都没排、出货段排了两站。
"""

import json
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROUTE_JSON = os.path.join(BASE_DIR, "route_plan.json")

DEFAULTS = {
    "run_module": "buy",
    "current_port": "",
    "stops": [],
}

STAGES = ("buy", "transit", "sell")
STAGE_LABELS = {"buy": "买货", "transit": "中转", "sell": "出货"}
STAGE_LIST_KEYS = {"buy": "buy_ports", "transit": "transit_ports", "sell": "sell_ports"}

# 一站只有这四个字段，多一个都不收
STOP_KEYS = ("stage", "port", "goods", "note")
TEXT_FIELDS = ("run_module", "current_port")

# 2026-09-29 你要求的第四种跑法：一次点启动把「进货 →（可选）中转 → 卖货」整趟走完。
# 它不是一条新的状态链，而是**让引擎按站次顺序依次去走那三条已有的链**，
# 所以模块名由引擎/界面认，数据层只多一件事：保存时检查这趟的顺序真能一路走下来。
TRIP_MODULE = "trip"


def _norm_goods(raw, where, stage):
    """这一站本次要买的货。数组顺序就是先买哪件后买哪件（你要求「项目内也按顺序」）。"""
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise ValueError(f"{where} 的『本次货物』必须是一个列表，现在是 {type(raw).__name__}")
    if raw and stage != "buy":
        raise ValueError(f"{where} 是{STAGE_LABELS[stage]}站，不能挂货物清单 —— "
                         f"只有「买货」站能挑货；卖货是货舱里有什么卖什么（点全部添加），"
                         f"中转段状态机里还没有")
    out = []
    for g in raw:
        if not isinstance(g, str):
            raise ValueError(f"{where} 的货物名只能是文字，现在是 {type(g).__name__}")
        name = g.strip()
        if not name:
            raise ValueError(f"{where} 的货物清单里有一项是空的 —— 去掉它，别留空项")
        if name in out:
            # 引擎没有「买几件」这个概念，一件货出现两次就会真买两次，所以在这里拦掉
            raise ValueError(f"{where} 的『{name}』出现了两次 —— 买货没有数量，重复=买两遍")
        out.append(name)
    return out


def _norm_stop(raw, i):
    """整一行站点。第 i 站是用来报错的，界面拿到的是中文，别说「index 2」。"""
    where = f"第 {i + 1} 站"
    if not isinstance(raw, dict):
        raise ValueError(f"{where} 必须是一个对象 {list(STOP_KEYS)}，现在是 {type(raw).__name__}")
    unknown = [k for k in raw if k not in STOP_KEYS]
    if unknown:
        raise ValueError(f"{where} 有界面不认识的项目: " + "、".join(sorted(unknown)))
    stage = (raw.get("stage") or "").strip()
    if stage not in STAGES:
        raise ValueError(f"{where} 的类型只能是 {'、'.join(STAGES)}（买货 / 中转 / 出货），现在是 {stage!r}")
    port = (raw.get("port") or "").strip()
    if not port:
        raise ValueError(f"{where}（{STAGE_LABELS[stage]}）没填港口名 —— 空着一站会让后面的顺序错位")
    note = raw.get("note") or ""
    if not isinstance(note, str):
        raise ValueError(f"{where} 的备注只能是文字，现在是 {type(note).__name__}")
    return {"stage": stage, "port": port,
            "goods": _norm_goods(raw.get("goods"), where, stage),
            "note": note.strip()}


def _check_sell_single(stops):
    """出货段整段只能有一站（2026-09-29 你指出「出货港口实际不可以多个」）。

    为什么是硬拦而不是提醒：卖货模块只认「当前所在港 + 类型是出货」的**那一站**，
    多排的出货站引擎永远不会读 —— 存下一份永远不会走的站，比报错更容易骗人
    （和「非买货段不许挂货物清单」是同一条口径）。
    """
    at = [i for i, s in enumerate(stops) if s["stage"] == "sell"]
    if len(at) <= 1:
        return
    listed = "、".join(f"第 {i + 1} 站『{stops[i]['port']}』" for i in at)
    raise ValueError(f"出货段只能有一站（一个出货港），现在有 {len(at)} 站：{listed} —— "
                     f"卖货模块只认一站，多排的永远不会走；"
                     f"确实要路过别的港就当「中转」排，或者删掉多余那站")


def _sort_trip_stages(stops):
    """整趟走法：进货（可多站）→ 中转（可选，可多站）→ 卖货，顺序不对就**自动重排**。

    2026-09-29 你拍的（「进货出货排序，强制按照」→ 选 A：后端保存时自动按规则重排）：
    以前排错了是当场拒绝保存，现在改成按 `买货 → 中转 → 卖货` 排好，再把动了哪几站回给界面。
    两条口径没动：
    ① 一站都没排照样拒（重排不出个所以然，那就是「还没排计划」）；
    ② 出货段两站由 `_check_sell_single` 先拒掉，所以到这里最多只有一个 sell，排完自然在最后。
    **同类内部保持你排的先后**（稳定排序，不按港口名排）：买货段的先后就是买货顺序，
    按名字排会把你勾的顺序打乱。
    重排后那份和原来那份**站数、内容一模一样，只有先后不同** —— 所以调用方可以拿
    「第几站」两边对上（见 `describe_reorder`），这里不返回一个说不清的元组。
    """
    if not stops:
        raise ValueError("完整一趟一站都没排 —— 先去「跑商设置」把进货（可选中转）和卖货排好")
    rank = {stage: i for i, stage in enumerate(STAGES)}
    return sorted(stops, key=lambda s: rank[s["stage"]])


def describe_reorder(before, after):
    """重排了哪几站：「第 X 站…→ 第 Y 站」一句一条，没动就不产出。

    单独一个函数、不塞进 `_sort_trip_stages` 的返回值，是为了让那边**只认位置不认内容**
    （按 stage 排完拿原下标回原位，是天然幂等的）。要是让那边返回「站 + 原下标」，
    这份规划就要被规整两遍（`load_route` 一次、接口再算一次），非买货段挂货会被重复检查。

    同一个港在同一趟里可以排两次（数据层只提醒不拦），所以按内容对上时**一站一个坑**：
    内容完全相同的两站按先后配到相同内容的各个新位置上（稳定排序保序），
    不能让它们都盯着最后一个位置 —— 那样没动的站会被报成「挪过」。
    """
    if len(before) != len(after):
        return
    def key(s):
        return (s["stage"], s["port"], tuple(s["goods"]), s["note"])
    slots = {}
    for pos, s in enumerate(after):
        slots.setdefault(key(s), []).append(pos)
    for i, s in enumerate(before):
        queue = slots.get(key(s))
        if not queue:
            continue                       # 内容对不上（不该发生），不蒙一个位置给人看
        pos = queue.pop(0)
        if pos != i:
            yield (f"第 {i + 1} 站{STAGE_LABELS[s['stage']]}『{s['port']}』"
                   f" → 第 {pos + 1} 站")


def normalize_route(record, moves=None):
    """规整整份规划：认得的字段收下、缺的补默认值，认不出的直接报错。

    不用默认值兜着未知字段：界面和后端对不上时，宁可当场拒绝，
    也别存下一份「看起来保存成功了、其实那次改动被丢掉了」的规划。

    `moves` 传一个列表给调用方才有效果：这次保存**因为整趟顺序被自动重排**了哪几站，
    一句一条写进去，给接口和界面回显用。重排只在 `run_module=trip` 时发生（单模块不看先后，
    移动模块的「下一站」就是数组顺序，替它排一下等于偷偷改了它的目的地）。
    重排信息只走 `moves` 这个出参，**不塞进返回的那份规划** —— 规划要原样落盘，
    多一个键下一次 `load_route` 就会当成「界面不认识的项目」拒读。
    """
    if not isinstance(record, dict):
        raise ValueError("规划必须是一个对象 {run_module, current_port, stops}")
    unknown = [k for k in record if k not in DEFAULTS]
    if unknown:
        raise ValueError("规划里有界面不认识的项目: " + "、".join(sorted(unknown)))
    out = {"run_module": "", "current_port": "", "stops": []}
    for key in TEXT_FIELDS:
        value = record.get(key, DEFAULTS[key])
        if not isinstance(value, str):
            raise ValueError(f"『{key}』只能填文字，现在是 {type(value).__name__}")
        out[key] = value.strip()          # 港口名两边的空格不算差异，存之前去掉
    stops = record.get("stops", DEFAULTS["stops"])
    if not isinstance(stops, list):
        raise ValueError(f"『站次』必须是一个列表，现在是 {type(stops).__name__}")
    out["stops"] = [_norm_stop(s, i) for i, s in enumerate(stops)]
    _check_sell_single(out["stops"])
    if out["run_module"] == TRIP_MODULE:
        # 只有真跑整趟才管先后；单模块那三条链各自只比那一站，排在前还是在后不影响它能不能跑
        before = out["stops"]
        out["stops"] = _sort_trip_stages(before)
        if moves is not None:
            moves.extend(describe_reorder(before, out["stops"]))
    return out


def load_route():
    """读取 route_plan.json；文件不存在就返回全空的规划（第一次用还没建过文件）。

    文件存在但内容不认（比如还是改格式之前的 buy_port / transit_* 那一版）→ 让它抛 ValueError：
    界面会把原因原样显示成「读取规划失败」，比悄悄给你一份空规划好 ——
    空规划看着像「我没填过」，实际是「文件里的东西没读进来」，这两种事差得远。
    """
    if not os.path.exists(ROUTE_JSON):
        # stops 必须是每次新建的列表：直接 dict(DEFAULTS) 会让所有人共用 DEFAULTS 里那一个，
        # 谁往里加一站就把默认值本身改了。
        return {**DEFAULTS, "stops": []}
    with open(ROUTE_JSON, "r", encoding="utf-8") as f:
        data = json.load(f)
    return normalize_route(data)


def save_route(route):
    with open(ROUTE_JSON, "w", encoding="utf-8") as f:
        json.dump(route, f, ensure_ascii=False, indent=2)


def ports_for_stage(route, stage):
    """某一段按顺序有哪几个港。段内顺序 = 数组里的先后。"""
    return [s["port"] for s in route.get("stops") or [] if s["stage"] == stage]


def stop_index(stops, cur, stage):
    """船停在 cur 时，本次跑某一段（买货 / 出货）是哪一站。返回 (下标, 这一趟里 cur 出现过的所有下标)。

    只认 `stage=` 指定的那一站（2026-09-27 你拍：「认当前所在港那一站」；2026-09-28 卖货照同一条）。
    同一个港在一趟里既买又卖很常见（北京买货 → … → 北京出货），所以不能只看港名，必须连 stage 一起对。
    cur 为空、或这个港没作为该段出现过 → 下标 -1，第二个返回值用来把「差在哪」说清楚。
    """
    if not cur:
        return -1, []
    at = [i for i, s in enumerate(stops) if s["port"] == cur]
    hit = [i for i in at if stops[i]["stage"] == stage]
    return (hit[0] if hit else -1), at


def _fill_trip(out, stops, cur, at):
    """整趟（run_module=trip）用的三样：船站在第几站、按顺序还要走哪几站、说不清时的人话原因。

    `at` 是「这个港名在这趟里出现的所有下标」（`stop_index` 顺手算出来的，不看 stage）。
    同一个港在一趟里出现两次很常见（汉堡买货 → … → 汉堡卖货），而 OCR 只读得出港名、
    读不出「第几次到」—— 你 2026-09-29 拍的是「重启时靠 OCR 读当前港，从那站继续」，
    所以这里认**第一次**出现的那一站，并把重复那次写进原因里说清，不自己猜。
    """
    if not at:
        out["trip_reason"] = (f"『{cur}』不在这一趟的站次里（排的是："
                              f"{' → '.join(s['port'] for s in stops) or '一站都没排'}）"
                              f" —— 整趟不知道该从哪一站走起")
        return
    start = at[0]
    out["trip_start_idx"] = start
    out["trip_stops"] = [{"idx": i, "stage": s["stage"], "port": s["port"],
                          "goods": list(s.get("goods") or []), "note": s.get("note") or ""}
                         for i, s in enumerate(stops) if i >= start]
    steps = " → ".join(f"第 {s['idx'] + 1} 站 {STAGE_LABELS[s['stage']]}『{s['port']}』"
                       for s in out["trip_stops"])
    out["trip_reason"] = (f"从『{cur}』这一站起，整趟按顺序走 {len(out['trip_stops'])} 站：{steps}")
    if len(at) > 1:
        again = "、".join(f"第 {i + 1} 站{STAGE_LABELS[stops[i]['stage']]}" for i in at[1:])
        out["trip_reason"] += (f"（『{cur}』在这趟还出现在 {again} —— OCR 只读得出港名、"
                               f"读不出第几次到，这里认第一次）")


def derive(route):
    """从站次列表算出界面和引擎都要用的几件事。只此一处，别在 JS 里再算一遍。

    - 三段的港口列表
    - next_port：**船停在 current_port 时，按顺序走的下一站是哪个港**
    - next_reason：算不出来（或者算出来是原地打转）时的人话原因
    - here_idx：后端认的「船站在第几站」的下标。同一个港在一趟里出现两次很正常
      （北京买货 → … → 北京出货），界面不能自己挑一个描边框，否则「哪一站是当前港」
      就有两套说法；界面就按这里给的下标描。没有匹配上时是 -1。
    - buy_stop_idx / buy_port / buy_goods / buy_reason：**本次买货港 + 该买哪几件货**。
      这就是买货模块要的「本次清单」，界面只负责显示和原样转发给启动接口。
    - sell_stop_idx / sell_port / sell_reason：**本次出货港**（2026-09-28 卖货模块加的）。
      和买货同一条规则 —— 认「当前所在港 + 类型是出货」那一站；卖货不吃货物清单
      （你拍的「货舱里有什么卖什么，全部添加」），所以这一段**没有** goods。
    """
    stops = route.get("stops") or []
    out = {"buy_ports": [], "transit_ports": [], "sell_ports": [],
           "next_port": "", "next_stage": "", "next_reason": "", "here_idx": -1,
           "buy_stop_idx": -1, "buy_port": "", "buy_goods": [], "buy_reason": "",
           "sell_stop_idx": -1, "sell_port": "", "sell_reason": "",
           "trip_start_idx": -1, "trip_stops": [], "trip_reason": ""}
    for stage, key in STAGE_LIST_KEYS.items():
        out[key] = ports_for_stage(route, stage)

    if not stops:
        out["next_reason"] = "这趟一站都没排（先去「跑商设置」加站）"
        out["buy_reason"] = "这趟一站都没排，没有本次买货港"
        out["sell_reason"] = "这趟一站都没排，没有本次出货港"
        out["trip_reason"] = "这趟一站都没排，整趟不知道该从哪一站走起"
        return out
    cur = route.get("current_port") or ""
    if not cur:
        out["next_reason"] = "没填「当前所在港」，看不出下一站是哪个"
        out["buy_reason"] = "没填「当前所在港」，认不出本次该在哪个港下单"
        out["sell_reason"] = "没填「当前所在港」，认不出本次该在哪个港出货"
        out["trip_reason"] = ("没填「当前所在港」—— 整趟要在点启动时自己 OCR 读一次港口名，"
                              "读到哪个港就从那一站起步；读不出或者读到的港不在这趟里，拒绝启动")
        return out

    idx, at = stop_index(stops, cur, "buy")
    _fill_trip(out, stops, cur, at)
    if idx >= 0:
        out["buy_stop_idx"] = idx
        out["buy_port"] = stops[idx]["port"]
        out["buy_goods"] = list(stops[idx].get("goods") or [])
        n = len(out["buy_goods"])
        out["buy_reason"] = (f"本次买货：第 {idx + 1} 站『{cur}』，挂了 {n} 件货"
                             + ("（一件都没挂，点启动会被拒绝）" if not n else
                                "，顺序：" + " → ".join(out["buy_goods"])))
    elif not at:
        out["buy_reason"] = (f"「当前所在港」填的是『{cur}』，它不在这一趟的站次里"
                             + (f"；这趟的买货港是 {'、'.join(out['buy_ports'])}"
                                if out["buy_ports"] else "（也没排任何买货站）"))
    else:
        kinds = "、".join(STAGE_LABELS[stops[i]["stage"]] for i in at)
        out["buy_reason"] = (f"『{cur}』在这一趟里是{kinds}站，不是买货站 —— "
                             f"买货模块只认「当前所在港 + 类型是买货」的那一站")

    idx_s, at_s = stop_index(stops, cur, "sell")
    if idx_s >= 0:
        out["sell_stop_idx"] = idx_s
        out["sell_port"] = stops[idx_s]["port"]
        out["sell_reason"] = (f"本次出货：第 {idx_s + 1} 站『{cur}』"
                              f"（货舱里有什么卖什么，这一站不挂货物清单）")
    elif not at_s:
        out["sell_reason"] = (f"「当前所在港」填的是『{cur}』，它不在这一趟的站次里"
                              + (f"；这趟的出货港是 {'、'.join(out['sell_ports'])}"
                                 if out["sell_ports"] else "（也没排任何出货站）"))
    else:
        kinds_s = "、".join(STAGE_LABELS[stops[i]["stage"]] for i in at_s)
        out["sell_reason"] = (f"『{cur}』在这一趟里是{kinds_s}站，不是出货站 —— "
                              f"卖货模块只认「当前所在港 + 类型是出货」的那一站")

    here = at
    if not here:
        out["next_reason"] = (f"「当前所在港」填的是『{cur}』，可它不在这一趟的站次里 "
                              f"（排的是：{' → '.join(s['port'] for s in stops)}）")
        return out
    out["here_idx"] = here[0]        # 先落在第一次出现的那一站，往下的分支会把它改成真正用作出发地的那一站
    for i in here:
        if i + 1 >= len(stops):
            continue
        out["here_idx"] = i
        nxt = stops[i + 1]
        out["next_port"] = nxt["port"]
        out["next_stage"] = nxt["stage"]
        if nxt["port"] == cur:
            out["next_reason"] = (f"第 {i + 2} 站又是『{cur}』本身 —— 出港要花真金币，"
                                  f"同港不叫移动")
        else:
            out["next_reason"] = (f"从『{cur}』出发，下一站是第 {i + 2} 站"
                                  f"{STAGE_LABELS[nxt['stage']]}港『{nxt['port']}』")
        return out
    out["next_reason"] = f"『{cur}』已经是这趟的最后一站，没有下一站了"
    return out


def find_stop_by_name(stops, name):
    """OCR 读出来的港名在这一趟里对到哪一站。返回 (下标, 对上的所有下标)，对不上是 (-1, [])。

    只做「包含」判断（和 plan_port 条件同一条口径，不上正则）：画面那块 roi 会读到**两行**，
    OCR 拼出来的串常常带着下面那行，所以是「读到的文字里含这个港名」而不是「等于」。
    反过来不行：读成「汊堡」就含不到「汉堡」—— 认不出就返回 -1，让调用方去拒绝启动，不猜。

    多个港名同时命中时取**名字最长**的那个（再按站次先后）：万一这趟里既有「伦敦」又有「东伦敦」，
    读到「东伦敦」里同样含「伦敦」，认成短的会把船叫去错的港。
    """
    hit = [i for i, s in enumerate(stops) if s["port"] and s["port"] in (name or "")]
    if not hit:
        return -1, []
    best = min(hit, key=lambda i: (-len(stops[i]["port"]), i))
    return best, hit


def warnings(route, plan_ports, goods_by_port=None):
    """能跑不能跑的提醒，返回中文字串列表。

    这里只提醒不拦：真正的拦在引擎启动那一步。
    goods_by_port：目录里「哪个港口有哪几件货」，用来核对某一站挂的货是不是真买得到。
    不传就跳过这一项检查（老调用方还能用），但界面走 /api/route 时一定会传。
    """
    out = []
    d = derive(route)
    stops = route.get("stops") or []
    is_trip = route.get("run_module") == TRIP_MODULE
    if not route.get("run_module"):
        out.append("没选本次运行模块：states.json 里有一个以上模块时引擎会拒绝启动，不知道该走哪条链。")
    if not d["buy_ports"]:
        out.append("这趟没有「买货」站：买货模块点启动会因为选不出买货港口被拒绝。")
    for port in d["buy_ports"]:
        if port not in plan_ports:
            out.append(f"买货港『{port}』在购物表格里一行货都没有，点启动会被拒绝 —— "
                       f"先去「表格」栏给这个港口加行，或者把这一站改成表格里已有的港口。")
    cur = route.get("current_port") or ""
    for i, s in enumerate(stops):
        where = f"第 {i + 1} 站『{s['port']}』"
        if s["stage"] != "buy":
            continue
        if not s.get("goods"):
            out.append(f"{where} 没勾任何货物：买货模块认「当前所在港」那一站的清单，"
                       f"空清单点启动会被拒绝（一步货都不买却照样点购买/确定）。")
        have = (goods_by_port or {}).get(s["port"])
        if have:
            missing = [g for g in s.get("goods") or [] if g not in have]
            if missing:
                out.append(f"{where} 挂的 {'、'.join(missing)} 在这个港的购物表格里没有 —— "
                           f"多半是表格那栏改过；点启动会被拒绝，去把货重新勾一遍。")
    if cur and d["buy_stop_idx"] < 0 and d["buy_ports"]:
        out.append(f"本次买货港认不出来：{d['buy_reason']}")
    dup = [p for p in set(d["buy_ports"]) if d["buy_ports"].count(p) > 1]
    if dup:
        out.append(f"这些港在买货段里出现了不止一次：{'、'.join(sorted(dup))} —— "
                   f"船停在这个港时后端只认**第一次**出现的那一站，第二站的清单不会生效，"
                   f"要么合并要么换个港名。")
    if route.get("run_module") == "sell":
        # 这三条只在「本次真跑卖货」时才提：只买不卖的趟里没排出货站是正常的，
        # 每次都提醒一句就成噪音了（买货那几条反过来也一样，只是买货是目前默认的模块）。
        if not d["sell_ports"]:
            out.append("这趟没有「出货」站：卖货模块拿「当前所在港 + 类型是出货」那一站的港名去核对画面，"
                       "一段都没排的话点启动会被拒绝。")
        if cur and d["sell_stop_idx"] < 0 and d["sell_ports"]:
            out.append(f"本次出货港认不出来：{d['sell_reason']}")
        # 「出货段排了两站」这种提醒没有了：数据层直接拒绝保存（见 _check_sell_single）
    if not cur:
        if is_trip:
            out.append("没填「当前所在港」：完整一趟会在点启动时自己 OCR 读一次港口名，"
                       "读到哪个港就从那一站起步；读不出（或读到的港不在这趟里）会拒绝启动。")
        else:
            out.append("没填「当前所在港」：买货模块认它定本次这一站，移动模块靠它算下一站；"
                       "不填这两个模块点启动都会被拒绝。")
    elif len(stops) >= 2 and not is_trip:
        # 只排一站的趟不存在「去哪一站」，就不拿「算不出下一站」去烦人
        if not d["next_port"]:
            out.append(f"算不出下一站：{d['next_reason']}")
        elif d["next_port"] == cur:
            out.append(f"下一站就是当前港：{d['next_reason']}")
    if is_trip:
        if stops and d["trip_start_idx"] < 0:
            out.append(f"整趟的起点认不出来：{d['trip_reason']}")
        if d["trip_start_idx"] > 0:
            out.append(f"整趟从第 {d['trip_start_idx'] + 1} 站起步，前面 "
                       f"{d['trip_start_idx']} 站本次不再走 —— 注意「重启续跑」这一条：买货没有数量概念，"
                       f"要是这一站的货上一轮已经买过，再点启动会**把它再买一遍**（花真金币）。")
        kinds_t = [s["stage"] for s in stops]
        if kinds_t and kinds_t[-1] != "sell":
            out.append("提醒：这趟最后一站不是「出货」站，整趟跑到那儿就买完了、货还在舱里没卖。"
                       "按你说的走法应该是 进货 →（可选）中转 → 卖货。")
    if d["transit_ports"]:
        if is_trip:
            out.append(f"提醒：中转站 {'、'.join(d['transit_ports'])} 只做「进一次港、再出一次港」"
                       "（你说的是补一次水粮）—— 到那儿不进交易所、不点买也不点卖。"
                       "⚠ 每次出航都花真金币（实测 699 一次、数额会变），排一个中转就是多花一次。")
        else:
            out.append("提醒：中转段现在只是存进文件，单模块里还没有「中转」这个模块，"
                       "它不会真的走 —— 顺序先按你拍的样子存着。要一路途经它们就选「完整一趟」。")
    if d["sell_ports"]:
        if is_trip:
            out.append("提醒：整趟的卖货那一段照样认账本 —— 链头要 `sell_pending` 是 1 才开卖，"
                       "而这个开关由买货链尾置 1。所以从中间某站起半趟时，如果这一趟还没买过货，"
                       "到出货港那一段会不起链（不会瞎卖）。")
        else:
            out.append("提醒：出货段已经有卖货模块了（把「本次运行模块」切成 sell 才会走），"
                       "而且链头还认账本：买货那趟没跑完（sell_pending 不是 1）它不会开卖。")
    return out
