"""route_plan 新格式离线自检：normalize / derive / warnings / 落盘读回 / 真接口 / 方案（preset）。
只碰 %TEMP% 下的临时 json，不碰项目里的 route_plan.json 和 route_presets.json，不碰 ADB。"""
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 项目根 = selfcheck/ 的上一级（换目录/换机器都不用改）
sys.path.insert(0, BASE)
import route_plan as rp  # noqa: E402

FAILS = []


def check(label, cond, detail=""):
    print("  %-4s %s %s" % ("PASS" if cond else "FAIL", label, detail))
    if not cond:
        FAILS.append(label)


def rlabel(res):
    return res.get("next_reason", "")


def blabel(res):
    return res.get("buy_reason", "")


def _stop(s):
    if isinstance(s, tuple):
        # ("buy", "北京") 或 ("buy", "北京", ["油画", "啤酒"])
        d = {"stage": s[0], "port": s[1]}
        if len(s) > 2:
            d["goods"] = list(s[2])
        return d
    return dict(s)


def route(module="sail", cur="北京", stops=()):
    return rp.normalize_route({"run_module": module, "current_port": cur,
                               "stops": [_stop(s) for s in stops]})


def route_mv(module="trip", cur="北京", stops=()):
    """和 route() 一样，但把 moves 那个出参一起带回来 —— 自动重排了哪几站只能这么拿。"""
    mv = []
    r = rp.normalize_route({"run_module": module, "current_port": cur,
                            "stops": [_stop(s) for s in stops]}, mv)
    return r, mv


TRIP = [("buy", "北京"), ("buy", "汉堡"), ("transit", "希洪"), ("sell", "北京")]

print("=" * 72)
print("A) normalize：字段白名单 + 每一站校验")
r = route("sail", "北京", TRIP)
check("三段列表按顺序出来",
      [s["port"] for s in r["stops"]] == ["北京", "汉堡", "希洪", "北京"], str(r["stops"]))
check("note 缺省补空文字", r["stops"][0]["note"] == "")
check("goods 缺省补空列表（老文件没这一项也能读）", r["stops"][0]["goods"] == [])
try:
    rp.normalize_route({"run_module": "buy", "buy_port": "北京"})
    check("旧的三个单值字段要拒绝（界面已经不发这套了）", False)
except ValueError as e:
    check("旧的三个单值字段要拒绝（界面已经不发这套了）", True, str(e))
for bad, label in [
    ({"stops": [{"stage": "buy", "port": ""}]}, "站点港口名空要拒绝"),
    ({"stops": [{"stage": "卖货", "port": "北京"}]}, "未知 stage 要拒绝"),
    ({"stops": [{"stage": "buy", "port": "北京", "extra": 1}]}, "站里未知字段要拒绝"),
    ({"stops": {"stage": "buy"}}, "stops 不是列表要拒绝"),
    ({"stops": ["北京"]}, "站不是对象要拒绝"),
    ({"stops": [{"stage": "buy", "port": "  "}]}, "只有空格的港口名也要拒绝"),
]:
    try:
        rp.normalize_route(bad)
        check(label, False)
    except ValueError as e:
        check(label, True, str(e)[:50])

print("=" * 72)
print("A2) goods：本次买哪几件，只挂在买货站上")
r = route("buy", "北京", [("buy", "北京", ["油画", "啤酒"])])
check("勾的顺序原样留着（先油画后啤酒）", r["stops"][0]["goods"] == ["油画", "啤酒"],
      str(r["stops"][0]))
check("每件货名字两边的空格去掉",
      route("buy", "北京", [("buy", "北京", [" 油画 "])])["stops"][0]["goods"] == ["油画"])
for bad, label in [
    ({"stops": [{"stage": "transit", "port": "希洪", "goods": ["油画"]}]},
     "非买货站挂货物要拒绝（只有买货段能挑）"),
    ({"stops": [{"stage": "sell", "port": "北京", "goods": ["油画"]}]},
     "出货站挂货物也要拒绝"),
    ({"stops": [{"stage": "buy", "port": "北京", "goods": "油画"}]}, "goods 不是列表要拒绝"),
    ({"stops": [{"stage": "buy", "port": "北京", "goods": [123]}]}, "货物名不是文字要拒绝"),
    ({"stops": [{"stage": "buy", "port": "北京", "goods": [""]}]}, "清单里有空项要拒绝"),
    ({"stops": [{"stage": "buy", "port": "北京", "goods": ["油画", "油画"]}]},
     "同一件货勾两次要拒绝（没有数量概念，重复=买两遍）"),
]:
    try:
        rp.normalize_route(bad)
        check(label, False)
    except ValueError as e:
        check(label, True, str(e)[:60])
check("中转站 goods 留空列表是允许的（不算挂清单）",
      rp.normalize_route({"stops": [{"stage": "transit", "port": "希洪", "goods": []}]})
      ["stops"][0]["goods"] == [])

print("=" * 72)
print("A3) 出货段整段只能一站（2026-09-29 你指出「出货港口实际不可以多个」）")
OK5 = [("buy", "北京", ["中国画"]), ("buy", "汉堡", ["啤酒"]),
       ("transit", "希洪"), ("transit", "伦敦"), ("sell", "北京")]
ok5 = route("sell", "北京", OK5)
check("买货两站 + 中转两站 + 出货一站：合法（只限出货段，别顺手把另两段也管死了）",
      len(ok5["stops"]) == 5 and rp.derive(ok5)["sell_stop_idx"] == 4, str(ok5)[:60])
for stops_, label, want in [
    ([("sell", "北京"), ("sell", "上海")], "出货段排两站 → 保存时就拒绝", "只能有一站"),
    ([("sell", "北京"), ("buy", "汉堡"), ("sell", "北京")],
     "同一个港排两个出货站也要拒绝（港名相同也一样）", "只能有一站"),
    ([("sell", "北京"), ("sell", "上海"), ("sell", "伦敦")], "三站同样拒绝，而且数量要说对", "有 3 站"),
]:
    try:
        route("sell", "北京", stops_)
        check(label, False)
    except ValueError as e:
        check(label, want in str(e), str(e)[:70])
try:
    route("sell", "北京", [("sell", "北京"), ("sell", "上海")])
    check("拒绝理由点名是哪几站（第几站 + 港名都写出来）", False)
except ValueError as e:
    check("拒绝理由点名是哪几站（第几站 + 港名都写出来）",
          "第 1 站" in str(e) and "第 2 站" in str(e) and "上海" in str(e), str(e)[:70])
check("拦在数据层就够了，warnings 不再重复提醒「出货段不止一次」",
      not any("不止一次" in x and "出货" in x
              for x in rp.warnings(ok5, ["北京", "汉堡"], {})),
      str(rp.warnings(ok5, ["北京", "汉堡"], {}))[:100])
check("出货段一站都没有也合法（只买不卖的趟照存）",
      rp.derive(route("buy", "北京", [("buy", "北京", ["中国画"])]))["sell_ports"] == [])

print("=" * 72)
print("B) derive：按站次算「下一站」")
d = rp.derive(route("sail", "北京", TRIP))
check("买货段两个港、按顺序", d["buy_ports"] == ["北京", "汉堡"], str(d["buy_ports"]))
check("中转段一个", d["transit_ports"] == ["希洪"])
check("出货段一个", d["sell_ports"] == ["北京"])
check("在北京 → 下一站是汉堡（不是自己）", d["next_port"] == "汉堡", rlabel(d))
d2 = rp.derive(route("sail", "汉堡", TRIP))
check("在汉堡 → 下一站是中转港希洪", d2["next_port"] == "希洪" and d2["next_stage"] == "transit", rlabel(d2))
d3 = rp.derive(route("sail", "北京", TRIP[:3] + [("sell", "北京")]))
check("同一趟里两个北京：从第一个北京出发算到汉堡", d3["next_port"] == "汉堡", rlabel(d3))
d4 = rp.derive(route("sail", "希洪", TRIP))
check("在希洪 → 下一站是出货港北京", d4["next_port"] == "北京" and d4["next_stage"] == "sell", rlabel(d4))
d5 = rp.derive(route("sail", "北京", [("buy", "北京")]))
check("只剩当前这一站 → 没有下一站并说清", d5["next_port"] == "" and "最后一站" in rlabel(d5), rlabel(d5))
d6 = rp.derive(route("sail", "", TRIP))
check("没填当前所在港 → 算不出下一站并说清", d6["next_port"] == "" and "没填" in rlabel(d6), rlabel(d6))
d7 = rp.derive(route("sail", "开罗", TRIP))
check("当前港不在这趟里 → 说清排的是哪几站", d7["next_port"] == "" and "不在这一趟" in rlabel(d7), rlabel(d7))
d8 = rp.derive(route("sail", "北京", [("buy", "北京"), ("transit", "北京")]))
check("下一站写成同一个港 → next_port 就是它，交给引擎拒",
      d8["next_port"] == "北京" and "同港" in rlabel(d8), rlabel(d8))
d9 = rp.derive(route("sail", "北京", []))
check("一站都没排 → 说清", d9["next_port"] == "" and "一站都没排" in rlabel(d9), rlabel(d9))
# here_idx：界面上「船在这儿」那个框描在哪一站，由后端给下标（同一趟里两个北京不能各描一个）
check("北京→汉堡→…：here_idx 认第 1 站（不是末尾那个同名北京）",
      rp.derive(route("sail", "北京", TRIP))["here_idx"] == 0)
