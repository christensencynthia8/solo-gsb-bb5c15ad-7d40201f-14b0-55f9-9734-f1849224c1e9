"""剪辑字幕重建：裁切/平移、端点切分与覆盖计数单元测试 + 接口测试。

运行：python -m pytest tests/ -q
"""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.parsers import parse_srt
from app.reconstruct import (
    CutSpec,
    build_reconstruct_srt,
    reconstruct_subtitles,
    validate_cuts,
)

client = TestClient(app)


def _tc(ms):
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, milli = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{milli:03d}"


def _srt(blocks):
    """blocks: [(start_ms, end_ms, [正文行...]), ...]"""
    out = []
    for i, (s, e, lines) in enumerate(blocks, start=1):
        out.append(f"{i}\n{_tc(s)} --> {_tc(e)}\n" + "\n".join(lines))
    return "\n\n".join(out) + "\n"


def _cut(lo, hi, t):
    return CutSpec(source_in=lo, source_out=hi, target_in=t)


def _run(blocks, cuts):
    return reconstruct_subtitles(parse_srt(_srt(blocks)), cuts)


# ---------- 基本裁切与原速平移 ----------

def test_basic_clip_and_shift():
    # 块 1000~4000，素材取 2000~5000 放到 8000 起 -> 8000~10000
    rows = _run(
        [(1000, 4000, ["你好"])],
        [_cut(2000, 5000, 8000)],
    )
    assert len(rows) == 1
    r = rows[0]
    assert (r.start_ms, r.end_ms) == (8000, 10000)
    assert r.start_tc == "00:00:08,000"
    assert r.end_tc == "00:00:10,000"
    assert r.block_positions == (1,)
    assert r.new_index == 1


def test_disjoint_intersection_dropped():
    rows = _run(
        [(1000, 2000, ["甲"]), (5000, 6000, ["乙"])],
        [_cut(3000, 4000, 0)],
    )
    assert rows == []


def test_touching_endpoint_is_empty():
    # 左闭右开：素材入点恰等于字幕出点，交集为空
    rows = _run([(1000, 2000, ["甲"])], [_cut(2000, 3000, 0)])
    assert rows == []
    # 素材出点恰等于字幕入点，同样为空
    rows = _run([(3000, 4000, ["甲"])], [_cut(2000, 3000, 0)])
    assert rows == []


def test_multiple_blocks_split_by_active_set():
    # 素材覆盖块 2、3，但二者在目标轴上首尾相接、有效集合不同 ->
    # 相邻区间不跨块合并，输出两段（每段一份正文，保留各自换行）
    rows = _run(
        [
            (0, 1000, ["块一"]),
            (1000, 2000, ["块二上", "块二下"]),
            (2000, 3000, ["块三"]),
        ],
        [_cut(1000, 3000, 5000)],
    )
    assert [(r.start_ms, r.end_ms, r.block_positions) for r in rows] == [
        (5000, 6000, (2,)),
        (6000, 7000, (3,)),
    ]
    assert rows[0].text == "块二上\n块二下"
    assert rows[0].body_lines == ("块二上", "块二下")
    assert rows[1].text == "块三"


def test_concurrent_active_blocks_join_by_newline_in_parse_order():
    # 两个块在源时间轴上重叠，同一段素材同时取到 -> 一个区间内按解析序位拼接
    rows = _run(
        [
            (1000, 3000, ["块一上", "块一下"]),
            (2000, 4000, ["块二"]),
        ],
        [_cut(0, 5000, 0)],
    )
    assert [(r.start_ms, r.end_ms, r.block_positions) for r in rows] == [
        (1000, 2000, (1,)),
        (2000, 3000, (1, 2)),
        (3000, 4000, (2,)),
    ]
    middle = rows[1]
    assert middle.text == "块一上\n块一下\n块二"
    assert middle.body_lines == ("块一上", "块一下", "块二")


