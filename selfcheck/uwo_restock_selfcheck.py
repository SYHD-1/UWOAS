"""补货倒计时读数层离线自检：解析规则 + 兜底规则 + 按港口记时刻的判据 + 区域登记 + 真图端到端。

只读 selfcheck/fixtures/restock_buy_page.png（钉在库里的购买页真图，那串是 00:10:32），
不写任何文件、不碰 ADB、不点任何东西。
"""
import os
import sys
import json

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 项目根 = selfcheck/ 的上一级（换目录/换机器都不用改）
sys.path.insert(0, BASE)
import restock  # noqa: E402

FAILS = []


def check(label, cond, detail=""):
    print("  %-4s %s %s" % ("PASS" if cond else "FAIL", label, detail))
    if not cond:
        FAILS.append(label)


print("=" * 72)
print("A) parse_countdown_seconds：小纸条")
cases = [
    ("00:09:22", 562, "正常一串"),
    ("00:09.22", 562, "第二个冒号被读成句点（实测踩过的放大坑）"),
    ("00.09:22", 562, "第一个冒号被读成句点"),
    ("00.09.22", 562, "两个冒号全成了句点"),
    ("00:00:00", 0, "到点了"),
    ("00:29:59", 1799, "刚刷完"),
    ("00:30:00", 1800, "整 30 分"),
    (" 00 : 09 : 22 ", 562, "字间有空格"),
    ("00：09：22", 562, "全角冒号"),
    ("0:09:22", None, "只有 5 位数字 —— 吞了一位，不敢猜"),
    ("00:09:222", None, "7 位 —— 冒号被读成了数字"),
    ("", None, "空字符串"),
    ("剩余", None, "整块被别的字盖住"),
    (None, None, "None"),
    ("00:69:22", None, "分越界（把 0 看成 6）"),
    ("00:09:72", None, "秒越界"),
    ("24:00:00", 86400, "时不按 24 上限卡（这游戏最多 30 分，越界交给等待上限管）"),
]
for text, want, label in cases:
    got = restock.parse_countdown_seconds(text)
    check(f"{label} -> {want!r}", got == want, f"实得 {got!r}（输入 {text!r}）")

print("-" * 72)
print("B) resolve_remaining：读不出就走 30 分钟兜底（你 2026-09-28 拍的）")
r = restock.resolve_remaining("00:09:22")
check("读得到标 ocr、秒数照画面", r == {"remaining_seconds": 562, "source": "ocr", "read_text": "00:09:22"}, str(r))
r = restock.resolve_remaining("00:00.00")
check("读到 0 就是 0（不许被当成读不出）", r["remaining_seconds"] == 0 and r["source"] == "ocr", str(r))
for bad in ["", None, "看不清", "00:69:22"]:
    r = restock.resolve_remaining(bad)
    check(f"读不出『{bad!r}』-> 兜底 {restock.FALLBACK_SECONDS} 秒 + source=fallback",
          r["remaining_seconds"] == restock.FALLBACK_SECONDS and r["source"] == "fallback", str(r))
check("常量对得上：兜底 30 分 / 上限 40 分 / 未知画面 120 秒",
      (restock.FALLBACK_SECONDS, restock.MAX_WAIT_SECONDS, restock.UNKNOWN_PAGE_SECONDS)
      == (1800, 2400, 120))

print("-" * 72)
print("C) remaining_text：给人看的说法")
for sec, want in [(562, "9 分 22 秒"), (0, "0 秒"), (45, "45 秒"), (1800, "30 分 0 秒"),
                  (3720, "1 小时 2 分"), (None, "0 秒")]:
    got = restock.remaining_text(sec)
    check(f"{sec} -> {want}", got == want, f"实得『{got}』")

print("-" * 72)
print("D) 区域登记：ocr_regions.json")
regions = json.load(open(os.path.join(BASE, "ocr_regions.json"), encoding="utf-8"))
region = next((x for x in regions if x.get("name") == restock.COUNTDOWN_REGION), None)
check(f"有名为『{restock.COUNTDOWN_REGION}』的区域", region is not None, str(region))
if region:
    check("roi 是实测的 [690,120,107,34]", list(region["roi"]) == [690, 120, 107, 34], str(region["roi"]))
    check("预处理 scale=2（实测这个倍数下整图+roi 一字不差）",
          (region.get("preprocess") or {}).get("scale") == 2, str(region.get("preprocess")))

print("-" * 72)
print("E) 真图端到端：喂钉在 fixtures 里的购买页截图（要加载 OCR 模型，等十几秒）")
import vision  # noqa: E402

# ⚠ 这里**故意不读**引擎调试图 state_screen_buy.png：那个文件每次实跑买货都会被覆盖成
# 当天的画面（2026-09-30 18:53 那次就把 9-24 那张顶掉了，本段当场红三条）。
# 自检要的真图一律复制进 selfcheck/fixtures/ 再读 —— 这是待办 T-060 的第一块落地。
shot = os.path.join(BASE, "selfcheck", "fixtures", "restock_buy_page.png")
check("真图在（不在就是被误删了，别去拿引擎调试图顶替）", os.path.exists(shot), shot)
PINNED = "00:10:32"          # 这张图上当时那串倒计时，2026-09-30 18:53 实跑留下
PINNED_SECONDS = 632         # = 10 分 32 秒
read = vision.ocr_text_by_region(shot, restock.COUNTDOWN_REGION)
check(f"区域读出来就是 {PINNED}", read == PINNED, f"实得『{read}』")
res = restock.resolve_remaining(read)
check("换算成 632 秒（10 分 32 秒），来源记 ocr",
      res["remaining_seconds"] == PINNED_SECONDS and res["source"] == "ocr", str(res))
