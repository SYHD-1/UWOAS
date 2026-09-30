"""图像识别核心模块（OpenCV 模板匹配）。

提供四个函数：
- find_template    ：单张模板匹配
- find_any_template：多张模板（最多 5 张），任一匹配成功即返回
- find_in_list     ：在列表区域查找，找不到则双向滑动（先向下看再向上看）并重试
- scroll_list_to_top：把列表滑回顶部，让每轮查找都从同一画面开始

所有查找函数返回 dict 或 None：
    {"cx": 中心x, "cy": 中心y, "confidence": 置信度, "rect": (x, y, w, h)}
"""

import json
import os
import time

import cv2
import numpy as np

# 本文件所在目录：所有数据文件路径都基于它拼绝对路径，不受启动目录影响
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _imread(path):
    """读取图片，失败时抛出带具体路径的异常。

    先试 cv2.imread；失败再用 np.fromfile + imdecode 兜底，
    兼容 cv2.imread 处理不了的路径（如含中文）。

    读不出来会短暂重试：并发场景下文件可能正被删除或还在写入
    （表现为空文件 / 半截图），一次失败不代表真的是坏图。
    """
    last_err = None
    for attempt in range(3):
        if not os.path.exists(path):
            last_err = FileNotFoundError(f"图片不存在: {path}")
        else:
            try:
                img = cv2.imread(path)
                if img is None:
                    with open(path, "rb") as f:
                        buf = np.frombuffer(f.read(), dtype=np.uint8)
                    img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
                if img is not None:
                    return img
                cause = "解码返回空"
            except Exception as e:
                # 空文件会让 imdecode 抛 cv2.error；文件被占用/删除抛 OSError
                cause = f"{type(e).__name__}: {e}"
            size = os.path.getsize(path) if os.path.exists(path) else "文件已被删除"
            last_err = ValueError(
                f"图片无法解码（可能为空/损坏/正在被写入）: {path}"
                f"（大小 {size}，第 {attempt + 1} 次尝试，原因 {cause}）"
            )
        if attempt < 2:
            time.sleep(0.15)
    raise last_err