check("在汉堡 → here_idx = 1", rp.derive(route("sail", "汉堡", TRIP))["here_idx"] == 1)
check("当前港只出现在最后一站 → 认那一站，但 next 为空",
      (lambda d: d["here_idx"] == 4 and d["next_port"] == "" and "最后一站" in rlabel(d))
      (rp.derive(route("sail", "伦敦", TRIP + [("buy", "伦敦")]))))
check("当前港不在这趟里 → here_idx = -1（界面不描框，别乱描）",
      rp.derive(route("sail", "开罗", TRIP))["here_idx"] == -1)
check("没填当前港 → here_idx = -1", rp.derive(route("sail", "", TRIP))["here_idx"] == -1)
check("连着两站同名：认第一站为出发地，下一站就是同名港本身（交给引擎拒）",
      (lambda d: d["here_idx"] == 0 and d["next_port"] == "北京" and "同港" in rlabel(d))
      (rp.derive(route("sail", "北京", [("buy", "北京"), ("buy", "北京"), ("sell", "汉堡")]))))

print("=" * 72)
print("B2) derive：本次买货这一站认哪个（你 2026-09-27 拍：认当前所在港那一站）")
TRIP_G = [("buy", "北京", ["中国画", "油画"]), ("buy", "汉堡", ["啤酒"]),
          ("transit", "希洪"), ("sell", "北京")]
dd = rp.derive(route("buy", "北京", TRIP_G))
check("在北京 → 本次买货港 = 北京，清单就是那一站挂的两件",
      dd["buy_port"] == "北京" and dd["buy_goods"] == ["中国画", "油画"], blabel(dd))
check("buy_stop_idx 给下标，界面拿它描当前这一站", dd["buy_stop_idx"] == 0, str(dd["buy_stop_idx"]))
check("理由里写清挂了几件、顺序如何", "2 件" in blabel(dd) and "中国画" in blabel(dd), blabel(dd))
check("在汉堡 → 本次清单换成汉堡那一站（只剩啤酒）",
      (lambda d: d["buy_port"] == "汉堡" and d["buy_goods"] == ["啤酒"] and d["buy_stop_idx"] == 1)
      (rp.derive(route("buy", "汉堡", TRIP_G))))
# 2026-09-29 出货段整段只能一站，所以这里的「上海」改当中转排；要测的事没变：
# 同一港既买又卖时，停在北京认的是买货那一站，不是出货站。
check("同一港既买又卖（北京买货…北京出货）：停在北京认的是买货那一站，不是出货站",
      (lambda d: d["buy_stop_idx"] == 0 and d["buy_goods"] == ["中国画"]
       and d["sell_stop_idx"] == 2)
      (rp.derive(route("buy", "北京", [("buy", "北京", ["中国画"]), ("transit", "上海"),
                                       ("sell", "北京")]))))
amb = rp.derive(route("buy", "北京", [("buy", "北京", ["油画"]), ("transit", "希洪"),
                                      ("buy", "北京", ["啤酒"])]))
check("同一港两个买货站：只认第一次出现那一站（第二个不生效）",
      amb["buy_stop_idx"] == 0 and amb["buy_goods"] == ["油画"], str(amb["buy_goods"]))
only_sell = rp.derive(route("buy", "北京", [("buy", "汉堡", ["啤酒"]), ("sell", "北京", [])]))
check("当前港在这一趟里只是出货站 → 认不出本次买货港并说清",
      only_sell["buy_stop_idx"] == -1 and only_sell["buy_port"] == ""
      and "不是买货站" in blabel(only_sell), blabel(only_sell))
noport = rp.derive(route("buy", "开罗", TRIP_G))
check("当前港压根不在这趟里 → buy_port 空 + 列出这趟的买货港",
      noport["buy_port"] == "" and "汉堡" in blabel(noport), blabel(noport))
nocur = rp.derive(route("buy", "", TRIP_G))
check("没填当前所在港 → 认不出本次买货港（买货模块会因此被引擎拒绝）",
      nocur["buy_port"] == "" and "没填" in blabel(nocur), blabel(nocur))
check("一站都没排 → 也认不出本次买货港",
      rp.derive(route("buy", "北京", []))["buy_port"] == "")
check("挂了但一件都没有 → buy_goods 是空列表（引擎拒启动，界面靠 warnings 提醒）",
      rp.derive(route("buy", "北京", [("buy", "北京", [])]))["buy_goods"] == [])
check("buy_ports（整趟的买货港列表）仍然留着，给中转/移动那套用",
      rp.derive(route("buy", "北京", TRIP_G))["buy_ports"] == ["北京", "汉堡"])

print("=" * 72)
print("B3) derive：本次出货港认哪个（2026-09-28 你拍：和买货同一条，认当前所在港那一站）")
TRIP_S = [("buy", "汉堡", ["啤酒"]), ("transit", "希洪"), ("sell", "北京")]
s1 = rp.derive(route("sell", "北京", TRIP_S))
check("在『北京』（它是出货站）→ sell_port = 北京、sell_stop_idx = 第 3 站",
      s1["sell_port"] == "北京" and s1["sell_stop_idx"] == 2, str(s1["sell_stop_idx"]))
check("理由里写清这一站不挂货物清单（货舱里有什么卖什么）",
      "货舱里有什么卖什么" in s1["sell_reason"], s1["sell_reason"])
check("出货段不算货物清单：derive 的 keys 里根本没有 sell_goods（别让人以为能勾）",
      "sell_goods" not in s1, str(sorted(s1)))
s2 = rp.derive(route("sell", "汉堡", TRIP_S))
check("当前港是买货站 → 认不出本次出货港，并说清「不是出货站」",
      s2["sell_port"] == "" and s2["sell_stop_idx"] == -1 and "不是出货站" in s2["sell_reason"],
      s2["sell_reason"])
s3 = rp.derive(route("sell", "开罗", TRIP_S))
check("当前港压根不在这趟里 → sell_port 空 + 列出这趟的出货港",
      s3["sell_port"] == "" and "北京" in s3["sell_reason"], s3["sell_reason"])
s4 = rp.derive(route("sell", "", TRIP_S))
check("没填当前所在港 → 认不出本次出货港（卖货模块会因此被引擎拒绝）",
      s4["sell_port"] == "" and "没填" in s4["sell_reason"], s4["sell_reason"])
s5 = rp.derive(route("sell", "北京", []))
check("一站都没排 → 也认不出本次出货港",
      s5["sell_port"] == "" and "一站都没排" in s5["sell_reason"], s5["sell_reason"])
both = rp.derive(route("sell", "北京", [("buy", "北京", ["中国画"]), ("sell", "北京")]))
check("同一港既买又卖：两段各认各的那一站，互不抢（buy 第1站 / sell 第2站）",
      both["buy_stop_idx"] == 0 and both["sell_stop_idx"] == 1,
      f"buy={both['buy_stop_idx']} sell={both['sell_stop_idx']}")
check("同一港连着两站（买货紧接着出货）→ 下一站算出来就是它自己，交给引擎拒（出港花真金币）",
      both["next_port"] == "北京" and "同港" in both["next_reason"], both["next_reason"])
check("出货站是这趟最后一站 → 没有下一站（到港就停，不自动接下一段）",
      s1["next_port"] == "" and "最后一站" in s1["next_reason"], s1["next_reason"])

print("=" * 72)
print("C) warnings：只提醒，不拦（拦在引擎）")
CAT = {"北京": ["中国画", "油画"], "汉堡": ["啤酒"], "希洪": ["奶酪"]}
w = rp.warnings(route("buy", "", [("buy", "上海")]), ["北京", "汉堡"])
check("购物表格里没有这个买货港要提醒", any("上海" in x and "购物表格" in x for x in w), str(w))
check("没填当前所在港要提醒", any("当前所在港" in x for x in w), str(w))
w2 = rp.warnings(route("buy", "北京", [("buy", "北京", ["中国画"])]), ["北京"], CAT)
check("一切正常时不该有提醒（只剩中转/出货那句就没有）", w2 == [], str(w2))
check("只排一站时不拿「算不出下一站」烦人", not any("下一站" in x for x in w2), str(w2))
w2b = rp.warnings(route("sail", "开罗", TRIP), ["北京", "汉堡"])
check("两站以上又算不出下一站 → 要提醒", any("算不出下一站" in x for x in w2b), str(w2b))
w3 = rp.warnings(route("sail", "北京", TRIP), ["北京", "汉堡"])
check("存了中转/出货就直说「只存不跑」", any("只存不跑" in x or "还没" in x for x in w3), str(w3))
check("空站列表会提醒这一趟没排站",
      any("没有「买货」站" in x for x in rp.warnings(route("buy", "北京", []), ["北京"])))
wa = rp.warnings(route("buy", "北京", [("buy", "北京", [])]), ["北京"], CAT)
check("买货站一件货都没勾 → 提醒（引擎那边会拒启动）",
      any("没勾任何货物" in x for x in wa), str(wa))
