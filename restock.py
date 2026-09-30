"""交易所购买页的补货倒计时：读数 + 按港口记的「下次补货时刻」+ 要不要等的判据。

只用读数和算术规则，不碰截图、不碰设备，所以能离线喂真图和小纸条验证。
"""

import time

COUNTDOWN_REGION = "补货倒计时"

# 用户 2026-09-28 拍板：那一轮 OCR 读不出数字，就按 30 分钟从头起算（宁可多等，不可早买）。
FALLBACK_SECONDS = 30 * 60

# 用户给的等待上限：40 分钟。游戏最多 30 分钟，多出来的是识别失败重试的余量。
MAX_WAIT_SECONDS = 40 * 60

# 用户要求：画面连续这么久认不出任何已知页面，就报告（多半是充值弹窗/全屏宣传页盖住了）。
UNKNOWN_PAGE_SECONDS = 120


def parse_countdown_seconds(text):
    """把 OCR 文字读成秒数，读不出返回 None。不用正则。

    只留数字这一招顺手解决了一个实测到的坑：把裁图放大再识别时，冒号偶尔被读成句点
    （`00:09:22` → `00:09.22` / `00.09:22`），分隔符长什么样都不用管。
    要求正好 6 位：游戏这一串固定是 HH:MM:SS 且补零，少一位说明吞了数字，
    多一位说明冒号被读成了数字 —— 这两种都不敢猜，交回调用方走 30 分钟兜底。
    分、秒越界（例如 `00:69:22`，把 0 看成了 6）同样算读不出。
    """
    digits = "".join(ch for ch in (text or "") if ch.isdigit())
    if len(digits) != 6:
        return None
    hours = int(digits[0:2])
    minutes = int(digits[2:4])
    seconds = int(digits[4:6])
    if minutes > 59 or seconds > 59:
        return None
    return hours * 3600 + minutes * 60 + seconds


def resolve_remaining(text):
    """一次读数的结论：{remaining_seconds, source, read_text}。

    source 只有两种：`ocr` = 画面上读到的，`fallback` = 读不出、按 30 分钟起算。
    写进状态和日志，让人一眼看出这一轮到底是不是真读到了数。
    """
    seconds = parse_countdown_seconds(text)
    read = " ".join((text or "").split())
    if seconds is None:
        return {"remaining_seconds": FALLBACK_SECONDS, "source": "fallback", "read_text": read}
    return {"remaining_seconds": seconds, "source": "ocr", "read_text": read}


def remaining_text(seconds):
    """给人看的剩余时间说法：562 -> 「9 分 22 秒」。"""
    total = max(int(seconds or 0), 0)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} 小时 {minutes} 分"
    if minutes:
        return f"{minutes} 分 {secs} 秒"
    return f"{secs} 秒"


def next_restock_at(read, now):
    """把「一次读数」换算成这个港口下次补货的时刻（epoch 秒，整数）。

    为什么要存时刻而不是存秒数：秒数一离画面就没意义了 —— 存成时刻，下次来买货
    只要对一下表就知道刷没刷，不用再盯着那串数字看。每天第一次来必然「没记过」，
    于是必然直接买（用户 2026-09-30 的口径：隔夜离线怎么都超过 30 分钟，货架早刷完了）。

    两种读数来源都照收：ocr 用画面那串，fallback 用 30 分钟兜底。读不出时宁可把
    时刻标晚一点（下次来多等一阵），不可标早（会被当成「已经刷过了」而直接买）。
    """
    return int(now) + max(int(read.get("remaining_seconds") or 0), 0)


def decide(stored, now):
    """拿这个港记下的时刻和现在对一下表：要不要等、等多少秒、为什么。

    返回 (should_wait, seconds, reason)，reason 只有三种说法：
      - `no_record` 没记过 —— 这个港今天还没买过货，直接买，一秒都不等；
      - `passed`    记的时刻已经到/过了 —— 到点刷过了，直接买；
      - `pending`   还在将来 —— 本地等到那个点，没等完不买。
    「正好等于现在」算已过：差 0 秒再等一轮没意义，游戏那一秒自己就刷了。
    stored 只该是 run_state 记下来的整数；别的（None / 字符串 / 浮点 / 布尔）一律按
    没记过处理 —— 不敢拿一个来路不明的时刻去决定等还是买。
    """
    if not isinstance(stored, int) or isinstance(stored, bool):
        return False, 0, "no_record"
    if stored <= int(now):
        return False, 0, "passed"
    return True, stored - int(now), "pending"


def moment_text(when):
    """把记下来的时刻说成人话：epoch 秒 -> 本地时间「18:24:07」；没记过 -> 空串。

    给界面和日志用 —— 前端不自己换时刻，口径全在这一份代码里。
    """
    if not isinstance(when, int) or isinstance(when, bool):
        return ""
    return time.strftime("%H:%M:%S", time.localtime(when))
