"""内存型样本触发 / 片段查询服务。

核心规则：
- 每个会话指定历史长度 H 与样本容量 C（1 <= H <= C <= 1000）。
- 样本逐条写入，序号从 1 连续递增。
- 触发以当前末条样本为中心：before 条在前、after 条在后；
  before < H，含中心总长 before + 1 + after <= C。
- 触发时历史不足返回 409；后续样本到齐后片段才完成。
- 存储保留集合 = 最近 H 条样本 ∪ 所有未删除片段引用的样本（同序号只计一次）；
  仅回收两者都不需要的样本。新样本无处可容纳返回 409，
  且不消耗序号、不改变历史或片段。
- 状态仅存于单进程内存，所有操作在同一把全局锁下串行执行，
  因而会话内各操作等效于某个串行顺序（必须以单 worker 运行）。
"""

from __future__ import annotations

from asyncio import Lock
from typing import Dict, List, Optional, Set
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel, Field

MAX_CAPACITY = 1000

app = FastAPI(
    title="Sample Trigger Service",
    version="1.0.0",
    description="单进程内存的样本触发 / 片段查询 HTTP 服务（状态不持久化）。",
)


# --------------------------------------------------------------------------- #
# 请求 / 响应模型
# --------------------------------------------------------------------------- #
class CreateSessionBody(BaseModel):
    H: int = Field(ge=1, le=MAX_CAPACITY, description="历史长度")
    C: int = Field(ge=1, le=MAX_CAPACITY, description="样本容量")


class WriteSampleBody(BaseModel):
    value: int = Field(description="整数样本值")


class TriggerBody(BaseModel):
    before: int = Field(ge=0, description="中心样本之前的条数")
    after: int = Field(ge=0, description="中心样本之后的条数")


class Item(BaseModel):
    seq: int
    value: Optional[int] = None


class FragmentMeta(BaseModel):
    id: str
    before: int
    after: int
    start_seq: int
    center_seq: int
    end_seq: int
    status: str  # "pending" | "completed"


class FragmentDetail(FragmentMeta):
    arrived: int
    items: List[Item]


class SessionInfo(BaseModel):
    id: str
    H: int
    C: int
    next_seq: int
    window_start: int
    stored_count: int
    history: List[Item]


# --------------------------------------------------------------------------- #
# 内存状态
# --------------------------------------------------------------------------- #
class Fragment:
    __slots__ = ("id", "before", "after", "start_seq", "center_seq", "end_seq")

    def __init__(self, fid: str, before: int, after: int,
                 start_seq: int, center_seq: int, end_seq: int) -> None:
        self.id = fid
        self.before = before
        self.after = after
        self.start_seq = start_seq
        self.center_seq = center_seq
        self.end_seq = end_seq


class Session:
    __slots__ = ("H", "C", "next_seq", "samples", "fragments")

    def __init__(self, H: int, C: int) -> None:
        self.H = H
        self.C = C
        self.next_seq = 1  # 下一条写入将使用的序号（也等于已分配序号数 + 1）
        self.samples: Dict[int, int] = {}  # seq -> value，仅保留仍被需要的样本
        self.fragments: Dict[str, Fragment] = {}  # 未删除的片段（插入即创建顺序）


_sessions: Dict[str, Session] = {}
# 单进程内存模型：全局锁使所有操作天然满足“等效于某个串行顺序”。
_lock = Lock()


# --------------------------------------------------------------------------- #
# 辅助函数（调用方需持锁）
# --------------------------------------------------------------------------- #
def _get_session(session_id: str) -> Session:
    session = _sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    return session


def _last_seq(session: Session) -> int:
    """当前末条样本序号；尚无样本时为 0。"""
    return session.next_seq - 1


def _window_start(session: Session) -> int:
    """当前历史窗口（最近 H 条）的最小序号；不足 H 条时从 1 开始。

    已写入序号为 1..next_seq-1，最近 H 条下界为
    (next_seq-1) - H + 1 = next_seq - H。
    """
    return max(1, session.next_seq - session.H)


def _retained_seqs(session: Session) -> Set[int]:
    """必须保留的序号集合：最近 H 条 ∪ 所有未删除片段引用的序号。"""
    needed: Set[int] = set()
    if session.next_seq > 1:
        needed.update(range(_window_start(session), session.next_seq))
    for frag in session.fragments.values():
        needed.update(range(frag.start_seq, frag.end_seq + 1))
    return needed


def _is_completed(session: Session, frag: Fragment) -> bool:
    """片段所需的最后一条已写入即完成。"""
    return frag.end_seq < session.next_seq


def _reclaim(session: Session) -> None:
    """回收历史与所有片段都不再需要的样本。"""
    keep = _retained_seqs(session)
    for seq in [s for s in session.samples if s not in keep]:
        del session.samples[seq]


def _fragment_detail(session: Session, frag: Fragment) -> dict:
    completed = _is_completed(session, frag)
    items: List[Item] = []
    arrived = 0
    for seq in range(frag.start_seq, frag.end_seq + 1):
        if seq < session.next_seq:
            # 范围内已到达的序号必定被保留：片段引用之，写入时保留集合
            # （含该片段）已校验容量，回收也不会删除它。
            arrived += 1
            items.append(Item(seq=seq, value=session.samples[seq]))
        else:
            items.append(Item(seq=seq, value=None))
    return {
        "id": frag.id,
        "before": frag.before,
        "after": frag.after,
        "start_seq": frag.start_seq,
        "center_seq": frag.center_seq,
        "end_seq": frag.end_seq,
        "status": "completed" if completed else "pending",
        "arrived": arrived,
        "items": items,
    }


