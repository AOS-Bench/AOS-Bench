# 入门指南

本指南介绍如何在一台普通的 Linux + Docker 主机上安装 AOS-Bench、运行一个工作负载，以及如何解读
输出。简要版见 [README](../README_zh.md)。[English](./getting_started.md)

## 1. 环境要求

| | |
|---|---|
| 操作系统 | Linux，cgroup v2 |
| Docker | Engine 20.10 及以上，带 `docker compose` v2 插件 |
| 资源 | 一个 stream trial 使用 4 CPU 和 8 GiB；建议至少 8 CPU、16 GiB，以及约 100 GB 可用磁盘（用于镜像） |
| 工具 | `git`、[Git LFS](https://git-lfs.com)、[uv](https://github.com/astral-sh/uv)、Python 3.12 及以上 |
| 模型访问 | agent 所用模型的 API key；部分验证器使用 LLM 评审，可另备 OpenRouter key |

检查 Docker：

```bash
docker compose version          # 应输出 v2.x
docker run --rm hello-world
```

如果没有 `docker compose` 子命令、但装了 `docker-compose`，把它链接为插件：

```bash
mkdir -p ~/.docker/cli-plugins
ln -sf "$(command -v docker-compose)" ~/.docker/cli-plugins/docker-compose
```

## 2. 安装

```bash
git clone --recurse-submodules https://github.com/AOS-Bench/AOS-Bench.git
cd AOS-Bench

# Harbor-AOS 有自己的环境，把 aos_bench 加入其 Python 路径
(cd harbor && uv sync --all-extras)
echo "$(pwd)" > "$(ls -d harbor/.venv/lib/python3*/site-packages)/aos_bench.pth"

# 每个 job 使用的 dispatcher 镜像，构建一次即可
docker build -t aos-bench/dispatcher:v1 -f aos_bench/pool/Dockerfile.dispatcher .
```

如果 clone 时没带 `--recurse-submodules`，执行 `git submodule update --init`。

## 3. 获取工作负载

任务和工作负载在 [AOS-Bench-Task](https://github.com/AOS-Bench/AOS-Bench-Task) 仓库。部分环境文件使用
[Git LFS](https://git-lfs.com) 存储，需先安装：

```bash
git lfs install
git clone https://github.com/AOS-Bench/AOS-Bench-Task.git data
```

| 目录 | 内容 |
|---|---|
| `data/stream/V1-0/valid-0917/medium/` | 10 个工作负载，每个 10 个任务 |
| `data/stream/V1-0/valid-0917/heavy/` | 5 个工作负载，每个 20 个任务 |
| `data/stream/V1-0/valid-0917/heavy-plus/` | 1 个 30 任务的工作负载（案例研究） |
| `data/stream/V1-0/valid-0917-individual-dedup/<tier>/` | 同一批任务，每个任务一个目录，用于 individual 运行 |

每个工作负载目录包含：
- `stream.toml`：资源、预构建镜像、释放计划；
- `environment/`：合并后的 Dockerfile；
- `tasks/<task-id>/`：每个任务的指令、测试和参考解。

格式说明见 [task_stream.md](./tasks/task_stream.md)（英文）。

`stream.toml` 指定了 Docker Hub 上的预构建镜像（`zhuerle/aosbench`），首次使用时自动拉取。建议先
`docker login`，避免触发匿名拉取限额。如需从 Dockerfile 构建，在 `harbor run` 中加上
`--force-build`，部分镜像构建需要一小时以上。

## 4. 选择 agent

| Agent | `--agent` | `--model` 示例 | 凭据（`--ae`） | 论文中的设置（`--ak`） |
|---|---|---|---|---|
| Claude Code | `claude-code` | `claude-opus-5` | `ANTHROPIC_API_KEY` | `version=2.1.241`、`reasoning_effort=high`、`permission_mode=bypassPermissions`、`disallowed_tools=WebSearch,WebFetch` |
| Codex | `codex` | `gpt-5.6-sol` | `OPENAI_API_KEY`、`CODEX_FORCE_API_KEY=1` | `version=0.50.0`、`reasoning_effort=high`、`wire_api=responses` |
| Kimi Code | `kimi-code` | `kimi/kimi-k3` | `KIMI_API_KEY` | `version=0.36.1` |

通过兼容接口使用其他模型：
- **Claude Code**：用 `ANTHROPIC_BASE_URL` 和 `ANTHROPIC_AUTH_TOKEN` 指向 Anthropic 兼容的接口。
  论文中的 GLM-5.3 和 DeepSeek-V4-Pro 就是这样运行的。
- **Codex**：用 `OPENAI_BASE_URL`。
- **Kimi Code**：用 `KIMI_MODEL_BASE_URL`、`KIMI_MODEL_API_KEY` 和 `KIMI_MODEL_PROVIDER_TYPE`
  （`kimi`、`openai` 或 `anthropic`）显式指定接口。

## 5. 运行一个工作负载

```bash
cd harbor
uv run harbor run \
  --path ../data/stream/V1-0/valid-0917/medium/v1-medium-10-cdf6049a \
  --task-type stream --aos-pool-v03 --env docker \
  --agent claude-code --model claude-opus-5 \
  --ak version=2.1.241 --ak reasoning_effort=high \
  --ak permission_mode=bypassPermissions --ak disallowed_tools=WebSearch,WebFetch \
  --ae ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
  --ve OPENROUTER_API_KEY="$OPENROUTER_API_KEY" \
  --agent-timeout-sec 9000 \
  --jobs-dir ../jobs --n-attempts 1 --yes
```

运行过程：
1. Harbor 为该 job 启动 dispatcher 容器，再启动 trial 容器（4 CPU、8 GiB）。
2. 安装 agent，并为它配置 dispatcher 的 MCP 工具。任务按 `stream.toml` 中的计划陆续释放；agent
   用 `todo_next` 领取任务、执行，再用 `todo_update` 汇报结果。
3. agent 停止或到达 `--agent-timeout-sec` 后，Harbor 关闭 dispatcher（之后不再接受领取和状态
   更新），并停止 agent 启动的全部进程。
4. 每个任务的验证器对共享工作区的最终状态打分。

heavy 工作负载用 `--agent-timeout-sec 14400`，30 任务工作负载用 `21600`。

**individual 运行**：把 `--path` 指向 `valid-0917-individual-dedup/` 下的某个档位目录，去掉
`--task-type stream --aos-pool-v03`，并使用 `--agent-timeout-sec 5400`。

**LLM 评审**：少数 WildClawBench 验证器通过 `OPENROUTER_API_KEY` 调用 LLM 评审，可选的变量还有
`OPENROUTER_BASE_URL`、`JUDGE_MODEL` 和 `AOS_JUDGE_VISION_MODEL`，用 `--ve` 传入。如果工作负载在
`[verifier.env]` 中声明了评审变量而它未设置，trial 会在开始时就失败，而不是等 agent 运行完才失败。

## 6. 查看结果

trial 写在 `jobs/<job-name>/<trial-name>/` 下：

| 路径 | 内容 |
|---|---|
| `verifier/reward.json` | 每个任务的 reward，范围 [0, 1] |
| `verifier/<task-id>/` | 每个任务的验证器输出 |
| `verifier/scheduling_metrics.json` | 每个任务的释放、领取、完成时间和最终状态 |
| `dispatcher/events.jsonl` | 每一次释放、领取和状态更新，带时间戳 |
| `agent/` | agent 的对话记录和会话日志 |
| `agent/.harbor-cgroup-resources.tsv` | trial 容器的 CPU、内存和 I/O 采样计数器 |
| `agent_stop.json` | dispatcher 是否已关闭，停止了多少个 agent 进程 |
| `result.json` | 各阶段耗时（环境准备、agent 执行、验证）和结果 |

运行中实时查看：

```bash
python -m aos_bench.monitor --jobs-dir jobs --port 4000   # 然后打开 http://localhost:4000
```

## 7. 使用代理或调度型 docker 代理

- **HTTP 代理**：通过 `--ae`（agent）和 `--ve`（验证器）传入 `http_proxy`、`https_proxy` 和
  `no_proxy`。`no_proxy` 中要保留 `aos_dispatcher`，以免发往 dispatcher 的 MCP 流量走代理。
- **队列标签**：如果你的 Docker 端点是一个按队列标签分发容器的代理，用 `HARBOR_JOB_QUEUE` 设置
  队列名。如果标签键不是 `harbor.job-queue`，再设置 `HARBOR_JOB_QUEUE_LABEL`。
- **绑核**：`HARBOR_CPUSET=0-3` 把 trial 容器固定在指定核上。

## 8. 常见问题

| 现象 | 原因和处理 |
|---|---|
| trial 卡在准备阶段，没有出现 `aos-disp-*` 容器 | 没有 dispatcher 镜像，按第 2 步构建 |
| `ModuleNotFoundError: aos_bench` | 漏了第 2 步写入 `aos_bench.pth` 的那一行 |
| 拉取 `zhuerle/aosbench` 时报 `pull access denied` 或限额错误 | 先 `docker login`，或加 `--force-build` |
| `reward.json` 中缺少某个任务 | 该任务的验证器没能运行，查看 `verifier/<task-id>/stdout.txt` |
| trial 立即失败，报 `Environment variable '…' not found` | 某个验证器需要的变量未设置，用 `--ve` 传入 |
| agent 进程被 OOM killer 结束 | 工作负载超过了 8 GiB。Docker 通过 cgroup 强制执行该限制；论文所用沙盒不强制执行，所以论文是在计分时处理超限的 |
