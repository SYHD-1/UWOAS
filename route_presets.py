"""跑商方案（preset）：把「一整套买货 / 中转 / 卖货计划」起个名字存起来，以后一次读出来用。

数据文件 route_presets.json 与 route_plan.json 同级：

    {
      "presets": [
        {
          "name": "北京买油画 → 热那亚出货",
          "run_module": "trip",
          "stops": [
            {"stage": "buy", "port": "北京", "goods": ["油画"], "note": ""},
            {"stage": "sell", "port": "热那亚", "goods": [], "note": "货舱里有什么卖什么"}
          ],
          "saved_at": "2026-09-29 15:20"
        }
      ]
    }

**2026-09-30 起方案一律是完整一趟（`run_module` 恒为 `"trip"`）**（你拍的口径：「跑商设置中
不再区分某一种类型，全部都是完整流程 trip」）。这个字段继续留在文件里（不删是为了 `summary()`
和自检不用改结构），但**保存时不再认前端传的 `run_module`**，一律写死成 `route_plan.TRIP_MODULE`：
跑商设置那一栏现在只编方案，方案 = 一整套买货 → 可选中转 → 出货，不存在「只买货的方案」了。
真跑哪几个方案、按什么顺序跑，交给 run_queue.json。

和 route_plan.json 的分工（2026-09-29 你拍的两条）：
- **route_plan.json = 现在这一趟**，引擎启动读它，改一次就是一次；
- **route_presets.json = 攒下来的多套计划**，引擎不读它，只有界面的「读取方案」会把它写进上面那份。

方案里**不含「当前所在港」**（你选的口径）：船今天停在哪个港是每次都要重新看的事，
存进方案就会在半年后骗人。所以读取方案只换「模块 + 站次 + 每站勾选的货」，
`current_port` 保持界面上现在填的那个 —— 站次里没有这个港时，后端算不出本次这一站，
`derive()` 的 reason 会当场说清楚，不会拿旧港名蒙混。

同名就是**覆盖**（界面在发请求前会先问你一次）。不做「自动改名叫 X(2)」那种事：
方案是拿来读的，读的人只认名字，悄悄多出一份近似名等于埋一份没人知道的重复计划。

站次本身一个字都不另写校验 —— 全部交给 `route_plan.normalize_route`：
「非买货段不许挂货物清单」「出货段只能一站」这些规矩，方案和未来那一趟必须完全一样，
不然会出现「存得进方案、读出来却保存不了」。
"""

import json
import os
import time

import route_plan

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PRESETS_JSON = os.path.join(BASE_DIR, "route_presets.json")

MAX_NAME_CHARS = 40


def _now():
    return time.strftime("%Y-%m-%d %H:%M")


def normalize_preset(record, moves=None):
    """规整一份方案：{name, run_module, stops}。不认识的项目直接报错，理由同 route_plan。

    `moves` 是透给 `route_plan.normalize_route` 的那份「重排了哪几站」，
    存方案时回给界面显示用；读方案库时不传（那一份早就排好了，也不该有人被通知）。
    """
    if not isinstance(record, dict):
        raise ValueError("方案必须是一个对象 {name, run_module, stops}")
    unknown = [k for k in record if k not in ("name", "run_module", "stops", "saved_at")]
    if unknown:
        raise ValueError("方案里有界面不认识的项目: " + "、".join(sorted(unknown)))

    name = record.get("name")
    if not isinstance(name, str):
        raise ValueError(f"方案名只能是文字，现在是 {type(name).__name__}")
    name = name.strip()
    if not name:
        raise ValueError("方案名是空的 —— 方案是靠名字读出来的，没名字就存不成")
    if len(name) > MAX_NAME_CHARS:
        raise ValueError(f"方案名太长了（{len(name)} 个字，最多 {MAX_NAME_CHARS} 个）—— "
                         f"下拉里放不下，也认不出来")

    # 站次走 route_plan 那一套校验：把方案当成一份「没有当前所在港的规划」规整一遍。
    # run_module 写死成 trip：方案一律完整一趟，前端传什么都不认（传了也无害，见文件头说明）。
    route = route_plan.normalize_route({
        "run_module": route_plan.TRIP_MODULE,
        "current_port": "",
        "stops": record.get("stops") or [],
    }, moves)
    if not route["stops"]:
        raise ValueError(f"方案『{name}』一站都没排 —— 空方案读出来只会把现在这趟抹掉")

    saved_at = record.get("saved_at")
    return {"name": name, "run_module": route["run_module"], "stops": route["stops"],
            "saved_at": saved_at.strip() if isinstance(saved_at, str) else _now()}


def load_presets():
    """读全部方案。文件不存在返回空列表（第一次用还没存过）；内容不认就抛，不返回空列表骗人。"""
    if not os.path.exists(PRESETS_JSON):
        return []
    with open(PRESETS_JSON, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):        # 手改过文件、直接写成一个数组，也认
        rows = data
    elif isinstance(data, dict):
        rows = data.get("presets")
        if not isinstance(rows, list):
            raise ValueError("route_presets.json 的『presets』必须是一个列表")
    else:
        raise ValueError("route_presets.json 必须是一个对象 {presets: [...]}")
    out = [normalize_preset(r) for r in rows]
    _check_names(out)
    return out


def _check_names(items):
    dup = sorted({i["name"] for i in items if [x["name"] for x in items].count(i["name"]) > 1})
    if dup:
        raise ValueError("方案名重复： " + "、".join(dup) + " —— 同名会让「读取」不知道读哪一份")


def save_presets(items):
    _check_names(items)
    with open(PRESETS_JSON, "w", encoding="utf-8") as f:
        json.dump({"presets": items}, f, ensure_ascii=False, indent=2)


def summary(preset):
    """列表上那一行的人话摘要：几站、怎么走、买几件、什么时候存的。"""
    stops = preset.get("stops") or []
    text = " → ".join(f"{route_plan.STAGE_LABELS.get(s['stage'], s['stage'])}·{s['port']}"
                      for s in stops)
    n_goods = sum(len(s.get("goods") or []) for s in stops)
    return {"name": preset["name"], "run_module": preset.get("run_module") or "",
            "saved_at": preset.get("saved_at") or "", "stop_count": len(stops),
            "route_text": text or "（空）", "goods_count": n_goods}


def find(items, name):
    for p in items:
        if p["name"] == name:
            return p
    return None


def to_route(preset, current_port):
    """方案 + 界面上现在的「当前所在港」→ 一份可以直接写进 route_plan.json 的规划。"""
    return {"run_module": preset.get("run_module") or "",
            "current_port": current_port or "",
            "stops": [dict(s, goods=list(s.get("goods") or [])) for s in preset.get("stops") or []]}
