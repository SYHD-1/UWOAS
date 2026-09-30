"""MuMu 模拟器命令行测试工具。

用法示例：
    python cli.py connect
    python cli.py screenshot -o shot.png
    python cli.py click -x 100 -y 200
    python cli.py swipe --x1 100 --y1 200 --x2 300 --y2 400
    python cli.py input -t "hello"
    python cli.py install -p ADBKeyboard.apk   # 安装 ADBKeyboard
    python cli.py ime             # 切换输入法为 ADBKeyboard（输入中文前）
    python cli.py input -t "你好世界"
    python cli.py test            # 连接后截图并点击屏幕中心
    python cli.py find --screen screen.png --template template.png --roi 100,200,400,300
    python cli.py ocr --image screen.png --roi 100,30,400,60
    python cli.py ocr --image screen.png --roi 100,30,400,60 --keyword "里斯本"
"""

import argparse
import sys

from mumu_controller import MuMuController
from vision import (find_template, ocr_find, ocr_find_in_region, ocr_text,
                    ocr_text_by_region)


def parse_args():
    parser = argparse.ArgumentParser(description="MuMu 模拟器控制工具")
    parser.add_argument("--host", default="127.0.0.1", help="MuMu 地址，默认 127.0.0.1")
    parser.add_argument("--port", type=int, default=16384, help="MuMu 端口，默认 16384")
    parser.add_argument("--adb", default=r"C:\Program Files\Netease\MuMu Player 12\shell\adb.exe", help="adb 可执行文件路径")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("connect", help="连接模拟器")

    p = sub.add_parser("screenshot", help="截图保存到本地")
    p.add_argument("-o", "--output", default="screenshot.png", help="保存路径")

    p = sub.add_parser("click", help="点击坐标")
    p.add_argument("-x", type=int, required=True)
    p.add_argument("-y", type=int, required=True)

    p = sub.add_parser("swipe", help="滑动")
    p.add_argument("--x1", type=int, required=True)
    p.add_argument("--y1", type=int, required=True)
    p.add_argument("--x2", type=int, required=True)
    p.add_argument("--y2", type=int, required=True)
    p.add_argument("--duration", type=int, default=300, help="滑动时长(毫秒)")

    p = sub.add_parser("input", help="输入文本")
    p.add_argument("-t", "--text", required=True)

    p = sub.add_parser("install", help="安装本地 apk 到模拟器")
    p.add_argument("-p", "--path", required=True, help="apk 文件路径")

    sub.add_parser("ime", help="切换输入法为 ADBKeyboard")

    p = sub.add_parser("find", help="模板匹配查找")
    p.add_argument("--screen", required=True, help="屏幕截图路径")
    p.add_argument("--template", required=True, help="模板图片路径")
    p.add_argument("--roi", default=None, help="匹配区域 x,y,w,h，例如 100,200,400,300")
    p.add_argument("--threshold", type=float, default=0.8, help="置信度阈值，默认 0.8")

    sub.add_parser("test", help="连接后截图并点击屏幕中心")

    p = sub.add_parser("ocr", help="OCR 文字识别")
    p.add_argument("--image", required=True, help="图片路径")
    p.add_argument("--roi", default=None, help="识别区域 x,y,w,h，例如 100,50,300,60")
    p.add_argument("--region", default=None, help="按命名区域识别（来自 ocr_regions.json）")
    p.add_argument("--keyword", default=None, help="查找关键词（可选，传入则用 ocr_find）")

    return parser.parse_args()


def main():
    args = parse_args()
    ctl = MuMuController(args.host, args.port, args.adb)

    if args.command == "connect":
        ok, msg = ctl.connect()
        print(msg if msg else "连接成功" if ok else "连接失败")

    elif args.command == "screenshot":
        if ctl.screenshot(args.output):
            print(f"截图已保存: {args.output}")
        else:
            print("截图失败")
            sys.exit(1)

    elif args.command == "click":
        ctl.click(args.x, args.y)
        print(f"已点击: ({args.x}, {args.y})")

    elif args.command == "swipe":
        ctl.swipe(args.x1, args.y1, args.x2, args.y2, args.duration)
        print(f"已滑动: ({args.x1},{args.y1}) -> ({args.x2},{args.y2})")

    elif args.command == "input":
        ctl.input_text(args.text)
        print(f"已输入: {args.text}")

    elif args.command == "install":
        ok, msg = ctl.install_apk(args.path)
        print(msg if msg else ("安装成功" if ok else "安装失败"))
        sys.exit(0 if ok else 1)

    elif args.command == "ime":
        if ctl.is_adbkeyboard_installed():
            ctl.set_adbkeyboard()
            print("ADBKeyboard 已安装，已切换输入法")
        else:
            print("ADBKeyboard 未安装，请先执行:")
            print("  python cli.py install -p <ADBKeyboard.apk 路径>")
            sys.exit(1)

    elif args.command == "find":
        roi = tuple(int(v) for v in args.roi.split(",")) if args.roi else None
        result = find_template(args.screen, args.template, roi=roi, threshold=args.threshold)
        if result:
            print(f"找到: 中心=({result['cx']}, {result['cy']}) "
                  f"置信度={result['confidence']} 矩形={result['rect']}")
        else:
            print("未找到")
            sys.exit(1)

    elif args.command == "test":
        ok, msg = ctl.connect()
        print(msg if msg else "连接成功")
        if ctl.screenshot("test_screenshot.png"):
            print("截图已保存: test_screenshot.png")
        # 点击屏幕中心 (720x1280 分辨率下的中心点，可按实际分辨率调整)
        ctl.click(360, 640)
        print("已点击屏幕中心 (360, 640)")

    elif args.command == "ocr":
        roi = tuple(int(v) for v in args.roi.split(",")) if args.roi else None
        if args.keyword:
            if args.region:
                result = ocr_find_in_region(args.image, args.keyword, args.region)
            else:
                result = ocr_find(args.image, args.keyword, roi=roi)
            if result["found"]:
                print(f"找到关键词「{args.keyword}」: 中心=({result['cx']}, {result['cy']})")
                print(f"完整文字: {result['text']}")
            else:
                print(f"未找到关键词「{args.keyword}」")
                print(f"完整文字: {result['text']}")
                sys.exit(1)
        else:
            if args.region:
                text = ocr_text_by_region(args.image, args.region)
            else:
                text = ocr_text(args.image, roi=roi)
            print(f"识别结果: {text}")


if __name__ == "__main__":
    main()
