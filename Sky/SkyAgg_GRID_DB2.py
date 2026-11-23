#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SkyAgg Global-Merged GRID database builder for Piano-PIR
Memory/time optimized version.

Main changes compared with the previous SkyAgg_GRID_DB_Piano.py:
1. FineGrid cells are stored as compact bit masks, not FrozenSet[int].
2. Cell arrays are stored in array('I') references into a mask pool, not dict[(c,r)].
3. Quadrants are scanned with two columns and merged directly; no directional
   FineGrid, cell-ref array, or mask pool is materialized.
4. Stale global masks are compacted during and after quadrant merges.
5. Coarse cells use compact numeric arrays instead of one Python object per cell.
6. Plotting dependencies are loaded only when visualization is requested.

Database export convention for Piano-PIR remains unchanged:
    one final grid cell = one DB record = one PIR index
    record = [count, point_id_1, ..., point_id_count, zero_padding]
"""

import argparse
import bisect
import gc
import json
import math
import os
import struct
import time
from array import array
from dataclasses import dataclass
from statistics import mean
from typing import Dict, FrozenSet, Iterable, Iterator, List, Optional, Sequence, Tuple, Union


SHOW_PLOTS = False
CHECK_GRID = False

# ============================================================
# 1. Data Structures and Mask Utilities
# ============================================================

@dataclass(frozen=True, slots=True)
class Point:
    id: int
    coords: Tuple[float, ...]

    @property
    def dim(self) -> int:
        return len(self.coords)

    @property
    def x(self) -> float:
        return self.coords[0]

    @property
    def y(self) -> float:
        return self.coords[1]


class MaskPool:
    """
    Deduplicate skyline masks.

    Instead of storing a Python frozenset for every grid cell, each cell stores
    a small integer reference into this pool. The actual skyline set is encoded
    as a Python int bit mask: point id i is represented by bit (i-1).
    """

    __slots__ = ("masks", "index")

    def __init__(self) -> None:
        self.masks: List[int] = [0]
        self.index: Optional[Dict[int, int]] = {0: 0}

    def intern(self, mask: int) -> int:
        if self.index is None:
            raise RuntimeError("cannot intern into a frozen MaskPool")
        idx = self.index.get(mask)
        if idx is not None:
            return idx
        idx = len(self.masks)
        self.masks.append(mask)
        self.index[mask] = idx
        return idx

    def get(self, ref: int) -> int:
        return self.masks[int(ref)]

    def __len__(self) -> int:
        return len(self.masks)

    def freeze(self) -> None:
        """Release the construction-only hash index while keeping refs readable."""
        self.index = None

    def thaw(self) -> None:
        """Rebuild the hash index when more masks need to be interned."""
        if self.index is None:
            self.index = {mask: ref for ref, mask in enumerate(self.masks)}

    def compact(self, refs: array, keep_index: bool = True) -> int:
        """
        Remove masks no longer referenced by a cell and rewrite refs in place.

        Global cells are overwritten after each quadrant merge. Without compaction,
        every intermediate mask remains strongly referenced by both ``masks`` and
        ``index`` for the rest of the build.
        """
        old_masks = self.masks
        old_count = len(old_masks)

        # Drop the old dict before allocating the remap table/new pool. The masks
        # themselves remain alive through old_masks until ref rewriting is done.
        self.index = None
        unseen = 0xFFFFFFFF
        remap = array("I", [unseen]) * old_count
        remap[0] = 0
        new_masks: List[int] = [0]

        for pos, old_ref_value in enumerate(refs):
            old_ref = int(old_ref_value)
            new_ref = remap[old_ref]
            if new_ref == unseen:
                new_ref = len(new_masks)
                if new_ref >= unseen:
                    raise OverflowError("MaskPool exceeds uint32 reference capacity")
                remap[old_ref] = new_ref
                new_masks.append(old_masks[old_ref])
            refs[pos] = new_ref

        self.masks = new_masks
        del old_masks
        del remap
        if keep_index:
            self.index = {mask: ref for ref, mask in enumerate(new_masks)}
        return old_count - len(new_masks)


@dataclass(slots=True)
class FineGrid:
    x_edges: List[float]
    y_edges: List[float]
    refs: array  # array('I'), length = num_cols * num_rows
    pool: MaskPool
    points: List[Point]
    dim: int = 2
    name: str = "fine"

    @property
    def num_cols(self) -> int:
        return len(self.x_edges) - 1

    @property
    def num_rows(self) -> int:
        return len(self.y_edges) - 1

    @property
    def num_cells(self) -> int:
        return self.num_cols * self.num_rows

    def idx(self, c: int, r: int) -> int:
        return c * self.num_rows + r

    def get_mask(self, c: int, r: int) -> int:
        if c < 0 or r < 0 or c >= self.num_cols or r >= self.num_rows:
            return 0
        return self.pool.get(self.refs[self.idx(c, r)])

    def set_mask(self, c: int, r: int, mask: int) -> None:
        self.refs[self.idx(c, r)] = self.pool.intern(mask)


@dataclass(slots=True)
class CoarseCell:
    x_range: Tuple[int, int]
    y_range: Tuple[int, int]
    result_ref: int
    error_sum: int
    max_extra: int


class CoarseCellStore(Sequence[CoarseCell]):
    """Sequence-compatible view backed by compact numeric arrays."""

    __slots__ = ("x_blocks", "y_blocks", "result_refs", "error_sums", "max_extras")

    def __init__(
        self,
        x_blocks: List[Tuple[int, int]],
        y_blocks: List[Tuple[int, int]],
        result_refs: array,
        error_sums: array,
        max_extras: array,
    ) -> None:
        expected = len(x_blocks) * len(y_blocks)
        if not (len(result_refs) == len(error_sums) == len(max_extras) == expected):
            raise ValueError("coarse cell arrays have inconsistent lengths")
        self.x_blocks = x_blocks
        self.y_blocks = y_blocks
        self.result_refs = result_refs
        self.error_sums = error_sums
        self.max_extras = max_extras

    def __len__(self) -> int:
        return len(self.result_refs)

    def _cell_at(self, index: int) -> CoarseCell:
        size = len(self)
        if index < 0:
            index += size
        if index < 0 or index >= size:
            raise IndexError("coarse cell index out of range")
        num_rows = len(self.y_blocks)
        c, r = divmod(index, num_rows)
        return CoarseCell(
            self.x_blocks[c],
            self.y_blocks[r],
            int(self.result_refs[index]),
            int(self.error_sums[index]),
            int(self.max_extras[index]),
        )

    def __getitem__(self, index: Union[int, slice]) -> Union[CoarseCell, List[CoarseCell]]:
        if isinstance(index, slice):
            return [self._cell_at(i) for i in range(*index.indices(len(self)))]
        return self._cell_at(index)

    def __iter__(self) -> Iterator[CoarseCell]:
        for index in range(len(self)):
            yield self._cell_at(index)


@dataclass(slots=True)
class CoarseGrid:
    fine: FineGrid
    x_blocks: List[Tuple[int, int]]
    y_blocks: List[Tuple[int, int]]
    cells: CoarseCellStore
    pool: MaskPool
    total_error: int
    max_cell_extra: int
    mode: str = ""

    @property
    def num_cols(self) -> int:
        return len(self.x_blocks)

    @property
    def num_rows(self) -> int:
        return len(self.y_blocks)

    @property
    def num_cells(self) -> int:
        return self.num_cols * self.num_rows

    def idx(self, c: int, r: int) -> int:
        return c * self.num_rows + r

    def get_cell(self, c: int, r: int) -> CoarseCell:
        return self.cells[self.idx(c, r)]

    def get_mask(self, c: int, r: int) -> int:
        return self.pool.get(self.cells.result_refs[self.idx(c, r)])


@dataclass(slots=True)
class FineGridStatistics:
    num_cols: int
    num_rows: int
    num_cells: int
    k_bar: float
    k_50: float
    k_90: float
    k_max: int
    vx: float
    vy: float


@dataclass(slots=True)
class RecommendedParameters:
    target_cols: int
    target_rows: int
    max_total_error: int
    max_cell_error: int
    target_coarse_cells: int
    compression_ratio: float
    stats: FineGridStatistics


def bit_of_id(pid: int) -> int:
    return 1 << (int(pid) - 1)


def mask_from_ids(ids: Iterable[int]) -> int:
    m = 0
    for pid in ids:
        if int(pid) <= 0:
            raise ValueError("point ids must be positive")
        m |= bit_of_id(int(pid))
    return m


def iter_mask_ids(mask: int):
    """Yield 1-based point ids encoded in a mask."""
    while mask:
        lsb = mask & -mask
        yield lsb.bit_length()
        mask ^= lsb


def mask_to_sorted_ids(mask: int) -> List[int]:
    return list(iter_mask_ids(mask))


def mask_size(mask: int) -> int:
    return int(mask).bit_count()


# ============================================================
# 2. Input and Basic Skyline Logic
# ============================================================


def read_points_from_txt(file_path: str, dimension: Optional[int] = None) -> List[Point]:
    points: List[Point] = []
    inferred_dim: Optional[int] = None

    with open(file_path, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            parts = [x.strip() for x in line.split(",")]
            try:
                coords = tuple(float(x) for x in parts)
            except ValueError:
                raise ValueError(f"Invalid numeric value at line {line_no}: {line}")

            if inferred_dim is None:
                inferred_dim = len(coords)
                if dimension is not None and inferred_dim != dimension:
                    raise ValueError(
                        f"Line {line_no} has dimension {inferred_dim}, but DIMENSION={dimension}."
                    )

            expected_dim = dimension if dimension is not None else inferred_dim
            if len(coords) != expected_dim:
                raise ValueError(
                    f"Inconsistent dimension at line {line_no}: expected {expected_dim}, got {len(coords)}."
                )
            if len(coords) < 2:
                raise ValueError("This implementation requires at least two dimensions.")

            points.append(Point(len(points) + 1, coords))

    if not points:
        raise ValueError("No valid points were loaded from the file.")
    return points


def dominates_min(a: Point, b: Point) -> bool:
    """
    Min-dominance in all stored coordinates.
    a dominates b iff a <= b in every dimension and < in at least one dimension.
    """
    le_all = all(x <= y for x, y in zip(a.coords, b.coords))
    lt_any = any(x < y for x, y in zip(a.coords, b.coords))
    return le_all and lt_any


def skyline_ids_from_candidates(candidates: Sequence[Point]) -> FrozenSet[int]:
    ans: List[int] = []
    for p in candidates:
        if not any(dominates_min(o, p) for o in candidates if o.id != p.id):
            ans.append(p.id)
    return frozenset(ans)


# ============================================================
# 3. Common Edges and NE Scanning FineGrid
# ============================================================


def unique_sorted(values: Iterable[float]) -> List[float]:
    return sorted(set(float(v) for v in values))


def build_common_original_edges(
    points: List[Point],
    x_pad_ratio: float = 0.08,
    y_pad_ratio: float = 0.08,
    origin_left: bool = True,
) -> Tuple[List[float], List[float]]:
    xs = unique_sorted(p.x for p in points)
    ys = unique_sorted(p.y for p in points)

    x_min, x_max = xs[0], xs[-1]
    y_min, y_max = ys[0], ys[-1]

    if origin_left:
        if x_min < 0 or y_min < 0:
            raise ValueError(
                "origin_left=True assumes non-negative coordinates. "
                "Set --no-origin-left if your data contains negative coordinates."
            )
        x_left = 0.0
        y_left = 0.0
    else:
        x_left = x_min
        y_left = y_min

    x_pad = max((x_max - x_min) * x_pad_ratio, 1.0)
    y_pad = max((y_max - y_min) * y_pad_ratio, 1.0)

    x_edges = sorted(set([x_left] + xs + [x_max + x_pad]))
    y_edges = sorted(set([y_left] + ys + [y_max + y_pad]))
    if len(x_edges) < 2 or len(y_edges) < 2:
        raise ValueError("Invalid edges.")
    return x_edges, y_edges


def transform_edges(edges: List[float], sign: int) -> List[float]:
    return sorted(set(float(sign) * e for e in edges))


def transform_points(points: List[Point], sx: int, sy: int) -> List[Point]:
    transformed: List[Point] = []
    for p in points:
        if len(p.coords) > 2:
            coords = (sx * p.coords[0], sy * p.coords[1]) + tuple(p.coords[2:])
        else:
            coords = (sx * p.coords[0], sy * p.coords[1])
        transformed.append(Point(p.id, coords))
    return transformed


def build_exact_fine_grid_scanning_with_edges(
    points: List[Point],
    x_edges: List[float],
    y_edges: List[float],
    name: str = "fine",
) -> FineGrid:
    """
    NE quadrant scanning over provided edges.

    Cell convention:
        (x_edges[c], x_edges[c+1]] x (y_edges[r], y_edges[r+1]]
    This is the same core scanning algorithm as before, but edges are supplied externally
    so different quadrant coordinate transforms can share a common original grid.
    Internally each cell stores a bit-mask reference, not a set object.
    """
    if not points:
        raise ValueError("points cannot be empty")

    num_cols = len(x_edges) - 1
    num_rows = len(y_edges) - 1
    pool = MaskPool()
    refs = array("I", [0]) * (num_cols * num_rows)
    grid = FineGrid(list(x_edges), list(y_edges), refs, pool, points, dim=2, name=name)

    point_by_id: Dict[int, Point] = {p.id: p for p in points}

    tmp: Dict[Tuple[float, float], List[int]] = {}
    for p in points:
        tmp.setdefault((p.x, p.y), []).append(p.id)

    # Store corner point ids as lists; they are usually length 1.
    corner_points: Dict[Tuple[float, float], List[int]] = tmp

    for c in reversed(range(num_cols)):
        for r in reversed(range(num_rows)):
            upper_right_corner = (x_edges[c + 1], y_edges[r + 1])

            right_mask = grid.get_mask(c + 1, r) if c + 1 < num_cols else 0
            upper_mask = grid.get_mask(c, r + 1) if r + 1 < num_rows else 0
            upper_right_mask = grid.get_mask(c + 1, r + 1) if (c + 1 < num_cols and r + 1 < num_rows) else 0

            # Equivalent to the previous Counter inclusion-exclusion:
            # keep id iff 1_R + 1_U - 1_UR > 0.
            current = ((right_mask | upper_mask) & ~upper_right_mask) | (right_mask & upper_mask)

            new_ids = corner_points.get(upper_right_corner)
            if new_ids:
                skyline_new_ids: List[int] = []
                if len(new_ids) == 1:
                    skyline_new_ids = new_ids
                else:
                    for pid in new_ids:
                        p = point_by_id[pid]
                        if not any(
                            dominates_min(point_by_id[oid], p)
                            for oid in new_ids
                            if oid != pid
                        ):
                            skyline_new_ids.append(pid)

                if skyline_new_ids:
                    dominated_mask = 0
                    for old_id in iter_mask_ids(current):
                        if any(dominates_min(point_by_id[nid], point_by_id[old_id]) for nid in skyline_new_ids):
                            dominated_mask |= bit_of_id(old_id)
                    current = (current & ~dominated_mask) | mask_from_ids(skyline_new_ids)

            grid.set_mask(c, r, current)

    return grid


# ============================================================
# 4. Four Quadrant FineGrid and Streaming Global Merge
# ============================================================

QUADRANTS: Dict[str, Tuple[int, int]] = {
    "NE": (1, 1),
    "NW": (-1, 1),
    "SW": (-1, -1),
    "SE": (1, -1),
}


def build_directional_fine_grid_by_transform(
    original_points: List[Point],
    original_x_edges: List[float],
    original_y_edges: List[float],
    quadrant: str,
) -> FineGrid:
    if quadrant not in QUADRANTS:
        raise ValueError(f"Unknown quadrant: {quadrant}")

    sx, sy = QUADRANTS[quadrant]
    t_points = transform_points(original_points, sx, sy)
    t_x_edges = transform_edges(original_x_edges, sx)
    t_y_edges = transform_edges(original_y_edges, sy)

    return build_exact_fine_grid_scanning_with_edges(
        t_points,
        t_x_edges,
        t_y_edges,
        name=f"fine_{quadrant}",
    )


def build_axis_index_map(n: int, sign: int, conservative_boundary: bool = True) -> List[Tuple[int, ...]]:
    """
    Map original fine-cell indices to transformed fine-cell indices.

    For sign=-1, cell boundary openness reverses after v -> -v, so an adjacent
    transformed cell is included when conservative_boundary=True.
    """
    out: List[Tuple[int, ...]] = []
    for i in range(n):
        if sign == 1:
            out.append((i,))
        else:
            mapped = n - 1 - i
            vals = [mapped]
            if conservative_boundary and mapped - 1 >= 0:
                vals.append(mapped - 1)
            out.append(tuple(v for v in vals if 0 <= v < n))
    return out


def or_directional_grid_into_global(
    global_grid: FineGrid,
    directional_grid: FineGrid,
    sx: int,
    sy: int,
    conservative_boundary: bool = True,
) -> None:
    """
    Merge one directional fine grid into the global fine grid in place.

    This replaces the old all-direction merge:
        merged = set(); merged.update(...); global_cells[(c,r)] = frozenset(merged)
    with mask-level OR and updates one quadrant at a time.
    """
    fx, fy = global_grid.num_cols, global_grid.num_rows
    x_map = build_axis_index_map(fx, sx, conservative_boundary)
    y_map = build_axis_index_map(fy, sy, conservative_boundary)

    g_refs = global_grid.refs
    g_pool = global_grid.pool
    d_get = directional_grid.get_mask
    gy = global_grid.num_rows

    for c in range(fx):
        mapped_x = x_map[c]
        base = c * gy
        for r in range(fy):
            add_mask = 0
            mapped_y = y_map[r]
            for tc in mapped_x:
                for tr in mapped_y:
                    add_mask |= d_get(tc, tr)
            if add_mask:
                idx = base + r
                old_mask = g_pool.get(g_refs[idx])
                new_mask = old_mask | add_mask
                if new_mask != old_mask:
                    g_refs[idx] = g_pool.intern(new_mask)


def build_axis_trigger_map(
    n: int,
    sign: int,
    conservative_boundary: bool = True,
) -> List[List[Tuple[int, Tuple[int, ...]]]]:
    """
    Group original cells by the last transformed index needed during reverse scan.

    A negative-axis conservative mapping needs at most two adjacent transformed
    cells. When the smaller index has been scanned, both values are available in
    the current/right columns (or current/upper rows), so the original cell can
    be merged exactly once.
    """
    triggers: List[List[Tuple[int, Tuple[int, ...]]]] = [[] for _ in range(n)]
    for original_index, mapped_indices in enumerate(
        build_axis_index_map(n, sign, conservative_boundary)
    ):
        triggers[min(mapped_indices)].append((original_index, mapped_indices))
    return triggers


def scan_directional_into_global(
    global_grid: FineGrid,
    original_points: List[Point],
    original_x_edges: List[float],
    original_y_edges: List[float],
    quadrant: str,
    conservative_boundary: bool = True,
) -> int:
    """
    Scan one quadrant using only two columns and merge it into global_grid.

    This has the same cell masks and boundary mapping as constructing a complete
    directional FineGrid followed by ``or_directional_grid_into_global``, but it
    avoids the directional cell-ref array and its entire mask pool.
    """
    if quadrant not in QUADRANTS:
        raise ValueError(f"Unknown quadrant: {quadrant}")

    sx, sy = QUADRANTS[quadrant]
    points = transform_points(original_points, sx, sy)
    x_edges = transform_edges(original_x_edges, sx)
    y_edges = transform_edges(original_y_edges, sy)
    num_cols = len(x_edges) - 1
    num_rows = len(y_edges) - 1
    if num_cols != global_grid.num_cols or num_rows != global_grid.num_rows:
        raise ValueError("directional and global grid dimensions differ")

    point_by_id: Dict[int, Point] = {p.id: p for p in points}
    corner_points: Dict[Tuple[float, float], List[int]] = {}
    for p in points:
        corner_points.setdefault((p.x, p.y), []).append(p.id)

    x_triggers = build_axis_trigger_map(
        num_cols,
        sx,
        conservative_boundary,
    )
    y_triggers = build_axis_trigger_map(
        num_rows,
        sy,
        conservative_boundary,
    )

    g_refs = global_grid.refs
    g_pool = global_grid.pool
    global_rows = global_grid.num_rows
    right_col: Optional[List[int]] = None
    released_masks = 0
    pool_count_at_start = len(g_pool)
    compact_growth_threshold = min(
        200_000,
        max(50_000, global_grid.num_cells // 8),
    )

    for c in reversed(range(num_cols)):
        current_col = [0] * num_rows
        for r in reversed(range(num_rows)):
            right_mask = right_col[r] if right_col is not None else 0
            upper_mask = current_col[r + 1] if r + 1 < num_rows else 0
            upper_right_mask = (
                right_col[r + 1]
                if right_col is not None and r + 1 < num_rows
                else 0
            )
            current = (
                ((right_mask | upper_mask) & ~upper_right_mask)
                | (right_mask & upper_mask)
            )

            new_ids = corner_points.get((x_edges[c + 1], y_edges[r + 1]))
            if new_ids:
                if len(new_ids) == 1:
                    skyline_new_ids = new_ids
                else:
                    skyline_new_ids = []
                    for pid in new_ids:
                        p = point_by_id[pid]
                        if not any(
                            dominates_min(point_by_id[oid], p)
                            for oid in new_ids
                            if oid != pid
                        ):
                            skyline_new_ids.append(pid)

                if skyline_new_ids:
                    dominated_mask = 0
                    for old_id in iter_mask_ids(current):
                        if any(
                            dominates_min(
                                point_by_id[nid],
                                point_by_id[old_id],
                            )
                            for nid in skyline_new_ids
                        ):
                            dominated_mask |= bit_of_id(old_id)
                    current = (
                        (current & ~dominated_mask)
                        | mask_from_ids(skyline_new_ids)
                    )

            current_col[r] = current

            # Every original cell is triggered exactly once, after all one-to-four
            # directional masks required by conservative mapping are available.
            for original_c, mapped_cols in x_triggers[c]:
                base = original_c * global_rows
                for original_r, mapped_rows in y_triggers[r]:
                    add_mask = 0
                    for transformed_c in mapped_cols:
                        source_col = (
                            current_col
                            if transformed_c == c
                            else right_col
                        )
                        if source_col is None:
                            raise RuntimeError("directional scan dependency missing")
                        for transformed_r in mapped_rows:
                            add_mask |= source_col[transformed_r]

                    if add_mask:
                        idx = base + original_r
                        old_mask = g_pool.get(g_refs[idx])
                        new_mask = old_mask | add_mask
                        if new_mask != old_mask:
                            g_refs[idx] = g_pool.intern(new_mask)

        right_col = current_col
        if (
            c == num_cols // 2
            and len(g_pool) - pool_count_at_start > compact_growth_threshold
        ):
            released_masks += g_pool.compact(g_refs, keep_index=True)

    return released_masks


def build_global_fine_grid(
    points: List[Point],
    x_pad_ratio: float = 0.08,
    y_pad_ratio: float = 0.08,
    origin_left: bool = True,
    conservative_boundary: bool = True,
) -> FineGrid:
    """
    Column-streaming version: each directional grid is scanned with two columns
    and merged directly into GlobalFineGrid. No full directional FineGrid exists.
    """
    original_x_edges, original_y_edges = build_common_original_edges(
        points,
        x_pad_ratio=x_pad_ratio,
        y_pad_ratio=y_pad_ratio,
        origin_left=origin_left,
    )

    num_cols = len(original_x_edges) - 1
    num_rows = len(original_y_edges) - 1
    print(f"Expected cols={num_cols}, rows={num_rows}, cells={num_cols * num_rows}")
    global_grid = FineGrid(
        x_edges=original_x_edges,
        y_edges=original_y_edges,
        refs=array("I", [0]) * (num_cols * num_rows),
        pool=MaskPool(),
        points=points,
        dim=2,
        name="global_fine",
    )

    for qname in ["NE", "NW", "SW", "SE"]:
        global_grid.pool.thaw()
        t0 = time.perf_counter()
        removed = scan_directional_into_global(
            global_grid,
            points,
            original_x_edges,
            original_y_edges,
            qname,
            conservative_boundary=conservative_boundary,
        )
        removed += global_grid.pool.compact(
            global_grid.refs,
            keep_index=False,
        )
        t1 = time.perf_counter()
        print(
            f"{qname}-Quadrant column scan/merge/compact time: {(t1 - t0) * 1000:.2f} ms "
            f"(released {removed} stale masks)"
        )
        gc.collect()

    print(f"GlobalFineGrid live unique mask count: {len(global_grid.pool)}")
    return global_grid


# ============================================================
# 5. Coarse Grid Evaluation and Mode 1 / Mode 2(Mixed)
# ============================================================


def initial_blocks(n: int) -> List[Tuple[int, int]]:
    return [(i, i + 1) for i in range(n)]


def jaccard_distance_mask(a: int, b: int) -> float:
    if not a and not b:
        return 0.0
    u = mask_size(a | b)
    if u == 0:
        return 0.0
    return 1.0 - mask_size(a & b) / u


def compute_x_line_weights(fine: FineGrid) -> List[float]:
    weights: List[float] = []
    for c in range(fine.num_cols - 1):
        total = 0.0
        for r in range(fine.num_rows):
            total += jaccard_distance_mask(fine.get_mask(c, r), fine.get_mask(c + 1, r))
        weights.append(total)
    return weights


def compute_y_line_weights(fine: FineGrid) -> List[float]:
    weights: List[float] = []
    for r in range(fine.num_rows - 1):
        total = 0.0
        for c in range(fine.num_cols):
            total += jaccard_distance_mask(fine.get_mask(c, r), fine.get_mask(c, r + 1))
        weights.append(total)
    return weights


def make_equal_blocks(n: int, target: int) -> List[Tuple[int, int]]:
    if n <= 0:
        raise ValueError("n must be positive")
    target = max(1, min(target, n))
    cuts = []
    for k in range(1, target):
        cut = round(k * n / target)
        if 0 < cut < n:
            cuts.append(cut)
    cuts = sorted(set(cuts))[: target - 1]
    boundaries = [0] + cuts + [n]
    return [(a, b) for a, b in zip(boundaries[:-1], boundaries[1:]) if b > a]


def make_top_weight_blocks(n: int, target: int, boundary_weights: List[float]) -> List[Tuple[int, int]]:
    if n <= 0:
        raise ValueError("n must be positive")
    target = max(1, min(target, n))
    if target == n:
        return initial_blocks(n)
    if target == 1:
        return [(0, n)]
    if len(boundary_weights) != max(0, n - 1):
        raise ValueError("boundary_weights length must be n - 1")

    num_cuts = target - 1
    if not boundary_weights or sum(boundary_weights) <= 1e-12:
        return make_equal_blocks(n, target)

    ranked = sorted(range(len(boundary_weights)), key=lambda i: boundary_weights[i], reverse=True)
    cuts = sorted(set(i + 1 for i in ranked[:num_cuts] if 0 < i + 1 < n))

    if len(cuts) < num_cuts:
        fallback = [round(k * n / target) for k in range(1, target)]
        cuts = sorted(set(cuts + [c for c in fallback if 0 < c < n]))

    cuts = cuts[:num_cuts]
    boundaries = [0] + cuts + [n]
    return [(a, b) for a, b in zip(boundaries[:-1], boundaries[1:]) if b > a]


def evaluate_single_block_mask(
    fine: FineGrid,
    x_range: Tuple[int, int],
    y_range: Tuple[int, int],
    pool: MaskPool,
) -> CoarseCell:
    block_mask = 0
    for c in range(x_range[0], x_range[1]):
        base = c * fine.num_rows
        for r in range(y_range[0], y_range[1]):
            block_mask |= fine.pool.get(fine.refs[base + r])

    error_sum = 0
    max_extra = 0
    for c in range(x_range[0], x_range[1]):
        base = c * fine.num_rows
        for r in range(y_range[0], y_range[1]):
            exact = fine.pool.get(fine.refs[base + r])
            extra = mask_size(block_mask & ~exact)
            error_sum += extra
            if extra > max_extra:
                max_extra = extra

    return CoarseCell(x_range, y_range, pool.intern(block_mask), error_sum, max_extra)


def evaluate_coarse_grid(
    fine: FineGrid,
    x_blocks: List[Tuple[int, int]],
    y_blocks: List[Tuple[int, int]],
    mode: str = "",
) -> CoarseGrid:
    pool = MaskPool()
    result_refs = array("I")
    error_sums = array("Q")
    max_extras = array("I")
    total_error = 0
    max_cell_extra = 0
    for xb in x_blocks:
        for yb in y_blocks:
            cell = evaluate_single_block_mask(fine, xb, yb, pool)
            result_refs.append(cell.result_ref)
            error_sums.append(cell.error_sum)
            max_extras.append(cell.max_extra)
            total_error += cell.error_sum
            if cell.max_extra > max_cell_extra:
                max_cell_extra = cell.max_extra

    stored_x_blocks = list(x_blocks)
    stored_y_blocks = list(y_blocks)
    cells = CoarseCellStore(
        stored_x_blocks,
        stored_y_blocks,
        result_refs,
        error_sums,
        max_extras,
    )
    pool.freeze()
    return CoarseGrid(
        fine,
        stored_x_blocks,
        stored_y_blocks,
        cells,
        pool,
        total_error,
        max_cell_extra,
        mode=mode,
    )


def build_grid_by_topweight_target_direct(
    fine: FineGrid,
    target_cols: int,
    target_rows: int,
    x_weights: Optional[List[float]] = None,
    y_weights: Optional[List[float]] = None,
) -> CoarseGrid:
    target_cols = max(1, min(target_cols, fine.num_cols))
    target_rows = max(1, min(target_rows, fine.num_rows))
    if x_weights is None:
        x_weights = compute_x_line_weights(fine)
    if y_weights is None:
        y_weights = compute_y_line_weights(fine)
    x_blocks = make_top_weight_blocks(fine.num_cols, target_cols, x_weights)
    y_blocks = make_top_weight_blocks(fine.num_rows, target_rows, y_weights)
    return evaluate_coarse_grid(fine, x_blocks, y_blocks, mode="mode1")

# ============================================================
# 6. Statistics and Budget Recommendation
# ============================================================


def cell_size_histogram(fine: FineGrid) -> Tuple[int, int, int, Dict[int, int]]:
    total_cells = fine.num_cells
    sum_sizes = 0
    max_size = 0
    hist: Dict[int, int] = {}
    pget = fine.pool.get
    for ref in fine.refs:
        size = mask_size(pget(ref))
        sum_sizes += size
        if size > max_size:
            max_size = size
        hist[size] = hist.get(size, 0) + 1
    return total_cells, sum_sizes, max_size, hist


def percentile_from_histogram(hist: Dict[int, int], total: int, q: float) -> float:
    if total <= 0:
        return 0.0
    if total == 1:
        return float(next(iter(hist.keys())))

    pos = q * (total - 1)
    lo_rank = int(math.floor(pos))
    hi_rank = int(math.ceil(pos))
    weight = pos - lo_rank

    def value_at_rank(rank: int) -> int:
        acc = 0
        for value in sorted(hist.keys()):
            acc += hist[value]
            if acc > rank:
                return value
        return max(hist.keys())

    lo_val = value_at_rank(lo_rank)
    hi_val = value_at_rank(hi_rank)
    return float(lo_val * (1.0 - weight) + hi_val * weight)


def compute_directional_variation_from_weights(fine: FineGrid, x_weights: List[float], y_weights: List[float]) -> Tuple[float, float]:
    vx = mean(x_weights) / fine.num_rows if x_weights else 0.0
    vy = mean(y_weights) / fine.num_cols if y_weights else 0.0
    return vx, vy


def compute_fine_grid_statistics(
    fine: FineGrid,
    x_weights: Optional[List[float]] = None,
    y_weights: Optional[List[float]] = None,
) -> FineGridStatistics:
    total_cells, sum_sizes, max_size, hist = cell_size_histogram(fine)
    if x_weights is None:
        x_weights = compute_x_line_weights(fine)
    if y_weights is None:
        y_weights = compute_y_line_weights(fine)
    vx, vy = compute_directional_variation_from_weights(fine, x_weights, y_weights)
    return FineGridStatistics(
        num_cols=fine.num_cols,
        num_rows=fine.num_rows,
        num_cells=fine.num_cells,
        k_bar=(sum_sizes / total_cells) if total_cells else 0.0,
        k_50=percentile_from_histogram(hist, total_cells, 0.50),
        k_90=percentile_from_histogram(hist, total_cells, 0.90),
        k_max=max_size,
        vx=vx,
        vy=vy,
    )


def recommend_error_budgets(stats: FineGridStatistics, pi_avg: float, pi_min: float) -> Tuple[int, int]:
    if not (0 < pi_avg <= 1):
        raise ValueError("pi_avg must be in (0,1]")
    if not (0 < pi_min <= 1):
        raise ValueError("pi_min must be in (0,1]")
    max_total_error = int(math.floor(stats.num_cells * stats.k_bar * (1.0 / pi_avg - 1.0)))
    max_cell_error = int(math.floor(stats.k_90 * (1.0 / pi_min - 1.0)))
    if pi_avg < 1.0:
        max_total_error = max(1, max_total_error)
    if pi_min < 1.0:
        max_cell_error = max(1, max_cell_error)
    return max_total_error, max_cell_error


def target_cols_rows_from_cell_budget(
    fine: FineGrid,
    target_cells: int,
    vx: float,
    vy: float,
    min_cols: int = 1,
    min_rows: int = 1,
) -> Tuple[int, int]:
    target_cells = max(1, min(target_cells, fine.num_cells))
    fx, fy = fine.num_cols, fine.num_rows
    eps = 1e-12
    ax = (fx ** 2) * max(vx, eps)
    ay = (fy ** 2) * max(vy, eps)
    if vx < eps and vy < eps:
        cols = int(round(math.sqrt(target_cells * fx / fy)))
    else:
        ratio = (ax / ay) ** 0.25
        cols = int(round(math.sqrt(target_cells) * ratio))
    cols = max(min_cols, min(fx, max(1, cols)))
    rows = int(math.ceil(target_cells / cols))
    rows = max(min_rows, min(fy, max(1, rows)))
    while cols * rows < target_cells and rows < fy:
        rows += 1
    while cols * rows < target_cells and cols < fx:
        cols += 1
    return cols, rows


def recommend_target_resolution(
    stats: FineGridStatistics,
    compression_ratio: Optional[float],
    max_grid_cells: Optional[int],
    min_target_cols: int,
    min_target_rows: int,
) -> Tuple[int, int, int, float]:
    if max_grid_cells is None:
        if compression_ratio is None or compression_ratio <= 0:
            raise ValueError("compression_ratio must be positive when max_grid_cells is None")
        target_cells = int(math.ceil(stats.num_cells / compression_ratio))
    else:
        target_cells = int(max_grid_cells)
    target_cells = max(1, min(target_cells, stats.num_cells))

    # A light-weight fake grid only for its dimensions.
    dummy_refs = array("I", [0]) * 1
    dummy = FineGrid([0.0] * (stats.num_cols + 1), [0.0] * (stats.num_rows + 1), dummy_refs, MaskPool(), [])
    cols, rows = target_cols_rows_from_cell_budget(
        dummy,
        target_cells,
        stats.vx,
        stats.vy,
        min_cols=min_target_cols,
        min_rows=min_target_rows,
    )
    achieved_ratio = stats.num_cells / max(1, cols * rows)
    return cols, rows, target_cells, achieved_ratio


def recommend_parameters(
    fine: FineGrid,
    x_weights: List[float],
    y_weights: List[float],
    pi_avg: float,
    pi_min: float,
    compression_ratio: Optional[float],
    max_grid_cells: Optional[int],
    min_target_cols: int,
    min_target_rows: int,
) -> RecommendedParameters:
    stats = compute_fine_grid_statistics(fine, x_weights, y_weights)
    max_total_error, max_cell_error = recommend_error_budgets(stats, pi_avg, pi_min)
    target_cols, target_rows, target_cells, achieved_ratio = recommend_target_resolution(
        stats,
        compression_ratio,
        max_grid_cells,
        min_target_cols,
        min_target_rows,
    )
    return RecommendedParameters(target_cols, target_rows, max_total_error, max_cell_error, target_cells, achieved_ratio, stats)


def grid_satisfies_budget(grid: CoarseGrid, max_total_error: int, max_cell_error: Optional[int]) -> bool:
    if grid.total_error > max_total_error:
        return False
    if max_cell_error is not None and grid.max_cell_extra > max_cell_error:
        return False
    return True


def build_grid_by_mixed(
    fine: FineGrid,
    target_cols: int,
    target_rows: int,
    max_total_error: int,
    max_cell_error: Optional[int],
    x_weights: Optional[List[float]] = None,
    y_weights: Optional[List[float]] = None,
    vx: Optional[float] = None,
    vy: Optional[float] = None,
    max_iter: int = 25,
) -> CoarseGrid:
    if x_weights is None:
        x_weights = compute_x_line_weights(fine)
    if y_weights is None:
        y_weights = compute_y_line_weights(fine)
    if vx is None:
        vx = mean(x_weights) / fine.num_rows if x_weights else 0.0
    if vy is None:
        vy = mean(y_weights) / fine.num_cols if y_weights else 0.0

    c0 = time.perf_counter()
    candidate = build_grid_by_topweight_target_direct(fine, target_cols, target_rows, x_weights, y_weights)
    c1 = time.perf_counter()
    print(f"\nbuild_coarse_grid_by_direct time: {(c1 - c0) * 1000:.2f} ms")
    print_grid_summary("Mode 2 initial TopWeight grid (mode1)", candidate)
    candidate.mode = "mode2"

    print("\nMode 2 budget check:")
    if grid_satisfies_budget(candidate, max_total_error, max_cell_error):
        print("  initial TopWeight grid already satisfies the error budgets, Mode 2 equals Mode 1")
        return candidate
    else:
        print("  initial TopWeight grid violates the error budgets")
        print(f"  total_positive_error={candidate.total_error}, max_total_error={max_total_error}")
        if max_cell_error is not None:
            print(f"  max_cell_extra={candidate.max_cell_extra}, max_cell_error={max_cell_error}")
        print("  start binary search for a finer grid")

    target_cells = max(1, candidate.num_cells)
    fine_cells = fine.num_cells
    low = target_cells + 1
    high = fine_cells
    best: Optional[CoarseGrid] = None
    seen = set()

    for it in range(max_iter):
        if low > high:
            break
        mid = (low + high) // 2
        cols, rows = target_cols_rows_from_cell_budget(
            fine,
            target_cells=mid,
            vx=vx,
            vy=vy,
            min_cols=target_cols,
            min_rows=target_rows,
        )
        key = (cols, rows)
        if key in seen:
            break
        seen.add(key)

        grid = build_grid_by_topweight_target_direct(fine, cols, rows, x_weights, y_weights)
        grid.mode = "mode2"
        ok = grid_satisfies_budget(grid, max_total_error, max_cell_error)
        print(
            f"  iter={it + 1}, target_cells={mid}, cols={cols}, rows={rows}, "
            f"actual_cells={grid.num_cells}, total_error={grid.total_error}, "
            f"max_extra={grid.max_cell_extra}, ok={ok}"
        )

        if ok:
            best = grid
            high = max(target_cells + 1, grid.num_cells - 1)
        else:
            low = grid.num_cells + 1

        # Free non-best grids as early as possible.
        if not ok:
            del grid
            gc.collect()

    if best is None:
        print("\nMode 2 warning: no searched grid satisfied the budget within max_iter, returning Mode 1")
        return candidate
    best.mode = "mode2"
    return best


# ============================================================
# 7. Query and Exact Refinement
# ============================================================


def locate_index(edges: List[float], value: float) -> int:
    if value < edges[0] or value > edges[-1]:
        raise ValueError(f"value {value} is outside grid range [{edges[0]}, {edges[-1]}]")
    i = bisect.bisect_left(edges, value) - 1
    if i < 0:
        i = 0
    if i >= len(edges) - 1:
        i = len(edges) - 2
    return i


def locate_block(blocks: List[Tuple[int, int]], fine_index: int) -> int:
    # Binary search because blocks are sorted and non-overlapping.
    lo, hi = 0, len(blocks) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        l, r = blocks[mid]
        if fine_index < l:
            hi = mid - 1
        elif fine_index >= r:
            lo = mid + 1
        else:
            return mid
    raise ValueError("fine_index is outside block range")


def query_fine_grid(fine: FineGrid, qx: float, qy: float) -> FrozenSet[int]:
    c = locate_index(fine.x_edges, qx)
    r = locate_index(fine.y_edges, qy)
    return frozenset(mask_to_sorted_ids(fine.get_mask(c, r)))


def query_grid(grid: CoarseGrid, qx: float, qy: float) -> FrozenSet[int]:
    fine = grid.fine
    fc = locate_index(fine.x_edges, qx)
    fr = locate_index(fine.y_edges, qy)
    gc = locate_block(grid.x_blocks, fc)
    gr = locate_block(grid.y_blocks, fr)
    return frozenset(mask_to_sorted_ids(grid.get_mask(gc, gr)))


# compatibility alias
query_coarse_grid = query_grid


def quadrant_of_point(qx: float, qy: float, p: Point) -> List[str]:
    names = []
    if p.x >= qx and p.y >= qy:
        names.append("NE")
    if p.x <= qx and p.y >= qy:
        names.append("NW")
    if p.x <= qx and p.y <= qy:
        names.append("SW")
    if p.x >= qx and p.y <= qy:
        names.append("SE")
    return names


def dominates_global_same_quadrant(a: Point, b: Point, qx: float, qy: float) -> bool:
    qa = set(quadrant_of_point(qx, qy, a))
    qb = set(quadrant_of_point(qx, qy, b))
    if not (qa & qb):
        return False
    da = (abs(a.x - qx), abs(a.y - qy))
    db = (abs(b.x - qx), abs(b.y - qy))
    le_all = da[0] <= db[0] and da[1] <= db[1]
    lt_any = da[0] < db[0] or da[1] < db[1]
    return le_all and lt_any


def refine_global_skyline_from_candidates(points: List[Point], candidate_ids: Iterable[int], qx: float, qy: float) -> FrozenSet[int]:
    by_id = {p.id: p for p in points}
    candidates = [by_id[i] for i in sorted(set(candidate_ids)) if i in by_id]
    ans = []
    for p in candidates:
        if not any(dominates_global_same_quadrant(o, p, qx, qy) for o in candidates if o.id != p.id):
            ans.append(p.id)
    return frozenset(ans)


def exact_global_skyline(points: List[Point], qx: float, qy: float) -> FrozenSet[int]:
    return refine_global_skyline_from_candidates(points, [p.id for p in points], qx, qy)


def check_no_false_negative_global(grid: CoarseGrid) -> bool:
    fine = grid.fine
    for c in range(fine.num_cols):
        for r in range(fine.num_rows):
            gc = locate_block(grid.x_blocks, c)
            gr = locate_block(grid.y_blocks, r)
            exact_mask = fine.get_mask(c, r)
            approx_mask = grid.get_mask(gc, gr)
            if exact_mask & ~approx_mask:
                print("False negative at global fine cell", (c, r))
                print("exact", mask_to_sorted_ids(exact_mask))
                print("approx", mask_to_sorted_ids(approx_mask))
                return False
    return True


# ============================================================
# 8. Export, Local Index, Decode
# ============================================================


def block_edges(edges: List[float], blocks: List[Tuple[int, int]]) -> List[float]:
    coords = [edges[blocks[0][0]]]
    for _, r in blocks:
        coords.append(edges[r])
    return coords


def grid_cell_logical_index(grid: CoarseGrid, grid_col: int, grid_row: int) -> int:
    return grid_col * grid.num_rows + grid_row


def grid_cell_for_query(grid: CoarseGrid, qx: float, qy: float) -> Tuple[int, int, int]:
    fine = grid.fine
    fc = locate_index(fine.x_edges, qx)
    fr = locate_index(fine.y_edges, qy)
    gc = locate_block(grid.x_blocks, fc)
    gr = locate_block(grid.y_blocks, fr)
    return gc, gr, grid_cell_logical_index(grid, gc, gr)


def encode_skyline_record(ids: Sequence[int], record_words: int) -> List[int]:
    ids_sorted = [int(x) for x in sorted(ids)]
    if len(ids_sorted) + 1 > record_words:
        raise ValueError("record_words is too small for this skyline record")
    record = [len(ids_sorted)] + ids_sorted
    record.extend([0] * (record_words - len(record)))
    return record


def decode_skyline_record(record: Sequence[int]) -> FrozenSet[int]:
    if not record:
        return frozenset()
    count = int(record[0])
    if count < 0 or count > len(record) - 1:
        raise ValueError(f"invalid record count={count}, record length={len(record)}")
    return frozenset(int(x) for x in record[1:1 + count] if int(x) != 0)


def export_grid_database_for_pir(grid: CoarseGrid, out_prefix: str, points: List[Point]) -> Tuple[str, str]:
    os.makedirs(os.path.dirname(out_prefix) or ".", exist_ok=True)

    max_len = 0
    for result_ref in grid.cells.result_refs:
        size = mask_size(grid.pool.get(result_ref))
        if size > max_len:
            max_len = size

    record_words = max(1, 1 + max_len)
    record_bytes = record_words * 8
    db_records = grid.num_cells

    db_path = out_prefix + ".pir.db"
    meta_path = out_prefix + ".pir.meta.json"

    with open(db_path, "wb") as f:
        for gc_ in range(grid.num_cols):
            for gr_ in range(grid.num_rows):
                ids = mask_to_sorted_ids(grid.get_mask(gc_, gr_))
                record = encode_skyline_record(ids, record_words)
                for v in record:
                    f.write(struct.pack("<Q", int(v)))

    meta = {
        "format": "skyagg_griddb_fixed_u64_v2_bitmask_optimized_builder",
        "endianness": "little",
        "uint_bits": 64,
        "db_path_hint": os.path.basename(db_path),
        "db_records": db_records,
        "record_words": record_words,
        "record_bytes": record_bytes,
        "db_file_bytes": db_records * record_bytes,
        "record_layout": "[count, point_id_1, ..., point_id_count, zero_padding]",
        "index_order": "index = grid_col * grid_num_rows + grid_row",
        "grid_num_cols": grid.num_cols,
        "grid_num_rows": grid.num_rows,
        "x_edges": grid.fine.x_edges,
        "y_edges": grid.fine.y_edges,
        "x_blocks": [list(x) for x in grid.x_blocks],
        "y_blocks": [list(y) for y in grid.y_blocks],
        "grid_x_edges": block_edges(grid.fine.x_edges, grid.x_blocks),
        "grid_y_edges": block_edges(grid.fine.y_edges, grid.y_blocks),
        "algorithm_mode": grid.mode,
        "num_points": len(points),
        "points": [{"id": p.id, "coords": list(p.coords)} for p in points],
    }

    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)

    return db_path, meta_path


def locate_pir_index_from_metadata(meta: dict, qx: float, qy: float) -> Tuple[int, int, int]:
    x_edges = [float(x) for x in meta["x_edges"]]
    y_edges = [float(y) for y in meta["y_edges"]]
    x_blocks = [tuple(x) for x in meta["x_blocks"]]
    y_blocks = [tuple(y) for y in meta["y_blocks"]]
    fc = locate_index(x_edges, qx)
    fr = locate_index(y_edges, qy)
    gc_ = locate_block(x_blocks, fc)
    gr_ = locate_block(y_blocks, fr)
    logical = gc_ * int(meta["grid_num_rows"]) + gr_
    return gc_, gr_, logical


def read_grid_database_record(db_path: str, meta: dict, index: int) -> List[int]:
    record_words = int(meta["record_words"])
    db_records = int(meta["db_records"])
    if index < 0 or index >= db_records:
        raise ValueError(f"index {index} out of range [0,{db_records})")
    offset = index * record_words * 8
    with open(db_path, "rb") as f:
        f.seek(offset)
        raw = f.read(record_words * 8)
    if len(raw) != record_words * 8:
        raise ValueError("short read from database file")
    return [struct.unpack_from("<Q", raw, i * 8)[0] for i in range(record_words)]


def save_query_index_file(meta_path: str, qx: float, qy: float, out_path: str) -> str:
    with open(meta_path, "r", encoding="utf-8") as f:
        meta = json.load(f)
    gc_, gr_, index = locate_pir_index_from_metadata(meta, qx, qy)
    info = {
        "query": [qx, qy],
        "grid_col": gc_,
        "grid_row": gr_,
        "pir_index": index,
        "meta": os.path.basename(meta_path),
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(info, f, indent=2, ensure_ascii=False)
    return out_path


# ============================================================
# 9. Summaries and Optional Visualization
# ============================================================


def coarse_weighted_candidate_average(grid: CoarseGrid) -> float:
    if grid.fine.num_cells == 0:
        return 0.0
    weighted_sum = 0
    index = 0
    for xb in grid.x_blocks:
        width = xb[1] - xb[0]
        for yb in grid.y_blocks:
            block_area = width * (yb[1] - yb[0])
            weighted_sum += mask_size(grid.pool.get(grid.cells.result_refs[index])) * block_area
            index += 1
    return weighted_sum / grid.fine.num_cells


def print_fine_summary(name: str, fine: FineGrid) -> None:
    total_cells, sum_sizes, max_size, _ = cell_size_histogram(fine)
    avg_size = (sum_sizes / total_cells) if total_cells else 0.0
    print(f"\n{name}")
    print(f"  cols={fine.num_cols}, rows={fine.num_rows}, cells={fine.num_cells}")
    print(f"  avg_size={avg_size:.2f}, max_size={max_size}")
    print(f"  unique_mask_count={len(fine.pool)}")


def print_grid_summary(name: str, grid: CoarseGrid) -> None:
    total_size = 0
    max_size = 0
    for result_ref in grid.cells.result_refs:
        size = mask_size(grid.pool.get(result_ref))
        total_size += size
        if size > max_size:
            max_size = size
    unweighted_avg = total_size / grid.num_cells if grid.num_cells else 0.0
    weighted_avg = coarse_weighted_candidate_average(grid)

    print(f"\n{name}")
    print(f"  cols={grid.num_cols}, rows={grid.num_rows}, cells={grid.num_cells}")
    print(f"  total_positive_error={grid.total_error}")
    print(f"  max_cell_extra={grid.max_cell_extra}")
    print(f"  avg_candidate_size={unweighted_avg:.2f}, max_candidate_size={max_size}")
    print(f"  avg_candidate_size_weighted_by_fine_cell={weighted_avg:.2f}")
    print(f"  unique_mask_count={len(grid.pool)}")
    if CHECK_GRID:
        print(f"  no_false_negative_on_global_fine={check_no_false_negative_global(grid)}")


def format_result_mask(mask: int, max_items: int = 8) -> str:
    if not mask:
        return "∅"
    ids = mask_to_sorted_ids(mask)
    if len(ids) <= max_items:
        return ",".join(map(str, ids))
    return ",".join(map(str, ids[:max_items])) + f"\n+{len(ids) - max_items}"


def plot_grid(grid, title: str, show_points: bool = True, show_cell_result: bool = True, figsize: Tuple[int, int] = (9, 7), max_items_per_cell: int = 8) -> None:
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib is not available")
        return

    if isinstance(grid, FineGrid):
        fine = grid
        x_edges = fine.x_edges
        y_edges = fine.y_edges
        x_blocks = initial_blocks(fine.num_cols)
        y_blocks = initial_blocks(fine.num_rows)
        def get_mask(ci, ri):
            return fine.get_mask(ci, ri)
        subtitle = f"fine grid: {fine.num_cols} x {fine.num_rows}"
    elif isinstance(grid, CoarseGrid):
        fine = grid.fine
        x_edges = block_edges(fine.x_edges, grid.x_blocks)
        y_edges = block_edges(fine.y_edges, grid.y_blocks)
        x_blocks = grid.x_blocks
        y_blocks = grid.y_blocks
        def get_mask(ci, ri):
            return grid.get_mask(ci, ri)
        subtitle = f"grid: {grid.num_cols} x {grid.num_rows}, total_error={grid.total_error}, max_cell_extra={grid.max_cell_extra}"
    else:
        raise TypeError("grid must be FineGrid or CoarseGrid")

    fig, ax = plt.subplots(figsize=figsize)
    for x in x_edges:
        ax.axvline(x, linewidth=0.8)
    for y in y_edges:
        ax.axhline(y, linewidth=0.8)
    if show_points:
        xs = [p.x for p in fine.points]
        ys = [p.y for p in fine.points]
        ax.scatter(xs, ys, s=30)
        for p in fine.points:
            ax.text(p.x, p.y, str(p.id), fontsize=9, ha="center", va="bottom")
    if show_cell_result:
        for ci, xb in enumerate(x_blocks):
            for ri, yb in enumerate(y_blocks):
                x0 = fine.x_edges[xb[0]]
                x1 = fine.x_edges[xb[1]]
                y0 = fine.y_edges[yb[0]]
                y1 = fine.y_edges[yb[1]]
                ax.text((x0 + x1) / 2, (y0 + y1) / 2, format_result_mask(get_mask(ci, ri), max_items_per_cell), fontsize=7, ha="center", va="center")
    ax.set_title(title + "\n" + subtitle)
    ax.set_xlabel("X")
    ax.set_ylabel("Y")
    ax.set_xlim(min(fine.x_edges), max(fine.x_edges))
    ax.set_ylim(min(fine.y_edges), max(fine.y_edges))
    plt.tight_layout()
    plt.show()


# ============================================================
# 10. Build Pipeline and CLI
# ============================================================


def build_final_global_grid(
    points: List[Point],
    mode: str,
    compression_ratio: float,
    max_grid_cells: Optional[int],
    min_target_cols: int,
    min_target_rows: int,
    pi_avg: float,
    pi_min: float,
    binary_search_max_iter: int,
    origin_left: bool,
    conservative_boundary: bool,
) -> Tuple[FineGrid, CoarseGrid, RecommendedParameters]:
    t0 = time.perf_counter()
    global_fine = build_global_fine_grid(
        points,
        origin_left=origin_left,
        conservative_boundary=conservative_boundary,
    )
    t1 = time.perf_counter()
    print(f"\nGlobal fine-grid construction time: {(t1 - t0) * 1000:.2f} ms")
    print_fine_summary("Global FineGrid after streaming merge NE/NW/SW/SE", global_fine)

    if SHOW_PLOTS:
        plot_grid(global_fine, title="Exact Global Fine Grid")

    w0 = time.perf_counter()
    x_weights = compute_x_line_weights(global_fine)
    y_weights = compute_y_line_weights(global_fine)
    w1 = time.perf_counter()
    print(f"\nBoundary weight computation time: {(w1 - w0) * 1000:.2f} ms")

    rec = recommend_parameters(
        global_fine,
        x_weights,
        y_weights,
        pi_avg=pi_avg,
        pi_min=pi_min,
        compression_ratio=compression_ratio,
        max_grid_cells=max_grid_cells,
        min_target_cols=min_target_cols,
        min_target_rows=min_target_rows,
    )

    print("\nRecommended parameters on GlobalFineGrid:")
    print(f"  target_cols={rec.target_cols}, target_rows={rec.target_rows}")
    print(f"  target_grid_cells={rec.target_coarse_cells}")
    print(f"  achieved_compression={rec.compression_ratio:.2f}")
    print(f"  total_precision={pi_avg:.2f}, cell_precision={pi_min:.2f}")
    print(f"  max_total_error={rec.max_total_error}")
    print(f"  max_cell_error={rec.max_cell_error}")

    if mode == "mode1":
        c0 = time.perf_counter()
        grid = build_grid_by_topweight_target_direct(global_fine, rec.target_cols, rec.target_rows, x_weights, y_weights)
        c1 = time.perf_counter()
        print(f"\nbuild_coarse_grid_by_direct time: {(c1 - c0) * 1000:.2f} ms")
        grid.mode = "mode1"
        if SHOW_PLOTS:
            plot_grid(grid, title="Mode 1: Target Size Direct TopWeight")
    elif mode == "mode2":
        c0 = time.perf_counter()
        grid = build_grid_by_mixed(
            global_fine,
            rec.target_cols,
            rec.target_rows,
            rec.max_total_error,
            rec.max_cell_error,
            x_weights,
            y_weights,
            rec.stats.vx,
            rec.stats.vy,
            max_iter=binary_search_max_iter,
        )
        c1 = time.perf_counter()
        print(f"\nbuild_grid_by_mixed time: {(c1 - c0) * 1000:.2f} ms")
        grid.mode = "mode2"
        if SHOW_PLOTS:
            plot_grid(grid, title="Mode 2: Mixed Budget")
    else:
        raise ValueError("mode must be mode1 or mode2")

    print_grid_summary(f"Final Global GRID ({mode})", grid)
    return global_fine, grid, rec


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a memory-optimized SkyAgg GRID database for Piano-PIR."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_build = sub.add_parser("build", help="Build GRID DB from point file")
    p_build.add_argument("--data", help="input point file", default="points.txt")
    p_build.add_argument("--mode", choices=["mode1", "mode2"], default="mode2")
    p_build.add_argument("--compression", type=float, default=10.0)
    p_build.add_argument("--max-coarse-cells", type=int, default=None)
    p_build.add_argument("--min-cols", type=int, default=10)
    p_build.add_argument("--min-rows", type=int, default=10)
    p_build.add_argument("--pi-avg", type=float, default=0.75)
    p_build.add_argument("--pi-min", type=float, default=0.50)
    p_build.add_argument("--max-iter", type=int, default=25)
    p_build.add_argument("--query", nargs=2, type=float, default=None, metavar=("QX", "QY"))
    p_build.add_argument("--export-prefix", default="./out/skyagg_griddb")
    p_build.add_argument("--origin-left", action="store_true", default=True)
    p_build.add_argument("--no-origin-left", dest="origin_left", action="store_false")
    p_build.add_argument("--no-conservative-boundary", action="store_true")

    p_loc = sub.add_parser("locate", help="Locate PIR index from metadata and query")
    p_loc.add_argument("--meta", required=True)
    p_loc.add_argument("--query", nargs=2, type=float, required=True, metavar=("QX", "QY"))
    p_loc.add_argument("--out", default=None, help="optional query index json output")

    p_decode = sub.add_parser("decode", help="Debug-read and decode one DB record without PIR")
    p_decode.add_argument("--db", required=True)
    p_decode.add_argument("--meta", required=True)
    p_decode.add_argument("--index", type=int, required=True)

    args = parser.parse_args()

    if args.cmd == "build":
        points = read_points_from_txt(args.data)
        print(f"Loaded {len(points)} points from {args.data}")

        global_fine, grid, rec = build_final_global_grid(
            points,
            mode=args.mode,
            compression_ratio=args.compression,
            max_grid_cells=args.max_coarse_cells,
            min_target_cols=args.min_cols,
            min_target_rows=args.min_rows,
            pi_avg=args.pi_avg,
            pi_min=args.pi_min,
            binary_search_max_iter=args.max_iter,
            origin_left=args.origin_left,
            conservative_boundary=not args.no_conservative_boundary,
        )

        db_path, meta_path = export_grid_database_for_pir(grid, args.export_prefix, points)
        print("\nExported GRID database for Piano-PIR:")
        print(f"  DB file      : {db_path}")
        print(f"  metadata     : {meta_path}")
        print(f"  DB records   : {grid.num_cells}")
        print("  one query -> one PIR index -> one full skyline record")

        if args.query is not None:
            qx, qy = args.query
            gc_, gr_, index = grid_cell_for_query(grid, qx, qy)
            candidate = query_grid(grid, qx, qy)
            refined = refine_global_skyline_from_candidates(points, candidate, qx, qy)
            exact = exact_global_skyline(points, qx, qy)
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            debug_record = read_grid_database_record(db_path, meta, index)
            debug_ids = decode_skyline_record(debug_record)
            qfile = args.export_prefix + ".query.json"
            save_query_index_file(meta_path, qx, qy, qfile)

            print("\nQuery local index:")
            print(f"  q=({qx}, {qy})")
            print(f"  grid cell=(col={gc_}, row={gr_})")
            print(f"  PIR index x={index}")
            print(f"  query-index file={qfile}")
            print("\nLocal DB debug decode, not PIR:")
            print(f"  raw record={debug_record}")
            print(f"  decoded candidate ids={sorted(debug_ids)}")
            print("\nLocal correctness check:")
            print(f"  candidate ids={sorted(candidate)}")
            print(f"  refined global skyline ids={sorted(refined)}")
            print(f"  exact global skyline ids={sorted(exact)}")
            print(f"  no false negative after candidate retrieval={exact.issubset(candidate)}")
            print(f"  refined equals exact={refined == exact}")

    elif args.cmd == "locate":
        qx, qy = args.query
        out = args.out or (os.path.splitext(args.meta)[0] + ".query.json")
        save_query_index_file(args.meta, qx, qy, out)
        with open(args.meta, "r", encoding="utf-8") as f:
            meta = json.load(f)
        gc_, gr_, index = locate_pir_index_from_metadata(meta, qx, qy)
        print("Located query PIR index:")
        print(f"  q=({qx}, {qy})")
        print(f"  grid cell=(col={gc_}, row={gr_})")
        print(f"  PIR index x={index}")
        print(f"  saved query-index file={out}")

    elif args.cmd == "decode":
        with open(args.meta, "r", encoding="utf-8") as f:
            meta = json.load(f)
        record = read_grid_database_record(args.db, meta, args.index)
        ids = decode_skyline_record(record)
        print("Decoded DB record without PIR:")
        print(f"  index={args.index}")
        print(f"  raw record={record}")
        print(f"  skyline ids={sorted(ids)}")


if __name__ == "__main__":
    main()
