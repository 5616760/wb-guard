# WorkBuddy 断流守护（wb-guard）

自定义模型不稳定时，WorkBuddy 经常在跑到一半被上游掐断（503、stream read error、长时间无响应），
得手动敲一句「继续」才能接着干。这个小工具盯着会话记录，一旦判定断了，自动切到那个任务、
把「继续」发进去。

纯 Python 标准库 + Win32 API，**不需要 pip 安装任何东西**，不改动 WorkBuddy 任何文件。

---

## 一、怎么判断"断了"

不猜日志，直接读 WorkBuddy 自己写的会话记录：

```
%USERPROFILE%\.workbuddy\projects\<项目目录>\<sessionId>.jsonl
```

每条事件都是 JSON，关键在最后一条有效事件：

| 最后一条事件 | 判定 | 动作 |
|---|---|---|
| assistant 消息 `status=incomplete` 且 `providerData.error` 存在 | **中断**（置信度最高） | 立刻续跑 |
| assistant 消息 `status=completed`，但正文是错误提示（"服务暂时不可用"等） | **中断** | 立刻续跑 |
| `function_call`（等工具返回）超过 `tool_stall_timeout`（默认 900s） | 卡死 | 续跑 |
| `reasoning` / `function_call_result` 超过 `stall_timeout`（默认 420s） | 卡死 | 续跑 |
| `user` 消息超过 `first_token_timeout`（默认 180s）没响应 | 卡死 | 续跑 |
| 其它（刚发出、正在跑） | 运行中 | 不动 |

真实中断记录长这样：

```json
{"type":"message","role":"assistant","status":"incomplete",
 "providerData":{"error":{"code":0,"status":503,
                          "message":"503 当前共享版请求较多、算力不足，请稍后重试。"}}}
```

> **踩过的坑**：`error.code` 有时是占位符 `0`，真实码在 `error.status`（503/404）或 message 文本里。
> 所以判定顺序是 `code` → `logStatus` → `status` → 从 message 文本反推，
> 并且**正文本身是错误提示也要当中断**——界面同样卡住，只是 `status` 写的是 `completed`。

## 二、多任务：一个任务断了，就切过去救它

WorkBuddy 可以同时开多个任务，**每个任务一个 jsonl**。早期版本只盯"最近写入的那个"，
别的任务断了根本看不见——这就是"有任务停了却没监控到"的根因。

现在每轮轮询会：

1. `scan_sessions()` 扫出所有 `session_freshness_hours` 内活跃的会话（默认最多 12 个）；
2. 逐个 `diagnose()`，全部结果存进 `guard.last_diags`；
3. `_pick_actionable()` 挑一个最该处理的：**中断优先于卡死，同样状态取最新的**；
4. 续跑前 `switch_session()` 用 UIA 在左侧任务列表里点到那个任务，**确认切成功了才发**。

### 标题是怎么对上的

WorkBuddy 顶部标题是绘制文本，UIA 读不到，所以不能靠"读当前标题"判断在哪个会话。
能可靠拿到的是会话文件里的 `ai-title` 记录——**`aiTitle` 字段就是左栏显示的文字**：

```json
{"type":"ai-title","aiTitle":"开发 WorkBuddy 运行状态检测与自动续跑程序"}
```

于是 `_session_title_candidates()` 按优先级给出三个候选，逐个在左栏里试：

| 候选 | 用途 |
|---|---|
| `aiTitle` | 左栏实际显示的文字，**主匹配** |
| 首条干净的用户消息 | 兜底（`_clean_user_text()` 剥掉 `<system-reminder>`/`<user_query>` 壳） |
| 项目目录名 | 再兜底 |

### 安全性：宁可不动，也不发错

切换链路任何一步失败（找不到任务、切完窗口没到前台、点击后焦点丢失），
都会**放弃本次发送**并记 `switch-failed`，日志明确写"避免发错会话"。
绝不可能把「继续」丢进一个不相干的任务里。

切换成功后用**像素指纹**（`header_fingerprint()`：内容区顶部一条带的 mean/stddev/边缘密度）
确认界面真的变了。实测同一会话指纹稳定（如 `233.3|54.8|0.0802`），切换后必然变化
（如 `0.0802 → 0.0135`）。

### 已知限制

- **属于某个"空间项目"的任务可能切不过去**。左栏默认只显示最近任务，项目里的任务要先在
  「空间」→ 展开该项目才会出现。这时日志会提示：
  `切换失败（目标「xxx」）。请确认该任务在左栏可见：若属于某个项目，先在「空间」里展开该项目一次`
  检测本身仍然有效（会报"需处理"），只是不自动发。
