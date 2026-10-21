# WeChatAntiRecall

给 Windows 版微信 4.x 打「防撤回」补丁的小工具：**改一个字节**，让对方撤回的消息留在聊天里。

- 特征码定位，不写死偏移（微信小版本更新后偏移会变）
- 写前自动备份、写后回读校验
- 一条命令还原，图形界面 / 命令行都能用
- 纯标准库实现，不需要装任何依赖

> 仅用于学习和个人研究。改客户端违反微信用户协议，风险自负，详见文末。

## 原理

微信收到「对方撤回了一条消息」时，服务端会下发一条 `revokemsg` 系统消息，内容大致是：

```xml
<sysmsg type="revokemsg">
  <revoketime>...</revoketime>
  <session>...</session>
  <newmsgid>1234567890</newmsgid>
  <replacemsg><![CDATA["张三" 撤回了一条消息]]></replacemsg>
</sysmsg>
```

客户端解析出 `newmsgid`（要撤回的那条消息的 id）和 `replacemsg`（替换上去的提示文字），然后按 id 找到原消息，用提示文字覆盖它。

`Weixin.dll` 里解析这段 XML 的代码长这样（4.1.15.53，VA `0x18230946D`）：

```asm
182309460  mov  rcx, [rbp+0x200]      ; newmsgid 字符串
182309467  call 0x180fc2180           ; 转成整数
18230946D  mov  [rsi+0x248], rax      ; ← 存进上下文
182309474  mov  [rbp+0x208], r13      ; 接着取 replacemsg
18230947B  lea  r8, [rip+...]         ; "replacemsg"
```

把这条 `mov` 换成 `sub`：

```asm
18230946D  sub  [rsi+0x248], rax
```

字段里就不再是真实的 newmsgid，而是一个无意义的值。后续「按 id 找原消息 → 用提示覆盖」的步骤匹配不到目标，于是**原消息原样保留，也不会出现撤回提示**。

指令长度不变（都是 7 字节），不会破坏后面的指令边界，这是它安全的原因。

特征码（`??` 为通配，躲开随版本变化的 disp32）：

```
原始: 48 89 86 ?? ?? ?? ??  4C 89 AD 08 02 00 00  4C 8D 05
补丁: 48 29 86 ?? ?? ?? ??  4C 89 AD 08 02 00 00  4C 8D 05
```

## 支持版本

| 微信版本 | 状态 |
|---|---|
| 4.1.12.0 及以上 | 支持（特征码来自公开补丁库，已验证 4.1.15.53） |
| 4.1.12.0 以下 | 未适配（解析逻辑不同，需要另找特征码） |

微信更新后如果提示「特征命中异常」，说明代码变了，需要在 `wxar/patterns.json` 里补一条新记录——不用改代码。

## 使用

### 图形界面

```powershell
python -m wxar gui
```

或者直接双击 `run_gui.bat`。列表里会显示版本和当前状态（未打补丁 / 已打补丁 / 未知），点「打补丁」即可；改 `Program Files` 会弹一次 UAC。

### 命令行

```powershell
python -m wxar list            # 列出所有安装及状态
python -m wxar status          # 看状态
python -m wxar patch --dry-run # 只看会改哪里，不写文件
python -m wxar patch           # 打补丁（自动提权）
python -m wxar restore         # 用最近的备份还原
```

输出示例：

```
[1] 4.1.15.53
  版本    : 4.1.15.53
  文件    : C:\Program Files\Tencent\Weixin\4.1.15.53\Weixin.dll
  大小    : 194.8 MB
  状态    : 未打补丁  偏移 0x0230886D
```

打补丁：

```
==> 4.1.15.53  C:\Program Files\Tencent\Weixin\4.1.15.53\Weixin.dll
  已写入，改动字节 1 个（偏移 0x0230886D 起）
  备份：Weixin.dll.bak-20261021-123929
  校验：通过（88eabf78f961… -> 2e010bb7e758…）
  重新打开微信即可生效。
```

多个安装（比如测试版和正式版并存）时用 `--wechat-version 4.1.15` 指定，或 `patch --all`。

### 打包成 exe

```powershell
powershell -ExecutionPolicy Bypass -File .\build.ps1
```

产物在 `dist\WeChatAntiRecall.exe`（单文件，无需 Python 环境）。

## 打补丁前后

| | 原版 | 打完 |
|---|---|---|
| 对方撤回后原消息 | 被删除 | **保留** |
| 「××撤回了一条消息」提示 | 显示 | 不显示 |

`replacemsg` 就是那条提示，它和「覆盖原消息」是同一个动作，所以保留消息的同时提示也会消失。这是当前 4.x 补丁的固有形态。

## 备份与还原

每次打补丁都会生成 `Weixin.dll.bak-<时间戳>`，和 DLL 放在同一目录。`restore` 默认取最新的一个：

```powershell
python -m wxar restore --backup "C:\...\Weixin.dll.bak-20261021-123929"
```

也可以手动还原（管理员 PowerShell）：

```powershell
Copy-Item "C:\Program Files\Tencent\Weixin\4.1.15.53\Weixin.dll.bak-20261021-123929" `
          "C:\Program Files\Tencent\Weixin\4.1.15.53\Weixin.dll" -Force
```

## 常见问题

**打补丁报「微信正在运行」**：从托盘图标右键退出，任务管理器里确认没有 `Weixin.exe` 残留。

**打完微信闪退或聊天异常**：立刻 `python -m wxar restore`，或按上面的手动方式还原。

**微信自动更新后失效**：更新会覆盖 `Weixin.dll`，补丁自然消失，重新打一次即可；如果新版本特征码变了，工具会报「特征命中异常」而不是乱改。

**安全软件报警**：改的是已签名的系统组件，签名校验必然失败，报毒/拦截属正常现象，自行判断是否放行。

## 风险

- **违反用户协议**：修改客户端属于腾讯明确禁止的行为，理论上存在账号风险（目前没有公开的封号案例，但请自行权衡）。
- **不是官方功能**：消息保留只在本地，换设备/清缓存后不会保留；对方那边照常显示「已撤回」。
- **签名失效**：DLL 的 Authenticode 签名会失效，微信目前不做启动校验，但未来版本可能会加。
- **更新即失效**：每次微信升级都要重打。

## 致谢

特征码的定位思路来自 [huiyadanli/RevokeMsgPatcher](https://github.com/huiyadanli/RevokeMsgPatcher)（GPL-3.0）的公开补丁库，本项目的代码与它无关，只是沿用了同一处字节特征。

## 许可

MIT