wm = rp.warnings(route("buy", "北京", [("buy", "北京", ["中国画", "走私货"])]), ["北京"], CAT)
check("挂的货在该港目录里买不到 → 点名是哪件",
      any("走私货" in x and "中国画" not in x.split("没有")[0] for x in wm), str(wm))
check("该港目录里有的货不算问题（别把中国画也一起报出来）",
      any("走私货" in x for x in wm) and not any("没有 中国画" in x for x in wm), str(wm))
wc = rp.warnings(route("buy", "北京", TRIP_G), ["北京", "汉堡"], CAT)
check("不传目录时跳过「货买不买得到」这项检查（老调用方不炸）",
      isinstance(wc, list) and not any("买不到" in x or "查不到" in x for x in wc), str(wc))
wam = rp.warnings(route("buy", "北京", [("buy", "北京", ["油画"]), ("transit", "希洪"),
                                        ("buy", "北京", ["啤酒"])]), ["北京"], CAT)
check("同一港排了两个买货站 → 提醒第二站不生效", any("不止一次" in x for x in wam), str(wam))
wsell = rp.warnings(route("buy", "北京", [("buy", "汉堡", ["啤酒"]), ("sell", "北京")]),
                    ["北京", "汉堡"], CAT)
check("当前港只是出货站、买货港排在别处 → 提醒本次买货港认不出来",
      any("不是买货站" in x for x in wsell), str(wsell))
wsm = rp.warnings(route("sell", "汉堡", TRIP_S), ["汉堡", "北京"], CAT)
check("跑卖货但当前港对不到出货站 → 提醒本次出货港认不出来",
      any("本次出货港认不出来" in x for x in wsm), str(wsm))
wns = rp.warnings(route("sell", "北京", [("buy", "汉堡", ["啤酒"])]), ["汉堡"], CAT)
check("跑卖货但这趟一段出货站都没排 → 提醒",
      any("没有「出货」站" in x for x in wns), str(wns))
wnb = rp.warnings(route("buy", "汉堡", TRIP_S), ["汉堡"], CAT)
check("只买不卖的趟不拿「没有出货站」烦人（那三条卖货提醒只在模块=sell 时才提）",
      not any("没有「出货」站" in x or "本次出货港" in x for x in wnb), str(wnb))
check("排了出货站就提醒「链头还认账本」（sell_pending 不是 1 不开卖）",
      any("sell_pending" in x for x in wnb), str(wnb))

print("=" * 72)
print("D) 落盘再读回来（这一节是补的：上次改格式只测了函数，把 load_route 整个漏掉了，")
print("   界面一打开就是 500 —— 光测 normalize/derive 测不到「读文件」这条真路径）")
TMPJSON = os.path.join(os.environ.get("TEMP", "/tmp"), "uwo_route_selfcheck.json")
real_path = rp.ROUTE_JSON
rp.ROUTE_JSON = TMPJSON
try:
    if os.path.exists(TMPJSON):
        os.remove(TMPJSON)
    empty = rp.load_route()
    check("文件还不存在时返回全空规划（第一次用）",
          empty["run_module"] == "buy" and empty["current_port"] == "" and empty["stops"] == [],
          str(empty))
    check("全空那份的 stops 是独立列表，不是 DEFAULTS 里那一个（加一站不能污染默认值）",
          empty["stops"] is not rp.DEFAULTS["stops"] and rp.load_route()["stops"] is not empty["stops"])
    one = rp.normalize_route({"run_module": "sail", "current_port": "北京",
                             "stops": [{"stage": "buy", "port": "北京",
                                        "goods": ["中国画", "油画"]},
                                       {"stage": "sell", "port": "北京", "note": "卖艺术品"}]})
    rp.save_route(one)
    back = rp.load_route()
    check("存下去的和读回来的一模一样（含 goods）", back == one, str(back))
    check("读回来之后 goods 还在（本次清单真落盘了）",
          back["stops"][0]["goods"] == ["中国画", "油画"], str(back["stops"][0]))
    check("读回来还能接着算本次买货这一站",
          rp.derive(back)["buy_goods"] == ["中国画", "油画"], str(rp.derive(back)["buy_reason"]))
    with open(TMPJSON, encoding="utf-8") as f:
        on_disk = f.read()
    check("文件里是中文原样（不是 \\u 转义），人手打开也认得", "中国画" in on_disk)
    with open(TMPJSON, "w", encoding="utf-8") as f:
        f.write('{"run_module": "buy", "buy_port": "北京", "transit_port": ""}')
    try:
        rp.load_route()
        check("文件里还是改格式前那套旧字段 → 读的时候要报错，不能当成空的", False)
    except ValueError as e:
        check("文件里还是改格式前那套旧字段 → 读的时候要报错，不能当成空的", True, str(e)[:60])
finally:
    if os.path.exists(TMPJSON):
        os.remove(TMPJSON)
    rp.ROUTE_JSON = real_path

print("=" * 72)
print("E) 直接调 app.py 里那两个接口函数，确认后端和新格式对得上")
# 不用 TestClient：这机器上没装 httpx，为一个自检装包不划算。
# FastAPI 的 @app.get/@app.put 装饰器返回的就是原函数，直接调它 = 走同一段代码
# （_route_view / normalize / derive / warnings 全在这条线上）。
try:
    import app as appmod
    rp.ROUTE_JSON = TMPJSON
    try:
        if os.path.exists(TMPJSON):
            os.remove(TMPJSON)
        body = appmod.route_get()
        check("GET /api/route（文件还没建过）返回全空而不是崩",
              body["stops"] == [] and body["current_port"] == "", str(body)[:120])
        check("返回里有界面要用的四样：port_options / catalog / derived / warnings",
              {"port_options", "catalog", "derived", "warnings"} <= set(body), str(sorted(body)))
        check("derived 里有 next_port / buy_port / buy_goods（前端只显示，不自算）",
              {"next_port", "next_reason", "buy_ports", "buy_port", "buy_goods"}
              <= set(body["derived"]), str(body["derived"]))
        out = appmod.route_put({"run_module": "sail", "current_port": "北京",
                                "stops": [{"stage": "buy", "port": "北京",
                                           "goods": ["中国画", "油画"]},
                                          {"stage": "transit", "port": "汉堡", "note": "卸货"}]})
        d = out["derived"]
        check("PUT 新格式存完立刻算出下一站是汉堡（中转站）",
              out["stops"][1]["port"] == "汉堡" and d["next_port"] == "汉堡"
              and d["next_stage"] == "transit", str(d))
        check("PUT 回来的 derived 里就带着本次清单（buy_port + buy_goods 两件）",
              d["buy_port"] == "北京" and d["buy_goods"] == ["中国画", "油画"], str(d))
        check("GET 回来读到的就是刚存的（真的写进文件了）",
              appmod.route_get()["stops"][0]["goods"] == ["中国画", "油画"])
        check("存下去的份里不会带出界面上的东西（只有三个字段）",
              set(appmod.route_get()) >= {"run_module", "current_port", "stops"}
              and set(rp.load_route()) == {"run_module", "current_port", "stops"},
              str(sorted(rp.load_route())))
        # catalog = 表格按港口分组，跑商设置的勾选框就吃它
        cat = appmod.route_get()["catalog"]
        check("catalog 按港口分组、值是该港的目录行（表格只是目录）",
              isinstance(cat, dict) and all(isinstance(v, list) for v in cat.values()),
              str(sorted(cat))[:120])
        check("catalog 每行带着 cargo_type / cabin_name（类别只在表格里存一处）",
              all({"goods_name", "cargo_type", "cabin_name"} <= set(r)
                  for rows in cat.values() for r in rows),
              str(next(iter(cat.values()), []))[:160])
        bp = appmod._goods_by_port(cat)
        check("_goods_by_port 把目录压成 {港口: [货名]}，给 warnings 核对用",
              all(isinstance(v, list) for v in bp.values()), str(bp)[:120])
        check("catalog 是按港口分组的（每组里的行都真属于那个港，不是整张表塞给每一站）",
              all(all((r.get("port") or "").strip() == p for r in rows)
                  for p, rows in cat.items()) and all(cat.values()), str(sorted(cat))[:120])
        warns = appmod.route_get()["warnings"]
        check("挂的货不在该港目录里 → 走接口也会被点名（表格端到端接上了）",
              ("油画" in (bp.get("北京") or [])) or any("油画" in x for x in warns),
              f"目录北京={bp.get('北京')} 提醒={warns}")
        try:
            appmod.route_put({"run_module": "buy", "buy_port": "北京"})
            check("旧格式的 PUT 要拒绝（400），不能「看着存成功了其实那次改动丢了」", False)
        except appmod.HTTPException as e:
            check("旧格式的 PUT 要拒绝（400），不能「看着存成功了其实那次改动丢了」",
                  e.status_code == 400, f"{e.status_code} {str(e.detail)[:60]}")
        try:
            appmod.route_put({"run_module": "buy", "current_port": "北京",
                              "stops": [{"stage": "sell", "port": "北京",
                                         "goods": ["中国画"]}]})
            check("出货站挂货物 → PUT 也要拒绝（400）", False)
        except appmod.HTTPException as e:
            check("出货站挂货物 → PUT 也要拒绝（400）", e.status_code == 400,
                  f"{e.status_code} {str(e.detail)[:60]}")
        try:
            appmod.route_put({"run_module": "sell", "current_port": "北京",
                              "stops": [{"stage": "sell", "port": "北京"},
                                        {"stage": "sell", "port": "上海"}]})
            check("出货段两站 → PUT 也拒绝（400，界面把原因显示成红字，而不是存了份走不到的）", False)
        except appmod.HTTPException as e:
            check("出货段两站 → PUT 也拒绝（400，界面把原因显示成红字，而不是存了份走不到的）",
                  e.status_code == 400 and "只能有一站" in str(e.detail),
                  f"{e.status_code} {str(e.detail)[:50]}")
        # 启动那一步交给引擎的值：买货港 + 清单都来自 derive，移动模块仍用下一站
        check("该交给引擎的三个值都算得出（买货港 + 清单 + 下一站；队列线程也是从 derive 取这几样）",
              d["buy_port"] == "北京" and isinstance(d["buy_goods"], list)
              and d["next_port"] == "汉堡")
    finally:
        if os.path.exists(TMPJSON):
            os.remove(TMPJSON)
        rp.ROUTE_JSON = real_path
