"""样本片段服务。

单进程内存态的 HTTP 服务：按会话维护连续编号的整数样本，
支持以当前末条为中心触发片段（前 before 条 + 中心 + 后 after 条），
片段等后续样本到齐后完成；最近 H 条历史与所有未删除片段共同占用容量 C。
"""
from __future__ import annotations

import asyncio

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, model_validator

app = FastAPI(title="sample-fragment-service", version="1.0.0")


# ---------------- 请求模型 ----------------

class CreateSessionRequest(BaseModel):
    history: int = Field(ge=1, le=1000, description="历史长度 H")
    capacity: int = Field(ge=1, le=1000, description="样本容量 C")

    @model_validator(mode="after")
    def _check_h_le_c(self) -> "CreateSessionRequest":
        if self.history > self.capacity:
            raise ValueError("必须满足 1 <= H <= C <= 1000")
        return self


class WriteSampleRequest(BaseModel):
    value: int = Field(description="整数样本值")


class TriggerRequest(BaseModel):
    before: int = Field(ge=0, description="中心之前的条数（非负整数）")
    after: int = Field(ge=0, description="中心之后的条数（非负整数）")


# ---------------- 会话状态 ----------------

class Fragment:
    __slots__ = ("fragment_id", "center", "before", "after")

    def __init__(self, fragment_id: int, center: int, before: int, after: int):
        self.fragment_id = fragment_id
        self.center = center
        self.before = before
        self.after = after

    @property
    def start(self) -> int:
        return self.center - self.before

    @property
    def end(self) -> int:
        return self.center + self.after


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=409, detail=detail)


class Session:
    """单个会话的全部内存状态；所有操作在 self.lock 下串行执行。"""

    def __init__(self, session_id: int, history: int, capacity: int):
        self.session_id = session_id
        self.history = history            # H
        self.capacity = capacity          # C
        self.next_seq = 1                 # 下一条样本的序号（从 1 连续编号）
        self.samples: dict[int, int] = {}  # 当前存活的样本：序号 -> 值
        self.fragments: dict[int, Fragment] = {}  # 未删除的片段
        self.next_fragment_id = 1
        self.lock = asyncio.Lock()

    # ---- 容量核算 ----

    def needed_seqs(self, upto: int) -> set[int]:
        """假设已写入 upto 条时，历史窗口与所有未删除片段共同需要的序号集合。

        同序号只计一次（集合语义）；片段只统计已写入的部分，
        未写入的后续样本在将来写入时再参与核算。
        """
        lo = max(1, upto - self.history + 1)
        needed = set(range(lo, upto + 1))
        for frag in self.fragments.values():
            hi = min(frag.end, upto)
            if frag.start <= hi:
                needed.update(range(frag.start, hi + 1))
        return needed

    def reclaim(self) -> None:
        """回收历史窗口与所有未删除片段都不再需要的样本。"""
        live = self.needed_seqs(self.next_seq - 1)
        for seq in list(self.samples):
            if seq not in live:
                del self.samples[seq]

    # ---- 操作 ----

    def write(self, value: int) -> int:
        """写入一条样本；容量不足时抛 409，不消耗序号、不改变历史或片段。"""
        n = self.next_seq
        needed = self.needed_seqs(n)
        if len(needed) > self.capacity:
            raise _conflict("容量不足：新样本无处容纳，序号未消耗，状态未改变")
        # 不变式：needed ⊆ 现有 samples ∪ {n}，故此处不会 KeyError
        kept = {seq: self.samples[seq] for seq in needed if seq != n}
        kept[n] = value
        self.samples = kept
        self.next_seq = n + 1
        return n

    def trigger(self, before: int, after: int) -> Fragment:
        """以当前末条为中心创建片段；历史不足时抛 409。"""
        if before >= self.history:
            raise HTTPException(
                status_code=400,
                detail=f"before 必须小于历史长度 H={self.history}",
            )
        if before + 1 + after > self.capacity:
            raise HTTPException(
                status_code=400,
                detail=f"before+1+after 不能超过样本容量 C={self.capacity}",
            )
        center = self.next_seq - 1
        if center - before < 1:
            raise _conflict("历史不足：已有样本不足以覆盖中心及其前 before 条")
        frag = Fragment(self.next_fragment_id, center, before, after)
        self.fragments[frag.fragment_id] = frag
        self.next_fragment_id += 1
        return frag

    def status_of(self, frag: Fragment) -> str:
        return "completed" if frag.end < self.next_seq else "pending"


