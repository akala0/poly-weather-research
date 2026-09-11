# Windows asyncio 回环阻塞修复记录（2026-09-11）

## 结论与范围

当前项目 Python 3.14 的默认 IPv4 `socketpair` / asyncio loop 创建阻塞已解除。
完整默认回归实际完成：**849 passed in 39.92s**，无 skip、无 deselection。
这只关闭本机异步测试环境阻塞，不是整体项目封板、真实网络采集验收或恢复授权。
项目继续 NOT SEALED；未启动 capture、Paper 或任何常驻服务。

## 直接证据

- 修复前，当前 Python 在 IPv4 `127.0.0.1`、IPv6 `::1` 的阻塞和非阻塞本机 TCP 连接均超时；独立 PowerShell/.NET 本机连接也超时。
- WFP 已有 NETEVENTS 开关为 on；只读取状态，未开启或修改审计设置。
- 本轮自建探针端口 9791、9807、9809、9823、9856 对应事件明确记录 `CLASSIFY_DROP`、`isLoopback=true`。
- IPv4 拒绝 filterId 为 74530，IPv6 为 74534；这些 ID 是当次运行态标识，不能作为长期固定 ID。
- 已核对 IPv4 74530：Windows 防火墙 `Query User`，provider 为 `FWPM_PROVIDER_MPSSVC_WF`，layer 为 `FWPM_LAYER_ALE_AUTH_RECV_ACCEPT_V4`，action 为 BLOCK。
- 72704 为另一子层的 PERMIT，不是拒绝原因。仅发现第三方过滤驱动或代理进程存在，不构成其导致本次阻塞的证据；未停止或修改它们。
- 未查明此前何时、由谁改变了系统策略；不能将本次诊断倒推为所有历史超时的根因。

原始 WFP dump 留在本机临时目录（含其他进程信息，不复制进仓库）：
`C:\Users\Administrator\AppData\Local\Temp\poly-wfp-c17379b59fec4aefac7e151ae37fa37a\state.xml`
及同目录 `events.xml`。

## 唯一系统修改

添加并保留以下入站允许规则，未关闭防火墙、改默认策略、修改代理或停止驱动：

| 字段 | 实际值 |
|---|---|
| Name | `PolyWeather-Python314-Loopback-TCP-In` |
| Program | `C:\Users\Administrator\AppData\Local\Programs\Python\Python314\python.exe` |
| Direction / Action | Inbound / Allow |
| Enabled / Profile | True / Any |
| Protocol | TCP |
| LocalAddress / RemoteAddress | `127.0.0.1` / `127.0.0.1` |
| LocalPort / RemotePort | Any / Any（socketpair 使用临时端口） |

规则绑定解释器，不是按项目目录授权；任何使用该解释器的程序均可接受本机 IPv4 TCP 连接，但不放行局域网或公网源地址。
首次同时指定 `::1` 的创建命令被 Windows 拒绝，随后确认该名称不存在，再创建仅 IPv4 规则。
**未修复 IPv6、PowerShell/.NET 或其他 Python 安装的回环访问**；没有扩大规则范围来顺带覆盖它们。
未来更换 Python 实际可执行路径时，应重新诊断，不能假定规则仍覆盖。

参考：[Microsoft New-NetFirewallRule 文档](https://learn.microsoft.com/en-us/powershell/module/netsecurity/new-netfirewallrule)。

如需撤销，仅删除本次创建的明确规则（本轮没有执行）：

```powershell
Remove-NetFirewallRule -Name 'PolyWeather-Python314-Loopback-TCP-In'
```

撤销后可能恢复此前测试阻塞；不要删除其他规则或重置整个防火墙。

## 验证

所有 Python 命令使用 `D:\poly\.venv\Scripts\python.exe`；测试的 TMP/TEMP 指向独立临时目录，避免使用实际 capture 的全局锁位置。

| 验证 | 结果 |
|---|---|
| 默认 socketpair 双向端点创建、数据传送、asyncio loop 创建及关闭 | PASS |
| 10 个全新 Python 进程重复上述验证，每进程 5 秒上限 | 10/10 PASS |
| 两项此前受阻 capture 异步入口测试 | 2 passed, 21 deselected in 1.34s |
| 完整 pytest，无 `-k` 或 ignore | **849 passed in 39.92s**，exit 0 |
| Ruff `check src tests` | All checks passed |
| `git diff --check` | 无 whitespace error；只有既存 LF/CRLF 提示 |
| `git status --short -- data` | 空 |

定向命令：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_collection_capture.py -q -k 'actual_recorder_fake_wire or invalid_identity_saved' -o faulthandler_timeout=20
```

完整命令：

```powershell
.\.venv\Scripts\python.exe -m pytest -q -o faulthandler_timeout=45
.\.venv\Scripts\python.exe -m ruff check src tests
```

完整测试由 Python `subprocess.run` 设置 180 秒外层超时；本次自然成功退出，没有触及上限。
定向测试使用 fake wire/注入 fetcher，不是公共市场连接成功证明。

## 安全与交接

- 本轮未修改生产代码、测试、registry 或历史验证报告；新增本记录，保留此前失败证据。
- 未启动或操作 weather、market、supervisor、signal、shadow、Task Scheduler、capture 或 Paper。
- 未读凭据、未接真实执行、未 commit/push。
- 未主动写正式 `data/`；Git 可见检查不等于 ignored 归档全量哈希审计。
- 查询了 Microsoft 公共技术文档；故障探针只连接本进程创建的本机套接字，没有市场/天气 API probe。
- 结算语义、来源完整性、隔离消费者独立审查及单独恢复授权等边界不因测试通过自动解除。
