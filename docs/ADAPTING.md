# 微信升级后怎么适配

微信每次发版都会重新编译 `Weixin.dll`，补丁点附近的指令和立即数可能变化，特征码随之失效。
这份文档的目标：**任何人（或任何 agent）照着做都能重新定位补丁点**，不需要原作者在场。

---

## 0. 先判断是不是真的要适配

症状：

```powershell
python -m wxar status
#   状态    : 无法识别
#   说明    : 特征命中异常（原始 0 次 / 补丁 0 次），可能是新版本
```

- **原始 1 次 / 补丁 0 次** → 文件是干净的，直接 `patch` 即可，不用适配
- **原始 0 次 / 补丁 1 次** → 已经打过补丁，不用动
- **两边都是 0 次** → 真的变了，继续往下看

---

## 1. 补丁到底在做什么（先理解，再动手）

服务端下发 `revokemsg` 系统消息，客户端解析出 `newmsgid`（撤回目标消息 id）和 `replacemsg`（提示文字），
然后按 id 找到原消息、用提示文字覆盖它。

补丁做的事：把「把 newmsgid 存进上下文」的那条 `mov` 改成 `sub`，让这个 id 变成脏值，
后续按 id 查原消息就查不到，原消息得以保留。

对应 4.1.15.53 的代码：

```asm
182309460  mov  rcx, [rbp+0x200]     ; newmsgid 字符串
182309467  call 0x180fc2180          ; atoll
18230946C  nop
18230946D  mov  [rsi+0x248], rax     ; ← 补丁点
182309474  mov  [rbp+0x208], r13
18230947B  lea  r8, [rip+...]        ; "replacemsg"
```

### 三条硬性不变量

1. **指令长度必须保持 7 字节**。`mov`（`48 89 86`+disp32）和 `sub`（`48 29 86`+disp32）同为 7 字节，
   只改 opcode 一个字节。长度变了，后面的指令全部错位，微信必然崩溃。
2. **改的必须是“写入撤回目标 id”这一条**。改错成别的 mov，轻则无效，重则写坏别的字段。
3. **modrm 和 disp32 不能动**。它们描述“写到哪里”，改了就不是原字段了。

> 所以适配的本质只有一件事：**在新版本里重新找到这条 7 字节的 `mov [reg+disp32], rax`**，
> 再确认它 `48 89 86` 里的 `89` 可以安全换成 `29`。

---

## 2. 快速路径：跑重定位工具

```powershell
# 建议先复制一份副本再分析，避免误改
python tools/relocate.py --dll "C:\Program Files\Tencent\Weixin\<版本>\Weixin.dll"
```

工具按可靠性从高到低逐级降级，四种结论：

| 输出 | 含义 | 下一步 |
|---|---|---|
| `精确匹配 search : 1 处` | 特征码仍有效 | 直接 `patch`，不用适配 |
| `精确匹配 replace: 1 处` | 这份已经打过补丁 | 用未打补丁的原始 DLL 复核 |
| `模糊匹配 : 1 处，漂移 N 字节` | 定位到了，但有个别字节变了 | 看漂移位置，按下面规则处理 |
| `⚠ 拒绝自动生成特征码` | 漂移落在**修改位**上 | 走第 3 节手工路径 |
| `⚠ 有 N 个同等匹配度的候选` | 有歧义 | `--pick 0x<偏移>` 指定，或手工确认 |
| `未自动定位到补丁点` + 字符串锚定候选 | 自动失败 | 按候选顺序人工比照第 3 节 |

### 漂移怎么处理

- **漂移在 `??` 区间**（disp32）：本来就不参与匹配，忽略即可
- **漂移在非修改位**（如 `4C 89 AD 08 02 00 00` 里的立即数）：工具会把该字节在 search / replace
  两边同时放开成 `??`，可以直接用
- **漂移在修改位**（`89` 这个 opcode）：**拒绝自动生成**。说明指令编码整体变了，
  照抄 `29` 可能写出完全不同的指令。必须人工确认新指令语义

---

## 3. 手工路径（工具给不出结论时）

### Step 1：找锚点字符串

`Weixin.dll` 里稳定存在这几个字符串，它们是 `revokemsg` 解析流程的标签：

```
revoketime
session
newmsgid
replacemsg
sysmsgtemplate
revokehistoryinjoinroommsg
mmchatroomtopmsg
```

在十六进制编辑器（010 Editor / HxD）里搜 `replacemsg`，记下它的 **VA**（不是文件偏移）。

### Step 2：找引用它的代码

