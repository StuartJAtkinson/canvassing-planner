"""Self-check for the route partitioner — no network needed."""
import networkx as nx

from app import (_rebalance, build_walk, canon, dividers_to_barriers, partition,
                 partition_by_component)


def test_partition():
    # 10-edge path graph: nodes 0..10, 20 addresses per edge, target 60 → ~3-edge routes
    edges = [canon(i, i + 1, 0) for i in range(10)]
    edge_addr = {e: 20 for e in edges}
    node_edges = {}
    for (u, v, k) in edges:
        node_edges.setdefault(u, []).append((u, v, k))
        node_edges.setdefault(v, []).append((u, v, k))
    node_xy = {i: (i, 0) for i in range(11)}  # straight line — nearest-to-seed order == BFS order here

    chunks = partition(edges, edge_addr, node_edges, target=60, node_xy=node_xy)

    covered = [e for c, _ in chunks for e in c]
    assert sorted(covered) == sorted(edges), "every edge assigned exactly once"
    assert all(40 <= t <= 80 for _, t in chunks), f"totals near target: {[t for _, t in chunks]}"
    # contiguity: each chunk's nodes form one connected run on the path
    for chunk, _ in chunks:
        nodes = sorted({n for u, v, _ in chunk for n in (u, v)})
        assert nodes == list(range(nodes[0], nodes[-1] + 1)), f"chunk not contiguous: {nodes}"


def test_dividers_to_barriers():
    # L-shaped path: 0--1 horizontal (y=0), 1--2 vertical (x=1)
    Gu = nx.MultiGraph()
    Gu.add_node(0, x=0.0, y=0.0)
    Gu.add_node(1, x=1.0, y=0.0)
    Gu.add_node(2, x=1.0, y=1.0)
    Gu.add_edge(0, 1, key=0)
    Gu.add_edge(1, 2, key=0)

    # vertical divider at x=0.5 crosses only the 0-1 edge
    divider = {"type": "LineString", "coordinates": [[0.5, -1], [0.5, 1]]}
    assert dividers_to_barriers([divider], Gu) == {canon(0, 1, 0)}


def test_partition_by_component():
    # Component A: 3-edge path, 60 addr/edge = 180 total, target 175 -> should be ONE route
    # (round(180/175) == 1), not folded into a global calc with component B.
    Gu = nx.MultiGraph()
    a_edges = [canon(i, i + 1, 0) for i in range(3)]
    for i in range(4):
        Gu.add_node(i)
    for u, v, k in a_edges:
        Gu.add_edge(u, v, key=k)

    # Component B: 10-edge path (disjoint node ids), 20 addr/edge = 200 total, target 60.
    b_edges = [canon(100 + i, 100 + i + 1, 0) for i in range(10)]
    for i in range(11):
        Gu.add_node(100 + i)
    for u, v, k in b_edges:
        Gu.add_edge(u, v, key=k)

    all_edges = a_edges + b_edges
    edge_addr = {e: 60 for e in a_edges} | {e: 20 for e in b_edges}
    node_edges = {}
    for (u, v, k) in all_edges:
        node_edges.setdefault(u, []).append((u, v, k))
        node_edges.setdefault(v, []).append((u, v, k))
    node_xy = {i: (i, 0) for i in range(4)} | {100 + i: (100 + i, 0) for i in range(11)}

    chunks = partition_by_component(Gu, all_edges, edge_addr, node_edges, target=175, node_xy=node_xy)

    covered = [e for c, _ in chunks for e in c]
    assert sorted(covered) == sorted(all_edges), "every edge assigned exactly once"
    # no chunk mixes edges from both components
    for chunk, _ in chunks:
        nodes = {n for u, v, _ in chunk for n in (u, v)}
        assert nodes <= set(range(4)) or nodes <= set(range(100, 111)), \
            f"chunk crosses components: {nodes}"
    # component A (close to target) comes back as exactly one route
    def nodes_of(chunk):
        return {n for u, v, _ in chunk for n in (u, v)}
    a_chunks = [c for c in chunks if nodes_of(c[0]) <= set(range(4))]
    assert len(a_chunks) == 1 and a_chunks[0][1] == 180, f"component A not a single route: {a_chunks}"