except Exception as e:
    check("接口这一节能跑起来（app 导入失败要单独看）", False,
          f"{type(e).__name__}: {e}")

print("=" * 72)
print("F) 方案（route_presets）：一整套计划起个名字存起来，一律完整一趟、不含「当前所在港」")
import route_presets as rps  # noqa: E402

TMPPRE = os.path.join(os.environ.get("TEMP", "/tmp"), "uwo_route_presets_selfcheck.json")
real_pre = rps.PRESETS_JSON
rps.PRESETS_JSON = TMPPRE
try:
    if os.path.exists(TMPPRE):
        os.remove(TMPPRE)
    check("方案文件还没建过 → 空列表（第一次用，不是报错）", rps.load_presets() == [])
    p = rps.normalize_preset({
        "name": "  北京买画 → 热那亚卖货  ", "run_module": "buy",
        "stops": [{"stage": "buy", "port": "北京", "goods": ["中国画", "油画"]},
                  {"stage": "sell", "port": "热那亚", "note": "货舱里有什么卖什么"}]})
    check("方案名两边的空格去掉", p["name"] == "北京买画 → 热那亚卖货", p["name"])
    check("方案就是五项 {name, run_module, stops, options, saved_at}",
          set(p) == {"name", "run_module", "stops", "options", "saved_at"}, str(sorted(p)))
    check("方案一律是完整一趟：前端传 buy 也被写成 trip（2026-09-30 起跑商设置不再分类型）",
          p["run_module"] == rp.TRIP_MODULE, p["run_module"])
    check("方案里根本没有「当前所在港」这一项（你 2026-09-29 选的口径）",
          "current_port" not in p, str(sorted(p)))
    check("没填 saved_at 时补一个「年-月-日 时:分」",
          len(p["saved_at"]) == 16 and p["saved_at"][4] == "-", p["saved_at"])
    check("每一站照样被规整成那四个字段（走 route_plan 同一套校验）",
          all(set(s) == set(rp.STOP_KEYS) for s in p["stops"]), str(p["stops"])[:70])
    # ---- 第二级（2026-10-01）：购买前改舱 / 切换配置 ----
    check("没传 options 就补一份全关掉的（2026-10-01 之前存的方案照样读得动，不是报错）",
          p["options"] == {"refit": {"enabled": False, "ship": "改良荒木船",
                                     "slots": [], "cargo_type": ""},
                           "switch_config": {"enabled": False}}, str(p["options"])[:90])
    pr = rps.normalize_preset({
        "name": "带改舱", "stops": [{"stage": "buy", "port": "北京", "goods": ["中国画"]}],
        "options": {"refit": {"enabled": True, "ship": "改良荒木船",
                              "slots": ["3行3列", "1行2列"], "cargo_type": "艺术作品"},
                    "switch_config": {"enabled": False}}})
    check("改舱的三项参数原样存下来（开关 / 船种 / 类别）",
          pr["options"]["refit"]["enabled"] is True
          and pr["options"]["refit"]["ship"] == "改良荒木船"
          and pr["options"]["refit"]["cargo_type"] == "艺术作品", str(pr["options"]["refit"])[:90])
    check("勾的格子存成固定格序（1行2列 在前、3行3列 在后），不是点击顺序 —— "
          "存点击顺序的话重开一次方案就像改动过",
          pr["options"]["refit"]["slots"] == ["1行2列", "3行3列"], str(pr["options"]["refit"]["slots"]))
    check("船种清单就一个：只有录过船舱页判据模板的那艘（改良荒木船，2026-09-23 实测）",
          rps.REFIT_SHIPS == ["改良荒木船"], str(rps.REFIT_SHIPS))
    check("能改的格子全是「可搭乘」栏 = 金币档；「无法搭乘」那些花蓝钻，根本不列进候选",
          rps.REFIT_SLOTS == ["1行2列", "1行3列", "3行3列"], str(rps.REFIT_SLOTS))
    check("类别就是「表格」栏那 17 种（复用 purchase_plan 一份清单，不另起名字）",
          len(rps.purchase_plan.CARGO_TYPES) == 17
          and "艺术作品" in rps.purchase_plan.CARGO_TYPES, str(len(rps.purchase_plan.CARGO_TYPES)))
    check("切换配置那一项只认一个开关（参数还没得配，多出来的项目一律拒）",
          rps.normalize_options({"switch_config": {"enabled": True}})
          == {"refit": {"enabled": False, "ship": "改良荒木船", "slots": [], "cargo_type": ""},
              "switch_config": {"enabled": True}}, str(rps.normalize_options({"switch_config": {"enabled": True}}))[:90])
    for bad, label in [
        ({"name": "", "run_module": "buy", "stops": [{"stage": "buy", "port": "北京"}]},
         "方案名是空的要拒绝（方案靠名字读）"),
        ({"name": "甲", "stops": []}, "一站都没排的空方案要拒绝（读出来等于把现在这趟抹掉）"),
        ({"name": "甲", "stops": [{"stage": "buy", "port": "北京"}], "current_port": "北京"},
         "方案里塞 current_port 要拒绝（界面不发、存了会骗人）"),
        ({"name": "甲", "stops": [{"stage": "buy", "port": "北京"},
                                  {"stage": "sell", "port": "上海"},
                                  {"stage": "sell", "port": "伦敦"}]},
         "方案里排两个出货站也要拒绝（和趟保存同一条规矩）"),
        ({"name": "甲", "stops": [{"stage": "sell", "port": "北京", "goods": ["油画"]}]},
         "方案里出货站挂货物也要拒绝"),
        ({"name": "问" * 41, "stops": [{"stage": "buy", "port": "北京"}]}, "方案名太长要拒绝"),
        ({"name": 123, "stops": [{"stage": "buy", "port": "北京"}]}, "方案名不是文字要拒绝"),
        ({"name": "甲", "stops": [{"stage": "buy", "port": ""}]}, "站里港名空要拒绝"),
        # 第二级那两块的拒绝分支：这一步点下去花真金币、改完退不回去，半成品不许存进方案库
        ({"name": "甲", "stops": [{"stage": "buy", "port": "北京"}],
          "options": {"refit": {"enabled": True, "ship": "改良荒木船",
                                "slots": [], "cargo_type": "艺术作品"}}},
         "勾了『购买前改舱』却一格都没勾要拒绝"),
        ({"name": "甲", "stops": [{"stage": "buy", "port": "北京"}],
          "options": {"refit": {"enabled": True, "ship": "改良荒木船",
                                "slots": ["1行2列"], "cargo_type": ""}}},
         "勾了『购买前改舱』却没选改成什么舱要拒绝"),
        ({"name": "甲", "stops": [{"stage": "buy", "port": "北京"}],
          "options": {"refit": {"enabled": True, "ship": "皇家商船",
                                "slots": ["1行2列"], "cargo_type": "艺术作品"}}},
         "船种没录过船舱页判据要拒绝（存进去那一步会去点一个它不认识的画面）"),
        ({"name": "甲", "stops": [{"stage": "buy", "port": "北京"}],
          "options": {"refit": {"enabled": True, "ship": "改良荒木船",
                                "slots": ["无法搭乘-1行1列"], "cargo_type": "宝石"}}},
         "「无法搭乘」那一栏的格子要拒绝（那些花蓝钻，2026-09-23 你明确要求不碰）"),
        ({"name": "甲", "stops": [{"stage": "buy", "port": "北京"}],
          "options": {"refit": {"enabled": True, "ship": "改良荒木船",
                                "slots": ["1行2列", "1行2列"], "cargo_type": "宝石"}}},
         "同一格勾两遍要拒绝"),
        ({"name": "甲", "stops": [{"stage": "buy", "port": "北京"}],
          "options": {"refit": {"enabled": True, "ship": "改良荒木船",
                                "slots": ["1行2列"], "cargo_type": "货物-宝石"}}},
         "类别不在那 17 种里要拒绝"),
        ({"name": "甲", "stops": [{"stage": "buy", "port": "北京"}],
          "options": {"refit": {"enabled": True, "ship": "改良荒木船", "slots": ["1行2列"],
                                "cargo_type": "宝石", "cabin_name": "大型宝石管理室"}}},
         "附加步骤里多出一个不认识的项目要拒绝（船舱名是算出来的，存两份迟早对不上）"),
        ({"name": "甲", "stops": [{"stage": "buy", "port": "北京"}],
          "options": {"switch_config": {"enabled": True, "config_name": "买货量+15%"}}},
         "『切换配置』除了开关还塞参数要拒绝（这一项是占位符，没有能校验的参数）"),
        ({"name": "甲", "stops": [{"stage": "buy", "port": "北京"}],
          "options": {"refitx": {"enabled": True}}},
         "附加步骤里出现不认识的名字要拒绝"),
        ({"name": "甲", "stops": [{"stage": "buy", "port": "北京"}], "options": []},
         "options 不是对象要拒绝"),
        ({"name": "甲", "stops": [{"stage": "buy", "port": "北京"}],
          "options": {"refit": {"enabled": "yes", "ship": "改良荒木船",
                                "slots": ["1行2列"], "cargo_type": "宝石"}}},
         "开关写成文字要拒绝（要/不要就是勾框，别的都不算）"),
    ]:
        try:
            rps.normalize_preset(bad)
            check(label, False)
        except ValueError as e:
            check(label, True, str(e)[:56])
    rps.save_presets([p])
    back = rps.load_presets()
    check("落盘再读回来一模一样（走的是真文件这条路径）", back == [p], str(back)[:90])
    with open(TMPPRE, encoding="utf-8") as f:
        disk = f.read()
    check("文件里是中文原样 + 外面套一层 {presets: [...]}，人手也改得动",
          "中国画" in disk and '"presets"' in disk, disk[:60])
    with open(TMPPRE, "w", encoding="utf-8") as f:
        json.dump([p], f, ensure_ascii=False)      # 模拟人手把文件改成裸数组
    check("手改成裸数组也认（这文件就是人手会去动的那种）", len(rps.load_presets()) == 1)
    with open(TMPPRE, "w", encoding="utf-8") as f:
        json.dump([p, p], f, ensure_ascii=False)
    try:
        rps.load_presets()
        check("文件里出现两个同名方案 → 读的时候报错，不知道读哪一份", False)
    except ValueError as e:
        check("文件里出现两个同名方案 → 读的时候报错，不知道读哪一份", "重复" in str(e), str(e)[:60])
    with open(TMPPRE, "w", encoding="utf-8") as f:
        f.write('{"buy_port": "北京"}')
    try:
        rps.load_presets()
        check("文件内容不认 → 抛错而不是给你一份空列表（空列表看着像「我没存过」）", False)
    except ValueError as e:
        check("文件内容不认 → 抛错而不是给你一份空列表（空列表看着像「我没存过」）",
              True, str(e)[:60])
    rps.save_presets([p, pr])
    back2 = rps.load_presets()
    check("带改舱参数的方案落盘再读回来一模一样（勾了哪几格、改成什么舱都不丢）",
          back2[1]["options"] == pr["options"], str(back2[1]["options"])[:90])
    with open(TMPPRE, "w", encoding="utf-8") as f:
        json.dump({"presets": [{"name": "老方案", "run_module": "trip",
                                "saved_at": "2026-09-29 15:20",
                                "stops": [{"stage": "buy", "port": "北京",
                                           "goods": ["油画"], "note": ""}]}]}, f, ensure_ascii=False)
    old = rps.load_presets()[0]
    check("盘上那一份根本没有 options（第二级是 2026-10-01 才加的）→ 读回来补全关掉的，不报错",
          old["options"]["refit"]["enabled"] is False
          and old["options"]["switch_config"]["enabled"] is False, str(old["options"])[:90])
    with open(TMPPRE, "w", encoding="utf-8") as f:
        json.dump({"presets": [dict(p, options={"refit": {"enabled": True, "ship": "皇家商船",
                                                          "slots": ["1行2列"],
                                                          "cargo_type": "宝石"}})]}, f, ensure_ascii=False)
    try:
        rps.load_presets()
        check("人手把盘上的船种改成没录过判据的 → 读的时候就报，不等点启动才炸", False)
    except ValueError as e:
        check("人手把盘上的船种改成没录过判据的 → 读的时候就报，不等点启动才炸",
              "判据" in str(e), str(e)[:70])
    rps.save_presets([p])
    sm = rps.summary(p)
    check("摘要一行讲清：几站、怎么走、几件货",
          sm["stop_count"] == 2 and sm["route_text"] == "买货·北京 → 出货·热那亚"
          and sm["goods_count"] == 2, str(sm))
    sm2 = rps.summary(pr)
    check("摘要带着附加步骤：不用点进二级，方案列表那一行就能看出这趟要先去造船所",
          sm2["options"]["refit"]["enabled"] is True
          and sm2["options_text"] == "改舱：改良荒木船 的 2 格[1行2列、3行3列] → 大型艺术作品管理室",
          sm2["options_text"])
    check("两项都没开时 options_text 是空串（列表那一行不多占一行）",
          sm["options_text"] == "", repr(sm["options_text"]))
    cfg_on = rps.normalize_preset({
        "name": "带换配置", "stops": [{"stage": "buy", "port": "北京"}],
        "options": {"switch_config": {"enabled": True}}})
    check("『切换配置』勾上了也只念成「还没实现」（界面上不许看着像已经配好了）",
          "还没实现" in rps.summary(cfg_on)["options_text"], rps.summary(cfg_on)["options_text"])
    check("find 按名字拿到整份（含站次），名字对不上给 None",
          rps.find([p], p["name"]) is p and rps.find([p], "没有这个名字") is None)
    t = rps.to_route(p, "热那亚")
    check("to_route：方案 + 界面上现在的当前港 → 一份能直接写进 route_plan.json 的规划",
          set(t) == set(rp.DEFAULTS) and t["current_port"] == "热那亚"
          and t["run_module"] == rp.TRIP_MODULE, str(sorted(t)))
    check("to_route 不把 options 交给规划：引擎还没有「改舱 / 换配置」这两步，"
          "写进一个没人读的文件比不写更坏（界面上那两块都写清了只是记在方案里）",
          "options" not in rps.to_route(pr, "北京"), str(sorted(rps.to_route(pr, "北京"))))
    check("to_route 的产物过得了规划那一套校验（不会「存得进方案、读出来保存不了」）",
          rp.normalize_route(t)["stops"][1]["port"] == "热那亚")
    t["stops"][0]["goods"].append("走私货")
    t["stops"][0]["note"] = "改一下"
    check("读出来的那份随便改，不会把方案本体改掉（列表各自一份）",
          p["stops"][0]["goods"] == ["中国画", "油画"] and p["stops"][0]["note"] == "")
    check("方案不含当前港：to_route 里这个港只能由调用方给，没给就是空",
          rps.to_route(p, "")["current_port"] == "")
