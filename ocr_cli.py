"""OCR 命令行客户端（走常驻服务 app.py，模型只加载一次）。

前提：先在另一个终端启动服务  python app.py
然后本脚本每次调用只做一次 HTTP 请求，无需重复加载 EasyOCR 模型。

用法：
    python ocr_cli.py --roi 77,4,258,76
    python ocr_cli.py --roi 77,4,258,76 --keyword 交易所
    python ocr_cli.py --roi 77,4,258,76 --scale 3
    python ocr_cli.py --roi 77,4,258,76 --binary-method otsu
    python ocr_cli.py                  # 不传 roi 则识别全屏
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8000"
# 首次调用服务端要加载模型，给足超时；后续调用秒回
REQUEST_TIMEOUT = 180


def parse_args():
    p = argparse.ArgumentParser(description="OCR 常驻服务命令行客户端")
    p.add_argument("--roi", default=None,
                   help="识别区域 x,y,w,h，例如 77,4,258,76；不传则全屏")
    p.add_argument("--scale", type=int, default=2, help="放大倍数，默认 2")
    p.add_argument("--binary-method", choices=["none", "adaptive", "otsu"],
                   default="none", help="二值化方式，默认 none（只做 CLAHE，不二值化）")
    p.add_argument("--keyword", default=None, help="查找关键词（包含匹配），不传则输出整段文字")
    p.add_argument("--url", default=DEFAULT_URL, help=f"服务地址，默认 {DEFAULT_URL}")
    return p.parse_args()


def call_server(url, payload):
    """向 /api/ocr/test 发 POST 请求，返回解析后的 JSON dict。"""
    req = urllib.request.Request(
        url.rstrip("/") + "/api/ocr/test",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    args = parse_args()
    roi = [int(v) for v in args.roi.split(",")] if args.roi else None
    payload = {
        "roi": roi,
        "scale": args.scale,
        "binary_method": None if args.binary_method == "none" else args.binary_method,
        "keyword": args.keyword,
    }

    started = time.time()
    try:
        result = call_server(args.url, payload)
    except urllib.error.HTTPError as e:
        # 服务端业务错误（503 未连接 / 500 OCR 失败），把 detail 原样打出来
        try:
            detail = json.loads(e.read().decode("utf-8")).get("detail", "")
        except Exception:
            detail = ""
        print(f"服务返回错误 HTTP {e.code}: {detail}")
        sys.exit(1)
    except urllib.error.URLError:
        print(f"无法连接 OCR 服务（{args.url}），请先在另一个终端启动：python app.py")
        sys.exit(2)

    elapsed = time.time() - started
    print(f"耗时: {elapsed:.2f}s")
    if result.get("mode") == "find":
        if result["found"]:
            print(f"找到关键词「{args.keyword}」: 中心=({result['cx']}, {result['cy']})")
        else:
            print(f"未找到关键词「{args.keyword}」")
        print(f"完整文字: {result['text']}")
        sys.exit(0 if result["found"] else 1)
    print(f"识别结果: {result.get('text', '')}")


if __name__ == "__main__":
    main()
