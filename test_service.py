"""端到端行为验证（不随镜像发布，仅本地验证用）。"""
from fastapi.testclient import TestClient

from app.main import app

c = TestClient(app)
ok = 0
fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"PASS {name}")
    else:
        fail += 1
        print(f"FAIL {name} {extra}")


def new(H, C, code=201):
    r = c.post("/sessions", json={"H": H, "C": C})
    assert r.status_code == code, (r.status_code, r.text)
    return r.json().get("id")


def write(sid, value, code=201):
    r = c.post(f"/sessions/{sid}/samples", json={"value": value})
    assert r.status_code == code, (r.status_code, r.text)
    return r


def trigger(sid, before, after, code=201):
    r = c.post(f"/sessions/{sid}/fragments",
               json={"before": before, "after": after})
    assert r.status_code == code, (r.status_code, r.text)
    return r


# 1. 创建校验
r = c.post("/sessions", json={"H": 6, "C": 5})
check("H>C -> 400", r.status_code == 400)
r = c.post("/sessions", json={"H": 0, "C": 5}); check("H=0 -> 422", r.status_code == 422)
r = c.post("/sessions", json={"H": 1, "C": 1001}); check("C>1000 -> 422", r.status_code == 422)

sid = new(5, 5)
check("unknown session -> 404",
      c.get("/sessions/nope").status_code == 404)

# 2. 无样本触发 -> 409
r = trigger(sid, 0, 0, code=409)

# 3. 写入 1..3
for v in (10, 20, 30):
    write(sid, v)
info = c.get(f"/sessions/{sid}").json()
check("next_seq=3", info["next_seq"] == 4, info)

# 4. 历史不足触发 -> 409，且不创建片段
r = trigger(sid, 4, 0, code=409)
lst = c.get(f"/sessions/{sid}/fragments").json()
check("failed trigger creates nothing", lst["count"] == 0, lst)

# 5. before>=H -> 400；总长>C -> 400
check("before>=H -> 400", trigger(sid, 5, 0, code=400).status_code == 400)
check("before+1+after>C -> 400", trigger(sid, 0, 5, code=400).status_code == 400)
r = trigger(sid, -1, 0, code=422); check("before<0 -> 422", r.status_code == 422)

# 6. 触发 前2后2，中心 seq3 -> pending，items 按序含 null
r = trigger(sid, 2, 2).json()
fid = r["id"]
check("trigger pending", r["status"] == "pending", r)
check("start/center/end", (r["start_seq"], r["center_seq"], r["end_seq"]) == (1, 3, 5), r)
check("arrived=3", r["arrived"] == 3, r)
check("items ordered w/ null",
      r["items"] == [
          {"seq": 1, "value": 10}, {"seq": 2, "value": 20},
          {"seq": 3, "value": 30}, {"seq": 4, "value": None},
          {"seq": 5, "value": None}], r["items"])

# 7. 补齐后续 -> completed
write(sid, 40); write(sid, 50)
d = c.get(f"/sessions/{sid}/fragments/{fid}").json()
check("completed after arrivals", d["status"] == "completed" and d["arrived"] == 5, d)

# 8. 容量：片段钉住 1..5，第6条 409 且不消耗序号
r = write(sid, 60, code=409)
info = c.get(f"/sessions/{sid}").json()
check("409 no seq consumed", info["next_seq"] == 6 and info["stored_count"] == 5, info)

# 9. 被片段钉住导致 seq6 409；删除片段后用相同请求重试。
#    删除时 seq1 仍在当前窗口[1,5]故被保留；写入 seq6 时新窗口[2,6]不再需要
#    seq1，它被原子回收，并集 {2..6} 恰好 C 条 -> 成功，序号不跳号。
check("delete -> 204",
      c.delete(f"/sessions/{sid}/fragments/{fid}").status_code == 204)
check("delete missing frag -> 404",
      c.delete(f"/sessions/{sid}/fragments/{fid}").status_code == 404)
r = write(sid, 60).json()
check("retry succeeds with seq6 after delete", r["seq"] == 6, r)
info = c.get(f"/sessions/{sid}").json()
check("history window 2..6, seq1 reclaimed",
      [i["seq"] for i in info["history"]] == [2, 3, 4, 5, 6]
      and info["stored_count"] == 5, info)

# 9b. 片段把窗口外的旧序号也钉住时，删除前写入 409，删除后即可重试
sid_evict = new(3, 5)
for v in (1, 2, 3, 4, 5):
    write(sid_evict, v)                  # 当前窗口 3..5
trigger(sid_evict, 2, 0)                 # 片段[3,5]（已完成）
trigger(sid_evict, 1, 2)                 # 片段[4,7] pending（中心5）
write(sid_evict, 6); write(sid_evict, 7) # 当前窗口 5..7，片段[4,7]完成
# seq8：新窗口6..8 ∪ {3,4,5} ∪ {4,5,6,7} = {3..8} 共 6 条 + 新条 -> 409
check("pinned older seqs block seq8",
      write(sid_evict, 8, code=409).status_code == 409)
fe0 = [f for f in c.get(f"/sessions/{sid_evict}/fragments").json()["fragments"]
       if f["before"] == 2][0]
