"""Ring topology. Distance is measured in hops; one hop is one tick (spec §3.1)."""
from __future__ import annotations


def ring_index(node: str) -> int:
    return int(node[1:]) - 1


def node_name(i: int) -> str:
    return f"P{i + 1}"


def neighbors(node: str, n: int) -> list[str]:
    i = ring_index(node)
    return sorted({node_name((i - 1) % n), node_name((i + 1) % n)} - {node})


def ring_hops(a: str, b: str, n: int) -> int:
    i, j = ring_index(a), ring_index(b)
    return min((i - j) % n, (j - i) % n)


def shortest_path(src: str, dst: str, n: int, severed: set[tuple[str, str]] = frozenset()) -> list[str] | None:
    """Shorter clear path (list of nodes, inclusive) or None. Clockwise wins ties."""
    si, di = ring_index(src), ring_index(dst)
    cands = []
    for steps, step in (((di - si) % n, 1), ((si - di) % n, -1)):
        if steps == 0:
            continue
        path = [node_name((si + k * step) % n) for k in range(steps + 1)]
        if all(tuple(sorted((path[k], path[k + 1]))) not in severed for k in range(len(path) - 1)):
            cands.append((steps, 0 if step == 1 else 1, path))
    if not cands:
        return None
    cands.sort()
    return cands[0][2]


def path_ticks(path: list[str]) -> int:
    return len(path) - 1