finally:
    if os.path.exists(TMPPRE):
        os.remove(TMPPRE)
    rps.PRESETS_JSON = real_pre

print("=" * 72)
print("G) 直接调四个方案接口：存 / 列 / 读（立刻生效）/ 删 —— 走真落盘")
# 上一轮教训：只测纯函数会漏掉整条接口线。这四个函数是界面唯一会碰到的入口。
try:
    import app as appmod  # noqa: F401
    rp.ROUTE_JSON = TMPJSON
    rps.PRESETS_JSON = TMPPRE
    try:
        for tmp in (TMPJSON, TMPPRE):
            if os.path.exists(tmp):
                os.remove(tmp)
        appmod.route_put({"run_module": "sail", "current_port": "热那亚",
                          "stops": [{"stage": "buy", "port": "伦敦", "goods": []}]})
        check("方案文件还没有 → 列表给空的而不是 500",
              appmod.route_preset_list() == {"presets": []}, str(appmod.route_preset_list())[:80])
        A = {"name": "方案甲", "run_module": "buy",
             "stops": [{"stage": "buy", "port": "北京", "goods": ["中国画"]},
                       {"stage": "sell", "port": "热那亚", "note": ""}]}
        sa = appmod.route_preset_save(A)
        check("存方案 → ok，第一次 replaced=False", sa["ok"] and not sa["replaced"], str(sa)[:90])
        check("存的这一刻没动「现在这一趟」（方案库和规划是两个文件，各管各的）",
              [s["port"] for s in rp.load_route()["stops"]] == ["伦敦"]
              and rp.load_route()["current_port"] == "热那亚", str(rp.load_route())[:90])
        appmod.route_preset_save({"name": "方案乙", "run_module": "sell",
                                  "stops": [{"stage": "sell", "port": "汉堡"}]})
        sb = appmod.route_preset_save(dict(A, run_module="sell"))
        check("同名就是覆盖，不另外起一份近似名（replaced=True，列表还是两条）",
              sb["replaced"] and len(appmod.route_preset_list()["presets"]) == 2,
              str(appmod.route_preset_list())[:90])
        check("覆盖后还在原来的位置（甲没跳到乙后面，顺序不会因为改一次就变）",
              [x["name"] for x in appmod.route_preset_list()["presets"]] == ["方案甲", "方案乙"],
              str([x["name"] for x in appmod.route_preset_list()["presets"]]))
        check("列表里带摘要也带完整站次（界面一次请求就能渲染，不用再打一次）",
              {"route_text", "stop_count", "goods_count", "stops"}
              <= set(appmod.route_preset_list()["presets"][0]),
              str(sorted(appmod.route_preset_list()["presets"][0])))
        # 第二级那两个勾框走的是同一条存盘线：界面上勾了，盘上就得真存着，列表那一行得念得出来
        appmod.route_preset_save(dict(A, options={
            "refit": {"enabled": True, "ship": "改良荒木船",
                      "slots": ["1行2列"], "cargo_type": "艺术作品"},
            "switch_config": {"enabled": True}}))
        row = [x for x in appmod.route_preset_list()["presets"] if x["name"] == "方案甲"][0]
        check("列表里现在也带 options / options_text 两项（界面那一行不用点进二级就知道要不要改舱）",
              {"options", "options_text"} <= set(row), str(sorted(row)))
        check("接口存 options：界面勾的格子、选的类别、那个配置勾都真落盘了",
              row["options"]["refit"]["slots"] == ["1行2列"]
              and row["options"]["refit"]["cargo_type"] == "艺术作品"
              and row["options"]["switch_config"]["enabled"] is True, str(row["options"])[:90])
        check("列表那一行念得出「改舱：改良荒木船 的 1 格[…] → 大型艺术作品管理室」，"
              "配置那一项也当场说清「还没实现」",
              "改舱：改良荒木船 的 1 格[1行2列] → 大型艺术作品管理室" in row["options_text"]
              and "还没实现" in row["options_text"], row["options_text"])
        r = appmod.route_preset_load({"name": "方案甲"})
        check("读取方案 → 立刻写进 route_plan.json 生效（你拍的「立即生效」，少一步）",
              rp.load_route()["run_module"] == rp.TRIP_MODULE
              and [s["port"] for s in rp.load_route()["stops"]] == ["北京", "热那亚"],
              str(rp.load_route())[:90])
        check("读取时「当前所在港」保持盘上原来那个（方案里没存它）",
              r["current_port"] == "热那亚" and rp.load_route()["current_port"] == "热那亚",
              r["current_port"])
        check("返回里带 loaded_from + 算好的 derived（界面直接显示本次出货港）",
              r["loaded_from"] == "方案甲" and r["derived"]["sell_port"] == "热那亚"
              and r["derived"]["buy_stop_idx"] == -1,
              str(r["derived"]["sell_reason"])[:60])
        check("读一个带着改舱的方案 → 写进规划的还是那三项 {run_module, current_port, stops}："
              "route_plan 那一层不认识 options，多塞一个字段整条链会在启动前就炸",
              set(rp.load_route()) == set(rp.DEFAULTS), str(sorted(rp.load_route())))
        for bad, label, code in [
            ({}, "没说要读哪个方案 → 400", 400),
            ({"name": "  "}, "方案名只有一串空格 → 400", 400),
            ({"name": "没存过这个名字"}, "读一个不存在的方案 → 400，不是崩", 400),
        ]:
            try:
                appmod.route_preset_load(bad)
                check(label, False)
            except appmod.HTTPException as e:
                check(label, e.status_code == code, f"{e.status_code} {str(e.detail)[:40]}")
        try:
            appmod.route_preset_save({"name": "方案丙", "run_module": "sell",
                                      "stops": [{"stage": "sell", "port": "北京"},
                                                {"stage": "sell", "port": "上海"}]})
            check("方案里排两个出货站 → 接口也拒绝（400）", False)
        except appmod.HTTPException as e:
            check("方案里排两个出货站 → 接口也拒绝（400）",
                  e.status_code == 400 and "只能有一站" in str(e.detail),
                  f"{e.status_code} {str(e.detail)[:50]}")
        check("上面那次被拒的存盘没留下半个方案（拒绝发生在写文件之前）",
              [x["name"] for x in appmod.route_preset_list()["presets"]] == ["方案甲", "方案乙"],
              str([x["name"] for x in appmod.route_preset_list()["presets"]]))
        for badopt, label in [
            ({"refit": {"enabled": True, "ship": "法兰西大商船",
                        "slots": ["1行2列"], "cargo_type": "宝石"}},
             "存一个没录过船舱页判据的船种 → 400，并把能选的那一种念出来"),
            ({"refit": {"enabled": True, "ship": "改良荒木船",
                        "slots": ["无法搭乘-2行2列"], "cargo_type": "宝石"}},
             "存一格「无法搭乘」（要花蓝钻）的仓位 → 400"),
            ({"refit": {"enabled": True, "ship": "改良荒木船", "slots": [], "cargo_type": ""}},
             "勾了改舱却没填格子也没选类别 → 400（不许留半成品）"),
        ]:
            try:
                appmod.route_preset_save({"name": "方案戊", "stops": [{"stage": "buy", "port": "北京"}],
                                          "options": badopt})
                check(label, False)
            except appmod.HTTPException as e:
                check(label, e.status_code == 400, f"{e.status_code} {str(e.detail)[:56]}")
        check("被拒的这几次也没在方案库里留下「方案戊」",
              [x["name"] for x in appmod.route_preset_list()["presets"]] == ["方案甲", "方案乙"],
              str([x["name"] for x in appmod.route_preset_list()["presets"]]))
        dl = appmod.route_preset_delete("方案乙")
        check("删除 → 报回删了谁、还剩几个", dl["deleted"] == "方案乙" and dl["left"] == 1, str(dl))
        check("删除只动方案库，现在这一趟照旧（别把在跑的计划一起删了）",
              [s["port"] for s in rp.load_route()["stops"]] == ["北京", "热那亚"],
              str(rp.load_route())[:90])
        try:
            appmod.route_preset_delete("方案乙")
            check("删已经没有了的方案 → 404 且说清「没删任何东西」", False)
        except appmod.HTTPException as e:
            check("删已经没有了的方案 → 404 且说清「没删任何东西」",
                  e.status_code == 404 and "没删任何东西" in str(e.detail),
                  f"{e.status_code} {str(e.detail)[:40]}")
    finally:
        for tmp in (TMPJSON, TMPPRE):
            if os.path.exists(tmp):
                os.remove(tmp)
        rp.ROUTE_JSON = real_path
        rps.PRESETS_JSON = real_pre