def test_gap_without_subtitles_split_and_skipped():
    # 块 1、2、3 均在素材内但彼此有空档：空档区间无字幕，直接跳过
    rows = _run(
        [
            (0, 1000, ["甲"]),
            (5000, 6000, ["中"]),
            (8000, 9000, ["丙"]),
        ],
        [_cut(0, 9000, 0)],
    )
    assert [(r.start_ms, r.end_ms, r.block_positions) for r in rows] == [
        (0, 1000, (1,)),
        (5000, 6000, (2,)),
        (8000, 9000, (3,)),
    ]


def test_block_outside_cut_dropped_but_inside_kept():
    # 块 1、3 与素材相交，块 2 完全在素材区间之外 -> 仅保留 1、3
    rows = _run(
        [
            (0, 1000, ["甲"]),
            (5000, 6000, ["不取"]),
            (8000, 9000, ["丙"]),
        ],
        [_cut(0, 2000, 0), _cut(8000, 9000, 5000)],
    )
    assert [(r.start_ms, r.end_ms, r.block_positions) for r in rows] == [
        (0, 1000, (1,)),
        (5000, 6000, (3,)),
    ]


# ---------- 乱序、重复、重叠 ----------

def test_unordered_cuts_output_sorted_by_target_time():
    # 两个片段在素材上顺序 3~4 在前、0~1 在后，但目标时间相反
    rows = _run(
        [(0, 1000, ["早"]), (3000, 4000, ["晚"])],
        [_cut(3000, 4000, 0), _cut(0, 1000, 5000)],
    )
    assert [(r.start_ms, r.end_ms, r.block_positions) for r in rows] == [
        (0, 1000, (2,)),
        (5000, 6000, (1,)),
    ]


def test_overlapping_target_intervals_same_block_shown_once():
    # 同一块被两个片段取到、目标区间重叠，重叠区内只出现一次（同序位去重）
    rows = _run(
        [(1000, 4000, ["甲"])],
        [
            _cut(1000, 3000, 0),       # 目标 0~2000
            _cut(2000, 4000, 1000),    # 目标 1000~3000
        ],
    )
    assert [(r.start_ms, r.end_ms, r.block_positions) for r in rows] == [
        (0, 3000, (1,)),
    ]
    assert rows[0].text == "甲"


def test_same_block_disjoint_target_intervals_not_merged():
    # 同一块被重复取到但目标区间互不相邻：输出两份（中间隔着无覆盖区间）
    rows = _run(
        [(1000, 2000, ["甲"])],
        [
            _cut(1000, 2000, 0),
            _cut(1000, 2000, 5000),
        ],
    )
    assert [(r.start_ms, r.end_ms, r.block_positions) for r in rows] == [
        (0, 1000, (1,)),
        (5000, 6000, (1,)),
    ]


def test_refcount_removes_only_after_last_coverage_ends():
    # 覆盖 A: 0~3000；覆盖 B（同块）: 2000~5000。
    # 3000 处 A 结束但 B 仍在 -> 块保持有效，直到 5000 才移除。
    rows = _run(
        [(0, 10000, ["长块"])],
        [
            _cut(0, 3000, 0),
            _cut(2000, 5000, 2000),
        ],
    )
    assert [(r.start_ms, r.end_ms, r.block_positions) for r in rows] == [
        (0, 5000, (1,)),
    ]


def test_same_time_add_remove_keeps_block_active():
    # 前一份覆盖结束与后一份覆盖开始恰在同一时刻：覆盖份数瞬时增减，
    # 块始终有效，相邻且集合相同的区间合并为一整段
    rows = _run(
        [(1000, 3000, ["甲"])],
        [
            _cut(1000, 3000, 0),     # 目标 0~2000
            _cut(2000, 2001, 2000),  # 目标 2000~2001（与前者首尾相接）
        ],
    )
    assert [(r.start_ms, r.end_ms) for r in rows] == [(0, 2001)]
    # 完全空交集的重复片段不产生任何事件
    rows2 = _run(
        [(1000, 3000, ["甲"])],
        [_cut(1000, 3000, 0), _cut(9000, 9001, 2000)],
    )
    assert [(r.start_ms, r.end_ms) for r in rows2] == [(0, 2000)]


