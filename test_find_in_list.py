"""find_in_list 双向滑动 + scroll_list_to_top 的离线测试（不需要模拟器 / ADB）。

原理：造一张很高的「虚拟列表」，用一个可变的滚动偏移量模拟页面滚动，
每次 capture() 就把当前偏移下的画面写成 PNG。这样完全不碰 ADB 也能验证
「找不到时先向下找、再向上找」和「滑回顶部」的逻辑。

运行：
    C:\\Users\\LukeJiangX1C2019\\AppData\\Local\\Python\\pythoncore-3.14-64\\python.exe test_find_in_list.py
"""

import os
import shutil
import sys
import tempfile

import cv2
import numpy as np

sys.stdout.reconfigure(encoding="utf-8")
from vision import find_in_list, scroll_list_to_top  # noqa: E402

SCREEN_W, SCREEN_H = 1600, 900
LIST_ROI = (300, 100, 800, 700)          # x, y, w, h
ROW_H = 100
N_ROWS = 12
LIST_H = LIST_ROI[3]                      # 可视区高度 700
MAX_OFF = N_ROWS * ROW_H - LIST_H         # 最大滚动偏移 500
STEP = 100                                # 每次滑动 = 滚一行
# 向下看（手指上拖）的手势，和 states.json 里 swipe_range 的语义一致
SWIPE_DOWN = (LIST_ROI[0] + LIST_ROI[2] // 2, 700, LIST_ROI[0] + LIST_ROI[2] // 2, 250)


class FakeList:
    """模拟一个可滚动的列表：记录滚动偏移，按偏移生成截图。"""

    def __init__(self, tmp_dir):
        self.tmp_dir = tmp_dir
        self.offset = 0
        self.swipes = []
        rng = np.random.default_rng(20260923)
        # 每行一段不同的随机纹理，保证模板之间不会互相误匹配
        self.content = rng.integers(0, 256, size=(N_ROWS * ROW_H, LIST_ROI[2], 3),
                                    dtype=np.uint8)

    def scroll_to(self, offset):
        self.offset = max(0, min(offset, MAX_OFF))

    def swipe(self, x1, y1, x2, y2):
        dy = y2 - y1
        if dy < 0:                      # 手指上拖 -> 看列表更下面
            self.scroll_to(self.offset + STEP)
            self.swipes.append("down")
        elif dy > 0:                    # 手指下拖 -> 看列表更上面
            self.scroll_to(self.offset - STEP)
            self.swipes.append("up")

    def capture(self):
        """按当前偏移把画面写成 PNG，返回路径（vision 的 capture 回调）。"""
        path = os.path.join(self.tmp_dir, "fake_screen.png")
        self.save_to(path)
        return path

    def save_to(self, path):
        canvas = np.zeros((SCREEN_H, SCREEN_W, 3), dtype=np.uint8)
        x, y, w, h = LIST_ROI
        visible = self.content[self.offset:self.offset + h]
        if visible.shape[0] < h:
            visible = np.pad(visible, ((0, h - visible.shape[0]), (0, 0), (0, 0)),
                             mode="edge")
        canvas[y:y + h, x:x + w] = visible
        cv2.imwrite(path, canvas)
        return True


def make_templates(fake, tmp_dir, rows):
    """把指定行裁出来存成模板图，返回 {行号: 路径}。"""
    out = {}
    for r in rows:
        crop = fake.content[r * ROW_H:(r + 1) * ROW_H]
        path = os.path.join(tmp_dir, f"tpl_row{r}.png")
        cv2.imwrite(path, crop)
        out[r] = path
    return out


# ---------- 断言小工具 ----------
PASSED = []
FAILED = []


def check(name, cond, detail=""):
    (PASSED if cond else FAILED).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}"
          + (f"  ->  {detail}" if detail else ""))


class FakeController:
    """冒充 MuMuController：点击只记录，截图/滑动交给 FakeList 的滚动模型。"""

    def __init__(self, fake):
        self.fake = fake
        self.clicks = []

    def click(self, x, y):
        self.clicks.append((int(x), int(y)))

    def swipe(self, x1, y1, x2, y2, duration=300):
        self.fake.swipe(x1, y1, x2, y2)

    def screenshot(self, path):
        return self.fake.save_to(path)


def test_buy_commodities_action(fake, tmp_dir, name_map):
    """离线跑一遍引擎的 buy_commodities 动作：两个商品一上一下，验证都能点到 + 会回位。"""
    import state_machine

    engine = state_machine.StateMachineEngine("fake_adb.exe", 16384, "fake_screen.png")
    engine.config = dict(state_machine.DEFAULT_CONFIG)
    # 截图走 FakeController 的滚动模型，_snap 只需报告成功；
    # _cap_path 指到临时目录，绝不覆盖项目里真实的 state_screen_*.png
    engine._snap = lambda ctl, path, what="截图": (ctl.screenshot(path), "fake")
    engine._cap_path = lambda prefix: os.path.join(tmp_dir, f"engine_{prefix}.png")
    engine._resolve_template = lambda name: (name_map[name], None, 0.8)

    ctl = FakeController(fake)
    fake.scroll_to(0)
    action = {
        "type": "buy_commodities",
        "templates": ["商品-顶部", "商品-底部"],
        "list_roi": list(LIST_ROI),
        "swipe_range": list(SWIPE_DOWN),
        "max_swipes": 5, "max_swipes_up": 5,
        "swipe_pause_ms": 0, "reset_to_top": True,
        "click_wait_ms": 0, "negotiation": False, "threshold": 0.8,
    }
    msg = engine._do_action(action, ctl, fake.capture())

    check("动作返回成功摘要", "成功 2 个，失败 0 个" in msg, msg)
    check("点击了 2 次", len(ctl.clicks) == 2, str(ctl.clicks))
    # 模板占满列表区宽度，所以中心 x 固定在区中央；
    # 第 11 行要滑到 offset=500 才可见，届时它渲染在列表区最顶行
    check("第一个商品点在首行", ctl.clicks and ctl.clicks[0] == (LIST_ROI[0] + 400, LIST_ROI[1] + 50),
          str(ctl.clicks))
    check("整批买完后列表已滑回顶部", fake.offset == 0, f"offset={fake.offset}")
    check("日志里有回位记录",
          any("滑回顶部" in l["message"] for l in engine._logs),
          str([l["message"] for l in engine._logs][-2:]))