def test_partition_spatial_priority():
    # Seed edge 0-1 at the origin; from node 1, two branches: FAR (node 2, way off at
    # x=100) and NEAR (node 3, right next door at x=1.1). FAR is earlier in node 1's
    # adjacency list, so plain graph-hop BFS would visit it before NEAR — nearest-to-seed
    # growth must visit NEAR first regardless of adjacency-list order.
    node_xy = {0: (0, 0), 1: (1, 0), 2: (100, 0), 3: (1.1, 0)}
    seed, far, near = canon(0, 1, 0), canon(1, 2, 0), canon(1, 3, 0)
    edges = [seed, far, near]
    edge_addr = {seed: 100, far: 10, near: 10}  # seed addr highest -> deterministic seed pick
    node_edges = {0: [seed], 1: [far, seed, near], 2: [far], 3: [near]}

    chunks = partition(edges, edge_addr, node_edges, target=120, node_xy=node_xy)
    chunk = chunks[0][0]
    assert chunk.index(near) < chunk.index(far), f"expected near edge grown before far edge: {chunk}"


def test_partition_no_dropped_streets():
    # One big seed edge alone satisfies the whole quota; the rest is an all-zero-address
    # tail (a through-road with no direct frontage) with no seed of its own and nothing
    # else positioned to reach it. Confirmed real bug: these used to vanish entirely.
    edges = [canon(i, i + 1, 0) for i in range(6)]
    edge_addr = {canon(0, 1, 0): 200}
    node_edges = {}
    for u, v, k in edges:
        node_edges.setdefault(u, []).append((u, v, k))
        node_edges.setdefault(v, []).append((u, v, k))

    chunks = partition(edges, edge_addr, node_edges, target=100)
    covered = [e for c, _ in chunks for e in c]
    assert sorted(covered) == sorted(edges), f"streets silently dropped: {set(edges) - set(covered)}"


def test_rebalance_across_zero_belt():
    # Mirrors the confirmed real-ward failure: a big route and a small route separated by
    # a belt of zero-address connector edges (footpaths). The old rebalancer livelocked
    # (zero-edge shuffles + connectivity rejections) and left the 41-271 spread; the
    # pairwise/bundle/frontier design must feed the small route through the belt.
    edges = [canon(i, i + 1, 0) for i in range(10)]
    edge_addr = {canon(i, i + 1, 0): 40 for i in range(5)}          # e0-e4: 200 addrs
    edge_addr |= {canon(8, 9, 0): 10, canon(9, 10, 0): 10}          # e5-e7 zero belt, tail 20
    node_edges = {}
    for u, v, k in edges:
        node_edges.setdefault(u, []).append((u, v, k))
        node_edges.setdefault(v, []).append((u, v, k))

    big, small = edges[:5], edges[5:]
    chunks = _rebalance([(big, 200), (small, 20)], edge_addr, node_edges, quota=110)

    covered = sorted(e for c, _ in chunks for e in c)
    assert covered == sorted(edges), "rebalance lost or duplicated edges"
    totals = sorted(t for _, t in chunks)
    assert totals[-1] - totals[0] <= 40, f"zero belt still blocks rebalancing: {totals}"
    for chunk, _ in chunks:  # both routes must stay contiguous on the path
        nodes = sorted({n for u, v, _ in chunk for n in (u, v)})
        assert nodes == list(range(nodes[0], nodes[-1] + 1)), f"chunk not contiguous: {nodes}"


def test_build_walk():
    # Square with a spur: 0-1-2-3-0 plus 3-4. Circuit must cover every edge (the spur
    # forces a deadhead repeat, so walk length > sum of edge lengths), and the order list
    # only contains allowed street names.
    Gu = nx.MultiGraph()
    xy = {0: (0, 0), 1: (1, 0), 2: (1, 1), 3: (0, 1), 4: (-1, 1)}
    for n, (x, y) in xy.items():
        Gu.add_node(n, x=float(x), y=float(y))
    edges = [(0, 1), (1, 2), (2, 3), (0, 3), (3, 4)]
    for u, v in edges:
        Gu.add_edge(u, v, key=0, length=100.0, name="Test Street")
    chunk = [canon(u, v, 0) for u, v in edges]

    walk_coords, order, walk_m = build_walk(Gu, chunk, top_node=0,
                                            allowed_names={"Test Street"})

    assert walk_m >= 100.0 * len(edges), f"circuit shorter than the edges it must cover: {walk_m}"
    assert order == ["Test Street"], f"order should collapse to the one allowed name: {order}"
    assert walk_coords and walk_coords[0][0] == (0.0, 0.0), "walk should start at top_node"


if __name__ == "__main__":
    test_partition()
    test_dividers_to_barriers()
    test_partition_by_component()
    test_partition_spatial_priority()
    test_partition_no_dropped_streets()
    test_rebalance_across_zero_belt()
    test_build_walk()
    print("ok")
