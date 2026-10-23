# hermes-doro

[English](../README.md) | **简体中文**

Doro — 跑在加固 Docker 容器里的私人 AI agent，基于 [Hermes Agent](https://github.com/NousResearch/hermes-agent)。

飞书 + QQ 双端在线，模型走任意 OpenAI 兼容接口，可选桥接宿主机 OpenCode 服务操作白名单项目。

## 架构

```
飞书 / QQ (WebSocket 出站, 无公网入口)
   │
   ▼
┌─────────────────────── Docker 容器 hermes ───────────────────────┐
│  Hermes Agent gateway (s6 降权运行)                               │
│  - 飞书消息收发 (lark-oapi / websockets) + QQ 官方开放平台          │
│  - TTS (edge-tts) / lark-cli 技能                                 │
│  - 写工具被限定在 /opt/data (HERMES_WRITE_SAFE_ROOT)               │
└──────┬──────────────────────────────────────┬────────────────────┘
       │ bind mount: data/ ↔ /opt/data       │ bind mount: /mnt/hermes-doro
       │   + config/ 四个单文件 + skills/*    │
       ▼                                     ▼
  宿主机 data/ (运行态) / config/ (真实配置)  宿主机 /mnt/hermes-doro (文件交换)
  / skills/ (公开层) / skills_private/ (私有层)
       │
       │ Basic Auth (HTTP, 仅 Docker bridge 172.17.0.1)
       ▼
┌─────────────── 宿主机 OpenCode user services ───────────────────┐
│  opencode-serve@<project>.service (每项目一个实例, systemd --user) │
└──────────────────────────────────────────────────────────────────┘
```

## 安全模型

容器边界 = 宿主机的护城河。

| 保证               | 手段                                                                                                                                                                  |
| ------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 降低宿主机风险     | `--cap-drop ALL`（仅加回启动和降权所需 5 个 cap）、`no-new-privileges`；s6 在容器内降权                                                                               |
| 不挂 Docker socket | 绝不 `-v /var/run/docker.sock`，不 `--privileged`                                                                                                                     |
| 项目写入范围       | 代码来自镜像；`data/` 保存运行时状态，`config/` 中四个真实配置文件可写挂载但不进 git；公开 `skills/<name>/` 可写挂载并由 git 跟踪；业务文件通过共享目录交换（宿主机 `SHARED_DATA_DIR`，默认 `/mnt/hermes-doro`；容器内同路径挂载） |
| 工具写域被限定     | `HERMES_WRITE_SAFE_ROOT=/opt/data`，写工具只能在数据卷内动手                                                                                                          |
| 没有公网入口       | 飞书/QQ 走 WebSocket 出站，不开任何 `-p` 端口；dashboard 关闭                                                                                                         |
| 破坏性命令有人审   | `approvals.mode: smart` + 用户白名单                                                                                                                                  |
| 资源被框死         | 内存 4G / CPU 2 / pids 512 限额                                                                                                                                       |

残余风险：容器内 terminal 后端仍可读同一容器、`/opt/data` 及 `/mnt/hermes-doro` 中可访问的文件（包括挂载到 `/opt/data/.env` 的 `config/.env`）。不要把该容器当作完全不可读的密钥保险箱。

## Skills 三层模型

Agent 的 skill 分三层，读写边界清晰：

| 层        | 容器内路径                  | 读写                                  | 来源与维护                                                                                                     |
| --------- | --------------------------- | ------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| 自带      | `/opt/hermes/skills/`       | 只读（root 所有，agent 进程无写权限） | 镜像内置，随 `./build.sh --pull` 升级；经 `skills.external_dirs` 只读注入索引                                  |
| 学习·公开 | `/opt/data/skills/<name>/`  | 读写                                  | 本仓库 `skills/<name>/` 逐目录 bind mount，单副本；agent 修改直接进入工作区，人工 review 后 commit             |
| 学习·私有 | `/opt/data/skills_private/` | 读写                                  | 本仓库 `skills_private/`（gitignored，仅目录内 README 进 git；可含多个私有仓库 clone）bind mount，默认自动挂载 |

- 自带层不复制进可写区：`hermes skills opt-out --remove`（marker 在 `data/.no-bundled-skills`，bind mount 持久）。
- agent 新建的 skill（`skill_manage`、`/learn`、curator）默认落在 `/opt/data/skills/` 的未挂载目录，仍是未跟踪的运行时内容。首次公开时，停止容器并 review，将该目录迁入本仓库 `skills/<name>/`，再运行 `./run.sh`。已公开 skill 的后续迭代直接反馈到仓库，无需复制。
- 私有 skills：私有仓库放在 `skills_private/` 下（必须是真实 clone）；挂载机制、`git clean` 注意事项与 `PRIVATE_SKILLS_SRC` 覆盖详见该目录下的 README。

公开 skill 使用顶层扁平目录：每个 `skills/<name>/SKILL.md` 是面向 agent 的协议，`skills/README.md` 是面向维护者的部署与使用说明。`run.sh` 自动发现并逐目录读写挂载。新增或移除公开 skill 后需 `./run.sh` 重建容器；仅 `docker restart` 不会更新挂载列表。

当前公开 skills：`opencode`（OpenCode 桥）、`lark-progress`（飞书多步骤任务播报）、`meego`（飞书项目/Meegle 操作；loader 技能，CLI 与 skill 均内置于镜像）、`lark-suite`（lark-cli 内嵌 28 个领域技能的指针）、`qq-progress`（QQ 多步骤任务播报，撤回重发）、`skill-creator`（创建/改进 skill 的工作流），详见 [`skills/README.md`](../skills/README.md)。

Review agent 的迭代并提交选定文件：

```bash
git status --short -- skills/
git diff -- skills/
```

仅提交公开的技能说明和必要资源；`__pycache__/` 已忽略。仓库 `.git` 不挂载进容器。`config/SOUL.md` 要求 agent 在创建或修改 skill 前加载 `skill-creator`，纯错别字修正除外。

## 快速开始

```bash
# 0) 准备共享目录（传文件先写临时名再改正式名，
#    避免对侧读到半截文件）。默认 /mnt/hermes-doro，可用环境变量覆盖：
#    SHARED_DATA_DIR=/path/to/dir ./run.sh（容器内始终挂载到 /mnt/hermes-doro）
sudo mkdir -p /mnt/hermes-doro

# 1) 首次运行：缺失的真实配置会从 example 自动生成，然后提示编辑并退出
./run.sh
$EDITOR config/.env                # 填模型 API key + 飞书/QQ 凭证和用户白名单
$EDITOR config/config.yaml         # 填真实模型 ID、接口地址（可用 api_mode）
$EDITOR config/opencode_projects.yaml  # 填宿主机项目路径（用 OpenCode 桥时）
# 可按需编辑 config/SOUL.md；已有真实文件不会被引导流程覆盖

# 2) 构建并启动（自动挂载公开 skills；Dockerfile 改过后需重新构建）
./build.sh && ./run.sh

# 3) 确认连上
docker logs -f hermes-doro
# 看到 [Lark] connected to wss://... 即链路通，私聊机器人发消息测试
```

拿到自己的飞书 `open_id`：临时在 `config/.env` 加 `GATEWAY_ALLOW_ALL_USERS=true` 并注释掉 `FEISHU_ALLOWED_USERS`，重启后给机器人发一条消息，然后：

```bash
docker exec hermes-doro grep -o 'ou_[a-z0-9]*' /opt/data/sessions/sessions.json
```

回填白名单、删掉 allow-all、再 `./run.sh`。

### QQ 机器人（官方开放平台）

1. 在 [q.qq.com](https://q.qq.com) 注册机器人，拿到 AppID / Secret，填入 `config/.env` 的 `QQ_APP_ID` / `QQ_CLIENT_SECRET`
2. `config/config.yaml` 的 `platforms.qq` 已预置 allowlist 骨架：临时在 `extra` 里加 `allow_all_users: true`，重启后私聊机器人一条消息，执行 `docker exec hermes-doro cat /opt/data/sessions/sessions.json | grep -o 'ou_[a-z0-9]*'` 拿到 openid 回填 `allow_from`，删掉 `allow_all_users` 再重启；群聊按需把 `group_policy` 改为 `allowlist`（群内需 @机器人 触发）
3. 正式环境（出沙箱）可能需要企业主体

## 模型配置

`config/config.yaml` 按真实服务填写（模板见 `config/config.yaml.example`），修改后 `./run.sh` 重建容器生效：

- `model.*`：主模型（任意 OpenAI 兼容端点；模型名以 `/v1/models` 返回为准；仅有 `/chat/completions` 的端点加 `api_mode: chat_completions`）
- `fallback_providers`：备用模型链，主模型限流/5xx/断连时自动切换，key 放 `config/.env`（`key_env`），不写进 config
- `context_length` 按模型真实窗口配置，自动压缩约在阈值 75% 触发

## OpenCode 桥（可选：让 Doro 控制宿主机项目）

由 `skills/opencode/` 提供并维护。要求宿主机 OpenCode **>= 2.0**（只讲 V2 server API，V1 已废弃）：在 `config/opencode_projects.yaml` 登记项目，宿主机侧用 `oc-servectl` 部署 systemd user services，`config/.env` 填入同一密码后 `./run.sh`。部署步骤、聊天命令（`/opencode ...`）、会话规则与安全边界见 [`skills/README.md`](../skills/README.md#opencode)。

## 飞书任务播报

多步骤任务采用两条消息：一条是 agent 创建并持续编辑的进度消息（每步「做什么+耗时」，末尾附 `Blockers` 与 `Learnings`），另一条是按结论、证据、验证和后续动作组织的最终回答；短问答不创建进度消息。协议细节、触发规则与相关 display 配置见 [`skills/README.md`](../skills/README.md#lark-progress)。

## 目录结构

```
hermes-doro/
├── Dockerfile          # 基于官方镜像，预装飞书 SDK、edge-tts、lark-cli 与烘焙的 meegle skill
├── build.sh            # 构建镜像（--pull 强制刷新 base 镜像）
├── run.sh              # 启动脚本与运行时限制（需先 ./build.sh）
├── update.sh           # 日常更新 SOP（可选 --rebuild-image）
├── skills/             # 公开 skills，git 跟踪；逐目录读写挂载，单副本
│   ├── README.md       # 各 skill 的部署与使用说明（维护者文档）
│   ├── lark-progress/  # 飞书多步骤任务播报协议
│   ├── meego/          # 飞书项目 (Meego/Meegle) loader 技能（CLI 与 skill 均内置于镜像）
│   ├── lark-suite/     # lark-cli 内嵌 28 个领域技能的指针/目录页
│   ├── opencode/       # OpenCode 桥（含 oc-servectl 与 systemd unit 模板）
│   ├── qq-progress/    # QQ 多步骤任务播报协议（撤回重发）
│   └── skill-creator/  # 上游 skill-creator 及评测工具
├── skills_private/     # 私有 skills（gitignored，仅 README 进 git）
├── docs/               # 翻译文档（中文 README）
├── .dockerignore       # 全忽略（Dockerfile 无 COPY，构建上下文为空）
├── .gitignore          # data/ 全量忽略；config/ 只跟踪 4 个 example
├── data/               # 运行时状态（挂载为 /opt/data，全量忽略）
└── config/             # 仅 4 个 example 模板进 git，真实配置本地保留
    ├── .env.example / .env
    ├── config.yaml.example / config.yaml
    ├── opencode_projects.yaml.example / opencode_projects.yaml
    └── SOUL.md.example / SOUL.md
```

`data/` 全量忽略，保存 `state.db`、`sessions/`、`logs/`、运行时认证状态等内容。`config/` 中四个真实文件分别读写挂载到 `/opt/data/` 下的同名路径。Docker 可能在 `data/` 留下同名的零字节挂载占位文件，实际配置以 `config/` 为准。真实配置与人格不出现在 `git diff` 中，需直接查看或与模板比对。

## 日常运维

```bash
docker logs -f hermes-doro                 # 实时日志
./run.sh                              # 改 config/ 或 skills 目录结构后重建容器（刷新挂载）
docker restart hermes-doro                 # 不改配置挂载时的日常重启
docker exec hermes-doro hermes gateway status
docker exec -it hermes-doro hermes tools   # 裁剪不用的 toolset，给系统提示瘦身
```

升级：`./update.sh --rebuild-image`（等价 `./build.sh --pull && ./run.sh`；`config/` 保留本地真实配置，`data/` 通过 bind mount 保留运行时状态）。

## 常见问题

**机器人连上了但不回消息** → 飞书后台没订阅事件或应用没发布：
- 开放平台 → 事件与回调 → 订阅 `im.message.receive_v1`
- 版本管理 → 创建版本并发布（企业应用需审批）
- 权限管理确认开了 `im:message`、`im:message:send_as_bot`

**model not found** → `config/config.yaml` 的 `default` 必须与端点实际暴露的模型名完全一致：
`curl -H "Authorization: Bearer <key>" <base_url>/models`

**改了 Dockerfile 不生效** → `./build.sh` 重建镜像后 `./run.sh`；`run.sh` 会优雅停止（≤60s）并重建 hermes 容器，但不会删除 `data/` 与 `config/`。

## 消息内常用命令

| 命令                    | 作用                                    |
| ----------------------- | --------------------------------------- |
| `/usage`                | 看当前上下文占用                        |
| `/compress`             | 立即压缩长对话                          |
| `/new`                  | 开新会话（记忆仍在）                    |
| `/sethome`              | 把当前会话设为 cron/通知落地            |
| `/model provider:model` | 手动切换模型（会话级，`--global` 写回） |
| `/reasoning low`        | 降低推理 token                          |

OpenCode 桥的 `/opencode ...` 命令（可选功能）见 [`skills/README.md`](../skills/README.md#opencode)。

## License

Apache-2.0