def test_adjacent_segments_with_different_sets_not_merged():
    # 块 1 全程被两段覆盖；块 2 较短，只在部分区间有效。
    # 集合在 {1,2} 与 {1} 之间反复变化，相邻区间即便首尾相接也不得合并。
    rows = _run(
        [(0, 10000, ["甲"]), (0, 4000, ["乙"])],
        [
            _cut(0, 10000, 0),
            _cut(0, 10000, 5000),
        ],
    )
    assert [(r.start_ms, r.end_ms, r.block_positions) for r in rows] == [
        (0, 4000, (1, 2)),
        (4000, 5000, (1,)),
        (5000, 9000, (1, 2)),
        (9000, 15000, (1,)),
    ]


def test_adjacent_segments_with_same_set_merge_across_cut_boundary():
    # 两段素材目标首尾相接、有效块集合相同：跨片段边界合并为一段
    rows = _run(
        [(0, 10000, ["甲"]), (0, 10000, ["乙"])],
        [
            _cut(0, 2000, 0),
            _cut(0, 4000, 2000),
        ],
    )
    assert [(r.start_ms, r.end_ms, r.block_positions) for r in rows] == [
        (0, 6000, (1, 2)),
    ]


def test_output_intervals_never_overlap():
    rows = _run(
        [(i * 1000, i * 1000 + 1000, [f"块{i}"]) for i in range(10)],
        [
            _cut(0, 10000, 0),
            _cut(2000, 8000, 1000),
            _cut(5000, 6000, 3000),
        ],
    )
    prev_end = 0
    for r in rows:
        assert r.start_ms < r.end_ms
        assert r.start_ms >= prev_end
        prev_end = r.end_ms
    assert [r.new_index for r in rows] == list(range(1, len(rows) + 1))


def test_multiline_body_preserved_in_export():
    rows = _run(
        [(1000, 3000, ["第一行", "第二行"])],
        [_cut(1000, 3000, 0)],
    )
    srt = build_reconstruct_srt(rows)
    assert srt == (
        "1\n"
        "00:00:00,000 --> 00:00:02,000\n"
        "第一行\n第二行\n"
    )


def test_empty_inputs_return_empty():
    assert reconstruct_subtitles([], [_cut(0, 10, 0)]) == []
    assert reconstruct_subtitles(parse_srt(_srt([(0, 10, ["甲"])])), []) == []


# ---------- 剪辑清单校验 ----------

def test_validate_cuts_ok():
    cuts = validate_cuts(
        [
            {"source_in": 0, "source_out": 100, "target_in": 0},
            {"source_in": 5, "source_out": 6, "target_in": 10**9},
        ]
    )
    assert cuts[0] == CutSpec(0, 100, 0)
    assert cuts[1].target_in == 10**9


def test_validate_cuts_errors_point_to_item_number():
    bad_cases = [
        ("not-list", "数组"),
        ([], "为空"),
        ([{"source_in": 0, "source_out": 1}], "第 1 项"),
        ([
            {"source_in": 0, "source_out": 1, "target_in": 0},
            {"source_in": 5, "source_out": 5, "target_in": 0},
        ], "第 2 项"),
        ([{"source_in": -1, "source_out": 1, "target_in": 0}], "第 1 项"),
        ([{"source_in": 1, "source_out": 0, "target_in": 0}], "大于"),
        ([{"source_in": "0", "source_out": 1, "target_in": 0}], "整数"),
        ([{"source_in": 0, "source_out": 1, "target_in": 1.5}], "整数"),
        ([42], "对象"),
    ]
    for raw, needle in bad_cases:
        with pytest.raises(ValueError) as exc:
            validate_cuts(raw)
        assert needle in str(exc.value)


def test_bool_values_rejected():
    with pytest.raises(ValueError):
        validate_cuts([{"source_in": True, "source_out": 1, "target_in": 0}])