在 `.text` 段里搜 `lea r8, [rip+X]` 且 `X` 指向这个字符串的指令。工具已经帮你列出来了：

```
字符串 replacemsg   @ VA 0x18942CF70，被引用 2 次
   引用点 0x1823090FB（文件 0x23084fb） 附近 mov [reg+disp], r64 共 3 处
   引用点 0x18230947B（文件 0x230887b） 附近 mov [reg+disp], r64 共 4 处
```

真正的位置在**最后一个**引用点附近（解析顺序是 revoketime → session → newmsgid → replacemsg）。

### Step 3：认形态

解析函数是重复的“取字段”序列：

```asm
mov  [rbp+0xXXX], r13          ; 初始化一个临时 string
lea  r8, [rip+...]             ; 字段名，如 "newmsgid"
lea  rcx, [rbp+0x...]
lea  rdx, [rbp+0x...]
call 取子节点                  ; 通常是 0x1806683d0
lea  rcx, [rbp+0x...]
call 判断非空                  ; 通常是 0x180222980
test al, al
jne  跳过
mov  rcx, [rbp+0x...]          ; 取字符串
call atoll                     ; 转数字
mov  [rsi+0x248], rax          ; ← 我们要的那条
```

### Step 4：定位那条 mov

它有三个特征，用来排除其它候选：

1. 紧跟在把 `newmsgid` 字符串转数字的 `call` 之后（中间通常有个 `nop`）
2. 后面紧跟 `mov [rbp+0x208], r13` + `lea r8, "replacemsg"`
3. 操作数是 `[rsi + disp32]`，`disp32` 是上下文结构里存 id 的字段

### Step 5：人工确认

把候选和下面的模板逐条比对，**全部吻合**才写进特征库：

```asm
mov  rcx, [rbp+0x200]
call <atoll>
nop
mov  [rsi+0x248], rax      ← 候选，必须是 48 89 86 xx xx xx xx
mov  [rbp+0x208], r13
lea  r8, [rip+...]         ; "replacemsg"
```

---

## 4. 写进特征库

改 `wxar/patterns.json`，**新增一个条目，不要改旧的**（旧版本用户还要能用）：

```json
{
  "id": "antirecall",
  "name": "防撤回",
  "min_version": "4.2.0.0",
  "max_version": "",
  "search":  "48 89 86 ?? ?? ?? ?? 4C 89 AD 08 02 00 00 4C 8D 05",
  "replace": "48 29 86 ?? ?? ?? ?? 4C 89 AD 08 02 00 00 4C 8D 05",
  "note": "……",
  "verified": ["4.2.0.0"]
}
```

规则：

- `??` 表示通配。**只固定跨版本稳定的字节**；disp32、立即数一律放开
- 上一段的 `max_version` 要等于新条目的 `min_version`，避免区间重叠
- `verified` 填你实测过的版本号
- 条目顺序即优先级，`pick_patch` 取第一个命中的

---

## 5. 验证清单（缺一不可）

```powershell
# 1) 特征码唯一性
python tools/relocate.py --dll "<副本>"

# 2) 在副本上走一遍完整流程（不碰真机）
python -m wxar patch   --file "<副本>" --yes --no-elevate
python -m wxar status  --file "<副本>"          # 应显示 已打补丁
python -m wxar restore --file "<副本>" --yes --no-elevate
Get-FileHash "<副本>" -Algorithm SHA256        # 应回到原始哈希

# 3) 单元测试
python -m unittest discover -s tests -v
```

4. **真机验收**：退出微信 → `python -m wxar patch` → 重开微信 → 自己发一条消息再撤回 →
   原消息应保留；再随便聊几句确认不闪退
5. 如果异常，立刻 `python -m wxar restore`

---

## 6. 历史记录

补丁点、漂移情况、验证程度都记在 **[HISTORY.md](HISTORY.md)** 的台账里。
适配完成后**必须追加一行**——下次遇到同类漂移，先翻台账比翻代码快得多。

> 偏移仅作参考，**永远靠特征码定位**，不要写死在代码里。

---

## 7. 禁忌

- ❌ 把偏移硬编码进代码（微信一更新就废）
- ❌ 只找到一个候选就直接写库，不验证唯一性
- ❌ 修改位漂移了还照抄 `29`
- ❌ 微信运行时改文件（`Weixin.exe` 会锁住 DLL）
- ❌ 改完不做副本验证，直接上真机
- ❌ 删掉或覆盖旧的 `.bak-*` 备份

---

## 8. 给接手的 agent

见仓库根目录的 [`AGENTS.md`](../AGENTS.md)：里面有项目结构、命令、硬性约束和提交流程。
