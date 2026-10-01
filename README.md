# UWOAS — 大航海「跑商」自动化调试台

一个跑在 **MuMu 模拟器**上的《大航海》辅助脚本：用**模板匹配 + OCR** 认游戏画面，
用一个**状态机**把「进港 → 买货 → 中转 → 出港 → 卖货」串成完整一趟，
再配一个本地网页界面用来框素材、编流程、排队列、看日志。

版本：见 [VERSION](VERSION)（当前 `0.1.0`）

---

## 一、环境要求

| 项目 | 要求 |
| --- | --- |
| 操作系统 | Windows |
| Python | 3.10+（本机实测 3.14） |
| 模拟器 | MuMu Player 12，ADB 端口 `16384` |
| 依赖 | 见 [requirements.txt](requirements.txt) |

## 二、安装

```bash
pip install -r requirements.txt
```

> EasyOCR 会自动带上 torch / Pillow 等，首次安装包比较大；
> 首次识别中文时还会联网下载模型，之后走本地缓存。

## 三、启动

双击 **`start.bat`**（等价于 `python start.py`），浏览器会自动打开
<http://127.0.0.1:8000>。**关闭那个 shell 窗口即停止服务。**

也可以直接：

```bash
python app.py
```

只想用命令行测识别，不启网页：

```bash
python cli.py        # 模板匹配
python ocr_cli.py    # OCR
```

## 四、跑之前要改的两处

1. **模拟器 adb.exe 路径** —— [app.py](app.py) 第 47 行的 `ADB_PATH`，
   默认是 `C:\Program Files\Netease\MuMu Player 12\shell\adb.exe`。
   MuMu 装在别的盘 / 别的目录时，把这行改成你自己的路径。
2. **adb 端口** —— 同一文件第 48 行 `ADB_PORT`，默认 `16384`。
   可以在「设置」栏看到当前程序实际用的路径和端口。

## 五、界面一栏一句话

| 栏 | 干什么 |
| --- | --- |
| **运行** | 选方案、排队列、启动、看日志和整趟进度 |
| **素材** | 模板库（框选识别用的图）+ OCR 区域库 |
| **调试** | 单步试一次识别 / 单步跑一个模块 |
| **流程** | 状态机编辑器（states.json） |
| **表格** | 跑商购物清单 = 纯目录：哪个港口买得到哪些货 |
| **跑商设置** | 编「这一趟怎么走」：买货站 → 中转站 → 出货站 |
| **设置** | 引擎参数 + 设备信息 |

表格栏是三级界面：一级只列港口名（可搜索、按拼音排）→ 点一个港口进二级看这个港有哪些货
→ 再点一件货进三级编辑（货物 / 类别 / 备注）。

## 六、自检（改完代码先跑这个）

```bash
python selfcheck\run_all.py        # 默认 1 轮
python selfcheck\run_all.py 3      # 断言里有随机落点，按老规矩连跑 3 轮
```

5 个离线自检脚本全部**不碰 ADB、不真点画面**，跑在假控制器上：

| 脚本 | 管什么 |
| --- | --- |
| `uwo_route_format_selfcheck.py` | 路线格式 normalize / derive / 落盘读回 |
| `uwo_module_selfcheck.py` | 状态机各动作、单模块跑法 |
| `uwo_restock_selfcheck.py` | 补货倒计时读数层（含一张钉在库里的真图） |
| `uwo_run_state_selfcheck.py` | 运行状态 / 待卖账 |
| `uwo_trip_selfcheck.py` | 完整一趟：站次绑定、逐站预检、前端契约 |

全部通过时退出码为 0，最后打印 `失败 0 项`。

## 七、目录结构

```
UWOAS_V0.1/
├─ app.py                 FastAPI 后端 + 所有 HTTP 接口
├─ state_machine.py       状态机引擎（动作执行、整趟接力）
├─ vision.py              图像识别核心：模板匹配 + OCR
├─ mumu_controller.py     模拟器控制（截图 / 点击 / 输入）
├─ route_plan.py          路线规划（站次、方案）
├─ route_presets.py       方案库
├─ run_queue.py           队列执行（once / repeat / 无限）
├─ run_state.py           运行状态、待卖账
├─ restock.py             补货倒计时读数
├─ purchase_plan.py       购物清单（纯目录）
├─ cli.py / ocr_cli.py    命令行测试入口
├─ start.py / start.bat   一键启动
├─ frontend/              原生 HTML / JS / CSS（无构建步骤）
├─ selfcheck/             离线自检套件
│  └─ fixtures/           自检用的固定测试图
├─ templates/             模板图（识别素材）
├─ templates.json         模板库登记表
├─ ocr_regions.json       OCR 区域登记表
├─ states.json            状态机配置
├─ purchase_plan.default.json    购物清单模板（入库，只作示例）
├─ route_plan.default.json       当前路线模板（入库）
├─ route_presets.default.json    方案库模板（入库）
├─ run_queue.default.json        队列配置模板（入库）
├─ purchase_plan.json     你自己的购物清单（本地文件，见下节，不进库）
├─ route_plan.json        你自己的当前路线（本地文件）
├─ route_presets.json     你自己的方案库（本地文件）
├─ run_queue.json         你自己的队列（本地文件）
├─ 跑商一趟操作流程.txt    操作流程说明
└─ 跑商流程.txt
```

### 你的个人跑商数据（不进版本库）

上面不带 `.default` 的那四个 `*.json` 存的是**每个人自己的**跑商数据（购物清单、路线、方案库、队列），
它们已写进 [.gitignore](.gitignore)，**不随版本库分发** —— 这样你更新代码 / 别人拉取更新时，
各自配好的方案不会被覆盖，也不会再出现 `git pull` 冲突。

**第一次用不用手动建文件**：读不到就当空表处理（`load_plan()` / `load_route()` /
`load_presets()` / `load_queue()` 都有这段兜底），在界面上配一遍自动就写出来了。
想从示例起步，把对应的 `*.default.json` 复制一份、去掉 `.default` 即可：

```bash
copy purchase_plan.default.json purchase_plan.json
```

其余三个同理（`route_plan` / `route_presets` / `run_queue`）。

## 八、已知限制

- 只针对**当前这一版游戏画面**做的识别判据；游戏改版后模板和 OCR 区域都要重框。
- 需要模拟器在运行且 ADB 连得上，否则截图类接口会返回 503。
- 前端是原生 JS，改完 **Ctrl+F5 硬刷新**即可生效，后端不用重启；
  改 `states.json` / `ocr_regions.json` 则会自动重新加载。