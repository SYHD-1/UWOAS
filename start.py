"""一键启动 MuMu 图像识别调试台：启动后端服务并自动打开浏览器。"""

import socket
import sys
import threading
import time
import webbrowser

URL = "http://127.0.0.1:8000"
PORT = 8000


def port_in_use(port, host="127.0.0.1"):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex((host, port)) == 0


def open_browser():
    time.sleep(2)  # 等后端服务起来
    webbrowser.open(URL)


if __name__ == "__main__":
    if port_in_use(PORT):
        print(f"端口 {PORT} 已被占用：调试台可能已经在运行，或上次启动未正常关闭。")
        print(f"请先关闭旧的调试台窗口，再重新启动；或直接打开 {URL} 使用。")
        sys.exit(1)
    print("正在启动 MuMu 图像识别调试台，浏览器将自动打开…")
    threading.Thread(target=open_browser, daemon=True).start()
    import app  # 复用 app.py 的启动逻辑；关闭 shell 窗口即停止服务
    app.run_server()
