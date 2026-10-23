# 工作目录说明

实际目录：`/Users/kafuuchino/Projects/qq-chatbot`。

代码、模型定义、模型数据、QQ 登录态和机器人数据均在这个目录内。Ollama 使用 Mac 上已安装的原生引擎，通过 launchd 后台运行；NapCat 使用 Docker 容器。此目录是统一管理目录，不是内含全部应用程序的独立安装包。

## 文件的作用

| 位置 | 内容 | 编辑或管理方式 |
| --- | --- | --- |
| `models/deepseek.modelfile` | 当前模型人设和创建参数 | 网页“模型人设”保存并构建 |
| `runtime/ollama/models` | Ollama 模型清单及共享权重 | 使用 `ollama list`、`ollama rm`、`ollama pull` 管理 |
| `runtime/napcat/compose.yaml` | 原 NapCat Compose 配置 | 网页“组件管理”启动、停止和重启 |
| `runtime/napcat/data` | QQ 登录态、NapCat 配置和插件 | 保留原有内容；避免手工删除 |
| `.local/config.json` | 群号、模型名、API 地址、生成参数及 WebUI 配置 | 网页“运行设置”保存并自动应用 |
| `.local/profiles.json` | 群友个人消息池 | 程序自动保存 |
| `启动仪表盘.command` | 日常唯一外部入口 | Finder 双击打开网页 |
| `web_console.py` / `webctl.py` | 独立管理台与后台服务入口 | 监听本机 8765，机器人停止后仍运行 |
| `.local/bot-api-token` | 管理台访问机器人内部 8766 接口的凭据 | 自动生成，请勿公开 |
| `.local/console.log` | 网页管理服务日志 | 网页“运行日志” |
| `.local/bot.log` | 机器人运行日志 | 网页“运行日志” |
| `.local/exports` | 已导出的文本与聊天记录 | 按需查看、归档 |
| `.local/workspace-migration.json` | 迁移路径、模型摘要及时间 | 迁移核对依据 |

Modelfile 是创建模型的配置，不是模型权重。编辑文件后必须重建，Ollama 才会使用新的人设。Python 请求中的上下文和生成参数还可能覆盖 Modelfile 中对应参数，二者作用不同。

## 直接使用新路径

| 原路径（已移除链接） | 现在使用的文件或目录 |
| --- | --- |
| `~/Documents/ChatGPT/聊天机器人` | 此工作目录 |
| `~/Modelfile.deepseek` | `models/deepseek.modelfile` |
| `~/.ollama/models` | `runtime/ollama/models` |
| `~/napcat` | `runtime/napcat` |

旧位置不再保留兼容符号链接。移除的是链接本身，模型、消息池和 QQ 数据仍在工作目录中。

Ollama 服务 `local.whatlike.ollama` 的 launchd 配置明确设置 `OLLAMA_MODELS` 为 `runtime/ollama/models` 的绝对路径，登录后也会自动启动，默认监听本机 `127.0.0.1:11434`。`ollamactl.py serve` 同时为当前登录会话设置模型路径，供之后启动的桌面应用沿用。日常管理使用仪表盘，无需再打开 Ollama 桌面应用。

NapCat 容器已按新目录重新创建，实际挂载路径直接指向工作目录中的 `runtime/napcat/data`。QQ 登录数据、配置和插件均沿用原文件。Python 虚拟环境也已在新目录重建，激活脚本和命令入口不再引用旧项目路径。

管理程序固定 Compose 项目名为 `napcat`，继续管理 `napcat-napcat-1`，不会另起一套同名业务。查看状态不会启动应用或模型。

## 备份与搬家

- 完整数据备份要包含整个工作目录，特别是 `runtime/` 和 `.local/`；Git 不包含模型权重、登录态和消息池。
- 迁移前的配置和模型定义备份位于 `.local/backups/before-workspace-*`。大模型使用原文件移动，未另存一份权重；`.local/workspace-migration.json` 记录了迁移前后的模型摘要以及权重 inode 核对结果。
- 如果以后改工作目录名称或位置，先在网页停止机器人、NapCat 和 Ollama，再用 `.venv/bin/python webctl.py stop` 停止管理台。移动目录后重新创建虚拟环境，双击新目录中的启动脚本，再从网页启动组件，以更新 launchd 配置及容器挂载。迁移属于维护操作，请不要只在 Finder 中改目录名后直接启动。
- 当前 `.venv` 可在本机继续使用；换一台机器时应根据 `requirements.txt` 重新创建，而不是直接依赖复制过去的虚拟环境。
- `~/Downloads/Modelfile` 是此前下载的历史参考文件，没有自动改动；当前统一编辑入口是工作目录中的 `models/deepseek.modelfile`。

## 当前验证边界

此次整理保留了用户最新人设，模型摘要、人设和参数均按迁移前后核对。Ollama 能读取迁移后的模型，机器人后台和仪表盘可从新路径启动。

已移除四个旧入口链接。网页管理版本的 59 项测试通过，已实测网页停止、启动及本地模型试聊。移除后成功从新目录重新加载 `deepseek-custom:latest`，Ollama 报告 100% GPU、8192 上下文，模型人设及参数保持一致。NapCat 容器已改为直接挂载新目录，目前日志提示等待扫码登录；在 http://127.0.0.1:6099 登录后，机器人会自动重连。仪表盘可查看实际连接状态。

参考：[Ollama 模型存储配置](https://docs.ollama.com/faq)、[Ollama Docker 部署说明](https://docs.ollama.com/docker)。