c.delete(f"/sessions/{sid_evict}/fragments/{fe0['id']}")
# 删除[3,5]后：新窗口6..8 ∪ [4,7] = {4..8} 共 5 条 -> 可容纳 seq8
r = write(sid_evict, 8).json()
check("seq8 admitted after releasing old reference", r["seq"] == 8, r)

# 10. 片段引用的旧序号即使滑出历史也保留；完成后删除才回收
sid2 = new(3, 10)
for v in range(1, 7):
    write(sid2, v * 100)  # seq1..6，历史窗口 4..6
r = trigger(sid2, 0, 2).json()   # 中心6，范围6..8 pending
fid2 = r["id"]
write(sid2, 700); write(sid2, 800)
d = c.get(f"/sessions/{sid2}/fragments/{fid2}").json()
check("frag2 completed content fixed",
      d["status"] == "completed" and
      [(i["seq"], i["value"]) for i in d["items"]] == [(6, 600), (7, 700), (8, 800)], d)
# 再写两条使历史窗口 6..8；片段内容不变
write(sid2, 900)
d = c.get(f"/sessions/{sid2}/fragments/{fid2}").json()
check("completed fragment immutable until deleted",
      [(i["seq"], i["value"]) for i in d["items"]] == [(6, 600), (7, 700), (8, 800)], d)

# 11. 片段重叠：同序号只计一次容量（H=2,C=3）
sid3 = new(2, 3)
write(sid3, 1)
trigger(sid3, 0, 2)  # 范围1..3
write(sid3, 2)       # seq2
# 此时中心2：片段[1,3] ∪ 写seq3窗口[2,3] = {1,2,3}，可容纳
write(sid3, 3)       # seq3
trigger(sid3, 1, 0)  # 重叠片段 2..3（前1）
lst = c.get(f"/sessions/{sid3}/fragments").json()
check("overlapping fragments coexist", lst["count"] == 2, lst)
info = c.get(f"/sessions/{sid3}").json()
check("union counts seq once (stored<=C)", info["stored_count"] == 3, info)
# seq4：窗口3..4 + 片段1..3 并集 {1,2,3,4} 超过 C=3 -> 409
check("seq4 rejected 409", write(sid3, 4, code=409).status_code == 409)

# 12. H=2,C=2：片段钉住滑出窗口的旧样本时 409；删除后旧样本随写入原子回收
sid4 = new(2, 2)
write(sid4, 11); write(sid4, 12)
trigger(sid4, 1, 0)  # 片段[1,2] 与窗口1..2 重合，钉住 seq1
# seq3：新窗口2..3 ∪ 片段1..2 = {1,2,3} > C -> 409
check("pinned seq1 -> seq3 409", write(sid4, 13, code=409).status_code == 409)
f = c.get(f"/sessions/{sid4}/fragments").json()["fragments"][0]
c.delete(f"/sessions/{sid4}/fragments/{f['id']}")
# 删除时 seq1 仍在当前窗口[1,2]故保留；写入 seq3 时新窗口[2,3]不再需要它，
# seq1 被原子回收，并集只有 {2,3}，重试立即成功、序号不跳。
r = write(sid4, 13).json()
check("seq3 admitted right after delete", r["seq"] == 3, r)
f2 = trigger(sid4, 1, 0).json()  # 片段[2,3]，与当前窗口重合
check("seq4 pinned by completed frag -> 409",
      write(sid4, 14, code=409).status_code == 409)
c.delete(f"/sessions/{sid4}/fragments/{f2['id']}")
check("seq4 admitted after delete", write(sid4, 14).json()["seq"] == 4)
# 未定义的集合级 DELETE 返回 405，且不影响状态
check("collection DELETE returns 405",
      c.delete(f"/sessions/{sid4}/fragments").status_code == 405)
info = c.get(f"/sessions/{sid4}").json()
check("405 left state intact", info["next_seq"] == 5, info)

# 13. pending 片段的“空洞”样本可回收：H=2,C=4
sid5 = new(2, 4)
write(sid5, 1); write(sid5, 2)
r = trigger(sid5, 0, 2).json()  # [2,4] pending
fid5 = r["id"]
write(sid5, 3)  # 窗口2..3 ∪ 片段2..4
# 写 seq4 完成
write(sid5, 4)
d = c.get(f"/sessions/{sid5}/fragments/{fid5}").json()
check("frag completed via later writes", d["status"] == "completed", d)
# seq1 既滑出窗口也不被片段引用，已回收；存储为 2,3,4
info = c.get(f"/sessions/{sid5}").json()
check("unreferenced seq1 reclaimed", 1 not in [i["seq"] for i in info["history"]]
      and info["stored_count"] == 3, info)

# 14. pending 片段到达部分可与窗口重叠，完成自身永不自阻塞（H=5,C=5, after=4）
sid6 = new(5, 5)
for v in range(1, 6):
    write(sid6, v)
trigger(sid6, 0, 4)  # 5..9，窗口每次推进
for v in range(6, 10):
    r = write(sid6, v)
    check(f"self-fragment admits seq{v}", r.status_code == 201, r.text)

print(f"\n{ok} passed, {fail} failed")
assert fail == 0
