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
          "options": {
            "refit": {"enabled": true, "ship": "改良荒木船",
                      "slots": ["1行2列", "1行3列", "3行3列"], "cargo_type": "艺术作品"},
            "switch_config": {
              "before_buy": {"enabled": true, "config_name": "买货配置"},
              "before_sail": {"enabled": false, "config_name": ""},
              "before_sell": {"enabled": true, "config_name": "卖货配置"}
            }
          },
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

`options` 是跑商设置里的附加步骤：`refit` 记录购买前改舱，`switch_config` 分别记录
每个买货港购买前、每次港口移动出港前、到达出货港卖货前是否切换，以及目标游戏配置名。
`to_route()` 故意不带 options：route_plan.json 只保存当前站次；队列启动时从方案库取出 options，
直接作为 `trip_options` 交给引擎。切换配置会按时点进入 states.json 的 switch_config 链，按名称
滚动 OCR 配置列表、点击对应行和“应用”、返回港口，再恢复原定的买货、移动或卖货入口。
购买前改舱仍只保存参数，尚未接入完整一趟。
"""

import json
import os
import time

import purchase_plan
import route_plan

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PRESETS_JSON = os.path.join(BASE_DIR, "route_presets.json")

MAX_NAME_CHARS = 40

# 「购买前改舱」目前只支持这一种船：船舱页那三张判据模板（UI-船舱-可搭乘-…）是
# 2026-09-23 照着「改良荒木船」录的，别的船种一张都没录。界面上它们是占位项，
# 存进来一个没录判据的船种，将来接引擎时就会拿着它去点一个不认识的画面。
REFIT_SHIPS = ["改良荒木船"]
# 只给「可搭乘船舱」那三格（金币档，模板已录且和另一组互斥）。
# 「无法搭乘」栏那些以「仓库」结尾的仓位要花**蓝色钻石**，2026-09-23 你明确要求
# 为节约账号资源不碰 —— 所以这里根本不列它们，勾不成也比勾错了能改强。
REFIT_SLOTS = ["1行2列", "1行3列", "3行3列"]
OPTION_KEYS = ("refit", "switch_config")
REFIT_KEYS = ("enabled", "ship", "slots", "cargo_type")
SWITCH_CONFIG_POINTS = ("before_buy", "before_sail", "before_sell")
SWITCH_CONFIG_POINT_KEYS = ("enabled", "config_name")


def default_options():
    """一份全关掉的附加步骤：老方案文件里没有 options 这一项时按它补，不报错。

    船种默认给那唯一一种，不给空串 —— 空串会让界面下拉里出现一个「（没选）」，
    而这一栏其实没得选。
    """
    return {
        "refit": {"enabled": False, "ship": REFIT_SHIPS[0], "slots": [], "cargo_type": ""},
        "switch_config": {
            point: {"enabled": False, "config_name": ""}
            for point in SWITCH_CONFIG_POINTS
        },
    }


def _must_bool(label, v):
    if not isinstance(v, bool):
        raise ValueError(f"{label}只能是要/不要（勾框），现在是 {type(v).__name__}")
    return v


def normalize_options(raw):
    """规整改舱参数与三个切换配置时点；不认识的项目一律报错。"""
    if raw is None:
        return default_options()
    if not isinstance(raw, dict):
        raise ValueError("options 必须是一个对象 {refit, switch_config}")
    unknown = [k for k in raw if k not in OPTION_KEYS]
    if unknown:
        raise ValueError("附加步骤里有不认识的项目: " + "、".join(sorted(unknown)))

    out = default_options()

    refit = raw.get("refit")
    if refit is not None:
        if not isinstance(refit, dict):
            raise ValueError("『购买前改舱』必须是一个对象 {enabled, ship, slots, cargo_type}")
        bad = [k for k in refit if k not in REFIT_KEYS]
        if bad:
            raise ValueError("『购买前改舱』里有不认识的项目: " + "、".join(sorted(bad)))
        enabled = _must_bool("『购买前改舱』的开关", refit.get("enabled", False))
        ship = (refit.get("ship") or "").strip() if refit.get("ship") is not None else ""
        ship = ship or out["refit"]["ship"]
        if ship not in REFIT_SHIPS:
            raise ValueError(f"『{ship}』的船舱页判据还没录，现在只能改: {'、'.join(REFIT_SHIPS)}")
        slots_raw = refit.get("slots") or []
        if not isinstance(slots_raw, list):
            raise ValueError("『购买前改舱』的仓位必须是一个列表")
        slots = [str(s).strip() for s in slots_raw if str(s).strip()]
        dup = sorted({s for s in slots if slots.count(s) > 1})
        if dup:
            raise ValueError("『购买前改舱』有仓位重复勾了: " + "、".join(dup))
        off = [s for s in slots if s not in REFIT_SLOTS]
        if off:
            raise ValueError("『购买前改舱』只能是「可搭乘船舱」这几格: "
                             + "、".join(REFIT_SLOTS) + "，现在是: " + "、".join(off))
        # 存成固定格序，不存前端传来的点击顺序：仓位是「第几格」不是「先改哪个」，
        # 跟着点击顺序落盘会让「重开一次方案」看起来都像改动过。
        slots = [s for s in REFIT_SLOTS if s in slots]
        cargo_type = (refit.get("cargo_type") or "").strip()
        if cargo_type and cargo_type not in purchase_plan.CARGO_TYPES:
            raise ValueError(f"舱位类别必须是那 {len(purchase_plan.CARGO_TYPES)} 种之一: {cargo_type}")
        if enabled and (not slots or not cargo_type):
            # 半成品不存：勾了「要改舱」却没说改哪几格、改成什么舱，等于让将来那一步自己猜，
            # 而它猜的方向是花金币点不可逆的按钮。
            raise ValueError("勾了『购买前改舱』要说清改哪几格、改成什么舱"
                             + ("（一格都没勾）" if not slots else "（没选类别）"))
        out["refit"] = {"enabled": enabled, "ship": ship,
                        "slots": slots, "cargo_type": cargo_type}

    cfg = raw.get("switch_config")
    if cfg is not None:
        if not isinstance(cfg, dict):
            raise ValueError("『切换配置』必须是一个对象 {before_buy, before_sail, before_sell}")
        # 旧版只有一个占位开关，没有配置名，实际从未接入队列。读到它时全部迁移为关闭，
        # 不能把一个没有目标名称的旧勾框变成会自动点击的真动作。
        if set(cfg).issubset({"enabled"}):
            _must_bool("旧版『切换配置』的开关", cfg.get("enabled", False))
        else:
            bad = [k for k in cfg if k not in SWITCH_CONFIG_POINTS]
            if bad:
                raise ValueError("『切换配置』里有不认识的时点: " + "、".join(sorted(bad)))
            normalized = {}
            labels = {
                "before_buy": "每个买货港购买前",
                "before_sail": "每次港口移动出港前",
                "before_sell": "到达出货港卖货前",
            }
            for point in SWITCH_CONFIG_POINTS:
                item = cfg.get(point) or {}
                if not isinstance(item, dict):
                    raise ValueError(f"『{labels[point]}』必须是一个对象 {{enabled, config_name}}")
                unknown = [k for k in item if k not in SWITCH_CONFIG_POINT_KEYS]
                if unknown:
                    raise ValueError(f"『{labels[point]}』里有不认识的项目: "
                                     + "、".join(sorted(unknown)))
                enabled = _must_bool(f"『{labels[point]}』的开关", item.get("enabled", False))
                name = (item.get("config_name") or "").strip()
                if len(name) > MAX_NAME_CHARS:
                    raise ValueError(f"『{labels[point]}』的游戏内配置名太长了"
                                     f"（{len(name)} 个字，最多 {MAX_NAME_CHARS} 个）")
                if enabled and not name:
                    raise ValueError(f"勾了『{labels[point]}』就必须填写游戏内自定义配置名")
                normalized[point] = {"enabled": enabled, "config_name": name}
            out["switch_config"] = normalized
    return out



def _now():
    return time.strftime("%Y-%m-%d %H:%M")


def normalize_preset(record, moves=None):
    """规整一份方案：{name, run_module, stops, options}。不认识的项目直接报错，理由同 route_plan。

    `moves` 是透给 `route_plan.normalize_route` 的那份「重排了哪几站」，
    存方案时回给界面显示用；读方案库时不传（那一份早就排好了，也不该有人被通知）。
    """
    if not isinstance(record, dict):
        raise ValueError("方案必须是一个对象 {name, run_module, stops, options}")
    unknown = [k for k in record if k not in ("name", "run_module", "stops", "options", "saved_at")]
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
            "options": normalize_options(record.get("options")),
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


def options_text(preset):
    """附加步骤那一块的人话摘要，给方案列表那一行和队列看。

    没开任何一项时给「」空串（界面上就不显示这一段），不开心的话一行字都不占。
    """
    opts = (preset or {}).get("options") or {}
    refit = opts.get("refit") or {}
    parts = []
    if refit.get("enabled"):
        slots = refit.get("slots") or []
        ct = refit.get("cargo_type") or ""
        parts.append(f"改舱：{refit.get('ship') or '（没选船）'} 的 "
                     f"{len(slots)} 格[{('、'.join(slots)) or '（没勾格）'}] → "
                     + (purchase_plan.cabin_name(ct) if ct else "（没选类别）"))
    cfg = opts.get("switch_config") or {}
    labels = {
        "before_buy": "买货前",
        "before_sail": "出港前",
        "before_sell": "卖货前",
    }
    enabled = [f"{labels[p]}→{(cfg.get(p) or {}).get('config_name') or '（没填名字）'}"
               for p in SWITCH_CONFIG_POINTS if (cfg.get(p) or {}).get("enabled")]
    if enabled:
        parts.append("切换配置：" + "、".join(enabled))
    return " ｜ ".join(parts)


def summary(preset):
    """列表上那一行的人话摘要：几站、怎么走、买几件、什么时候存的、附加步骤开了哪几样。"""
    stops = preset.get("stops") or []
    text = " → ".join(f"{route_plan.STAGE_LABELS.get(s['stage'], s['stage'])}·{s['port']}"
                      for s in stops)
    n_goods = sum(len(s.get("goods") or []) for s in stops)
    return {"name": preset["name"], "run_module": preset.get("run_module") or "",
            "saved_at": preset.get("saved_at") or "", "stop_count": len(stops),
            "route_text": text or "（空）", "goods_count": n_goods,
            "options": normalize_options(preset.get("options")),
            "options_text": options_text(preset)}


def find(items, name):
    for p in items:
        if p["name"] == name:
            return p
    return None


def to_route(preset, current_port):
    """方案 + 当前所在港 → route_plan；options 由队列直接交给引擎，不写入规划文件。"""
    return {"run_module": preset.get("run_module") or "",
            "current_port": current_port or "",
            "stops": [dict(s, goods=list(s.get("goods") or [])) for s in preset.get("stops") or []]}
