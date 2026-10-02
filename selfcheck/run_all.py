"""跑全部 6 个离线自检，打印每个的 PASS/FAIL 计数和退出码。

为什么要连跑 3 轮：断言里有人手随机落点（模板矩形内随机取点），
单轮通过不代表稳定 —— 这是之前定下的规矩。
"""
import os
import re
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 项目根 = selfcheck/ 的上一级（换目录/换机器都不用改）
SCRIPTS = ["uwo_route_format_selfcheck.py", "uwo_module_selfcheck.py",
           "uwo_restock_selfcheck.py", "uwo_run_state_selfcheck.py",
           "uwo_trip_selfcheck.py", "uwo_queue_selfcheck.py"]
ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 else 1

total_fail = 0
for rnd in range(1, ROUNDS + 1):
    print("#" * 70)
    print(f"# 第 {rnd} 轮")
    for name in SCRIPTS:
        path = os.path.join(BASE, "selfcheck", name)
        p = subprocess.run([sys.executable, "-X", "utf8", path],
                           capture_output=True, text=True, encoding="utf-8", cwd=BASE)
        out = p.stdout or ""
        npass = len(re.findall(r"^\s*PASS", out, re.M))
        nfail = len(re.findall(r"^\s*FAIL", out, re.M))
        bad = [l.strip() for l in out.splitlines() if l.strip().startswith("FAIL")]
        total_fail += nfail + (0 if p.returncode in (0,) else 1)
        print(f"  {name:<36} rc={p.returncode} PASS={npass} FAIL={nfail}")
        for b in bad[:12]:
            print("      ", b[:150])
        if p.returncode and not bad:
            print("      stderr:", (p.stderr or "")[-300:])
print("#" * 70)
print("三轮累计失败 =", total_fail)
sys.exit(1 if total_fail else 0)