- **锁屏时完全无法发送 —— 这是 Windows 的安全边界，不是程序 bug**。
  2026-10-22 在 Win11 26200 上逐条实测过（`python wb_guard.py lockcheck` 可复现）：

  | 通道 | 锁屏时 | 证据 |
  |---|---|---|
  | `SendInput` 模拟键鼠 | ✗ | 前台被锁屏界面占用，输入不落到目标窗口 |
  | `SetForegroundWindow` | ✗ | 返回 0，`err=18` |
  | UIA（uiautomation） | ✗ | 树塌成空 Pane，读到 0 个任务项、标题为空 |
  | 剪贴板 `OpenClipboard` | ✗ | 写入失败，读回 `None` |
  | `PostMessage` → 标准 Win32 控件 | ✓ | 记事本 `Edit` 实测成功写入 `lock-test-OK` |
  | `PostMessage` → Electron | ✗ | WorkBuddy 主窗口 **0 个子控件**，无可投递目标 |
  | `OpenInputDesktop` 读桌面名 | ✗（不可信） | 锁屏时仍返回 `Default`，不能反推「没锁」 |

  所以「锁屏下还能发指令」做不到。守护的处理方式是：
  1. 发送前检测（三条判据取并集：前台是锁屏界面 / `LogonUI.exe` 存在 / 输入桌面非 `Default`），
     命中就跳过并记 `workstation-locked`，**不消耗重试次数**（锁屏几小时也不会把 attempts 耗光）；
  2. **主动阻止锁屏**（唯一纯软件能做、不用管理员权限的事）：`config.json` 里
     `keep_awake: true` + `keep_display_on: true`，守护运行期间调 `SetThreadExecutionState`
     挡住空闲睡眠与熄屏，并在锁屏状态每轮变化时续一次；
  3. 面板顶部常驻红色提示「⚠ 桌面已锁屏：检测正常，但锁屏下无法发送输入」，
     `status` 显示「可发送：否」——绝不谎报已发送；
  4. 解锁后**自动恢复**，不需要重开程序。

  注意 `keep_awake` 只挡「空闲触发」的睡眠/熄屏，**挡不住用户手动 Win+L**。
  要彻底无人值守，还得关掉屏保和自动锁屏：

  ```
  # 屏保（需在「设置 → 个性化 → 锁屏界面 → 屏幕保护程序」里取消，或）
  powercfg /change monitor-timeout-ac 0      # 显示器永不关闭
  powercfg /change standby-timeout-ac 0      # 系统永不睡眠
  ```
  更彻底的是组策略 `Computer Configuration → Administrative Templates → Control Panel →
  Personalization → Do not display the lock screen`（需管理员；且只挡锁屏、不挡熄屏）。
- **权限必须一致**：如果 WorkBuddy 是"以管理员身份运行"，守护也要用管理员权限启动，
  否则系统会拦截模拟输入（UIPI）。

## 三、怎么"续"

1. 确认你已经 15 秒没动键鼠（`user_idle_seconds`），避免抢你焦点；
2. 把 WorkBuddy 主窗口拉到前台，确认真的到前台了；
3. **切到出问题的那个任务**（UIA 点击左栏条目，像素指纹校验）；
4. 点一下输入框（按窗口比例算位置，不是死坐标）；
5. 剪贴板写入「继续」→ Ctrl+V → 回车（用完把剪贴板原内容还回去）；
6. 等 25 秒，看**那个会话的**文件有没有长出新记录：
   - 长了 → 记一次成功，清状态；
   - 没长 → 退避重试（20s、40s、80s… 上限 300s，最多 6 次）；
7. 同一个中断连续 8 次都救不回来 → 判定模型整体不可用，自动暂停 15 分钟再试，不再无脑刷屏。

第 1、2 次会先点输入框再粘贴；第 3 次起改成"只粘贴不点击"（策略轮换），
防止某次点击位置不对时一直卡在同一个动作上。

## 四、快速开始

**双击 `wb-guard.exe` 即可**——直接弹出图形面板，不出现命令行窗口。

日常操作全在面板上，不需要敲命令：

| 面板上的操作 | 作用 |
|---|---|
| 「开始监控」 | 开始自动检测中断并续跑（等价于 `watch`） |
| 「只观察不发送」勾选框 | 只报告不发送（等价于 `watch --dry-run`） |
| 「立即检查」 | 只跑一次判定，不常驻 |
| 「停止」 | 停止监控 |
| 「开机自启」勾选框 | 勾上写入 HKCU 启动项，取消则删除 |
| 「立即续跑选中任务」 | 对表格里选中的任务立刻发「继续」 |
| 「打开日志」 | 用记事本打开 `logs/wb_guard.log` |

