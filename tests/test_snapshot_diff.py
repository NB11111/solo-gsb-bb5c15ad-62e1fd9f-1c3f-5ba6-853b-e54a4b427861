"""快照差异接口测试。

覆盖：
- added / removed / changed 三种差异的识别与内容（字幕以生效文本与
  更正版本判断，缺口以裁决区间端点判断，未变化的不返回）；
- 反向比较（新快照 vs 旧快照）得到 removed；
- after/limit 分页只计差异条目，next_after 指向本页最后一条差异，
  稀疏变化逐页拉取不漏项；
- 相同快照 / 无变化快照比较为空差异；
- 跨频道 409、快照不存在 404、参数校验 422。
"""

from app.db import write_tx


def submit(client, channel, seq, revision, text="t"):
    resp = client.post(
        f"/channels/{channel}/fragments",
        json={"seq": seq, "revision": revision, "text": text},
    )
    assert resp.status_code == 200
    return resp.json()


def decide_gap(client, channel, start_seq, end_seq):
    resp = client.post(
        f"/channels/{channel}/gaps",
        json={"start_seq": start_seq, "end_seq": end_seq},
    )
    assert resp.status_code == 200
    return resp.json()


def correct(client, channel, seq, expected_version, text, reason="r"):
    resp = client.post(
        f"/channels/{channel}/finalized/{seq}/corrections",
        json={
            "expected_version": expected_version,
            "text": text,
            "reason": reason,
        },
    )
    assert resp.status_code == 200
    return resp.json()


def make_snapshot(client, channel, key):
    resp = client.post(
        f"/channels/{channel}/snapshots", json={"idempotency_key": key}
    )
    assert resp.status_code == 200
    return resp.json()


def diff(client, from_id, to_id, **params):
    return client.get(
        "/snapshots/diff", params={"from": from_id, "to": to_id, **params}
    )


def build_evolved_channel(client, channel="news"):
    """快照 A：1=字幕、2-3=缺口、4=字幕（更正 v1）、5=字幕；
    演进后快照 B：1 被更正、4 再更正、6 裁决为缺口并释放缓冲的 7。
    """
    submit(client, channel, 1, 1, "第一条")
    submit(client, channel, 4, 1, "第四条")
    submit(client, channel, 5, 1, "第五条")
    decide_gap(client, channel, 2, 3)
    correct(client, channel, 4, 0, "第四条-改")
    snap_a = make_snapshot(client, channel, "snap-a")

    correct(client, channel, 1, 0, "第一条-改")
    correct(client, channel, 4, 1, "第四条-再改")
    submit(client, channel, 7, 1, "第七条")
    decide_gap(client, channel, 6, 6)
    snap_b = make_snapshot(client, channel, "snap-b")
    return snap_a, snap_b


# ---------- 差异识别 ----------


def test_diff_added_removed_changed_and_unchanged(client):
    snap_a, snap_b = build_evolved_channel(client)
    resp = diff(client, snap_a["snapshot_id"], snap_b["snapshot_id"])
    assert resp.status_code == 200
    body = resp.json()
    assert body["channel"] == "news"
    assert body["from_snapshot_id"] == snap_a["snapshot_id"]
    assert body["to_snapshot_id"] == snap_b["snapshot_id"]
    assert body["from_watermark"] == 5
    assert body["to_watermark"] == 7

    items = body["items"]
    # 2、3（缺口）、5（未更正字幕）未变化，不出现在结果中
    assert [(i["change"], i["seq"]) for i in items] == [
        ("changed", 1),
        ("changed", 4),
        ("added", 6),
        ("added", 7),
    ]
    assert body["next_after"] == 7

    # 字幕变更：from/to 两侧各带冻结的生效文本与更正版本
    first = items[0]
    assert first["from_item"]["kind"] == "subtitle"
    assert first["from_item"]["text"] == "第一条"
    assert first["from_item"]["correction_version"] == 0
    assert first["from_item"]["revision"] == 1
    assert first["to_item"]["text"] == "第一条-改"
    assert first["to_item"]["correction_version"] == 1

    fourth = items[1]
    assert fourth["from_item"]["text"] == "第四条-改"
    assert fourth["from_item"]["correction_version"] == 1
    assert fourth["to_item"]["text"] == "第四条-再改"
    assert fourth["to_item"]["correction_version"] == 2

    # 新增缺口：from_item 为 null，to_item 带裁决区间端点
    gap = items[2]
    assert gap["from_item"] is None
    assert gap["to_item"]["kind"] == "gap"
    assert gap["to_item"]["start_seq"] == 6
    assert gap["to_item"]["end_seq"] == 6
    assert gap["to_item"]["text"] is None
    assert gap["to_item"]["correction_version"] is None

    # 新增字幕
    added = items[3]
    assert added["from_item"] is None
    assert added["to_item"]["kind"] == "subtitle"
    assert added["to_item"]["text"] == "第七条"
    assert added["to_item"]["correction_version"] == 0