except Exception as e:
    check("方案接口这一节能跑起来（app 导入失败要单独看）", False, f"{type(e).__name__}: {e}")

print("=" * 72)
print("H) 完整一趟（trip）：保存时的站次顺序 + 整趟走法 + OCR 读出来的港名认第几站")


def expect_ve(label, fn, contains=""):
    """整趟那几条约束都必须是 ValueError（接口层才有 400 可译），并且原因里点名是哪一站。"""
    try:
        fn()
        check(label, False)
    except ValueError as e:
        check(label, contains in str(e), str(e)[:60])


TRIP_OK = [("buy", "北京", ["中国画"]), ("buy", "汉堡", ["啤酒"]),
           ("transit", "希洪"), ("transit", "伦敦"), ("sell", "热那亚")]
TWICE = [("buy", "北京", ["中国画"]), ("transit", "希洪"), ("sell", "北京")]

check("trip：进货多站 → 中转多站 → 出货一站，这五站原样存进去",
      [(s["stage"], s["port"]) for s in route(module="trip", cur="北京", stops=TRIP_OK)["stops"]]
      == [("buy", "北京"), ("buy", "汉堡"), ("transit", "希洪"), ("transit", "伦敦"), ("sell", "热那亚")])
check("trip：只排买货和出货（没有中转）也合法 —— 中转就是你说的「可选」",
      len(route(module="trip", cur="北京",
                 stops=[("buy", "北京", ["中国画"]), ("sell", "热那亚")])["stops"]) == 2)
check("trip：只买不卖也存得进去（少排一段不当错，只在提醒里说）",
      len(route(module="trip", cur="北京",
                 stops=[("buy", "北京", ["中国画"]), ("transit", "希洪")])["stops"]) == 2)
expect_ve("trip：一站都没排 → 拒绝（整趟没地方走起）",
          lambda: route(module="trip", cur="北京", stops=[]), "一站都没排")

# ---- 2026-09-29 你拍的 A：顺序排错了**不再拒绝保存**，后端按 买货 → 中转 → 卖货 自动重排。
#      下面这几条原来全是在验「拒绝 + 400」，现在反过来验「存得进去 + 排成什么样」。
r_sell_mid, mv_a = route_mv(cur="希洪", stops=[("transit", "希洪"), ("sell", "北京"),
                                               ("buy", "北京", ["中国画"])])
check("卖货站夹在中间 → 不拒绝：保存时挪到最后一站",
      [(s["stage"], s["port"]) for s in r_sell_mid["stops"]]
      == [("buy", "北京"), ("transit", "希洪"), ("sell", "北京")],
      str([(s["stage"], s["port"]) for s in r_sell_mid["stops"]]))
check("   动了哪几站如实报（第 2 站出货 → 第 3 站），不是悄悄挪",
      mv_a == ["第 1 站中转『希洪』 → 第 2 站", "第 2 站出货『北京』 → 第 3 站",
               "第 3 站买货『北京』 → 第 1 站"], str(mv_a))