# ---------------- 会话注册表（单进程内存） ----------------

class Registry:
    def __init__(self) -> None:
        self.sessions: dict[int, Session] = {}
        self.next_id = 1


registry = Registry()


def _get_session(session_id: int) -> Session:
    sess = registry.sessions.get(session_id)
    if sess is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return sess


def _fragment_summary(sess: Session, frag: Fragment) -> dict:
    return {
        "fragment_id": frag.fragment_id,
        "center": frag.center,
        "before": frag.before,
        "after": frag.after,
        "start": frag.start,
        "end": frag.end,
        "status": sess.status_of(frag),
    }


def _fragment_detail(sess: Session, frag: Fragment) -> dict:
    # 片段引用且已写入的样本一定仍被保留（未被回收），按序号升序返回
    hi = min(frag.end, sess.next_seq - 1)
    return {
        **_fragment_summary(sess, frag),
        "samples": [
            {"seq": seq, "value": sess.samples[seq]}
            for seq in range(frag.start, hi + 1)
        ],
    }


# ---------------- HTTP 接口 ----------------

@app.get("/")
async def root() -> dict:
    return {"service": "sample-fragment-service", "docs": "/docs"}


@app.post("/sessions", status_code=201)
async def create_session(req: CreateSessionRequest) -> dict:
    # 单事件循环内无 await，创建过程天然原子
    sid = registry.next_id
    registry.next_id += 1
    registry.sessions[sid] = Session(sid, req.history, req.capacity)
    return {"session_id": sid, "history": req.history, "capacity": req.capacity}


@app.get("/sessions/{session_id}")
async def get_session(session_id: int) -> dict:
    sess = _get_session(session_id)
    async with sess.lock:
        return {
            "session_id": sess.session_id,
            "history": sess.history,
            "capacity": sess.capacity,
            "next_seq": sess.next_seq,
            "used": len(sess.samples),
            "stored_seqs": sorted(sess.samples),
            "fragments": sorted(sess.fragments),
        }


@app.post("/sessions/{session_id}/samples", status_code=201)
async def write_sample(session_id: int, req: WriteSampleRequest) -> dict:
    sess = _get_session(session_id)
    async with sess.lock:
        seq = sess.write(req.value)
        return {"seq": seq, "value": req.value}


@app.post("/sessions/{session_id}/triggers", status_code=201)
async def trigger(session_id: int, req: TriggerRequest) -> dict:
    sess = _get_session(session_id)
    async with sess.lock:
        frag = sess.trigger(req.before, req.after)
        return _fragment_summary(sess, frag)


@app.get("/sessions/{session_id}/fragments")
async def list_fragments(session_id: int) -> list:
    sess = _get_session(session_id)
    async with sess.lock:
        return [
            _fragment_summary(sess, sess.fragments[fid])
            for fid in sorted(sess.fragments)
        ]


@app.get("/sessions/{session_id}/fragments/{fragment_id}")
async def get_fragment(session_id: int, fragment_id: int) -> dict:
    sess = _get_session(session_id)
    async with sess.lock:
        frag = sess.fragments.get(fragment_id)
        if frag is None:
            raise HTTPException(status_code=404, detail="片段不存在")
        return _fragment_detail(sess, frag)


@app.delete("/sessions/{session_id}/fragments/{fragment_id}", status_code=204)
async def delete_fragment(session_id: int, fragment_id: int) -> None:
    sess = _get_session(session_id)
    async with sess.lock:
        if sess.fragments.pop(fragment_id, None) is None:
            raise HTTPException(status_code=404, detail="片段不存在")
        # 解除引用后立刻回收不再被任何一方需要的样本
        sess.reclaim()