> 配置 `config.json`、日志 `logs/`、状态 `state.json` 都在 exe 同目录。
> 放到别处也能运行，但会生成一份默认配置；想沿用现有配置就放在本目录。

命令行入口仍然保留（给自动化/排障用）：

```bash
wb-guard.exe                 # 打开面板（默认）
wb-guard.exe watch           # 无界面常驻监控
wb-guard.exe watch --dry-run # 无界面只观察
wb-guard.exe stop            # 停止所有守护进程
wb-guard.exe autostart on    # 开机自启（等价于勾面板）
wb-guard.exe autostart off   # 取消开机自启
```

原来的 6 个 `.bat` 已归档到 `legacy-bat/`，功能完全被 exe 覆盖。

## 五、命令

| 命令 | 作用 |
|---|---|
| `status` | 打印进程/窗口/**所有活跃任务**的判定结果表 |
| `watch` | 常驻监控（默认动作） |
| `watch --once` | 只检查一次，需要时立刻处理 |
| `watch --dry-run` | 只报告，不发送 |
| `panel` / `gui` | 图形面板（含任务状态表格 + 手动续跑按钮） |
| `stop` | 停止所有守护进程（含 onefile 的引导进程） |
| `autostart on\|off` | 开机自启开关 |
| `probe` | 截一张窗口图，红点标出"续跑时会点哪里"，用来核对位置 |
| `replay <文件>` | 对指定 jsonl 跑一次判定（离线复盘） |
| `selftest` | 判定逻辑自检（12 个合成用例） |
| `test-input` | 输入通道自检（往记事本粘贴，不碰 WorkBuddy） |

## 六、配置（config.json）

| 键 | 默认 | 说明 |
|---|---|---|
| `continue_prompt` | `"继续"` | 续跑时发送的内容，可改成"继续执行任务" |
| `poll_interval` | 5 | 轮询间隔（秒） |
| `stall_timeout` | 420 | 思考/工具已返回的卡死阈值 |
| `tool_stall_timeout` | 900 | 工具执行中的阈值，长命令要留足时间 |
| `first_token_timeout` | 180 | 发出消息后等首字的时间 |
| `max_attempts` | 6 | 同一中断最多重试次数 |
| `backoff_base` / `backoff_max` | 20 / 300 | 退避基数与上限（秒） |
| `user_idle_seconds` | 15 | 你最近有操作就先不抢焦点 |
| `click_composer` | true | 发之前是否点一下输入框 |
| `composer_offset_ratio` | 0.12 | 输入框距底部占窗口高度比例（限制在 100~180px） |
| `composer_x_ratio` | 0.45 | 横向位置比例（单栏 / 右侧开文档面板都在框内） |
| `verify_seconds` | 25 | 发送后校验等待 |
| `session_freshness_hours` | 8 | 只监控最近活跃的会话 |
| `max_sessions` | 12 | 一次最多检查多少个会话（多任务并行时控制开销） |
| `switch_session` | true | 中断时是否先切到目标任务再发 |
| `switch_back_after_recover` | true | 救完别的任务后，是否把界面切回用户原来那个对话 |
| `switch_wait_seconds` | 4.0 | 切换后等待界面响应的秒数 |
| `max_expand_rounds` | 3 | 左栏找不到时展开「查看更多」的轮数 |
| `project_filter` | `""` | 只监控 cwd 含该子串的会话 |
| `dry_run` | false | 全局只观察模式 |
| `max_consecutive_failures` / `pause_minutes` | 8 / 15 | 连续失败后暂停 |
| `auto_restart_app` | false | WorkBuddy 没运行是否自动拉起 |
| `target_exe` | WorkBuddy.exe | 目标进程名（自测时可指 notepad.exe） |

## 七、边界与注意事项

- **不重启 WorkBuddy**：默认只发消息，不杀进程、不改配置。`auto_restart_app` 默认关闭。
- **不重复发**：同一中断记录只处理一次，处理过会记在 `state.json`，不会每次轮询都发一遍。
- **不会打断你**：你 15 秒内动过键鼠就不发；发送前会确认窗口确实到了前台，到不了就放弃这一轮。
- **不会发错会话**：切换失败就整体放弃这一轮，绝不退而"发给当前会话"。
- **长命令误判**：跑 `npm install`、大文件拷贝这类十分钟以上的命令时，把 `tool_stall_timeout` 调大（比如 1800）。
- **历史残留记录**：一个 8 小时前就断掉的会话也会被报成"需处理"。看 `status` 的"距上次事件"列，
  太老的可以忽略，或调小 `session_freshness_hours`。

## 八、已验证

- `selftest`：**12 个判定用例全过**——正常结束 / 明确中断 / 工具卡死 / 正在跑 / 等待首字 /
  长时间无响应 / 尾部是计费记录 / `code:0`+`status:503` / `404 模型已在网关中关闭` /
  正文是错误提示 / 长正文提到"超时"不误判 / 短正常回复不误判。
- **多任务扫描**：实测 10 个活跃会话，正确标出 2 个中断（REA 的 404、floorplan-3d 的 503），
  `status` 表格里任务名是左栏同款文字。
- **会话切换**：实测在「在吗？」和当前会话之间来回切换成功，像素指纹每次都变
  （`0.0604 → 0.0135 → 0.1284`），证明界面确实换了。
  **标题对齐是切换成功的关键**：早期版本误用"用户首句"做标题，和左栏的 `aiTitle` 摘要
  毫无共同前缀，导致永远匹配不上；改成优先读 `ai-title.aiTitle` 后才通。
- **切换失败安全**：目标不在左栏时，守护终止且**不发送**，日志"切换失败，终止（避免发错会话）"。
- **锁屏安全**：实测锁屏状态下 `workstation_locked()` 正确返回 True，续跑直接中止并给出
  可操作提示，不会出现"日志说发了但没反应"。
- **历史战绩**：`state.json` 记录本会话期间自动续跑成功 4 次（都是上游 503 抖动），
  说明单任务场景的自动恢复早已在线上跑着。
- 输入通道：记事本实测，文字确实粘贴进去并回车换行（`test-input` + 截图）。
- 位置探针：`probe` 红点落在 WorkBuddy 输入框内（单栏、右侧开文档面板两种版式均核对）。

## 九、打包（重新生成 wb-guard.exe）

源码由两部分组成：

- `wb_guard.py` —— 核心逻辑（判定、续跑、GUI）。
- `wb_launcher.py` —— 单文件启动器，把原来 6 个 bat 的子命令收进一个 exe。

打包命令（需先 `pip install pyinstaller`）：

```bash
python -m PyInstaller --noconfirm --onefile --windowed --name wb-guard ^
  --hidden-import tkinter --hidden-import tkinter.ttk wb_launcher.py
```

`--windowed` 生成 GUI 子系统程序（Subsystem=2），双击不弹黑窗；
不要误用 `--console`，那会带出一个命令行窗口。

产物在 `dist/wb-guard.exe`，复制到本目录即可使用。

注意两点（都已在内置逻辑里处理，改代码时别破坏）：

1. `wb_guard.py` 用 `sys.frozen` 判断运行环境，打包后配置/日志走 exe 所在目录，
   不落到 `_MEIPASS` 临时目录（否则每次重启都丢配置）。
2. 启动器用 **import 调用** `wb_guard.main()`，不起子进程——onefile 的
   `sys.executable` 指向 exe 自身，子进程调用会死循环。

## 十、排错

| 现象 | 处理 |
|---|---|
| 日志说"已发送"但没反应 | ① 先看 `status` 的"桌面状态"是不是"已锁屏"（最常见）② 权限是否一致（见上）③ 跑 `probe` 看红点是否在输入框内，不在就调 `composer_x_ratio` / `composer_offset_ratio` ④ 把 `click_composer` 设为 false 试试纯粘贴 |
| "切换失败（目标「xxx」）" | 该任务属于某个"空间项目"，左栏默认不显示。手动在「空间」里展开一次该项目；或先在界面上打开那个任务一次，之后它就会出现在最近列表里 |
| "工作站已锁屏，无法发送输入" | 桌面被锁了，模拟输入被系统拦截。解锁即可；要无人值守就关掉自动锁屏 |
| 救完别的任务后人被拽到那个对话了 | 正常，发完会自动切回（`switch_back_after_recover`）。若没切回，说明原任务也不在左栏可见列表里 |
| 老是误判"卡死" | 调大 `stall_timeout` / `tool_stall_timeout`；或先用 `--dry-run` 观察一段时间再定阈值 |
| 一直判定 interrupted 但界面看起来正常 | 可能是历史残留记录，`status` 看"距上次事件"；必要时 `replay` 该文件确认 |
| 面板打不开 | 改用 `wb-guard.exe watch` 走控制台，看报错 |
| 停不掉守护 | `wb-guard.exe stop`；若仍残留，任务管理器结束 `wb-guard.exe` 即可 |
| 想立刻救某个任务 | 面板里选中那一行，点「立即续跑选中任务」（忽略冷却立即执行） |

日志：`logs/wb_guard.log`（超 2MB 自动轮转）；状态：`state.json`；截图：`logs/*.png`。
