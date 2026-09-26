"""剪辑字幕重建：按 JSON 剪辑数组把原 SRT 素材裁切、平移到目标时间轴。

规则：

- 每个剪辑项含非负整数毫秒 ``source_in`` / ``source_out`` / ``target_in``，
  出点须严格大于入点；片段允许乱序、重复，目标区间允许重叠，按**原速**映射
  （目标时间 = target_in + 源时间 − source_in）。
- 每个原字幕块与每段素材区间求**左闭右开**交集，裁切后整体平移到目标位置；
  空交集直接丢弃。
- 以映射后全部端点切分时间轴：逐段统计当前有效的原字幕块，按**解析序位**
  排列，各块保留自身正文换行，块间再以换行连接。
- 同一原块被多个片段覆盖时只显示一次，直到**最后一份覆盖结束**才移除
  （按覆盖份数计数，而非简单置位）。
- 仅合并时间上相邻且有效原块集合相同的区间；无字幕区间跳过。
- 导出按时间从 1 编号、彼此无重叠的 SRT。
"""
from __future__ import annotations

from dataclasses import dataclass

from .parsers import SrtItem, format_tc

MAX_CUTS = 2000          # 剪辑项数量上限

_CUT_KEYS = ("source_in", "source_out", "target_in")


@dataclass(frozen=True)
class CutSpec:
    source_in: int       # 素材入点（毫秒，左闭）
    source_out: int      # 素材出点（毫秒，右开，> source_in）
    target_in: int       # 目标入点（毫秒，左闭）


@dataclass
class ReconstructRow:
    new_index: int                       # 重新编号（从 1 开始）
    start_ms: int
    end_ms: int
    start_tc: str
    end_tc: str
    block_positions: tuple[int, ...]     # 有效原块的解析序位（升序）
    text: str                            # 合并后正文（保留换行，供预览）
    body_lines: tuple[str, ...]          # 导出用正文行


def validate_cuts(raw: object, max_items: int = MAX_CUTS) -> list[CutSpec]:
    """校验剪辑数组，非法时抛 ValueError 并指出**项号**（从 1 开始）。"""
    if not isinstance(raw, list):
        raise ValueError("剪辑数组必须是 JSON 数组（每项为一个剪辑区间对象）")
    if not raw:
        raise ValueError("剪辑数组为空：至少需要一个剪辑区间")
    if len(raw) > max_items:
        raise ValueError(f"剪辑区间数量超过 {max_items} 个的上限，请拆分后再重建")

    cuts: list[CutSpec] = []
    for pos, item in enumerate(raw, start=1):
        label = f"剪辑清单第 {pos} 项"
        if not isinstance(item, dict):
            raise ValueError(f"{label}必须是 JSON 对象，含 source_in/source_out/target_in")
        values = {}
        for key in _CUT_KEYS:
            if key not in item:
                raise ValueError(f"{label}缺少字段 {key}")
            value = item[key]
            # 布尔值在 Python 中也是 int，须显式排除
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{label}的 {key} 必须是非负整数毫秒")
            if value < 0:
                raise ValueError(
                    f"{label}的 {key} 必须是非负整数毫秒（当前为 {value}）"
                )
            values[key] = value
        if values["source_out"] <= values["source_in"]:
            raise ValueError(
                f"{label}出点 {values['source_out']} 须大于入点 "
                f"{values['source_in']}（区间左闭右开，不可为空）"
            )
        cuts.append(
            CutSpec(
                source_in=values["source_in"],
                source_out=values["source_out"],
                target_in=values["target_in"],
            )
        )
    return cuts


def _mapped_intersections(
    item: SrtItem, cuts: list[CutSpec]
) -> list[tuple[int, int]]:
    """原字幕块与每段素材求左闭右开交集并平移；丢弃空交集。"""
    spans: list[tuple[int, int]] = []
    for cut in cuts:
        lo = max(item.start_ms, cut.source_in)
        hi = min(item.end_ms, cut.source_out)
        if lo >= hi:
            continue    # 左闭右开：相等或相离均为空交集
        offset = cut.target_in - cut.source_in
        spans.append((lo + offset, hi + offset))
    return spans


def reconstruct_subtitles(
    items: list[SrtItem], cuts: list[CutSpec]
) -> list[ReconstructRow]:
    """把原字幕按剪辑区间裁切平移，重建为目标时间轴上的无重叠字幕段。"""
    if not items or not cuts:
        return []

    # 事件：(映射后时间, 原块解析序位, +1 进入 / -1 离开)。
    # 同一原块可被多段素材覆盖，用覆盖份数计数，最后一份结束才移除。
    events: list[tuple[int, int, int]] = []
    for pos, item in enumerate(items, start=1):
        for start_ms, end_ms in _mapped_intersections(item, cuts):
            events.append((start_ms, pos, +1))
            events.append((end_ms, pos, -1))
    events.sort(key=lambda e: e[0])

    # 按映射后全部端点切分时间轴，记录每个非空区间的有效原块集合（解析序位升序）。
    segments: list[list] = []
    active: dict[int, int] = {}
    prev_t: int | None = None
    i = 0
    while i < len(events):
        t = events[i][0]
        if prev_t is not None and t > prev_t and active:
            segments.append([prev_t, t, tuple(sorted(active))])
        # 同一时刻的全部事件合并处理，使点长度覆盖（+1/-1 同刻）不产生区间。
        while i < len(events) and events[i][0] == t:
            _, block_pos, delta = events[i]
            count = active.get(block_pos, 0) + delta
            if count:
                active[block_pos] = count
            else:
                active.pop(block_pos, None)
            i += 1
        prev_t = t

    # 仅合并相邻且有效原块集合相同的区间（中间隔着无字幕区间的不合并）。
    merged: list[list] = []
    for start_ms, end_ms, blocks in segments:
        if (
            merged
            and merged[-1][1] == start_ms
            and merged[-1][2] == blocks
        ):
            merged[-1][1] = end_ms
        else:
            merged.append([start_ms, end_ms, blocks])

    rows: list[ReconstructRow] = []
    for new_index, (start_ms, end_ms, blocks) in enumerate(merged, start=1):
        body_lines: list[str] = []
        for block_pos in blocks:
            item = items[block_pos - 1]
            body_lines.extend(item.body_lines or (item.raw_text,))
        rows.append(
            ReconstructRow(
                new_index=new_index,
                start_ms=start_ms,
                end_ms=end_ms,
                start_tc=format_tc(start_ms),
                end_tc=format_tc(end_ms),
                block_positions=blocks,
                text="\n".join(body_lines),
                body_lines=tuple(body_lines),
            )
        )
    return rows


def build_reconstruct_srt(rows: list[ReconstructRow]) -> str:
    """生成重建后的 SRT：保留各块正文（含多行），从 1 重新编号。"""
    blocks = []
    for row in rows:
        lines = [str(row.new_index), f"{row.start_tc} --> {row.end_tc}"]
        lines.extend(row.body_lines or (row.text,))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"