def main():
    tmp_dir = tempfile.mkdtemp(prefix="uwo_test_")
    try:
        fake = FakeList(tmp_dir)
        tpl = make_templates(fake, tmp_dir, [0, 2, 5, 11])
        first = fake.capture()

        def run(template_row, **kw):
            """在指定起始偏移下查找某一行模板。"""
            fake.scroll_to(kw.pop("from_offset", 0))
            fake.swipes = []
            screen = fake.capture()
            return find_in_list(screen, tpl[template_row], LIST_ROI,
                                swipe_range=SWIPE_DOWN, capture=fake.capture,
                                swipe_func=fake.swipe, swipe_pause_ms=0, **kw)

        print("\n[1] 首屏就能找到 -> 不应滑动，方向 none")
        r = run(0, max_swipes=3, max_swipes_up=3)
        check("找到第 0 行", r is not None, str(r and r["confidence"]))
        check("swipe_direction == none", r and r["swipe_direction"] == "none")
        check("swipes_used == 0", r and r["swipes_used"] == 0)
        check("实际没滑动", fake.swipes == [], str(fake.swipes))

        print("\n[2] 目标在列表下方 -> 只向下滑动就能找到（原有能力）")
        r = run(11, max_swipes=5, max_swipes_up=0)
        check("找到第 11 行", r is not None)
        check("方向 down", r and r["swipe_direction"] == "down")
        check("滑动次数 5", r and r["swipes_used"] == 5, str(r and r["swipes_used"]))
        check("全程只向下滑", set(fake.swipes) == {"down"}, str(fake.swipes))

        print("\n[3] 列表停在底部、目标在顶部 -> 必须靠新增的向上查找")
        r = run(0, from_offset=MAX_OFF, max_swipes=3, max_swipes_up=6)
        check("找到第 0 行", r is not None)
        check("方向 up", r and r["swipe_direction"] == "up", str(r and r["swipe_direction"]))
        check("先向下滑 3 次再向上滑",
              fake.swipes[:3] == ["down"] * 3 and "up" in fake.swipes, str(fake.swipes))

        print("\n[4] 回归：max_swipes_up=0 时保持旧行为（上方目标找不到）")
        r = run(0, from_offset=MAX_OFF, max_swipes=3, max_swipes_up=0)
        check("确实找不到", r is None, "旧版语义，未配置新参数时行为不变")
        check("没有向上滑动", "up" not in fake.swipes, str(fake.swipes))

        print("\n[5] 目标真的不在列表里 -> 两个方向都滑满后返回 None")
        fake.swipes = []
        fake.scroll_to(0)
        rng = np.random.default_rng(7)
        noise = os.path.join(tmp_dir, "tpl_noise.png")
        cv2.imwrite(noise, rng.integers(0, 256, size=(ROW_H, LIST_ROI[2], 3), dtype=np.uint8))
        fake.capture()
        r = find_in_list(fake.capture(), noise, LIST_ROI, swipe_range=SWIPE_DOWN,
                         max_swipes=3, max_swipes_up=4, capture=fake.capture,
                         swipe_func=fake.swipe, swipe_pause_ms=0)
        check("返回 None", r is None)
        check("共滑动 7 次（3 下 + 4 上）", len(fake.swipes) == 7, str(fake.swipes))

        print("\n[6] scroll_list_to_top 把列表滑回顶部")
        fake.swipes = []
        fake.scroll_to(MAX_OFF)
        n = scroll_list_to_top(MAX_OFF // STEP + 1, fake.swipe, swipe_range=SWIPE_DOWN,
                               capture=fake.capture, swipe_pause_ms=0)
        check("偏移回到 0", fake.offset == 0, f"offset={fake.offset}")
        check("滑动方向全为 up", set(fake.swipes) == {"up"}, str(fake.swipes))
        check("滑动次数 = 上限 + 1", n == MAX_OFF // STEP + 1, f"n={n}")

        print("\n[7] 多滑几次不会滑过头（已过顶仍安全）")
        n = scroll_list_to_top(10, fake.swipe, swipe_range=SWIPE_DOWN,
                               capture=fake.capture, swipe_pause_ms=0)
        check("偏移仍为 0", fake.offset == 0, f"offset={fake.offset}")

        print("\n[8] 目标在当前位置上方（偏移 500 时第 2 行不可见）")
        r = run(2, from_offset=MAX_OFF, max_swipes=3, max_swipes_up=5)
        check("找到第 2 行", r is not None)
        check("方向 up", r and r["swipe_direction"] == "up", str(r and r["swipe_direction"]))
        check("向上滑 3 次后命中", r and r["swipes_used"] == 3, str(r and r["swipes_used"]))

        print("\n[9] 引擎动作集成测试：buy_commodities 双向查找 + 自动回位")
        test_buy_commodities_action(fake, tmp_dir,
                                    {"商品-顶部": tpl[0], "商品-底部": tpl[11]})

        print(f"\n{'=' * 52}")
        print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
        if FAILED:
            print("失败项: " + ", ".join(FAILED))
        return 1 if FAILED else 0
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
