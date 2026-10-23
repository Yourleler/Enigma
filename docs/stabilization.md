# Enigma 0.1.1 稳定基线

本轮在现有 Python 内核上修复已复现的问题。模型协议、CLI、记忆和编排仍由 Enigma 自己实现，运行时依赖保持为空。

## 完成与验收

`TaskState.status = completed` 表示模型回合正常结束。`verification_status` 独立记录 `unverified`、`verified` 或 `failed`；模型的完成说明不能替代验收。普通运行会显示这项状态。

需要执行独立验收时，可以设置：

```bash
.venv/bin/enigma --verify-command '.venv/bin/python -m pytest -q' '修复测试失败'
```

验收命令使用同一审批策略，并受超时限制。验收失败、预算停止和模型错误在 one-shot 模式返回非零退出码。未设置独立检查的回答保持 `unverified`。

编排支持机读计划：

```xml
<plan>{"steps":[{"goal":"创建 ready.txt","allowed_tools":["write_file"],"verification":{"files":{"ready.txt":"ready"}}}]}</plan>
```

`verification.files` 的值为完整预期 UTF-8 内容，或 `null`（仅检查文件存在）；也可以设置 `verification.command`。命令需要包含在允许的工具能力内，并通过审批。描述性 Markdown 计划仍兼容，但没有独立检查时结果为 `UNVERIFIED`。整项任务的 `completion_check` 在最终审查后执行，各步骤可分别声明自己的检查。

这些步骤验收条件应在最终交付时仍然成立，后续步骤破坏早先条件会阻止完成。多步骤或修复后会重新检查条件；单步骤且没有修复时保留已经通过的命令结果，只重新检查文件条件。独立 verifier 可以重复检查，但模型的相同工具死循环仍受限制。

## 已修复的边界

- 交互任务通过审批队列向主线程请求输入，保持 `ask` 策略，拒绝不产生写入；终端在按键轮询与审批输入之间切换模式并恢复。
- 模型错误、取消和其他异常生成终止状态与报告；记录错误类型和阶段。持久化本身失败时保留原始异常并报告保存失败。
- XML 工具正文中的结束标签不会再截断文件；正文内的保留标签不被误当成参数；歧义的多个调用被拒绝。JSON 工具参数适合复杂代码。普通行动说明不会自动成为 final。
- OpenAI/Anthropic SSE 使用增量 UTF-8 解码，处理最后无换行事件，失败和未完成流不会返回成功的部分文本；Ollama 流也需要完成标记。
- SessionStore 使用原子写入并校验 session id；会话、状态、trace、report、数据库字段和长期记忆写入使用统一的已知密钥脱敏策略。
- shell 使用结构化退出码、超时和取消状态，取消/超时会终止进程组；子进程不争抢交互 stdin。
- 等待模型时的取消在工具派发前再次确认；被取消的规划、执行、审查或验收不会进入自动修复。KeyboardInterrupt 也清理进程组并保存取消原因。
- 修复过程保留全部尚未执行的原计划步骤；审查支持结构化结果和中文严重级别，解析失败不会变成“无问题”，Python 语法错误阻断验收。
- 基准在执行层限制工具，并记录工具策略符合情况；verifier 有独立超时。
- 工具预算耗尽后保留无工具的 final 机会；中文记忆加入可测试的词法召回。

## 执行权限与验证范围

文件工具仍锚定工作区路径。`run_shell` 是经审批的宿主执行能力，结果明确记录 `confinement = unrestricted`；工作目录不是 OS 沙箱。此版本不承诺任意 shell 的外部副作用隔离或 exactly-once 恢复。网络连接中的取消仍受 provider 的网络超时边界约束。

测试包含实际文件读写、审批拒绝、子进程超时/取消、数据库与持久文件扫描、机读计划验收，以及本地 HTTP 服务驱动的模型客户端 → 工具 → final 完整链路。本地 HTTP 服务是受控响应，不代表真实模型效果。

## 可复现检查

开发依赖固定为 pytest 9.1.1 和 Ruff 0.17.2，Ruff 规则在项目配置中固定，避免继承机器设置。CI 配置 Python 3.10/3.12 和 Linux/macOS/Windows，实际远端结果需在推送后观察。

```bash
.venv/bin/python -m ruff check .
.venv/bin/python -m pytest -q
.venv/bin/enigma --help
```

本次本机使用 Python 3.12.14。真实模型联调需要配置并启动 Ollama 或相应兼容 API 后端。

2026-10-23 本机最终确认：287 个测试全部通过，Ruff 检查、安装后的 `enigma --help` 和 `git diff --check` 均通过。原有 242 个测试保留，并新增 45 个稳定性回归用例。真实模型后端未参与这次验收，受控 HTTP 链路验证的是客户端、工具与验收流程。