# --------------------------------------------------------------------------- #
# 健康检查
# --------------------------------------------------------------------------- #
@app.get("/")
def root() -> dict:
    return {"service": "sample-trigger", "docs": "/docs", "health": "/health"}


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "sessions": len(_sessions)}


# --------------------------------------------------------------------------- #
# 会话
# --------------------------------------------------------------------------- #
@app.post("/sessions", status_code=201)
async def create_session(body: CreateSessionBody) -> SessionInfo:
    if body.H > body.C:
        raise HTTPException(status_code=400, detail="must satisfy H <= C")
    async with _lock:
        session_id = uuid4().hex
        _sessions[session_id] = Session(body.H, body.C)
        return SessionInfo(
            id=session_id, H=body.H, C=body.C, next_seq=1,
            window_start=1, stored_count=0, history=[],
        )


@app.get("/sessions/{session_id}")
async def get_session(session_id: str) -> SessionInfo:
    async with _lock:
        s = _get_session(session_id)
        history = [
            Item(seq=seq, value=s.samples[seq])
            for seq in range(_window_start(s), s.next_seq)
            if seq in s.samples
        ]
        return SessionInfo(
            id=session_id, H=s.H, C=s.C, next_seq=s.next_seq,
            window_start=_window_start(s),
            stored_count=len(s.samples), history=history,
        )


# --------------------------------------------------------------------------- #
# 样本写入
# --------------------------------------------------------------------------- #
@app.post("/sessions/{session_id}/samples", status_code=201)
async def write_sample(session_id: str, body: WriteSampleBody) -> dict:
    async with _lock:
        s = _get_session(session_id)
        seq = s.next_seq

        # 写入成功后历史窗口将覆盖 [window_lo, seq]（含新条共至多 H 条）。
        window_lo = max(1, seq - s.H + 1)
        needed: Set[int] = set(range(window_lo, seq + 1))
        for frag in s.fragments.values():
            needed.update(range(frag.start_seq, frag.end_seq + 1))

        # 只有当前已存储的序号才真正占用容量；尚未到达的片段引用不占容量。
        occupied = len(needed.intersection(s.samples))
        if occupied + 1 > s.C:
            # 拒绝：不消耗序号、不改变历史或片段，客户端可删除片段后重试。
            raise HTTPException(
                status_code=409,
                detail=(
                    "capacity exhausted: sample rejected, no sequence consumed; "
                    "delete a fragment to release references and retry"
                ),
            )

        s.samples[seq] = body.value
        s.next_seq += 1
        _reclaim(s)

        return {"seq": seq, "value": body.value, "next_seq": s.next_seq}


# --------------------------------------------------------------------------- #
# 片段触发 / 查询 / 删除
# --------------------------------------------------------------------------- #
@app.post("/sessions/{session_id}/fragments", status_code=201)
async def trigger_fragment(session_id: str, body: TriggerBody) -> dict:
    async with _lock:
        s = _get_session(session_id)

        # 永久性参数错误优先于瞬时的“历史不足”。
        if body.before >= s.H:
            raise HTTPException(
                status_code=400, detail=f"before must be < H (H={s.H})"
            )
        total = body.before + 1 + body.after
        if total > s.C:
            raise HTTPException(
                status_code=400,
                detail=f"total length before+1+after={total} exceeds C={s.C}",
            )

        center = _last_seq(s)
        if center == 0:
            raise HTTPException(
                status_code=409, detail="history insufficient: no sample yet"
            )
        start = center - body.before
        if start < 1:
            # 前面样本条数不够；以后续写入的新末条重新触发即可。
            raise HTTPException(
                status_code=409,
                detail=(
                    f"history insufficient: need {body.before} samples before "
                    f"center seq {center}, only {center - 1} available"
                ),
            )

        end = center + body.after
        frag = Fragment(uuid4().hex, body.before, body.after, start, center, end)
        s.fragments[frag.id] = frag
        # 触发不写入任何样本；前部已在历史窗口内，后部尚未到达，故无需回收。
        return _fragment_detail(s, frag)


@app.get("/sessions/{session_id}/fragments")
async def list_fragments(session_id: str) -> dict:
    async with _lock:
        s = _get_session(session_id)
        fragments = [
            FragmentMeta(
                id=f.id, before=f.before, after=f.after,
                start_seq=f.start_seq, center_seq=f.center_seq,
                end_seq=f.end_seq,
                status="completed" if _is_completed(s, f) else "pending",
            )
            for f in s.fragments.values()
        ]
        return {"count": len(fragments), "fragments": fragments}


@app.get("/sessions/{session_id}/fragments/{fragment_id}")
async def get_fragment(session_id: str, fragment_id: str) -> dict:
    async with _lock:
        s = _get_session(session_id)
        frag = s.fragments.get(fragment_id)
        if frag is None:
            raise HTTPException(status_code=404, detail="fragment not found")
        return _fragment_detail(s, frag)


@app.delete("/sessions/{session_id}/fragments/{fragment_id}", status_code=204)
async def delete_fragment(session_id: str, fragment_id: str) -> Response:
    async with _lock:
        s = _get_session(session_id)
        if fragment_id not in s.fragments:
            raise HTTPException(status_code=404, detail="fragment not found")
        del s.fragments[fragment_id]
        # 仅解除引用；此刻历史与其它片段都不需要的样本立即回收，
        # 腾出的容量可用于重试此前 409 的写入。
        _reclaim(s)
        return Response(status_code=204)
