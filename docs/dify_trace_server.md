# Dify Workflow Trace Server

这个脚本用于自部署 Dify 场景：本地机器传入一个会话 ID，服务器返回该会话关联的 workflow run 及每个节点的时间线、输入、过程数据、输出、状态、错误和元数据。

## 依据

- Dify 的应用级运行记录在 `workflow_runs`。
- Dify 的节点级 tracing 记录在 `workflow_node_executions`，字段包括 `inputs`、`process_data`、`outputs`、`execution_metadata`、`created_at`、`finished_at` 等。
- Chatflow/Advanced Chat 的消息表 `messages` 会记录 `conversation_id` 与 `workflow_run_id` 的关联。
- End user 的外部会话标识在 `end_users.session_id`。
- 如果节点输入/输出过大，Dify 可能将完整 JSON offload 到 `workflow_node_execution_offload` + `upload_files` 指向的本地/对象存储文件。脚本支持本地存储读取。

## 环境变量

必填。Dify Docker Compose 默认没有把 PostgreSQL 的 `5432` 映射到宿主机端口，所以数据库地址要看脚本运行位置：

如果把本服务也作为 Dify compose 里的一个服务运行，或运行在同一个 Docker 网络里的容器中，使用服务名 `db_postgres`：

```bash
export DIFY_TRACE_DATABASE_URL='postgresql+psycopg2://postgres:你的密码@db_postgres:5432/dify'
```

如果脚本直接运行在 Linux 宿主机上，只有在你额外给 `db_postgres` 加了端口映射时才能用 `127.0.0.1:5432`：

```yaml
db_postgres:
  ports:
    - "127.0.0.1:5432:5432"
```

然后才可以：

```bash
export DIFY_TRACE_DATABASE_URL='postgresql+psycopg2://postgres:你的密码@127.0.0.1:5432/dify'
```

建议填写：

```bash
export DIFY_TRACE_API_KEY='换成一个长随机字符串'
export DIFY_TRACE_HOST='0.0.0.0'
export DIFY_TRACE_PORT='2916'
```

如果你的 Dify 使用本地文件存储，并希望读取被 offload 的完整节点输入/输出：

```bash
export DIFY_TRACE_STORAGE_LOCAL_PATH='/path/to/dify/docker/volumes/app/storage'
```

`DIFY_TRACE_STORAGE_LOCAL_PATH` 要指向 Dify API 容器里的 `STORAGE_LOCAL_PATH` 对应宿主机挂载目录。常见 docker compose 部署可以检查 `docker/docker-compose.yaml` 中 API 服务的 volume。

## 启动

先在 Dify 的 docker compose 目录里探测数据库是否可访问：

```bash
cd /path/to/dify/docker
/opt/script_tools/scripts/dify_db_probe.sh
```

这个探测脚本复用了之前 `dify_conversation_trace.sh` / `dify_token_usage_yesterday.sh` 的连接方式：自动识别 `db`、`db_postgres`、`postgres` 服务，并通过 `docker compose exec -T <db_service> psql` 进入数据库容器执行只读检查。它不要求 PostgreSQL 暴露宿主机端口。

在服务器安装依赖后运行：

```bash
pip install fastapi 'uvicorn[standard]' sqlalchemy psycopg2-binary pydantic
python scripts/dify_trace_server.py
```

后台运行：

```bash
nohup python scripts/dify_trace_server.py > dify-trace.log 2>&1 &
```

如果不想暴露 PostgreSQL 端口，更推荐把本服务加入 Dify compose 网络。示例：

```yaml
services:
  dify_trace:
    image: python:3.11-slim
    working_dir: /app
    volumes:
      - /opt/script_tools:/app
      - ./volumes/app/storage:/dify-storage:ro
    environment:
      DIFY_TRACE_DATABASE_URL: postgresql+psycopg2://postgres:${DB_PASSWORD:-difyai123456}@db_postgres:5432/${DB_DATABASE:-dify}
      DIFY_TRACE_API_KEY: 换成一个长随机字符串
      DIFY_TRACE_HOST: 0.0.0.0
      DIFY_TRACE_PORT: 2916
      DIFY_TRACE_STORAGE_LOCAL_PATH: /dify-storage
    command: >
      sh -c "pip install fastapi 'uvicorn[standard]' sqlalchemy psycopg2-binary pydantic
      && python scripts/dify_trace_server.py"
    ports:
      - "2916:2916"
    depends_on:
      db_postgres:
        condition: service_healthy
```

systemd 示例：

```ini
[Unit]
Description=Dify Workflow Trace Server
After=network.target

[Service]
WorkingDirectory=/opt/script_tools
Environment=DIFY_TRACE_DATABASE_URL=postgresql+psycopg2://postgres:你的密码@127.0.0.1:5432/dify
Environment=DIFY_TRACE_API_KEY=换成一个长随机字符串
Environment=DIFY_TRACE_HOST=0.0.0.0
Environment=DIFY_TRACE_PORT=2916
Environment=DIFY_TRACE_STORAGE_LOCAL_PATH=/opt/dify/docker/volumes/app/storage
ExecStart=/usr/bin/python3 /opt/script_tools/scripts/dify_trace_server.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

## 调用

自动识别：优先尝试 `workflow_run_id`，再尝试 `conversation_id`，再尝试 `end_users.session_id`。

```bash
curl -H "Authorization: Bearer $DIFY_TRACE_API_KEY" \
  "http://服务器IP:2916/trace/你的会话ID?kind=auto&limit_runs=20"
```

明确传入 Dify conversation id：

```bash
curl -H "Authorization: Bearer $DIFY_TRACE_API_KEY" \
  "http://服务器IP:2916/trace/CONVERSATION_ID?kind=conversation"
```

明确传入 Dify workflow run id：

```bash
curl -H "Authorization: Bearer $DIFY_TRACE_API_KEY" \
  "http://服务器IP:2916/trace/WORKFLOW_RUN_ID?kind=workflow_run"
```

POST 方式：

```bash
curl -X POST "http://服务器IP:2916/trace" \
  -H "Authorization: Bearer $DIFY_TRACE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"session_id":"你的会话ID","kind":"auto","limit_runs":20,"include_graph":false}'
```

返回结构中 `traces[*].timeline` 就是按节点执行顺序排列的完整追踪。自定义 workflow 工具节点通常表现为 `node_type=tool`，其工具信息会出现在 `execution_metadata.tool_info`，实际输入输出在对应节点的 `inputs`、`process_data`、`outputs`。
