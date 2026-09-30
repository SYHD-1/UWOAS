"""跑商购物表格：每行写「在哪个港口、买哪件货、它属于哪一类」。

数据文件 purchase_plan.json 与 states.json / templates.json 同级。
一行 = 一个港口的一种货物，所以同一个港口可以填好几行：

    {
      "rows": [
        {"port": "希洪", "goods_name": "羊毛", "cargo_type": "家畜", "note": "皮货也算这栏"},
        {"port": "希洪", "goods_name": "白葡萄酒", "cargo_type": "酒", "note": ""}
      ]
    }

字段含义（全部由人工填写，脚本不会去游戏里读）：
- port        港口名称，可以重复
- goods_name  货物名称，必须是一张「商品-XX」模板去掉前缀的那几个字（见 GOODS_PREFIX），
              界面上是打几个字搜出候选再点一下，不是手打的，所以错不了
- cargo_type  类别（货物类型），取值必须是 CARGO_TYPES 之一
- note        备注，仅给人看，脚本不读

港口名既然能重复，一行的唯一键就是「港口|货物名称」（见 row_key）；启动时人工逐行选。
船舱名不存进文件，由 cargo_type 现算（cabin_name）：写两处改了一处就对不上，
界面和日志里显示的是算出来的那个。
"""

import json
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PLAN_JSON = os.path.join(BASE_DIR, "purchase_plan.json")

# 造船所「变更船舱」候选列表里的 17 种货物舱（金币档，名称一律「大型XX管理室」）。
# 这份清单是人工对照游戏抄下来的常量，运行时不去游戏里抓。
CARGO_TYPES = [
    "染料", "酒", "家畜", "药品", "杂货", "调味料", "嗜好品", "纤维制品",
    "工艺品", "食品原料", "宝石", "纺织品", "香料", "香辛料",
    "艺术作品", "物资", "贵金属",
]


def cabin_name(cargo_type):
    """类别 -> 对应的船舱名（候选列表里那一行的文字）。"""
    return f"大型{cargo_type}管理室"


# 买货靠模板匹配，约定：一件货的模板名 = 「商品-」+ 货物名称（一字不差）。
# 这条命名规则只有这一处，界面搜索候选、状态机动作、模板库都从这里取。
GOODS_PREFIX = "商品-"


def goods_template_name(goods_name):
    """货物名称 -> 该用的模板名。"""
    return GOODS_PREFIX + (goods_name or "").strip()


def goods_name_from_template(name):
    """模板名 -> 货物名称；不是「商品-XX」这种就返回 None（用前缀判断，不用正则）。"""
    n = (name or "").strip()
    if not n.startswith(GOODS_PREFIX):
        return None
    return n[len(GOODS_PREFIX):] or None


def row_key(row):
    """一行的唯一键：港口|货物名称。货物名称里不会出现竖线，直接拼就够区分。"""
    return f"{(row.get('port') or '').strip()}|{(row.get('goods_name') or '').strip()}"


def decorate(row):
    """给一行补上算出来的船舱名和行键，只用于接口返回和界面显示，不写回文件。"""
    out = dict(row)
    ct = row.get("cargo_type")
    out["cabin_name"] = cabin_name(ct) if ct else ""
    out["key"] = row_key(row)
    return out


def normalize_row(record):
    """规整一行表格：{port, goods_name, cargo_type, note}。校验不过抛 ValueError。"""
    if not isinstance(record, dict):
        raise ValueError("每一行必须是对象 {port, goods_name, cargo_type, note}")
    port = (record.get("port") or "").strip()
    if not port:
        raise ValueError("港口名称不能为空")
    goods_name = (record.get("goods_name") or "").strip()
    if not goods_name:
        raise ValueError(f"港口『{port}』这一行的货物名称不能为空")
    cargo_type = (record.get("cargo_type") or "").strip()
    if cargo_type not in CARGO_TYPES:
        raise ValueError(f"『{goods_name}』的类别必须是这 17 种之一: {cargo_type or '(空)'}")
    return {
        "port": port,
        "goods_name": goods_name,
        "cargo_type": cargo_type,
        "note": (record.get("note") or "").strip(),
    }


def load_plan():
    """读取 purchase_plan.json，不存在则返回空表格。"""
    if not os.path.exists(PLAN_JSON):
        return {"rows": []}
    with open(PLAN_JSON, "r", encoding="utf-8") as f:
        data = json.load(f)
    rows = data.get("rows")
    if not isinstance(rows, list):
        raise ValueError("purchase_plan.json 的 rows 必须是数组")
    return {"rows": rows}


def save_plan(plan):
    with open(PLAN_JSON, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)


def find_row(plan, key):
    """按行键（港口|货物名称）精确取一行。启动时人工逐行选，所以不做模糊匹配。

    没有这一行返回 None —— 多半是表格刚被人改过、名字里多了个空格。
    """
    key = (key or "").strip()
    if not key:
        return None
    for row in plan.get("rows", []):
        if row_key(row) == key:
            return row
    return None


def rows_for_port(plan, port):
    """取某个港口名下的全部行（表格里的填写顺序），供「一次跑完本港口的几行货」用。

    港口名两边空格不算差异。返回的是原 dict 的引用，不复制 —— 引擎只读不改。
    """
    p = (port or "").strip()
    if not p:
        return []
    return [r for r in plan.get("rows", []) if (r.get("port") or "").strip() == p]
