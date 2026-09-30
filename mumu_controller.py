"""MuMu 模拟器 ADB 控制模块。

通过 ADB 连接 MuMu 模拟器（默认 127.0.0.1:7555），
提供截图、点击、滑动、输入文本等基础控制能力。
"""

import os
import re
import subprocess
import threading
import time

# 本文件所在目录：调用方只传文件名时，用它拼成绝对路径，避免受启动时工作目录影响
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 16384

# MuMu 里键盘和触摸共用同一个原始输入设备，设备名固定；
# event 编号会随启动顺序变化，所以按名字解析而不是写死 event4。
KEY_DEVICE_NAME = "Xiaomi Input"
_EVENT_PATH_RE = re.compile(r"^/dev/input/event\d+$")

# 全局截图锁。ADB 并发调用本身就不稳，而且本地截图文件的「写」和「读」不能重叠
# （重叠会 WinError 32 文件被占用，或 cv2 读到半截空文件）。
# 调试台点一次「刷新截图」和状态机后台线程每轮截图，走的必须是这一把锁 ——
# 各自加各自的锁等于没加。
SCREENSHOT_LOCK = threading.Lock()


class MuMuController:
    """封装对 MuMu 模拟器的 ADB 操作。"""

    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT, adb_path=r"C:\Program Files\Netease\MuMu Player 12\shell\adb.exe"):
        self.host = host
        self.port = port
        self.addr = f"{host}:{port}"
        self.adb_path = adb_path
        self._key_device = None

    def _run(self, *args):
        """执行一条 ADB 命令并返回 subprocess.CompletedProcess。"""
        cmd = [self.adb_path, *args]
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    def connect(self):
        """连接 MuMu 模拟器，返回 (是否成功, 输出信息)。"""
        proc = self._run("connect", self.addr)
        return proc.returncode == 0, proc.stdout.strip()

    def screenshot(self, save_path):
        """截图并保存到本地路径，返回是否成功。

        只传文件名（如 "screen.png"）时会落到本文件所在目录，不受启动时
        工作目录影响；设备端用唯一文件名，避免并发请求互相覆盖导致拉到坏图；
        拉取后校验文件非空，避免"空文件也算成功"。
        """
        # 相对路径统一转成基于项目目录的绝对路径
        if not os.path.isabs(save_path):
            save_path = os.path.join(BASE_DIR, save_path)
        parent = os.path.dirname(save_path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        # 设备端也用唯一文件名：并发时（如前端自动刷新 + OCR 同时截图）
        # 不会一个在写、另一个在读同一个文件，从而拉不到半截坏图
        remote = (f"/sdcard/mumu_screenshot_{os.getpid()}"
                  f"_{threading.get_ident()}_{int(time.time() * 1000)}.png")

        # 先删除本地旧文件，避免拉取失败时误判为成功（残留旧截图）。
        # 文件可能正被别的线程读取（如 cv2.imread 打开中），Windows 下
        # os.remove 会抛 WinError 32「另一个程序正在使用此文件」，
        # 所以重试几次；实在删不掉就直接让 adb pull 覆盖，不中断流程。
        for _ in range(3):
            if not os.path.exists(save_path):
                break
            try:
                os.remove(save_path)
                break
            except OSError:
                time.sleep(0.1)

        try:
            screencap = self._run("-s", self.addr, "shell", "screencap", "-p", remote)
            if screencap.returncode != 0:
                return False
            pull = self._run("-s", self.addr, "pull", remote, save_path)
            if pull.returncode != 0:
                return False
        finally:
            # 清理设备端临时文件，避免 /sdcard 堆积
            self._run("-s", self.addr, "shell", "rm", "-f", remote)

        # 文件必须存在且非空，才算截图成功
        return os.path.exists(save_path) and os.path.getsize(save_path) > 0

    def click(self, x, y):
        """点击屏幕坐标 (x, y)。"""
        self._run("-s", self.addr, "shell", "input", "tap", str(x), str(y))

    def swipe(self, x1, y1, x2, y2, duration=300):
        """从 (x1, y1) 滑动到 (x2, y2)，duration 为毫秒。"""
        self._run(
            "-s", self.addr, "shell", "input", "swipe",
            str(x1), str(y1), str(x2), str(y2), str(duration),
        )

    def resolve_key_device(self, name=KEY_DEVICE_NAME):
        """按设备名解析原始输入设备路径，找不到返回 None。

        结果会缓存：模拟器不重启设备编号不会变，省掉每次按键前的一轮 adb 往返。
        """
        if self._key_device:
            return self._key_device
        proc = self._run("-s", self.addr, "shell", "getevent", "-pl")
        path = None
        for line in proc.stdout.splitlines():
            text = line.strip()
            if text.startswith("add device"):
                found = re.search(r"(/dev/input/event\d+)", text)
                path = found.group(1) if found else None
            elif text.startswith("name:"):
                device_name = text.split(":", 1)[1].strip().strip('"')
                if device_name == name and path:
                    self._key_device = path
                    return path
        return None

    def send_key(self, code, hold_ms=80):
        """向原始输入设备发送一次按键（按下 + 抬起），返回 (是否成功, 信息)。

        不能用 `adb shell input keyevent`：它在 Android 框架层 injectInputEvent，
        绕过了 /dev/input，而这个游戏读的是原始事件，所以 keyevent 它收不到。
        code 是 Linux 输入码（EV_KEY 的编号），不是 Android KeyEvent 键码。
        """
        code = int(code)
        if code < 0:
            return False, f"键码不能为负: {code}"
        device = self.resolve_key_device()
        if not device:
            return False, f"未找到名为 {KEY_DEVICE_NAME} 的输入设备"
        # 设备路径来自 getevent 输出，拼进 shell 前必须校验，防止注入
        if not _EVENT_PATH_RE.match(device):
            return False, f"非法的输入设备路径: {device}"
        hold = max(int(hold_ms), 0) / 1000
        cmd = (
            f"sendevent {device} 1 {code} 1 && sendevent {device} 0 0 0"
            f" && sleep {hold:.3f} && sendevent {device} 1 {code} 0"
            f" && sendevent {device} 0 0 0"
        )
        proc = self._run("-s", self.addr, "shell", cmd)
        if proc.returncode != 0:
            self._key_device = None   # 设备可能已失效，下次重新解析
            return False, (proc.stderr or proc.stdout).strip()
        return True, device

    def is_adbkeyboard_installed(self):
        """检查 ADBKeyboard 是否已安装，返回 bool。"""
        proc = self._run(
            "-s", self.addr, "shell", "pm", "list", "packages",
            "com.android.adbkeyboard",
        )
        return "com.android.adbkeyboard" in proc.stdout

    def install_apk(self, apk_path):
        """安装本地 apk 文件到模拟器，返回 (是否成功, 输出信息)。"""
        proc = self._run("-s", self.addr, "install", "-r", apk_path)
        output = (proc.stdout + proc.stderr).strip()
        return proc.returncode == 0, output

    def set_adbkeyboard(self):
        """启用并切换当前输入法为 ADBKeyboard。

        输入中文前需先安装 ADBKeyboard.apk
        （包名 com.android.adbkeyboard），否则无效。
        """
        self._run(
            "-s", self.addr, "shell", "ime", "enable",
            "com.android.adbkeyboard/.AdbIME",
        )
        self._run(
            "-s", self.addr, "shell", "ime", "set",
            "com.android.adbkeyboard/.AdbIME",
        )

    def input_text(self, text):
        """输入文本，自动区分 ASCII 与中文。

        纯 ASCII 文本走 adb 原生 `input text`；
        含中文等非 ASCII 文本走 ADBKeyboard 广播方式，
        需先调用 set_adbkeyboard() 切换输入法。
        """
        if text.isascii():
            escaped = text.replace(" ", "%s")
            self._run("-s", self.addr, "shell", "input", "text", escaped)
        else:
            self._input_via_adbkeyboard(text)

    def _input_via_adbkeyboard(self, text):
        """通过 ADBKeyboard 广播输入中文文本。"""
        # 转义双引号与反斜杠，避免破坏设备端 shell 解析
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        cmd = f'am broadcast -a ADB_INPUT_TEXT --es msg "{escaped}"'
        self._run("-s", self.addr, "shell", cmd)

    def clear_text(self):
        """清空当前输入框里的文字（ADBKeyboard 的 ADB_CLEAR_TEXT 广播）。

        用在「搜港名」这种输入框会残留上一次内容的地方：不清空就会搜成「汉堡上海」，
        列表筛出 0 行，接着按固定坐标点那一行就点到别处去了。
        ⚠ 实测（2026-09-27 港口间移动录制）：这条广播**只有游戏底部那条白色输入栏开着**
        才生效，栏关着时它 result=0 但什么都不清 —— 广播的返回值不能当成功依据。
        """
        self._run("-s", self.addr, "shell", "am broadcast -a ADB_CLEAR_TEXT")

    def input_bar_state(self):
        """游戏底部那条白色输入栏开着没有 —— 一条 dumpsys 就判得出，不截图、不 OCR、不模板。

        判据来自 2026-09-27 港口间移动录制（港口间移动-流程记录.md 2.4）：
        `mServedView=` 那行花括号里第一个字段是可见性位，V=开着 / G=收着；
        配套的 `mServedView pos` 行里 y 开着时是正数（实测 806~835，随输入法变），收着时是 -1000。
        两个信号都验、取「与」：万一不同步就当没开 —— 顶多多点一次框，不会误以为能打进去。

        为什么非判它不可：栏收着时 ADB_INPUT_TEXT / ADB_CLEAR_TEXT 两条广播照样回 result=0，
        但一个字都不落地（2026-09-30 实机搜索框空着就是这么被骗过去的）—— 广播返回值不能当依据。
        反过来别用 mHaveConnection / mInputShown / mVisibleBound：录制已证实栏收着时它们也是 true。

        耗时单次实测 217~298ms，比截图 + OCR 快一个量级，放重试环里划算。
        返回 (是否开着, 人话说明)；读不出字段时返回 (None, 原因)，由调用方决定退回什么判据。
        """
        proc = self._run("-s", self.addr, "shell", "dumpsys input_method")
        if proc.returncode != 0:
            return None, f"dumpsys input_method 失败：{(proc.stderr or proc.stdout).strip()[:120]}"
        vis = None
        y = None
        for line in proc.stdout.splitlines():
            s = line.strip()
            # 只认行首这一条：dumpsys 里还有两处把 mServedView= 写在别的字段中间（嵌套的 wrapper），
            # 那不是当前正在服务的那个 view，用 startswith 天然排掉。
            if vis is None and s.startswith("mServedView="):
                fields = s.partition("{")[2].split()
                if len(fields) >= 2:
                    vis = fields[1][:1]      # GFED..CL. / VFED..CL.
            elif s.startswith("mServedView pos"):
                after = s.partition("y=")[2].split()
                if after:
                    try:
                        y = int(after[0])
                    except ValueError:
                        pass
        if vis is None:
            return None, "dumpsys 里没读到 mServedView 的可见性位（游戏可能没焦点、或系统版本字段改名）"
        if y is None:
            return None, f"读到可见性位 {vis}，但没读到 mServedView pos 的 y"
        return (vis == "V" and y > 0), f"可见位 {vis} / y={y}"


def capture(ctl, save_path, what="截图"):
    """所有 ADB 截图的唯一入口：串行加锁 + 先删旧文件 + 校验非空。

    返回 (是否成功, 说明或失败原因)。

    调试台点「刷新截图」（HTTP 线程）和状态机引擎每轮截图（后台线程）都必须走这里：
    两边共用模块级那把 SCREENSHOT_LOCK，不会同时抢 ADB、也不会一个在写同一个 png、
    另一个在读 —— 那正是 WinError 32（文件被占用）和读到半截空文件的根源。
    锁只在截图这几百毫秒里持有，不跨动作，等不到也只是排队，不会卡死流程。
    """
    if not os.path.isabs(save_path):
        save_path = os.path.join(BASE_DIR, save_path)
    with SCREENSHOT_LOCK:
        parent = os.path.dirname(save_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        # 先删本地旧文件：截图失败时不会误读到上一次的旧内容残留。
        # 文件可能正被别的线程读取（Windows 下 os.remove 抛 WinError 32），
        # 所以重试几次；实在删不掉就让 adb pull 直接覆盖。
        for _ in range(3):
            if not os.path.exists(save_path):
                break
            try:
                os.remove(save_path)
                break
            except OSError:
                time.sleep(0.1)
        try:
            ok = ctl.screenshot(save_path)
        except Exception as e:
            return False, f"{what}异常: {type(e).__name__}: {e}"
        if not ok:
            return False, f"{what}失败（screencap/pull 返回非 0）"
        if not os.path.exists(save_path):
            return False, f"{what}失败：截图文件未生成 ({save_path})"
        size = os.path.getsize(save_path)
        if size <= 0:
            return False, f"{what}失败：截图文件为空 ({os.path.basename(save_path)})"
        return True, f"{size} 字节"