def test_diff_reverse_direction_yields_removed(client):
    snap_a, snap_b = build_evolved_channel(client)
    body = diff(client, snap_b["snapshot_id"], snap_a["snapshot_id"]).json()
    assert body["from_watermark"] == 7
    assert body["to_watermark"] == 5
    items = body["items"]
    assert [(i["change"], i["seq"]) for i in items] == [
        ("changed", 1),
        ("changed", 4),
        ("removed", 6),
        ("removed", 7),
    ]
    # changed 的 from/to 随方向互换
    assert items[0]["from_item"]["text"] == "第一条-改"
    assert items[0]["to_item"]["text"] == "第一条"
    # removed：to_item 为 null，from_item 带原内容
    assert items[2]["to_item"] is None
    assert items[2]["from_item"]["kind"] == "gap"
    assert items[3]["to_item"] is None
    assert items[3]["from_item"]["text"] == "第七条"


def test_diff_subtitle_judged_by_version_even_when_text_same(client):
    """生效文本相同但更正版本不同也算变更。"""
    submit(client, "c1", 1, 1, "原文")
    snap_a = make_snapshot(client, "c1", "a")
    correct(client, "c1", 1, 0, "原文")  # 文本不变，版本 +1
    snap_b = make_snapshot(client, "c1", "b")

    body = diff(client, snap_a["snapshot_id"], snap_b["snapshot_id"]).json()
    assert [(i["change"], i["seq"]) for i in body["items"]] == [("changed", 1)]
    item = body["items"][0]
    assert item["from_item"]["text"] == item["to_item"]["text"] == "原文"
    assert item["from_item"]["correction_version"] == 0
    assert item["to_item"]["correction_version"] == 1


def test_diff_gap_judged_by_interval_endpoints(client):
    """同一序号两侧都是缺口但裁决区间端点不同 -> changed。"""
    snap_a = make_snapshot(client, "c1", "a")["snapshot_id"]
    snap_b = make_snapshot(client, "c1", "b")["snapshot_id"]
    with write_tx() as conn:
        conn.execute(
            "INSERT INTO snapshot_items (snapshot_id, seq, kind, start_seq, end_seq) "
            "VALUES (?, 1, 'gap', 1, 3)",
            (snap_a,),
        )
        conn.execute(
            "INSERT INTO snapshot_items (snapshot_id, seq, kind, start_seq, end_seq) "
            "VALUES (?, 1, 'gap', 1, 5)",
            (snap_b,),
        )
        # 端点完全相同的缺口不产生差异
        conn.execute(
            "INSERT INTO snapshot_items (snapshot_id, seq, kind, start_seq, end_seq) "
            "VALUES (?, 2, 'gap', 2, 4)",
            (snap_a,),
        )
        conn.execute(
            "INSERT INTO snapshot_items (snapshot_id, seq, kind, start_seq, end_seq) "
            "VALUES (?, 2, 'gap', 2, 4)",
            (snap_b,),
        )

    body = diff(client, snap_a, snap_b).json()
    assert [(i["change"], i["seq"]) for i in body["items"]] == [("changed", 1)]
    item = body["items"][0]
    assert item["from_item"]["end_seq"] == 3
    assert item["to_item"]["end_seq"] == 5


def test_diff_kind_change_detected(client):
    """同一序号一侧为字幕、一侧为缺口 -> changed。"""
    snap_a = make_snapshot(client, "c1", "a")["snapshot_id"]
    snap_b = make_snapshot(client, "c1", "b")["snapshot_id"]
    with write_tx() as conn:
        conn.execute(
            "INSERT INTO snapshot_items "
            "(snapshot_id, seq, kind, revision, original_text, effective_text, "
            " correction_version) VALUES (?, 1, 'subtitle', 1, 'x', 'x', 0)",
            (snap_a,),
        )
        conn.execute(
            "INSERT INTO snapshot_items (snapshot_id, seq, kind, start_seq, end_seq) "
            "VALUES (?, 1, 'gap', 1, 1)",
            (snap_b,),
        )

    body = diff(client, snap_a, snap_b).json()
    assert [(i["change"], i["seq"]) for i in body["items"]] == [("changed", 1)]
    item = body["items"][0]
    assert item["from_item"]["kind"] == "subtitle"
    assert item["to_item"]["kind"] == "gap"