r_buy_split, mv_b = route_mv(cur="北京", stops=[("buy", "北京", ["中国画"]), ("transit", "希洪"),
                                                ("buy", "汉堡", ["啤酒"]), ("sell", "热那亚")])
check("两个买货站中间夹一站 → 不拒绝：买货站挨着排到最前、中转挪到它们后面",
      [(s["stage"], s["port"]) for s in r_buy_split["stops"]]
      == [("buy", "北京"), ("buy", "汉堡"), ("transit", "希洪"), ("sell", "热那亚")],
      str([(s["stage"], s["port"]) for s in r_buy_split["stops"]]))
check("   同类内部保持**你排的先后**（买货顺序就是勾选顺序，不会按港名重排）",
      [s["port"] for s in r_buy_split["stops"] if s["stage"] == "buy"] == ["北京", "汉堡"],
      str(mv_b))
check("   反过来也成立：你先排的汉堡不会被「北京」这个名字提前",
      [s["port"] for s in route_mv(cur="汉堡", stops=[("buy", "汉堡", ["啤酒"]),
                                                      ("buy", "北京", ["中国画"]),
                                                      ("sell", "热那亚")])[0]["stops"]]
      == ["汉堡", "北京", "热那亚"])
r_after_sell, mv_c = route_mv(cur="北京", stops=[("buy", "北京", ["中国画"]), ("sell", "热那亚"),
                                                 ("transit", "希洪")])
check("出货站后面还挂着中转 → 排完卖货在最后一站、中转在它前面",
      [s["stage"] for s in r_after_sell["stops"]] == ["buy", "transit", "sell"],
      str([s["stage"] for s in r_after_sell["stops"]]))
check("   重排只挪位置：一站不多、一站不少，挂的货跟着自己那一站走",
      len(r_after_sell["stops"]) == 3
      and [s["goods"] for s in r_after_sell["stops"]] == [["中国画"], [], []])
expect_ve("trip：排了两个出货站仍然先撞「只能一站」那道闸（自动重排不饶它）",
          lambda: route(module="trip", cur="北京",
                        stops=[("buy", "北京", ["中国画"]), ("sell", "热那亚"),
                               ("sell", "北京")]),
          "只能有一站")
mv_idem = []
r_again = rp.normalize_route(dict(r_buy_split, run_module="trip"), mv_idem)
check("排好的那份再存一次：不再动任何一站（重排幂等，读盘↔存盘来回跑不会漂）",
      r_again["stops"] == r_buy_split["stops"] and mv_idem == [], str(mv_idem))
mv_ok = []
route_mv(cur="北京", stops=TRIP_OK)
check("本来就按 买货 → 中转 → 卖货 排的：moves 是空的（不虚报「我排过了」）",
      mv_ok == [], str(mv_ok))
check("同样这份乱序，单模块（buy）照旧存得进去、顺序也**原样不动**"
      " —— 重排只管整趟，移动模块的下一站就是数组顺序，替它排等于偷改目的地",
      [(s["stage"], s["port"]) for s in route(module="buy", cur="北京",
                                              stops=[("transit", "希洪"),
                                                     ("buy", "北京", ["中国画"])])["stops"]]
      == [("transit", "希洪"), ("buy", "北京")])
r_sail, mv_single = route_mv(module="sail", stops=[("sell", "热那亚"),
                                                   ("buy", "北京", ["中国画"]),
                                                   ("transit", "希洪")])
check("   单模块（sail）即使带着出货站在最前也不排、moves 也是空的"
      "（排了就等于偷改它的下一站）",
      mv_single == [] and [s["stage"] for s in r_sail["stops"]] == ["sell", "buy", "transit"],
      str([s["stage"] for s in r_sail["stops"]]))
r_dup, mv_dup = route_mv(cur="北京", stops=[("buy", "北京", ["中国画"]),
                                            ("buy", "北京", ["中国画"]),
                                            ("sell", "热那亚")])
check("内容一模一样的两站（同港同货排两次）已经有序 → 一站都不报「挪过」"
      "（按内容对上时一站一个坑，不能让两站都盯着最后一个位置）",
      mv_dup == [], str(mv_dup))
r_dup2, mv_dup2 = route_mv(cur="北京", stops=[("sell", "热那亚"), ("buy", "北京", ["中国画"]),
                                              ("buy", "北京", ["中国画"])])
check("   真动了才报：两站相同的买货站各自对上自己的新位置，出货站报第 1 → 第 3 站",
      mv_dup2 == ["第 1 站出货『热那亚』 → 第 3 站", "第 2 站买货『北京』 → 第 1 站",
                  "第 3 站买货『北京』 → 第 2 站"], str(mv_dup2))

d_trip = rp.derive(route(module="trip", cur="北京", stops=TRIP_OK))
check("trip_stops = 从船这一站起、按顺序还要走的每一站（含本站，带着站号）",
      [(s["idx"], s["stage"], s["port"]) for s in d_trip["trip_stops"]]
      == [(0, "buy", "北京"), (1, "buy", "汉堡"), (2, "transit", "希洪"),
          (3, "transit", "伦敦"), (4, "sell", "热那亚")], str(d_trip["trip_stops"])[:80])
check("trip_start_idx 认的是本站（引擎从这里起步，不是从第 1 站硬走）",
      d_trip["trip_start_idx"] == 0, str(d_trip["trip_start_idx"]))
check("trip_stops 里带着该站挂的货（引擎到这一站直接拿它下单，不再回界面查）",
      d_trip["trip_stops"][1]["goods"] == ["啤酒"], str(d_trip["trip_stops"][1]))
check("每一站都是 {idx, stage, port, goods, note} 五项，不多不少",
      all(set(s) == {"idx", "stage", "port", "goods", "note"} for s in d_trip["trip_stops"]),
      str(sorted(d_trip["trip_stops"][0])))
d_mid = rp.derive(route(module="trip", cur="希洪", stops=TRIP_OK))
check("船在中转港：整趟从第 3 站起步，前面两站本次不再走（你拍的「从那站继续」）",
      d_mid["trip_start_idx"] == 2 and len(d_mid["trip_stops"]) == 3,
      f"{d_mid['trip_start_idx']} / {len(d_mid['trip_stops'])}")
d_last = rp.derive(route(module="trip", cur="热那亚", stops=TRIP_OK))
check("船已经在出货港：整趟就剩最后那一站",
      d_last["trip_start_idx"] == 4 and d_last["trip_stops"][0]["stage"] == "sell",
      str(d_last["trip_stops"])[:60])
d_away = rp.derive(route(module="trip", cur="上海", stops=TRIP_OK))
check("船在这趟之外的港：认不出起点（-1），原因里要把这趟排了哪些港列出来",
      d_away["trip_start_idx"] == -1 and not d_away["trip_stops"]
      and "上海" in d_away["trip_reason"] and "北京" in d_away["trip_reason"],
      d_away["trip_reason"][:70])
check("trip_reason 用人话把整趟念一遍（界面就显示这一行，不自己再算）",
      "从『北京』这一站起" in d_trip["trip_reason"]
      and "第 5 站 出货『热那亚』" in d_trip["trip_reason"], d_trip["trip_reason"][:80])
d_noport = rp.derive(route(module="trip", cur="", stops=TRIP_OK))
check("没填当前所在港：trip 的原因要说清「点启动时引擎自己 OCR 读一次」",
      "自己 OCR 读" in d_noport["trip_reason"], d_noport["trip_reason"][:70])
check("一站都没排时 trip_reason 也是人话（界面不会显示成空白；这种规划存不进去，上面那条已验）",
      "一站都没排" in rp.derive({"run_module": "trip", "current_port": "北京", "stops": []})["trip_reason"])
d_tw = rp.derive(route(module="trip", cur="北京", stops=TWICE))
check("同一个港在这趟出现两次（北京买 → … → 北京卖）：认第一次，也就是买货那一站",
      d_tw["trip_start_idx"] == 0 and d_tw["trip_stops"][0]["stage"] == "buy",
      str(d_tw["trip_stops"])[:60])
check("   原因里必须写明「还出现在第 3 站出货」，不能让人以为这趟已经卖过了",
      "还出现在" in d_tw["trip_reason"] and "第 3 站出货" in d_tw["trip_reason"],
      d_tw["trip_reason"][-60:])

fs_ok = route(module="trip", cur="北京", stops=TRIP_OK)["stops"]
fs_tw = route(module="trip", cur="北京", stops=TWICE)["stops"]
check("OCR 读到「北京」→ 认第 1 站", rp.find_stop_by_name(fs_ok, "北京") == (0, [0]),
      str(rp.find_stop_by_name(fs_ok, "北京")))
check("这块 roi 会读到两行：拼串里含港名照样认得出（只按包含、不上正则）",
      rp.find_stop_by_name(fs_ok, "汉堡\n啤酒原料")[0] == 1,
      str(rp.find_stop_by_name(fs_ok, "汉堡\n啤酒原料")))
check("读成别字（汊堡）→ 认不出返回 -1，让调用方拒绝启动，不猜是哪个港",
      rp.find_stop_by_name(fs_ok, "汊堡")[0] == -1)
