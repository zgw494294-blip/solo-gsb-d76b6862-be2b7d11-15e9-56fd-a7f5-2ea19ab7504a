# 样本触发 HTTP 服务（FastAPI）

单进程内存服务：创建会话 → 逐条写入整数样本（序号从 1 连续编号）→ 以当前末条样本为中心触发“片段”，服务保留中心前后的样本；历史不足时触发返回 `409`，后续样本到齐后片段自动完成。片段可重叠、可删除，删除后容量释放，可重试此前被拒绝的写入。

状态**仅存在于单进程内存**中，重启即丢失；因此服务必须以单 worker 运行（镜像已内置该参数）。

## 启动

需要 Docker + Docker Compose v2，单条命令：

```bash
docker compose up --build -d
```

- 访问地址：<http://localhost:8000>
- 端口：`8000`（宿主机）→ `8000`（容器），如需更改端口修改 `docker-compose.yml` 中的 `"8000:8000"`。
- 交互式文档（Swagger UI）：<http://localhost:8000/docs>
- 健康检查：<http://localhost:8000/health>

停止：`docker compose down`

无 Docker 时可本地运行（Python 3.11）：

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

## 规则摘要

- 创建会话指定历史长度 `H`、样本容量 `C`，约束 `1 ≤ H ≤ C ≤ 1000`。
- 触发参数 `before` / `after` 均为非负整数，且 `before < H`、`before + 1 + after ≤ C`（违反返回 `400`）。
- 触发时以**当前末条**样本为中心；前面不足 `before` 条返回 `409`（以后续新末条重新触发即可）。
- 片段创建后等待后续样本：到达 `after` 条后状态由 `pending` 变为 `completed`，内容按序号升序返回。
- 保留集合 = **最近 H 条样本** ∪ **所有未删除片段引用的样本**，同一序号只计一次；两者都不需要的样本立即回收。
- 新样本在保留集合中无处容纳时返回 `409`：**不消耗序号、不改变历史或片段**；删除某个片段释放引用后可用**相同请求重试**。
- 删除片段只解除该片段的引用；已完成片段在删除前内容保持不变。
- 所有操作在进程内同一把锁下串行执行，会话内各操作等效于某个串行顺序。

## HTTP 调用示例

以下示例用 `curl` 演示（假设 `H=5, C=5`）。

### 1. 创建会话

```bash
curl -s -X POST http://localhost:8000/sessions \
  -H 'Content-Type: application/json' \
  -d '{"H": 5, "C": 5}'
```

```json
{ "id": "a1b2...", "H": 5, "C": 5, "next_seq": 1, "window_start": 1, "stored_count": 0, "history": [] }
```

把会话 ID 存到变量：

```bash
SID=a1b2...   # 替换为上一步返回的 id
```

### 2. 逐条写入整数样本

```bash
curl -s -X POST http://localhost:8000/sessions/$SID/samples \
  -H 'Content-Type: application/json' -d '{"value": 10}'
# {"seq":1,"value":10,"next_seq":2}

curl -s -X POST http://localhost:8000/sessions/$SID/samples \
  -H 'Content-Type: application/json' -d '{"value": 20}'
# {"seq":2,"value":20,"next_seq":3}

curl -s -X POST http://localhost:8000/sessions/$SID/samples \
  -H 'Content-Type: application/json' -d '{"value": 30}'
# {"seq":3,"value":30,"next_seq":4}
```

### 3. 历史不足时触发 → 409

末条是序号 3，而 `before=4` 时前面只有 2 条：

```bash
curl -s -i -X POST http://localhost:8000/sessions/$SID/fragments \
  -H 'Content-Type: application/json' -d '{"before": 4, "after": 0}'
# HTTP/1.1 409 Conflict
# {"detail":"history insufficient: need 4 samples before center seq 3, only 2 available"}
```

### 4. 成功触发（前 2 后 2，中心为序号 3）

```bash
curl -s -X POST http://localhost:8000/sessions/$SID/fragments \
  -H 'Content-Type: application/json' -d '{"before": 2, "after": 2}'
```

```json
{
  "id": "f9e0...",
  "before": 2, "after": 2,
  "start_seq": 1, "center_seq": 3, "end_seq": 5,
  "status": "pending",
  "arrived": 3,
  "items": [
    {"seq": 1, "value": 10},
    {"seq": 2, "value": 20},
    {"seq": 3, "value": 30},
    {"seq": 4, "value": null},
    {"seq": 5, "value": null}
  ]
}
```

