# sample-fragment-service

保留触发前后样本的 HTTP 服务（Python + FastAPI，状态仅存于单进程内存）。

- 创建会话时指定历史长度 `H` 与样本容量 `C`（`1 ≤ H ≤ C ≤ 1000`）。
- 逐条写入整数样本，序号从 1 开始连续编号。
- 触发以**当前末条**为中心生成片段：前 `before` 条 + 中心 + 后 `after` 条
  （`before`、`after` 为非负整数，`before < H`，且 `before + 1 + after ≤ C`）。
  历史不足返回 `409`；`after > 0` 时片段处于 `pending`，后续样本到齐后变为 `completed`。
- 片段可重叠。查询片段返回 `pending` / `completed` 状态，以及按序号升序排列的序号和值。
- **容量核算**：最近 `H` 条历史与所有未删除片段引用的样本共同占用容量 `C`，
  同一序号只计一次（并集）；仅回收两者都不再需要的样本。
  新样本无处容纳时写入返回 `409`，**不消耗序号、不改变历史或片段**。
- 删除片段只解除该片段的引用，回收后写入可重试；已完成片段在删除前内容不变。
- 会话内所有操作按会话级锁串行执行，等效于某个串行顺序。

## 启动

单条命令（Docker Compose）：

```bash
docker compose up --build
```

- 访问地址：http://localhost:8000
- 交互式接口文档（Swagger UI）：http://localhost:8000/docs
- 端口：`8000`（宿主机 `8000` → 容器 `8000`，可在 `docker-compose.yml` 中调整）

也可以只用 Docker：

```bash
docker build -t sample-fragment-service .
docker run --rm -p 8000:8000 sample-fragment-service
```

## 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/sessions` | 创建会话，指定 `history`(H)、`capacity`(C) |
| GET | `/sessions/{sid}` | 查看会话状态（已用容量、存活序号、片段列表） |
| POST | `/sessions/{sid}/samples` | 写入一条整数样本 |
| POST | `/sessions/{sid}/triggers` | 以当前末条为中心触发片段 |
| GET | `/sessions/{sid}/fragments` | 列出所有未删除片段 |
| GET | `/sessions/{sid}/fragments/{fid}` | 查询片段状态与样本内容 |
| DELETE | `/sessions/{sid}/fragments/{fid}` | 删除片段（解除引用并回收样本） |

错误码：`404` 会话/片段不存在；`409` 历史不足或容量不足；
`400` 触发参数违反 `before < H` 或 `before+1+after ≤ C`；`422` 请求体不合法。

## 调用示例

### 1. 创建会话（H=2，C=3）

```bash
curl -s -X POST http://localhost:8000/sessions \
  -H 'Content-Type: application/json' \
  -d '{"history": 2, "capacity": 3}'
# {"session_id":1,"history":2,"capacity":3}
```

### 2. 逐条写入样本

```bash
for v in 10 20 30; do
  curl -s -X POST http://localhost:8000/sessions/1/samples \
    -H 'Content-Type: application/json' -d "{\"value\": $v}"; echo
done
# {"seq":1,"value":10}
# {"seq":2,"value":20}
# {"seq":3,"value":30}
```

历史窗口只保留最近 H=2 条，序号 1 已被回收：

```bash
curl -s http://localhost:8000/sessions/1
# {"session_id":1,"history":2,"capacity":3,"next_seq":4,"used":2,"stored_seqs":[2,3],"fragments":[]}
```

### 3. 触发片段（中心=当前末条 3，前 1 条 + 中心 + 后 1 条）

```bash
curl -s -X POST http://localhost:8000/sessions/1/triggers \
  -H 'Content-Type: application/json' \
  -d '{"before": 1, "after": 1}'
# {"fragment_id":1,"center":3,"before":1,"after":1,"start":2,"end":4,"status":"pending"}
```

### 4. 查询片段：pending → completed

```bash
curl -s http://localhost:8000/sessions/1/fragments/1
# {"fragment_id":1,...,"status":"pending","samples":[{"seq":2,"value":20},{"seq":3,"value":30}]}

curl -s -X POST http://localhost:8000/sessions/1/samples \
  -H 'Content-Type: application/json' -d '{"value": 40}'
# {"seq":4,"value":40}   —— 片段所需的后续样本到齐

curl -s http://localhost:8000/sessions/1/fragments/1
# {"fragment_id":1,...,"status":"completed",
#  "samples":[{"seq":2,"value":20},{"seq":3,"value":30},{"seq":4,"value":40}]}
```

### 5. 容量不足 → 409，删除后重试成功

片段 1 仍引用序号 2–4，历史窗口需要 3–4，并集 {2,3,4} 已占满 C=3，
再写一条需要 {2,3,4,5} 共 4 个位置，超出容量：

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8000/sessions/1/samples \
  -H 'Content-Type: application/json' -d '{"value": 50}'
# 409   —— 序号未消耗，next_seq 仍为 5

curl -s -X DELETE http://localhost:8000/sessions/1/fragments/1 -o /dev/null -w '%{http_code}\n'
# 204   —— 解除引用，序号 2 被回收

curl -s -X POST http://localhost:8000/sessions/1/samples \
  -H 'Content-Type: application/json' -d '{"value": 50}'
# {"seq":5,"value":50}   —— 回收后重试成功，序号连续
```

### 6. 历史不足 → 409

```bash
curl -s -X POST http://localhost:8000/sessions \
  -H 'Content-Type: application/json' -d '{"history": 2, "capacity": 3}'
# {"session_id":2,...}

curl -s -X POST http://localhost:8000/sessions/2/triggers \
  -H 'Content-Type: application/json' -d '{"before": 1, "after": 0}'
# 409 {"detail":"历史不足：已有样本不足以覆盖中心及其前 before 条"}
```

## 本地开发（可选）

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```