check("读到空串 / None → 同样认不出（不许把空读数当成某个港）",
      rp.find_stop_by_name(fs_ok, "")[0] == -1 and rp.find_stop_by_name(fs_ok, None)[0] == -1)
check("同名两站：返回第一个 + 所有命中（第 3 站也在里面，界面要能提示这一趟还有第二次）",
      rp.find_stop_by_name(fs_tw, "北京") == (0, [0, 2]), str(rp.find_stop_by_name(fs_tw, "北京")))
check("港名互为子串时认长的那个（读到「东伦敦」不能算成「伦敦」，会把船叫去错的港）",
      rp.find_stop_by_name(route(module="trip", cur="伦敦", stops=[("transit", "东伦敦"),
                                                                  ("transit", "伦敦")])["stops"],
                          "东伦敦")[0] == 0)

w_trip = rp.warnings(route(module="trip", cur="北京", stops=TRIP_OK),
                     ["北京", "汉堡", "热那亚"],
                     {"北京": ["中国画"], "汉堡": ["啤酒"], "热那亚": []})
check("trip + 排了中转：提醒「只进一次港、再出一次港补水粮」，并且说清每次出航花真金币",
      any("水粮" in x and "金币" in x for x in w_trip), str([x for x in w_trip if "水粮" in x])[:70])
check("trip + 最后一站不是出货：提醒这一趟跑到头货还在舱里",
      any("货还在舱里没卖" in x for x in rp.warnings(
          route(module="trip", cur="北京",
                stops=[("buy", "北京", ["中国画"]), ("transit", "希洪")]),
          ["北京", "希洪"], {"北京": ["中国画"], "希洪": []})))
check("trip + 从中间某站起步：必须提醒「这一站买过的货会再买一遍」（花真金币的坑，不能藏）",
      any("再买一遍" in x for x in rp.warnings(
          route(module="trip", cur="汉堡", stops=TRIP_OK),
          ["北京", "汉堡", "热那亚"],
          {"北京": ["中国画"], "汉堡": ["啤酒"], "热那亚": []})))
check("trip + 没填当前所在港：说的是「引擎点启动时自己 OCR 读一次」，不是老那句「不填会被拒绝」",
      any("自己 OCR 读" in x and "不填这两个模块" not in x for x in rp.warnings(
          route(module="trip", cur="", stops=TRIP_OK),
          ["北京", "汉堡", "热那亚"], {"北京": ["中国画"], "汉堡": ["啤酒"], "热那亚": []})))
check("trip + 排了出货站：提醒卖货那一段照样认账本（sell_pending 不是 1 不开卖）",
      any("sell_pending" in x for x in w_trip))
check("单模块（buy）那份同样的站次，还是老那句「单模块里还没有中转」—— 整趟的话不许串台",
      any("单模块里还没有「中转」" in x for x in rp.warnings(
          route(module="buy", cur="北京", stops=TRIP_OK),
          ["北京", "汉堡", "热那亚"], {"北京": ["中国画"], "汉堡": ["啤酒"], "热那亚": []})))

# 真接口这一条也要走一遍：约束只在 route_plan 里写一份，接口、方案库、界面都吃它
rp.ROUTE_JSON = TMPJSON
rps.PRESETS_JSON = TMPPRE
try:
    for tmp in (TMPJSON, TMPPRE):
        if os.path.exists(tmp):
            os.remove(tmp)
    import app as appmod  # noqa: E402
    ok_rec = {"run_module": "trip", "current_port": "北京",
              "stops": [{"stage": "buy", "port": "北京", "goods": ["中国画"]},
                        {"stage": "transit", "port": "希洪"},
                        {"stage": "sell", "port": "热那亚"}]}
    view = appmod.route_put(ok_rec)
    check("PUT 一份合法的整趟 → 200，盘上存的就是它",
          view["run_module"] == "trip"
          and [s["stage"] for s in json.load(open(TMPJSON, encoding="utf-8"))["stops"]]
          == ["buy", "transit", "sell"])
    check("   返回里带着整趟走法（界面直接用，不自己算）",
          len(view["derived"]["trip_stops"]) == 3 and view["derived"]["trip_start_idx"] == 0,
          str(view["derived"]["trip_reason"])[:60])
    check("   本来就有序的那份不回「重排过」（界面不会念一句没发生过的事）",
          view["reordered"] is False and view["reorder_moves"] == [],
          f"{view.get('reordered')} {view.get('reorder_moves')}")
    bad_rec = {"run_module": "trip", "current_port": "北京",
               "stops": [{"stage": "buy", "port": "北京", "goods": ["中国画"]},
                         {"stage": "sell", "port": "热那亚"},
                         {"stage": "buy", "port": "汉堡", "goods": ["啤酒"]}]}
    v2 = appmod.route_put(bad_rec)
    check("PUT 一份乱序的整趟 → 200 而不是 400（2026-09-29 你选的 A：保存时自动重排）",
          [s["stage"] for s in v2["stops"]] == ["buy", "buy", "sell"]
          and v2["reordered"] is True, str([s["stage"] for s in v2["stops"]]))
    check("   盘上落的就是排好的那份（不是界面传来的乱序原样）",
          [s["stage"] for s in json.load(open(TMPJSON, encoding="utf-8"))["stops"]]
          == ["buy", "buy", "sell"])
    check("   回给界面的明细点名是哪一站从第几挪到第几",
          any("出货『热那亚』" in m for m in v2["reorder_moves"])
          and any("第 3 站" in m for m in v2["reorder_moves"]), str(v2["reorder_moves"]))
    check("   derived 的整趟走法跟着排好的顺序（界面念的那行和引擎走的必须是同一份）",
          [s["stage"] for s in v2["derived"]["trip_stops"]] == ["buy", "buy", "sell"],
          str(v2["derived"]["trip_reason"])[:70])
    v3 = appmod.route_put({"run_module": "buy", "current_port": "北京",
                           "stops": [{"stage": "sell", "port": "热那亚"},
                                     {"stage": "buy", "port": "北京", "goods": ["中国画"]}]})
    check("   单模块（buy）同样这份先后 → 不排、也不回 reordered：先后只对整趟有意义",
          v3["reordered"] is False and [s["stage"] for s in v3["stops"]] == ["sell", "buy"],
          str([s["stage"] for s in v3["stops"]]))
    try:
        appmod.route_put({"run_module": "trip", "current_port": "北京",
                          "stops": [{"stage": "sell", "port": "热那亚"},
                                    {"stage": "sell", "port": "北京"}]})
        check("整趟 + 两站出货 → PUT 仍然 400，盘上那份一个字都不动（拒在写文件之前）", False)
    except appmod.HTTPException as e:
        check("整趟 + 两站出货 → PUT 仍然 400，盘上那份一个字都不动（拒在写文件之前）",
              e.status_code == 400
              and [s["stage"] for s in json.load(open(TMPJSON, encoding="utf-8"))["stops"]]
              == ["sell", "buy"], str(e.detail)[:46])
    appmod.route_preset_save({"name": "整趟·北京到热那亚", "run_module": "trip",
                              "stops": ok_rec["stops"]})
    loaded = appmod.route_preset_load({"name": "整趟·北京到热那亚"})
    check("方案库能把整趟存成名字、读回来照样是整趟（校验一个字都不另写）",
          loaded["run_module"] == "trip" and len(loaded["derived"]["trip_stops"]) == 3,
          str(loaded["derived"]["trip_reason"])[:60])
    check("   读方案仍然不动「当前所在港」：还是盘上那一份里的『北京』（方案里根本没存它）",
          loaded["current_port"] == "北京", repr(loaded["current_port"]))
    ps = appmod.route_preset_save({"name": "乱序整趟", "run_module": "trip",
                                   "stops": bad_rec["stops"]})
    check("方案里存乱序的整趟 → 照样存进去，且存的是排好的那份（和保存规划同一条规矩）",
          ps["reordered"] is True
          and ps["preset"]["route_text"].split(" → ")
          == ["买货·北京", "买货·汉堡", "出货·热那亚"], ps["preset"]["route_text"])
    check("   被自动重排的方案读回来不会再排第二次（幂等；读出来和存进去的是同一份顺序）",
          appmod.route_preset_load({"name": "乱序整趟"})["reordered"] is False)
    try:
        appmod.route_preset_save({"name": "两站出货", "run_module": "trip",
                                  "stops": [{"stage": "sell", "port": "热那亚"},
                                            {"stage": "sell", "port": "北京"}]})
        check("方案里排两个出货站 → 仍然 400（重排只挪位置，不多站）", False)
    except appmod.HTTPException as e:
        check("方案里排两个出货站 → 仍然 400（重排只挪位置，不多站）",
              e.status_code == 400 and "只能有一站" in str(e.detail),
              f"{e.status_code} {str(e.detail)[:46]}")
except Exception as e:
    check("整趟这一节的接口路径能跑起来（app 导入失败要单独看）", False,
          f"{type(e).__name__}: {e}")
finally:
    for tmp in (TMPJSON, TMPPRE):
        if os.path.exists(tmp):
            os.remove(tmp)
    rp.ROUTE_JSON = real_path
    rps.PRESETS_JSON = real_pre

print("=" * 72)
print("结果:", "全部通过" if not FAILS else "失败项 = %s" % FAILS)
sys.exit(1 if FAILS else 0)