# ---------- 空差异 ----------


def test_diff_same_snapshot_is_empty(client):
    submit(client, "c1", 1, 1, "one")
    snap = make_snapshot(client, "c1", "k")
    body = diff(client, snap["snapshot_id"], snap["snapshot_id"]).json()
    assert body["channel"] == "c1"
    assert body["from_watermark"] == body["to_watermark"] == 1
    assert body["items"] == []
    assert body["next_after"] is None


def test_diff_no_changes_between_snapshots_is_empty(client):
    submit(client, "c1", 1, 1, "one")
    snap_a = make_snapshot(client, "c1", "a")
    snap_b = make_snapshot(client, "c1", "b")
    body = diff(client, snap_a["snapshot_id"], snap_b["snapshot_id"]).json()
    assert body["items"] == []
    assert body["next_after"] is None


def test_diff_empty_snapshots_is_empty(client):
    snap_a = make_snapshot(client, "ghost", "a")
    snap_b = make_snapshot(client, "ghost", "b")
    body = diff(client, snap_a["snapshot_id"], snap_b["snapshot_id"]).json()
    assert body["from_watermark"] == body["to_watermark"] == 0
    assert body["items"] == []


# ---------- 分页 ----------


def test_diff_pagination_counts_only_diff_items(client):
    snap_a, snap_b = build_evolved_channel(client)
    sid_a, sid_b = snap_a["snapshot_id"], snap_b["snapshot_id"]

    body = diff(client, sid_a, sid_b, limit=2).json()
    assert [(i["change"], i["seq"]) for i in body["items"]] == [
        ("changed", 1),
        ("changed", 4),
    ]
    assert body["next_after"] == 4

    body = diff(client, sid_a, sid_b, after=4, limit=2).json()
    assert [(i["change"], i["seq"]) for i in body["items"]] == [
        ("added", 6),
        ("added", 7),
    ]
    assert body["next_after"] == 7

    body = diff(client, sid_a, sid_b, after=7, limit=2).json()
    assert body["items"] == []
    assert body["next_after"] is None


def test_diff_pagination_sparse_changes_no_missing(client):
    """大量未变化条目中只有稀疏差异：limit=1 逐页拉取不漏项。"""
    for i in range(1, 11):
        submit(client, "sparse", i, 1, f"第{i}条")
    snap_a = make_snapshot(client, "sparse", "a")
    correct(client, "sparse", 3, 0, "第三条-改")
    correct(client, "sparse", 9, 0, "第九条-改")
    snap_b = make_snapshot(client, "sparse", "b")

    seen, after = [], 0
    while True:
        body = diff(
            client, snap_a["snapshot_id"], snap_b["snapshot_id"],
            after=after, limit=1,
        ).json()
        if not body["items"]:
            assert body["next_after"] is None
            break
        assert len(body["items"]) == 1
        assert body["next_after"] == body["items"][-1]["seq"]
        seen.extend(body["items"])
        after = body["next_after"]
    assert [i["seq"] for i in seen] == [3, 9]
    assert all(i["change"] == "changed" for i in seen)


# ---------- 错误路径 ----------


def test_diff_cross_channel_rejected(client):
    submit(client, "c1", 1, 1, "one")
    submit(client, "c2", 1, 1, "one")
    snap_a = make_snapshot(client, "c1", "k")
    snap_b = make_snapshot(client, "c2", "k")
    resp = diff(client, snap_a["snapshot_id"], snap_b["snapshot_id"])
    assert resp.status_code == 409
    assert resp.json()["error"] == "cross_channel_diff"


def test_diff_unknown_snapshot_returns_404(client):
    submit(client, "c1", 1, 1, "one")
    snap = make_snapshot(client, "c1", "k")
    resp = diff(client, snap["snapshot_id"], "no-such")
    assert resp.status_code == 404
    assert resp.json()["error"] == "snapshot_not_found"
    resp = diff(client, "no-such", snap["snapshot_id"])
    assert resp.status_code == 404
    assert resp.json()["error"] == "snapshot_not_found"


def test_diff_param_validation(client):
    snap = make_snapshot(client, "c1", "k")
    sid = snap["snapshot_id"]
    # 缺少 from / to
    assert client.get("/snapshots/diff", params={"to": sid}).status_code == 422
    assert client.get("/snapshots/diff", params={"from": sid}).status_code == 422
    # after / limit 越界
    assert diff(client, sid, sid, after=-1).status_code == 422
    assert diff(client, sid, sid, limit=0).status_code == 422
    assert diff(client, sid, sid, limit=10001).status_code == 422
