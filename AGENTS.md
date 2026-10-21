# AGENTS.md

给接手这个仓库的人或 agent 的速查表。修改前请先读完本文件。

## 这个项目是什么

给 Windows 版微信 4.x 打「防撤回」补丁：把 `revokemsg` 解析流程里
`mov [rsi+0x248], rax`（写入撤回目标 id）改成 `sub [rsi+0x248], rax`，
让后续按 id 查找原消息失败，原消息因此保留。改动 **1 个字节**，指令长度不变。

真正的知识在 [`docs/ADAPTING.md`](docs/ADAPTING.md)，升级适配必读。

## 目录

```
wxar/core.py        定位/备份/写入/校验/还原，不含 UI，不提权
wxar/cli.py         命令行：list/status/patch/restore/gui
wxar/gui.py         Tkinter 三步式界面（定位 → 信息 → 确认执行）
wxar/patterns.json  特征库：版本区间 + search/replace 字节串，?? 为通配
tools/relocate.py   微信升级后重新定位补丁点（维护者工具）
docs/ADAPTING.md    适配手册（原理、手工定位、验证清单、禁忌）
docs/HISTORY.md     适配台账：每次适配的版本/偏移/漂移/验证
tests/              单元测试，纯逻辑，不需要真实 DLL
build.ps1           PyInstaller 打包成两个单文件 exe
```

## 常用命令

```powershell
python -m unittest discover -s tests -v     # 测试
python -m wxar status                       # 看状态
python -m wxar gui                          # 图形界面
python tools/relocate.py --dll <副本>        # 重新定位补丁点
powershell -ExecutionPolicy Bypass -File .\build.ps1   # 打包

# 打包产物自检（无人值守/锁屏环境可用）
$env:WXAR_SELFTEST_OUT="$env:TEMP\st.txt"; .\dist\WeChatAntiRecall-GUI.exe --selftest
```

## 硬性约束

1. **不硬编码偏移**，一切通过 `patterns.json` 的特征码定位。
2. **命中必须唯一**。0 次或多次都中止，不允许“猜一个”。
3. **改后指令长度必须不变**（7 字节），只允许改 opcode 一个字节。
4. **写前备份、写后回读校验**，任何一步失败都要能用 `.bak-*` 还原。
5. **先在副本上验证**（`--file` 指向临时拷贝），再动真机。
6. **微信运行时不改文件**（`Weixin.exe` 会锁 DLL）。
7. **不提交** `dist/`、`build/`、`*.bak*`、真实 DLL 或用户数据。

## 微信升级了怎么办

读 [`docs/ADAPTING.md`](docs/ADAPTING.md)，适配完在 [`docs/HISTORY.md`](docs/HISTORY.md) 追加一行台账。要点：

1. 先翻 `docs/HISTORY.md`，看同类漂移以前怎么处理的
2. `python tools/relocate.py --dll <新版本 DLL 副本>`
3. 精确命中 → 无需适配；模糊命中且漂移只在非修改位 → 采纳工具输出的条目
4. 修改位漂移 / 多候选歧义 → 按文档手工确认，**不要**照抄旧字节
5. 新条目追加进 `patterns.json`，用 `min_version` / `max_version` 分段，不改旧条目
6. 跑测试 + 副本全流程 + 真机验收，最后填 `verified`
7. 在 `docs/HISTORY.md` 记账：日期、版本、原始 SHA256、偏移、指令、漂移、验证程度

## 提交约定

- 提交信息用中文，格式 `fix(scope): 摘要` / `feat(scope): 摘要`
- 涉及特征库变更时，提交说明里写清楚：适配的微信版本、补丁点偏移、验证方式
- 改动 `core.py` 的写入逻辑时，必须同时更新 `tests/`