### 5. 继续写入，片段到齐后完成

```bash
curl -s -X POST http://localhost:8000/sessions/$SID/samples \
  -H 'Content-Type: application/json' -d '{"value": 40}'   # seq 4
curl -s -X POST http://localhost:8000/sessions/$SID/samples \
  -H 'Content-Type: application/json' -d '{"value": 50}'   # seq 5
```

查询片段列表：

```bash
curl -s http://localhost:8000/sessions/$SID/fragments
```

查询单个片段（`FID` 为触发时返回的片段 id）：

```bash
curl -s http://localhost:8000/sessions/$SID/fragments/$FID
```

```json
{
  "id": "f9e0...",
  "before": 2, "after": 2,
  "start_seq": 1, "center_seq": 3, "end_seq": 5,
  "status": "completed",
  "arrived": 5,
  "items": [
    {"seq": 1, "value": 10}, {"seq": 2, "value": 20},
    {"seq": 3, "value": 30}, {"seq": 4, "value": 40},
    {"seq": 5, "value": 50}
  ]
}
```

### 6. 容量耗尽 → 409，删除片段后重试

`C=5` 时，一个覆盖序号 1–5 的片段会把这些样本全部钉住，第 6 条无处容纳：

```bash
curl -s -i -X POST http://localhost:8000/sessions/$SID/samples \
  -H 'Content-Type: application/json' -d '{"value": 60}'
# HTTP/1.1 409 Conflict
# {"detail":"capacity exhausted: sample rejected, no sequence consumed; ..."}
```

删除片段解除引用后，用**完全相同的请求**重试即可成功（序号不跳号）：

```bash
curl -s -X DELETE http://localhost:8000/sessions/$SID/fragments/$FID
# HTTP/1.1 204 No Content

curl -s -X POST http://localhost:8000/sessions/$SID/samples \
  -H 'Content-Type: application/json' -d '{"value": 60}'
# {"seq":6,"value":60,"next_seq":7}
```

### 7. 查询会话（含最近 H 条历史）

```bash
curl -s http://localhost:8000/sessions/$SID
```

```json
{
  "id": "a1b2...", "H": 5, "C": 5,
  "next_seq": 7, "window_start": 2, "stored_count": 5,
  "history": [
    {"seq": 2, "value": 20}, {"seq": 3, "value": 30},
    {"seq": 4, "value": 40}, {"seq": 5, "value": 50},
    {"seq": 6, "value": 60}
  ]
}
```

## 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/sessions` | 创建会话，body：`{"H": 5, "C": 5}` |
| GET | `/sessions/{id}` | 查询会话状态与最近 H 条历史 |
| POST | `/sessions/{id}/samples` | 写入一条整数样本，body：`{"value": 10}` |
| POST | `/sessions/{id}/fragments` | 以当前末条为中心触发，body：`{"before": 2, "after": 2}` |
| GET | `/sessions/{id}/fragments` | 列出全部未删除片段及状态 |
| GET | `/sessions/{id}/fragments/{fid}` | 查询片段：状态、序号与值（按序号升序） |
| DELETE | `/sessions/{id}/fragments/{fid}` | 删除片段、解除引用并回收容量 |
| GET | `/health` | 健康检查 |

## 状态码

| 状态码 | 含义 |
| --- | --- |
| `201` | 创建会话 / 写入样本 / 触发片段成功 |
| `204` | 删除片段成功 |
| `400` | 参数语义错误：`H > C`、`before ≥ H`、`before+1+after > C` |
| `404` | 会话或片段不存在 |
| `409` | 历史不足无法触发；或容量不足拒绝写入（不消耗序号，可重试） |
| `422` | 请求体类型/范围校验失败（如负数、非整数） |

## 项目结构

```
.
├── app/main.py          # 服务实现
├── test_service.py      # 行为验证脚本（直接 python3 test_service.py 运行）
├── requirements.txt
├── Dockerfile           # 单 worker uvicorn
├── docker-compose.yml   # 单条命令启动
└── README.md
```

> 说明：服务必须单进程单 worker（`--workers 1`），水平扩容会导致内存状态不一致。
> 行为验证脚本额外依赖 `httpx`（`pip install httpx`，本地校验用，不属于运行时依赖）。