def test_too_many_cuts():
    raw = [{"source_in": 0, "source_out": 1, "target_in": 0}]
    with pytest.raises(ValueError, match="上限"):
        validate_cuts(raw * 3, max_items=2)


# ---------- 接口 ----------

SRT = _srt([
    (1000, 3000, ["你好，世界！", "Hello, world!"]),
    (4000, 6000, ["第二段"]),
])

CUTS = [
    {"source_in": 0, "source_out": 5000, "target_in": 10000},
    {"source_in": 4000, "source_out": 6000, "target_in": 14000},
]


def _post(path, payload):
    return client.post(path, json=payload)


def test_api_preview():
    resp = _post("/api/reconstruct", {"srt": SRT, "cuts": CUTS})
    assert resp.status_code == 200
    data = resp.json()
    assert data["summary"]["cuts"] == 2
    rows = data["rows"]
    # 片段一：块1 1000~3000 -> 11000~13000；块2 取 4000~5000 -> 14000~15000
    # 片段二：块2 4000~6000 -> 14000~16000（与前一份同块目标相接，覆盖延至 16000）
    assert [(r["start_tc"], r["end_tc"], r["block_positions"]) for r in rows] == [
        ("00:00:11,000", "00:00:13,000", [1]),
        ("00:00:14,000", "00:00:16,000", [2]),
    ]
    assert rows[0]["text"] == "你好，世界！\nHello, world!"
    assert rows[0]["new_index"] == 1


def test_api_srt_download():
    resp = _post("/api/reconstruct.srt", {"srt": SRT, "cuts": CUTS})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert "reconstruct.srt" in resp.headers["content-disposition"]
    text = resp.content.decode("utf-8")
    assert text.startswith("1\n00:00:11,000 --> 00:00:13,000\n你好，世界！\nHello, world!")
    assert "\n\n2\n00:00:14,000 --> 00:00:16,000\n第二段\n" == text[text.index("\n\n2\n"):]


def test_api_bad_cut_returns_400_with_item_number():
    cuts = [
        {"source_in": 0, "source_out": 1000, "target_in": 0},
        {"source_in": 5, "source_out": 5, "target_in": 0},
    ]
    resp = _post("/api/reconstruct", {"srt": SRT, "cuts": cuts})
    assert resp.status_code == 400
    assert "第 2 项" in resp.json()["detail"]


def test_api_empty_cuts_returns_400():
    resp = _post("/api/reconstruct", {"srt": SRT, "cuts": []})
    assert resp.status_code == 400
    assert "剪辑数组" in resp.json()["detail"]


def test_api_cuts_not_array_returns_400():
    resp = _post(
        "/api/reconstruct",
        {"srt": SRT, "cuts": {"source_in": 0, "source_out": 1, "target_in": 0}},
    )
    assert resp.status_code == 400


def test_api_bad_srt_returns_400():
    resp = _post(
        "/api/reconstruct",
        {"srt": "没有时间码", "cuts": CUTS},
    )
    assert resp.status_code == 400
    assert "SRT" in resp.json()["detail"]


def test_api_non_overlapping_output_with_overlapping_inputs():
    # 多片段目标大面积重叠，导出仍须无重叠、从 1 编号
    cuts = [
        {"source_in": 0, "source_out": 10000, "target_in": 0},
        {"source_in": 0, "source_out": 10000, "target_in": 1000},
        {"source_in": 0, "source_out": 10000, "target_in": 2000},
    ]
    resp = _post("/api/reconstruct", {"srt": SRT, "cuts": cuts})
    assert resp.status_code == 200
    rows = resp.json()["rows"]
    prev_end = -1
    for i, r in enumerate(rows, start=1):
        assert r["new_index"] == i
        assert r["start_tc"] < r["end_tc"]
        start_ms = _parse_tc(r["start_tc"])
        assert start_ms >= prev_end
        prev_end = _parse_tc(r["end_tc"])


def _parse_tc(tc):
    h, m, rest = tc.split(":")
    s, ms = rest.split(",")
    return int(h) * 3600000 + int(m) * 60000 + int(s) * 1000 + int(ms)