def _match_in_region(screen, template, roi=None):
    """在 screen 图中匹配 template。

    返回 (置信度, (中心x, 中心y), (x, y, w, h))；区域比模板小则返回 None。
    """
    sh, sw = screen.shape[:2]
    th, tw = template.shape[:2]

    # 裁剪匹配区域（不传 roi 就是全图）
    if roi:
        x, y, w, h = roi
        x = max(0, min(x, sw - 1))
        y = max(0, min(y, sh - 1))
        w = min(w, sw - x)
        h = min(h, sh - y)
        region = screen[y:y + h, x:x + w]
    else:
        x = y = 0
        w, h = sw, sh
        region = screen

    if region.shape[0] < th or region.shape[1] < tw:
        return None  # 区域比模板还小，不可能匹配上

    result = cv2.matchTemplate(region, template, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(result)
    center = (x + max_loc[0] + tw // 2, y + max_loc[1] + th // 2)
    rect = (x + max_loc[0], y + max_loc[1], tw, th)
    return max_val, center, rect


def _load(screen_path, template_path):
    """读取屏幕图与模板图，失败时抛出带路径的异常。"""
    screen = _imread(screen_path)
    template = _imread(template_path)
    if template.max() == template.min():
        raise ValueError(f"模板图为纯色，无法匹配: {template_path}")
    return screen, template


def find_template(screen_path, template_path, roi=None, threshold=0.8):
    """在屏幕图中查找模板，匹配成功返回 dict，否则返回 None。

    roi 为可选 (x, y, w, h)，只在该区域匹配；不传则全图匹配。
    """
    screen, template = _load(screen_path, template_path)
    match = _match_in_region(screen, template, roi)
    if match is None or match[0] < threshold:
        return None
    confidence, center, rect = match
    return {"cx": center[0], "cy": center[1],
            "confidence": round(float(confidence), 4), "rect": rect}


def find_any_template(screen_path, template_paths, roi=None, threshold=0.8):
    """多张模板（最多 5 张），任意一张匹配成功即返回该结果。

    返回结果中额外包含 "template" 字段，指明匹配到的是哪张模板。
    """
    if len(template_paths) > 5:
        raise ValueError("template_paths 最多支持 5 张模板")
    for template_path in template_paths:
        result = find_template(screen_path, template_path, roi=roi, threshold=threshold)
        if result:
            result["template"] = template_path
            return result
    return None


def find_in_list(screen_path, template_path, list_roi, swipe_range=None,
                 max_swipes=5, capture=None, swipe_func=None, threshold=0.8,
                 max_swipes_up=0, swipe_pause_ms=800):
    """在 list_roi 区域内查找模板；找不到就滑动 + 重新截图重试。

    两个方向（以「看到列表哪一段」为准，不是手指方向）：
    - 向下看：用 swipe_range 翻 max_swipes 次，看列表更下面的内容
    - 向上看：向下看完仍找不到，再反向翻 max_swipes_up 次，退回列表更上面的内容
              （max_swipes_up=0 时关闭，行为同旧版）

    参数：
    - swipe_range    : (x1,y1,x2,y2) 向下看的手势；不传则在 list_roi 内自动生成。
                       向上看的手势由它自动反向推出，不用单独配
    - capture        : 无参函数，返回新的截图路径（滑动后重新截图用）
    - swipe_func     : 滑动函数，接收 (x1, y1, x2, y2)
    - swipe_pause_ms : 每次滑动后等待页面停稳的毫秒数
    - threshold      : 匹配阈值，默认 0.8

    命中时返回 dict 会额外带两个字段，供调用方记日志 / 决定要不要回位：
    - swipes_used    : 命中前实际滑了多少次
    - swipe_direction: "none"（首屏就有）/ "down" / "up"

    示例：
        find_in_list("screen.png", "item.png", (0, 200, 720, 600),
                     swipe_range=(687, 700, 687, 250), max_swipes=3,
                     max_swipes_up=3,
                     capture=lambda: "new.png",
                     swipe_func=lambda x1, y1, x2, y2: ctl.swipe(x1, y1, x2, y2))
    """
    if capture is None:
        # 没有 capture 就只能看眼前这一张，滑动重试毫无意义
        return find_template(screen_path, template_path, roi=list_roi, threshold=threshold)
    if (max_swipes or max_swipes_up) and swipe_func is None:
        raise ValueError("传了 max_swipes / max_swipes_up 就必须同时传 swipe_func")

    down = swipe_range or _default_swipe(list_roi)
    up = _reverse_swipe(down)
    pause = max(int(swipe_pause_ms), 0) / 1000

    result = find_template(screen_path, template_path, roi=list_roi, threshold=threshold)
    if result:
        result.update(swipes_used=0, swipe_direction="none")
        return result

    for direction, times in (("down", max_swipes), ("up", max_swipes_up)):
        rng = down if direction == "down" else up
        for i in range(1, int(times) + 1):
            swipe_func(*rng)
            time.sleep(pause)
            screen_path = capture()
            result = find_template(screen_path, template_path,
                                   roi=list_roi, threshold=threshold)
            if result:
                result.update(swipes_used=i, swipe_direction=direction)
                return result
    return None


def scroll_list_to_top(max_times, swipe_func, swipe_range=None, list_roi=None,
                       capture=None, swipe_pause_ms=800):
    """把列表滑回顶部：朝「向上看」的方向固定滑 max_times 次。

    不做任何识别判断——到顶后再滑就停住不动，不会滑过头。
    目的是让每一轮买货都从同一个画面开始，结果可复现。

    手势与 find_in_list 用同一套参数：传 swipe_range（向下看的手势），
    不传则按 list_roi 自动推；两者都不传会报错。
    多滑几次没有副作用，所以调用方一般传「向下查找的次数 + 1」。
    返回实际滑动次数。
    """
    if not swipe_range and not list_roi:
        raise ValueError("scroll_list_to_top 需要 swipe_range 或 list_roi 之一来确定滑动手势")
    up = _reverse_swipe(swipe_range or _default_swipe(list_roi))
    pause = max(int(swipe_pause_ms), 0) / 1000
    n = 0
    for _ in range(max(int(max_times), 0)):
        swipe_func(*up)
        n += 1
        time.sleep(pause)
        if capture:
            capture()
    return n


def _reverse_swipe(swipe_range):
    """把滑动手势反向：(x1,y1,x2,y2) -> (x2,y2,x1,y1)，即翻回列表的另一头。"""
    x1, y1, x2, y2 = swipe_range
    return (x2, y2, x1, y1)


def _default_swipe(list_roi):
    """默认滑动：在列表区域内从下往上滑（模拟上滑翻列表，即向下看）。"""
    x, y, w, h = list_roi
    return (x + w // 2, y + int(h * 0.8), x + w // 2, y + int(h * 0.2))


# ============================================================
# OCR 文字识别（EasyOCR）
# 惰性初始化：首次调用时才加载 Reader（会自动下载模型），
# 未安装 easyocr 时，vision.py 的模板匹配功能不受影响。
# ============================================================

OCR_REGIONS_JSON = os.path.join(BASE_DIR, "ocr_regions.json")
_READER = None  # 模块级单例：缓存的 EasyOCR Reader（首次调用时初始化）


def _get_reader():
    """返回缓存的 EasyOCR Reader 单例，首次调用时才初始化（会下载模型）。"""
    global _READER
    if _READER is None:
        import easyocr
        _READER = easyocr.Reader(["ch_sim", "en"], gpu=False)
    return _READER


def _crop_roi(img, roi):
    """裁剪 ROI 区域，返回 (子图, 偏移x, 偏移y)。roi 会钳制到图片边界内。"""
    sh, sw = img.shape[:2]
    x, y, w, h = roi
    x = max(0, min(x, sw - 1))
    y = max(0, min(y, sh - 1))
    w = min(w, sw - x)
    h = min(h, sh - y)
    return img[y:y + h, x:x + w], x, y


def _preprocess(img, scale=2, binary_method=None):
    """OCR 预处理：放大 → 灰度 → CLAHE 均衡化 →（可选）二值化，最终转回 BGR。

    binary_method 默认 None（不二值化）；显式传 "adaptive" / "otsu" 才做二值化。
    """
    if scale and scale != 1:
        img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    gray = clahe.apply(gray)
    if binary_method == "adaptive":
        gray = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                     cv2.THRESH_BINARY, 31, 15)
    elif binary_method == "otsu":
        _, gray = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)


def _poly_center(poly):
    """计算多边形/矩形四点框的中心坐标。"""
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    return (sum(xs) / len(xs), sum(ys) / len(ys))


def ocr_text(image_path, roi=None, preprocess=True, scale=2, binary_method=None):
    """识别图片中的文字，返回拼接后的字符串。

    参数：
    - roi        ：可选 (x, y, w, h)，只识别该区域
    - preprocess ：True 时放大→灰度→二值化
    - scale      ：放大倍数，默认 2
    - binary_method：二值化方式 "adaptive" / "otsu"，None 则不二值化
    """
    img = _imread(image_path)
    if roi:
        img, _, _ = _crop_roi(img, roi)
    if preprocess:
        img = _preprocess(img, scale=scale, binary_method=binary_method)
    texts = _get_reader().readtext(img, detail=0)
    return "".join(texts)


def ocr_find(image_path, keyword, roi=None, preprocess=True, scale=2, binary_method=None):
    """在 ROI 内 OCR，判断结果是否包含关键词（包含即可，不要求完全相等）。

    返回 {"found": bool, "text": 完整文字, "cx": 关键词中心x, "cy": 关键词中心y}，
    未找到时 cx/cy 为 None。cx/cy 为原始图片像素坐标（已换算回 ROI 偏移和缩放）。
    """
    img = _imread(image_path)
    ox = oy = 0
    if roi:
        img, ox, oy = _crop_roi(img, roi)
    if preprocess:
        img = _preprocess(img, scale=scale, binary_method=binary_method)
        factor = scale
    else:
        factor = 1

    # detail=1 返回 (box, text, conf)，需要 box 才能定位关键词中心
    results = _get_reader().readtext(img, detail=1)
    full_text = "".join(text for _, text, _ in results)
    for box, text, _ in results:
        if keyword in text:
            cx, cy = _poly_center(box)
            return {"found": True, "text": full_text,
                    "cx": round(ox + cx / factor), "cy": round(oy + cy / factor)}
    return {"found": False, "text": full_text, "cx": None, "cy": None}


# ============================================================
# 命名 OCR 区域管理（ocr_regions.json）
# ============================================================

def _load_ocr_regions():
    if not os.path.exists(OCR_REGIONS_JSON):
        return []
    with open(OCR_REGIONS_JSON, "r", encoding="utf-8") as f:
        return json.load(f)


def _find_region(name):
    for r in _load_ocr_regions():
        if r.get("name") == name:
            return r
    return None


def ocr_text_by_region(image_path, region_name):
    """按命名区域识别文字。区域信息（roi + 预处理参数）来自 ocr_regions.json。"""
    region = _find_region(region_name)
    if region is None:
        raise ValueError(f"未找到 OCR 区域: {region_name}")
    roi = region.get("roi")
    pp = region.get("preprocess")
    return ocr_text(
        image_path,
        roi=tuple(roi) if roi else None,
        preprocess=pp is not None,
        scale=(pp or {}).get("scale", 2),
        binary_method=(pp or {}).get("binary_method", None),
    )


def ocr_find_in_region(image_path, keyword, region_name):
    """按命名区域查找关键词，返回是否包含（同 ocr_find 的返回结构）。"""
    region = _find_region(region_name)
    if region is None:
        raise ValueError(f"未找到 OCR 区域: {region_name}")
    roi = region.get("roi")
    pp = region.get("preprocess")
    return ocr_find(
        image_path, keyword,
        roi=tuple(roi) if roi else None,
        preprocess=pp is not None,
        scale=(pp or {}).get("scale", 2),
        binary_method=(pp or {}).get("binary_method", None),
    )