check("界面/日志用的文字是「10 分 32 秒」",
      restock.remaining_text(res["remaining_seconds"]) == "10 分 32 秒")
check("换掉的图也照样讲得通：读数在 0~1800 秒之间（超过 30 分钟说明喂错图了）",
      0 <= res["remaining_seconds"] <= restock.FALLBACK_SECONDS, str(res["remaining_seconds"]))

print("-" * 72)
print("F) next_restock_at：一次读数换算成「这个港下次几点补货」的时刻")
import time as _t  # noqa: E402

NOW = 1000000
check("画面读到 9 分 22 秒 -> 时刻 = 现在 + 562 秒",
      restock.next_restock_at(restock.resolve_remaining("00:09:22"), NOW) == NOW + 562,
      str(restock.next_restock_at(restock.resolve_remaining("00:09:22"), NOW)))
check("读到 0 -> 时刻就是现在（不许顺手放大成 30 分钟）",
      restock.next_restock_at(restock.resolve_remaining("00:00:00"), NOW) == NOW)
check("读不出 -> 按 30 分钟记，宁可把时刻标晚（下次多等一阵，不可当成已刷新）",
      restock.next_restock_at(restock.resolve_remaining(""), NOW) == NOW + 1800,
      str(restock.next_restock_at(restock.resolve_remaining(""), NOW)))
check("落盘要的是整数，浮点「现在」也收口",
      isinstance(restock.next_restock_at({"remaining_seconds": 10}, 1000000.7), int)
      and restock.next_restock_at({"remaining_seconds": 10}, 1000000.7) == 1000010)
check("负数读数 -> 夹到现在，不会记出一个过去的时刻",
      restock.next_restock_at({"remaining_seconds": -5}, NOW) == NOW)
check("读数缺字段 -> 当 0 秒处理，不抛", restock.next_restock_at({}, NOW) == NOW)

print("-" * 72)
print("G) decide：拿记下的时刻和现在对一下表，决定要不要等")
for stored, want, label in [
    (None, (False, 0, "no_record"), "没记过 = 这个港今天第一次来买 -> 直接买（用户 09-30 报的错）"),
    (NOW - 8 * 3600, (False, 0, "passed"), "8 小时前记的（隔夜离线）-> 不等"),
    (NOW, (False, 0, "passed"), "正好等于现在 -> 算已到，再等一轮没意义"),
    (NOW - 1, (False, 0, "passed"), "刚过 1 秒 -> 不等"),
    (NOW + 1, (True, 1, "pending"), "还差 1 秒 -> 等，秒数照差值"),
    (NOW + 1800, (True, 1800, "pending"), "还差 30 分钟 -> 等"),
    (NOW + 86400, (True, 86400, "pending"), "时刻离谱也照实返回（40 分钟那道闸在引擎里，不在这一步）"),
    (-1, (False, 0, "passed"), "负数时刻（账本那层就进不来，真进来也只说明「早过了」）-> 买"),
]:
    got = restock.decide(stored, NOW)
    check(f"{label} -> {want}", got == want, f"实得 {got}")
for junk in ["1000562", 1000562.0, True, False, {}, [], ""]:
    got = restock.decide(junk, NOW)
    check(f"账本里是垃圾值 {junk!r} -> 按没记过处理（不敢拿它决定等还是买）",
          got == (False, 0, "no_record"), f"实得 {got}")

today_first = restock.decide(None, NOW)                    # 今天早上第一次来买
recorded = restock.next_restock_at(restock.resolve_remaining("00:29:00"), NOW)
later = restock.decide(recorded, NOW + 60)                 # 1 分钟后再来一趟
next_day = restock.decide(recorded, NOW + 9 * 3600)        # 隔夜再来
check("连着看一整天：首次不等 -> 当天第二次按记的时刻等 -> 隔夜又不等",
      today_first[0] is False and later == (True, recorded - int(NOW + 60), "pending")
      and next_day[0] is False, f"{today_first} / {later} / {next_day}")

print("-" * 72)
print("H) moment_text：给界面和日志的时刻说法")
check("没记过 -> 空串（界面那行显示「没记过」）", restock.moment_text(None) == "")
for junk in [True, False, "1000562", 1000562.0, [], {}, 0.5]:
    check(f"垃圾值 {junk!r} -> 也是空串，不抛", restock.moment_text(junk) == "")
got = restock.moment_text(NOW + 562)
check("整数 -> 本地时间 HH:MM:SS（前端不自己换算）",
      got == _t.strftime("%H:%M:%S", _t.localtime(NOW + 562)) and got.count(":") == 2, got)

print("=" * 72)
if FAILS:
    print(f"RESULT: {len(FAILS)} FAIL -> {FAILS}")
    sys.exit(1)
print("RESULT: ALL PASS (RESTOCK_OK)")
