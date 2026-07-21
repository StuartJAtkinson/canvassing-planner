"""Canvassing route planner — draw an area, get leafleting routes of ~150-200 addresses.

Run:  uvicorn app:app --port 8000   →  http://localhost:8000
"""
import hashlib
import heapq
import math
import json
import os
import sqlite3
import time
from collections import Counter
from pathlib import Path

import geopandas as gpd
import networkx as nx
import osmnx as ox
import pandas as pd  # already a geopandas dependency — used here for boolean masking
import requests
from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from shapely.geometry import LineString, MultiPoint, mapping, shape
from shapely.ops import unary_union

# overpass-api.de silently blocks outbound connections from Cloud Run's shared IP
# ranges at the network level (confirmed: ConnectTimeout, never even reaches the
# server — same query works instantly from a home connection). kumi.systems does
# accept the connection from Cloud Run, it's just slower to answer under load
# (confirmed: ReadTimeout at 60s, so raise the ceiling rather than switch again).
# osmnx's default "politely wait for a slot" behaviour is disabled too — a slow/
# blocked request should fail into the existing try/except fallback, not hang
# quietly past Cloud Run's own request timeout looking stuck to the user.
ox.settings.overpass_url = os.environ.get("OVERPASS_ENDPOINT", "https://overpass.kumi.systems/api")
ox.settings.requests_timeout = 120
ox.settings.overpass_rate_limit = False

app = FastAPI()

UPRN_DB = Path(__file__).parent / "data" / "uprn.sqlite"

# ponytail: unbounded, in-process, no TTL — this is a local single-user tool, so a
# dict that lives for the life of the server is simpler than any real cache library.
_graph_cache: dict[str, tuple] = {}
_elev_cache: dict[str, dict] = {}


def get_graph(poly):
    """Cached (undirected, projected) walk network for poly. Ward-scale OSMnx fetches
    take 10-30s and the wizard flow now hits the same ward from several endpoints.

    ox.graph_from_polygon() silently drops all but one piece of a MultiPolygon (confirmed:
    fetching a 2-piece MultiPolygon directly returned nodes only from the first piece, zero
    from the second, even though the second piece has real streets on its own) — a cutout
    that splits the ward into separate shapes must fetch each piece individually and compose.
    """
    # Ward boundaries often run down the middle of a road/footpath; a 6 m fringe pulls
    # those edge-of-ward ways into the walk network so routes can travel along them.
    # Addresses are fetched with the unbuffered polygon, so no out-of-ward houses join.
    m3857 = 6 / math.cos(math.radians(poly.centroid.y))  # 3857 metres shrink by cos(lat)
    poly = gpd.GeoSeries([poly], crs=4326).to_crs(3857).buffer(m3857).to_crs(4326).iloc[0]
    key = hashlib.md5(poly.wkb).hexdigest()
    if key not in _graph_cache:
        polys = list(poly.geoms) if poly.geom_type == "MultiPolygon" else [poly]
        graphs = []
        for p in polys:
            try:
                graphs.append(ox.graph_from_polygon(p, network_type="walk", simplify=True))
            except Exception:
                continue  # this piece has no walkable streets of its own — not fatal
        if not graphs:
            raise ValueError("No walkable streets found in this shape.")
        G = graphs[0] if len(graphs) == 1 else nx.compose_all(graphs)
        Gu = ox.convert.to_undirected(G)
        Gp = ox.project_graph(Gu)
        _graph_cache[key] = (Gu, Gp)
    return _graph_cache[key]


def fetch_elevation(key, points):
    """points: {id: (lat, lon)} → {id: elevation}, cached per (key, id) so repeat calls
    for the same ward (grid overlay, then routing) don't re-hit open-elevation.com.
    Best-effort: raises on a hard failure, caller decides how to degrade."""
    cache = _elev_cache.setdefault(key, {})
    todo = {i: p for i, p in points.items() if i not in cache}
    ids = list(todo)
    for i in range(0, len(ids), 400):
        batch = ids[i:i + 400]
        locs = [{"latitude": todo[j][0], "longitude": todo[j][1]} for j in batch]
        r = requests.post("https://api.open-elevation.com/api/v1/lookup",
                          json={"locations": locs}, timeout=30)
        r.raise_for_status()
        for j, res in zip(batch, r.json()["results"]):
            cache[j] = res["elevation"]
    return {i: cache[i] for i in points if i in cache}


def clean_poly(geom):
    """GeoJSON dict -> shapely geometry, repaired if invalid. Client-side polygon math
    (turf.difference, our own /snap) can produce technically self-intersecting rings;
    buffer(0) is the standard shapely fix-up, applied once here rather than at every
    call site that might receive a client-drawn/erased/snapped shape."""
    poly = shape(geom)
    return poly if poly.is_valid else poly.buffer(0)


def to_wgs84(geom_3857):
    """EPSG:3857 shapely geometry -> EPSG:4326, for geometry returned to the browser
    (fetch_landuse_blobs works in 3857 for accurate metre-based .within() checks)."""
    return gpd.GeoSeries([geom_3857], crs=3857).to_crs(4326).iloc[0]


def fetch_landuse_blobs(poly):
    """(commercial_blob, industrial_blob, field_blob) in EPSG:3857, each None if nothing
    mapped. field_blob is any open land nobody lives in — fields, meadows, woods, parks,
    recreation grounds, car parks, cemeteries — which still wastes route-planning area
    when it sits inside a ward boundary."""
    def blob(tags):
        try:
            lu = ox.features_from_polygon(poly, tags=tags)
            lu = lu[lu.geometry.geom_type.isin(["Polygon", "MultiPolygon"])]  # amenity=parking etc. can be nodes — points break turf.union client-side
            if not len(lu):
                return None
            return lu.to_crs(3857).geometry.union_all()
        except Exception:
            return None
    field_tags = {"landuse": ["farmland", "farmyard", "meadow", "orchard", "forest", "grass",
                              "cemetery", "recreation_ground", "village_green"],
                  "natural": ["wood", "grassland", "scrub"],
                  "leisure": ["park", "recreation_ground", "garden", "pitch", "golf_course",
                              "playground", "sports_centre"],
                  "amenity": ["parking", "grave_yard"]}
    return (blob({"landuse": ["commercial", "retail"]}), blob({"landuse": ["industrial"]}),
            blob(field_tags))


def dividers_to_barriers(dividers, Gu):
    """Street edges crossed by a manually-drawn divider line — same barrier mechanism
    the hill-splitting feature uses, just a different source of barrier edges."""
    barriers = set()
    for div in dividers:
        dline = shape(div)
        for u, v, k, d in Gu.edges(keys=True, data=True):
            eline = d.get("geometry") or LineString(
                [(Gu.nodes[u]["x"], Gu.nodes[u]["y"]), (Gu.nodes[v]["x"], Gu.nodes[v]["y"])])
            if dline.intersects(eline):
                barriers.add(canon(u, v, k))
    return barriers


def load_uprn(poly):
    """OS Open UPRN points (every GB address, official/free) clipped to poly.
    Returns None if the local db hasn't been built (see get_uprn.py)."""
    if not UPRN_DB.exists():
        return None
    minx, miny, maxx, maxy = poly.bounds
    con = sqlite3.connect(UPRN_DB)
    rows = con.execute(
        "SELECT lon, lat FROM uprn WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?",
        (miny, maxy, minx, maxx)).fetchall()
    con.close()
    if not rows:
        return None
    pts = gpd.points_from_xy([r[0] for r in rows], [r[1] for r in rows])
    gdf = gpd.GeoDataFrame(geometry=pts, crs=4326)
    gdf = gdf[gdf.within(poly)]
    return gdf if len(gdf) else None

# Tableau-10 minus brown/grey — those vanish against an OSM basemap
COLORS = ["#e15759", "#4e79a7", "#59a14f", "#f28e2b", "#b07aa1",
          "#76b7b2", "#edc948", "#ff9da7"]

MAX_AREA_KM2 = 100  # whole urban wards fit easily; big rural wards just fetch slowly

# roads that can carry letterboxes — addresses only ever snap to these
ADDRESSABLE = {"residential", "living_street", "unclassified", "service", "road",
               "tertiary", "secondary", "primary", "trunk",
               "tertiary_link", "secondary_link", "primary_link", "trunk_link"}

STEEP_GRADE = 0.08  # >8% average slope on a 30 m+ segment = route boundary


class PlanReq(BaseModel):
    polygon: dict              # GeoJSON geometry drawn on the map / ward boundary
    target: int = 175          # addresses per route (150-200 band midpoint)
    exclude_industrial: bool = True
    hills: bool = False        # split routes at steep hills (extra API calls, slower)
    dividers: list = []        # GeoJSON LineStrings — manual route-splitting lines


class PolyReq(BaseModel):
    polygon: dict


class SnapReq(BaseModel):
    polygon: dict
    geometries: list           # GeoJSON LineString/Polygon geometries to snap


class RerouteReq(BaseModel):
    polygon: dict              # the planning shape — resolves the cached street graph
    edges: list                # [[u, v, k], ...] the route's remaining edges
    allowed: list = []         # street names eligible for the walk-order list
    top: dict | None = None    # {"lat","lon"} of the route's start marker, if any
    use_elevation: bool = False   # recompute the start point from terrain (Label Routes) instead of keeping top


@app.get("/")
def index():
    # no-store: this is actively being edited — without it, browsers can serve a stale
    # cached copy of index.html indefinitely on repeat visits to the same tab (no network
    # request at all), which is exactly what happened here.
    return FileResponse("index.html", headers={"Cache-Control": "no-store"})


def canon(u, v, k):
    """Undirected multigraph edge id, orientation-independent."""
    return (u, v, k) if u <= v else (v, u, k)


def first(x):
    return x[0] if isinstance(x, list) else x


def deg2_to_km2(poly):
    return poly.area * 111.32 * 111.32 * 0.59  # crude deg→km², fine for UK-latitude estimates


def partition(all_edges, edge_addr, node_edges, target, node_xy=None, barriers=frozenset()):
    """Balanced contiguous routes: n = round(total/target), every route aims at the
    same quota total/n. Greedy nearest-to-seed growth (flood-fill by geographic distance,
    not graph-hop order — keeps a route filling one compact area instead of snaking through
    several neighbourhoods), then forced merging down to exactly n chunks, then a rebalance
    pass that shifts boundary streets from oversized routes to adjacent undersized ones.
    Routes never GROW through a barrier (steep) edge, though one can still seed a route if
    it has addresses of its own. node_xy=None falls back to plain BFS (graph-hop) order.
    """
    grand_total = sum(edge_addr.get(e, 0) for e in all_edges)
    n_routes = max(1, round(grand_total / target))
    quota = grand_total / n_routes

    def mid(e):
        u, v, _ = e
        ux, uy = node_xy[u]
        vx, vy = node_xy[v]
        return (ux + vx) / 2, (uy + vy) / 2

    unassigned = set(all_edges)
    chunks = []
    while True:
        seeds = [e for e in unassigned if edge_addr.get(e, 0) > 0]
        if not seeds:
            break
        seed = max(seeds, key=lambda e: edge_addr.get(e, 0))
        chunk, total = [], 0
        seen = {seed}
        if node_xy:
            sx, sy = mid(seed)
            frontier = [(0.0, 0, seed)]  # (dist², tiebreak, edge) — heapq needs orderable items
            counter = 1
        else:
            frontier = [seed]
        while frontier and total < quota:
            e = heapq.heappop(frontier)[-1] if node_xy else frontier.pop(0)
            if e not in unassigned:
                continue
            unassigned.discard(e)
            chunk.append(e)
            total += edge_addr.get(e, 0)
            if e in barriers:
                continue  # claimed, but don't spread beyond it
            u, v, _ = e
            for n in (u, v):
                for ne in node_edges.get(n, ()):
                    if ne in unassigned and ne not in seen:
                        seen.add(ne)
                        if node_xy:
                            nx_, ny_ = mid(ne)
                            d2 = (nx_ - sx) ** 2 + (ny_ - sy) ** 2
                            heapq.heappush(frontier, (d2, counter, ne))
                            counter += 1
                        else:
                            frontier.append(ne)
        chunks.append((chunk, total))

    def nodes_of(chunk):
        return {n for u, v, _ in chunk for n in (u, v)}

    # Streets with no addresses of their own (a through-road with no direct frontage) never
    # seed a chunk, and if growth elsewhere stops at quota before reaching them they're left
    # in `unassigned` forever — confirmed via a direct repro that this silently drops real,
    # walkable streets from every route. Sweep them into whichever existing chunk touches
    # them (multi-pass: grabbing edges can expose further leftovers next pass); if a whole
    # component seeded no chunk at all, its leftovers become one chunk of their own so the
    # streets still show up somewhere instead of vanishing.
    if unassigned:
        changed = True
        while unassigned and changed:
            changed = False
            for i, (chunk, total) in enumerate(chunks):
                cn = nodes_of(chunk)
                grabbed = [e for e in unassigned if e[0] in cn or e[1] in cn]
                if grabbed:
                    chunks[i] = (chunk + grabbed, total + sum(edge_addr.get(e, 0) for e in grabbed))
                    unassigned -= set(grabbed)
                    changed = True
        if unassigned:
            chunks.append((list(unassigned), sum(edge_addr.get(e, 0) for e in unassigned)))
            unassigned = set()

    # Force down to exactly n_routes: repeatedly fold the smallest chunk into
    # whichever adjacent (shared-node) chunk lands closest to quota afterwards.
    # BFS growth alone leaves far more chunks than n_routes (small cul-de-sacs
    # seed their own chunk, mid-size chunks land just under quota and never
    # trigger a "runt" merge) — this loop is what actually delivers n_routes.

    while len(chunks) > n_routes:
        chunks.sort(key=lambda c: c[1])
        chunk, total = chunks[0]
        cn = nodes_of(chunk)
        best = None
        for j in range(1, len(chunks)):
            other, ot = chunks[j]
            if cn & nodes_of(other):
                score = abs(ot + total - quota)
                if best is None or score < best[0]:
                    best = (score, j)
        if best is None:
            break  # geographically isolated from every other chunk — leave it
        _, j = best
        other, ot = chunks[j]
        chunks[j] = (other + chunk, ot + total)
        chunks.pop(0)

    return _rebalance(chunks, edge_addr, node_edges, quota)


def _rebalance(chunks, edge_addr, node_edges, quota):
    """Diffuse addresses across route boundaries, pairwise: every ADJACENT PAIR of routes
    whose totals differ by more than the tolerance moves one boundary "bundle" from bigger
    to smaller, in passes, until no pair can improve. Confirmed on a real whole-ward run
    that the previous largest-route-first design livelocked (thousands of futile
    zero-address shuffles, iteration cap, 41-271 spread); this lands ~±15% of target.

    The three ideas that make it converge on real street networks:
    - pairwise, not largest-first: an undersized route gets fed by its own neighbours even
      while a bigger imbalance elsewhere is stuck;
    - bundles, not single edges: street areas are tree-like (cul-de-sacs), so a boundary
      edge usually carries a dangling branch — the move takes the branch along, since
      moving the edge alone would disconnect the donor and always be rejected;
    - zero-belt frontier advance: when a boundary is all zero-address connector edges
      (footpaths/lanes), the small route absorbs one zero bundle so the donor's addressed
      streets become the boundary next pass, instead of being blocked forever."""
    edges_of = [set(c) for c, _ in chunks]
    totals = [t for _, t in chunks]
    owner = {e: i for i, es in enumerate(edges_of) for e in es}
    tol = max(quota * 0.12, 15)

    def transfer_bundle(ci, e):
        """Edges ci hands over if it gives up e: just e when ci stays connected without
        it; otherwise e plus every split-off piece except the largest (most addresses,
        then most edges), so the donor keeps its main body."""
        rest = edges_of[ci] - {e}
        if not rest:
            return None
        adj = {}
        for (u, v, k) in rest:
            adj.setdefault(u, []).append((v, (u, v, k)))
            adj.setdefault(v, []).append((u, (u, v, k)))
        pieces = []
        unseen = set(rest)
        while unseen:
            start = next(iter(unseen))
            piece, stack = {start}, [start[0], start[1]]
            seen_n = set(stack)
            while stack:
                n = stack.pop()
                for m, ee in adj.get(n, ()):
                    piece.add(ee)
                    if m not in seen_n:
                        seen_n.add(m)
                        stack.append(m)
            pieces.append(piece)
            unseen -= piece
        if len(pieces) == 1:
            return [e]
        pieces.sort(key=lambda p: (sum(edge_addr.get(x, 0) for x in p), len(p)))
        return [e] + [x for p in pieces[:-1] for x in p]

    def neighbour_pairs():
        pairs = set()
        for e, ci in owner.items():
            u, v, _k = e
            for n in (u, v):
                for ne in node_edges.get(n, ()):
                    cj = owner.get(ne)
                    if cj is not None and cj != ci:
                        pairs.add((min(ci, cj), max(ci, cj)))
        return pairs

    for _ in range(300):  # passes; the real whole-ward test converges well within this
        pairs = sorted(neighbour_pairs(), key=lambda p: -abs(totals[p[0]] - totals[p[1]]))
        moved_any = False
        for a_, b_ in pairs:
            hi, lo = (a_, b_) if totals[a_] >= totals[b_] else (b_, a_)
            diff = totals[hi] - totals[lo]
            if diff <= tol:
                continue
            lo_nodes = {n for u, v, _ in edges_of[lo] for n in (u, v)}
            boundary = [e for e in edges_of[hi] if e[0] in lo_nodes or e[1] in lo_nodes]
            best = None       # (post-move gap, bundle, addresses moved)
            best_zero = None  # smallest all-zero bundle, for the frontier advance
            for e in boundary:
                bundle = transfer_bundle(hi, e)
                if bundle is None:
                    continue
                A = sum(edge_addr.get(x, 0) for x in bundle)
                if A == 0:
                    if best_zero is None or len(bundle) < len(best_zero):
                        best_zero = bundle
                else:
                    # any bundle that strictly shrinks this pair's gap counts, even if
                    # donor and receiver swap roles — lumpy cul-de-sac branches rarely
                    # split the gap exactly
                    score = abs((totals[hi] - A) - (totals[lo] + A))
                    if score < diff and (best is None or score < best[0]):
                        best = (score, bundle, A)
            bundle, A = (best[1], best[2]) if best else (best_zero, 0)
            if bundle:
                for x in bundle:
                    edges_of[hi].discard(x)
                    edges_of[lo].add(x)
                    owner[x] = lo
                totals[hi] -= A
                totals[lo] += A
                moved_any = True
        if not moved_any:
            break
    return [(list(es), t) for es, t in zip(edges_of, totals) if es]


def edge_label(Gu, u, v, k):
    """Street name for an edge, or its highway type in brackets when unnamed."""
    d = Gu.get_edge_data(u, v, k) or {}
    name = first(d.get("name"))
    if not name:
        name = f"({first(d.get('highway', 'path'))})"
    return name


def build_walk(Gu, chunk, top_node, allowed_names):
    """Chinese-postman walk for one route's edge set → (walk_coords, order, walk_m).
    order lists street names in walking sequence (consecutive dupes collapsed, only names
    in allowed_names); walk_m is the full circuit length incl. deadhead repeats.

    MultiGraph, not Graph: a plain Graph collapses parallel edges between the same two
    junctions (two different streets sharing both endpoints) into one, so reconstructing
    geometry later via an arbitrary Gu.get_edge_data(u,v)[min(d)] could draw a totally
    different real street than the one actually in this route's chunk. Keeping the real
    key lets every circuit step resolve back to its own true edge."""
    H = nx.MultiGraph()
    edge_key_of = {}
    for u, v, k in chunk:
        H.add_edge(u, v, key=k)
        edge_key_of[(min(u, v), max(u, v))] = k
    walk_coords, order = [], []
    walk_m = 0.0
    for comp in nx.connected_components(H):
        sub = H.subgraph(comp).copy()
        if sub.number_of_edges() == 0:
            continue
        try:
            eulerized = nx.eulerize(sub)
            source = top_node if top_node in eulerized else None
            circuit = list(nx.eulerian_circuit(eulerized, source=source, keys=True))
        except Exception:
            circuit = list(sub.edges(keys=True))
        coords = []
        for u, v, k in circuit:
            # eulerize() duplicates edges with a synthetic key not in the real graph
            # (confirmed: duplicate keys never match Gu) — fall back to the edge this
            # route actually owns for that node pair rather than an arbitrary parallel one.
            real_k = k if Gu.has_edge(u, v, k) else edge_key_of.get((min(u, v), max(u, v)), k)
            d = Gu.get_edge_data(u, v)
            seg = None
            if d:
                d0 = d.get(real_k, d[min(d)])
                walk_m += d0.get("length", 0)
                if "geometry" in d0:
                    seg = list(d0["geometry"].coords)
            if seg is None:
                seg = [(Gu.nodes[u]["x"], Gu.nodes[u]["y"]),
                       (Gu.nodes[v]["x"], Gu.nodes[v]["y"])]
            ux, uy = Gu.nodes[u]["x"], Gu.nodes[u]["y"]
            if abs(seg[0][0] - ux) + abs(seg[0][1] - uy) > \
               abs(seg[-1][0] - ux) + abs(seg[-1][1] - uy):
                seg = seg[::-1]
            coords.extend(seg)
            nm = edge_label(Gu, u, v, real_k)
            if nm in allowed_names and (not order or order[-1] != nm):
                order.append(nm)
        walk_coords.append(coords)
    return walk_coords, order, walk_m


def partition_by_component(Gu, all_edges, edge_addr, node_edges, target, node_xy=None, barriers=frozenset()):
    """Run partition() independently per connected component of Gu, so a cutout that splits
    the street network into physically disconnected pieces gives each piece its own
    target/quota — a component already close to target lands on n_routes=1 instead of being
    folded into one global total/target across areas that were never routable together."""
    chunks = []
    for comp_nodes in nx.connected_components(Gu):
        comp_edges = [e for e in all_edges if e[0] in comp_nodes]
        if not comp_edges:
            continue
        comp_node_edges = {n: node_edges[n] for n in comp_nodes if n in node_edges}
        chunks.extend(partition(comp_edges, edge_addr, comp_node_edges, target,
                                 node_xy=node_xy, barriers=barriers))
    return chunks


def _steps(req: PlanReq):
    """The whole pipeline as a generator of NDJSON progress lines; last line is the result."""
    t0 = time.time()

    def msg(m):
        return json.dumps({"msg": m, "t": round(time.time() - t0, 1)}) + "\n"

    poly = clean_poly(req.polygon)
    area_km2 = deg2_to_km2(poly)
    if area_km2 > MAX_AREA_KM2:
        yield json.dumps({"error": f"Area is {area_km2:.0f} km² — max {MAX_AREA_KM2}."}) + "\n"
        return

    # --- street network -------------------------------------------------------
    yield msg(f"Area {area_km2:.1f} km² — fetching walkable street network from OpenStreetMap…")
    if poly.geom_type == "MultiPolygon":
        yield msg(f"Shape has {len(poly.geoms)} separate pieces — fetching a street "
                  "network for each (slow the first time)…")
    try:
        Gu, Gp = get_graph(poly)
    except Exception as e:
        yield json.dumps({"error": f"No walkable streets found ({e})."}) + "\n"
        return
    yield msg(f"Street network: {Gu.number_of_nodes()} junctions, {Gu.number_of_edges()} segments")

    # --- addresses -------------------------------------------------------------
    # OS Open UPRN (official, every GB address point) is far more complete than
    # OSM's addr:housenumber tagging, which is patchy — that's what undercounted
    # areas like Pontefract North. Use it whenever get_uprn.py has been run.
    addrs = None
    addr_source = None
    if UPRN_DB.exists():
        yield msg("Fetching address points (OS Open UPRN — official GB address list)…")
        addrs = load_uprn(poly)
        yield msg(f"Found {len(addrs) if addrs is not None else 0} address points")
        if addrs is not None:
            addr_source = "OS Open UPRN"
    if addrs is None:
        yield msg("Fetching address points (OpenStreetMap addr:housenumber — may undercount)…")
        try:
            addrs = ox.features_from_polygon(poly, tags={"addr:housenumber": True})
            yield msg(f"Found {len(addrs)} address points")
            addr_source = "OpenStreetMap"
        except Exception:
            yield msg("No OSM address points here.")

    if addrs is not None and req.exclude_industrial:
        yield msg("Fetching industrial/commercial/retail land use to exclude…")
        commercial_blob, industrial_blob, _ = fetch_landuse_blobs(poly)
        n0 = len(addrs)
        cen = addrs.to_crs(3857).geometry.centroid
        exclude = pd.Series(False, index=addrs.index)
        if commercial_blob is not None:
            exclude |= cen.within(commercial_blob)
        if industrial_blob is not None:
            exclude |= cen.within(industrial_blob)
        addrs = addrs[~exclude]
        yield msg(f"Excluded {n0 - len(addrs)} addresses in industrial/commercial/retail areas")

    # --- snap addresses to streets, by NAME first ------------------------------
    # An address tagged addr:street="Foo Road" counts against Foo Road even when a
    # rear footpath is physically closer. Only ADDRESSABLE road types can receive
    # addresses — footways/paths never can.
    edge_addr: dict = {}
    estimated = False
    if addrs is not None and len(addrs):
        yield msg("Snapping addresses to their nearest addressable street…")
        edges_gdf = ox.convert.graph_to_gdfs(Gp, nodes=False)
        elig = edges_gdf[edges_gdf["highway"].apply(first).isin(ADDRESSABLE)]
        if len(elig):
            def normname(s):
                return str(s).strip().lower() if s is not None else ""
            elig_names = elig["name"].apply(first).map(normname) if "name" in elig else None
            pts = addrs.to_crs(Gp.graph["crs"]).geometry.centroid
            streets_col = (addrs["addr:street"].map(normname)
                           if "addr:street" in addrs else None)

            def snap(sub_gdf, sub_pts):
                _, pos = sub_gdf.sindex.nearest(sub_pts.values, return_all=False)
                for uvk in sub_gdf.index[pos]:
                    e = canon(*uvk)
                    edge_addr[e] = edge_addr.get(e, 0) + 1

            matched = 0
            if streets_col is not None and elig_names is not None:
                by_name = {n: g for n, g in elig.groupby(elig_names) if n}
                for name, grp_pts in pts.groupby(streets_col):
                    if name and name in by_name:
                        snap(by_name[name], grp_pts)
                        matched += len(grp_pts)
                unmatched_mask = ~streets_col.isin(by_name.keys()) | (streets_col == "")
                rest = pts[unmatched_mask]
            else:
                rest = pts
            if len(rest):
                snap(elig, rest)
            yield msg(f"Snapped {matched} by street name, {len(pts) - matched} by proximity")

    if not edge_addr:
        # ponytail: last-resort only — reached when UPRN isn't installed (see
        # get_uprn.py) and OSM has no address tags either.
        estimated = True
        addr_source = "estimated"
        yield msg("⚠ No usable address data — estimating ~1 door per 15 m of residential street")
        for u, v, k, d in Gu.edges(keys=True, data=True):
            if first(d.get("highway")) in ("residential", "living_street", "unclassified"):
                edge_addr[canon(u, v, k)] = max(1, int(d.get("length", 0) / 15))
    if not edge_addr:
        yield json.dumps({"error": "No addresses or residential streets found in that area."}) + "\n"
        return

    # --- elevation: always, for the downhill "top" marker; barriers only if asked ---
    elev_key = hashlib.md5(poly.wkb).hexdigest()
    yield msg(f"Fetching elevation for {Gu.number_of_nodes()} junctions (open-elevation.com)…")
    try:
        elev = fetch_elevation(elev_key, {n: (Gu.nodes[n]["y"], Gu.nodes[n]["x"]) for n in Gu.nodes})
        yield msg(f"Elevation: {len(elev)}/{Gu.number_of_nodes()} junctions")
    except Exception as e:
        elev = {}
        yield msg(f"⚠ Elevation service failed ({e}) — no downhill marker or hill splits")

    barriers = set()
    if req.hills and elev:
        for u, v, k, d in Gu.edges(keys=True, data=True):
            length = d.get("length") or 1
            if length > 30 and abs(elev.get(u, 0) - elev.get(v, 0)) / length > STEEP_GRADE:
                barriers.add(canon(u, v, k))
        yield msg(f"{len(barriers)} steep segments (>{STEEP_GRADE:.0%} grade) become route boundaries")

    if req.dividers:
        yield msg(f"Intersecting {len(req.dividers)} manual divider line(s) with the street network…")
        div_barriers = dividers_to_barriers(req.dividers, Gu)
        barriers |= div_barriers
        yield msg(f"{len(div_barriers)} street segments split by dividers")

    # --- centrality: spine streets ----------------------------------------------
    yield msg("Computing street centrality (betweenness)…")
    n = Gu.number_of_nodes()
    bc = nx.betweenness_centrality(Gu, k=min(300, n), weight="length", seed=0)
    street_bc = Counter()
    for u, v, k, d in Gu.edges(keys=True, data=True):
        name = first(d.get("name"))
        if name:
            street_bc[name] += bc[u] + bc[v]
    spine = [s for s, _ in street_bc.most_common(5)]
    yield msg(f"Spine streets: {', '.join(spine) or '—'}")

    # --- partition into routes ----------------------------------------------------
    yield msg(f"Partitioning into routes of ~{req.target} addresses…")
    node_edges: dict = {}
    all_edges = []
    node_xy = {n: (d["x"], d["y"]) for n, d in Gu.nodes(data=True)}
    for u, v, k in Gu.edges(keys=True):
        e = canon(u, v, k)
        all_edges.append(e)
        node_edges.setdefault(u, []).append(e)
        node_edges.setdefault(v, []).append(e)
    n_components = nx.number_connected_components(Gu)
    if n_components > 1:
        yield msg(f"Street network has {n_components} disconnected areas — planning each separately")
    chunks = partition_by_component(Gu, all_edges, edge_addr, node_edges, req.target,
                                     node_xy=node_xy, barriers=barriers)
    yield msg(f"{len(chunks)} routes (target ~{area_km2 / len(chunks):.2f} km² each): "
              + ", ".join(str(t) for _, t in chunks) + " addresses")

    # --- build route output --------------------------------------------------------
    yield msg("Computing walk order for each route (Chinese-postman circuits)…")
    name_routes = Counter()

    def edge_name(u, v, k):
        return edge_label(Gu, u, v, k)

    routes = []
    n_chunks = len(chunks)
    for i, (chunk, total) in enumerate(chunks):
        # enumerable stage — the client renders this as a real progress bar
        yield json.dumps({"progress": {"done": i, "total": n_chunks,
                                       "label": "Computing walk orders"}}) + "\n"
        streets: dict = {}
        for u, v, k in chunk:
            name = edge_name(u, v, k)
            s = streets.setdefault(name, {"name": name, "addresses": 0})
            s["addresses"] += edge_addr.get((u, v, k), 0)
        # unnamed paths with no addresses are connectivity, not deliverable streets
        streets = {n: s for n, s in streets.items()
                   if s["addresses"] > 0 or not n.startswith("(")}
        for name in streets:
            name_routes[name] += 1

        nodes_in_chunk = {n for u, v, _ in chunk for n in (u, v)}
        top_node = max((n for n in nodes_in_chunk if n in elev), key=elev.get, default=None)
        top = ({"lat": Gu.nodes[top_node]["y"], "lon": Gu.nodes[top_node]["x"]}
               if top_node is not None else None)

        walk_coords, order, walk_m = build_walk(Gu, chunk, top_node, set(streets))

        feats = []
        for u, v, k in chunk:
            d = Gu.get_edge_data(u, v, k) or {}
            if "geometry" in d:
                cs = list(d["geometry"].coords)
            else:
                cs = [(Gu.nodes[u]["x"], Gu.nodes[u]["y"]),
                      (Gu.nodes[v]["x"], Gu.nodes[v]["y"])]
            # u/v/k + per-edge addresses let the route document's click-to-erase tool
            # identify each edge and rebalance its own street table client-side
            feats.append({"type": "Feature",
                          "properties": {"name": edge_name(u, v, k), "u": u, "v": v, "k": k,
                                         "addresses": edge_addr.get((u, v, k), 0)},
                          "geometry": {"type": "LineString", "coordinates": cs}})

        named = [n for n in streets if not n.startswith("(")]
        main = max(named, key=lambda n: street_bc.get(n, 0)) if named else None

        # convex-hull footprint, same crude deg->km2 conversion used for the area cap
        all_coords = [c for f in feats for c in f["geometry"]["coordinates"]]
        route_area_km2 = (deg2_to_km2(MultiPoint(all_coords).convex_hull)
                          if len(all_coords) >= 3 else 0.0)

        routes.append({
            "id": i + 1,
            "color": COLORS[i % len(COLORS)],
            "addresses": total,
            "area_km2": round(route_area_km2, 2),
            "walk_km": round(walk_m / 1000, 1),
            "main": main,
            "top": top,
            "streets": sorted(streets.values(), key=lambda s: -s["addresses"]),
            "order": order,
            "walk": {"type": "MultiLineString", "coordinates": walk_coords},
            "edges": {"type": "FeatureCollection", "features": feats},
        })

    for r in routes:
        for s in r["streets"]:
            s["partial"] = name_routes[s["name"]] > 1

    yield msg(f"Done in {time.time() - t0:.1f} s")
    yield json.dumps({"result": {
        "estimated": estimated,
        "address_source": addr_source,
        "spine_streets": spine,
        "total_addresses": sum(r["addresses"] for r in routes),
        "routes": routes,
    }}) + "\n"


@app.post("/addresses")
def addresses(req: PolyReq):
    """Every address point in poly, classified red/gold/grey (residential/commercial/
    industrial) for the ward-wizard's first step. Not streamed — a single JSON reply,
    since this alone is fast next to the full /plan pipeline."""
    poly = clean_poly(req.polygon)
    addrs = load_uprn(poly)
    if addrs is None:
        try:
            addrs = ox.features_from_polygon(poly, tags={"addr:housenumber": True})
        except Exception as e:
            print(f"addresses: Overpass fetch failed: {type(e).__name__}: {e}")
            addrs = None
    empty = {"points": {"type": "FeatureCollection", "features": []},
             "commercial_geom": None, "industrial_geom": None, "field_geom": None,
             "counts": {"residential": 0, "commercial": 0, "industrial": 0}}
    if addrs is None or not len(addrs):
        return empty

    commercial_blob, industrial_blob, field_blob = fetch_landuse_blobs(poly)
    cen3857 = addrs.to_crs(3857).geometry.centroid
    cen4326 = addrs.geometry.centroid
    commercial_mask = (cen3857.within(commercial_blob) if commercial_blob is not None
                       else pd.Series(False, index=addrs.index))
    industrial_mask = (cen3857.within(industrial_blob) if industrial_blob is not None
                       else pd.Series(False, index=addrs.index))
    category = pd.Series("residential", index=addrs.index)
    category[commercial_mask] = "commercial"
    category[industrial_mask] = "industrial"  # industrial wins if a point is in both

    features = [{"type": "Feature", "properties": {"category": cat},
                "geometry": {"type": "Point", "coordinates": [pt.x, pt.y]}}
               for cat, pt in zip(category, cen4326)]
    counts = category.value_counts().to_dict()
    return {
        "points": {"type": "FeatureCollection", "features": features},
        "commercial_geom": mapping(to_wgs84(commercial_blob)) if commercial_blob is not None else None,
        "industrial_geom": mapping(to_wgs84(industrial_blob)) if industrial_blob is not None else None,
        "field_geom": mapping(to_wgs84(field_blob)) if field_blob is not None else None,
        "counts": {"residential": counts.get("residential", 0),
                  "commercial": counts.get("commercial", 0),
                  "industrial": counts.get("industrial", 0)},
    }


@app.post("/snap")
def snap(req: SnapReq):
    """Snap each vertex of the given geometries to its nearest street-graph node —
    cleans up hand-drawn cutout/divider lines so they follow real roads."""
    poly = clean_poly(req.polygon)
    try:
        Gu, _ = get_graph(poly)
    except Exception as e:
        return {"error": f"No street network available ({e})."}

    def snap_ring(ring):
        xs, ys = [c[0] for c in ring], [c[1] for c in ring]
        nn = ox.distance.nearest_nodes(Gu, xs, ys)
        return [[Gu.nodes[n]["x"], Gu.nodes[n]["y"]] for n in nn]

    def snap_poly(coords):
        # snapping each vertex independently can cross rings into a self-intersecting
        # shape; buffer(0) is the standard shapely fix-up for that.
        fixed = shape({"type": "Polygon", "coordinates": [snap_ring(r) for r in coords]})
        return fixed if fixed.is_valid else fixed.buffer(0)

    snapped = []
    for geom in req.geometries:
        if geom["type"] == "Polygon":
            snapped.append(mapping(snap_poly(geom["coordinates"])))
        elif geom["type"] == "MultiPolygon":
            # a cutout that splits the ward produces a MultiPolygon — snap each piece
            pieces = [snap_poly(c) for c in geom["coordinates"]]
            snapped.append(mapping(unary_union(pieces)))
        else:  # LineString
            coords = snap_ring(geom["coordinates"])
            snapped.append({"type": "LineString", "coordinates": coords})
    return {"geometries": snapped}


@app.post("/reroute")
def reroute(req: RerouteReq):
    """Recompute one route's walk order after the user erases edges from it on the route
    document. Stateless: the client holds names/addresses per edge and sends back the
    surviving [u,v,k] list; only the circuit needs the graph."""
    poly = clean_poly(req.polygon)
    try:
        Gu, _ = get_graph(poly)
    except Exception as e:
        return {"error": f"No street network available ({e})."}
    chunk = [canon(*e) for e in req.edges]
    chunk = [e for e in chunk if Gu.has_edge(*e)]
    if not chunk:
        return {"error": "No edges left in this route."}
    top_node = None
    if req.use_elevation:
        nodes_in_chunk = {n for u, v, _ in chunk for n in (u, v)}
        try:
            elev_key = hashlib.md5(poly.wkb).hexdigest()
            elev = fetch_elevation(elev_key, {n: (Gu.nodes[n]["y"], Gu.nodes[n]["x"]) for n in nodes_in_chunk})
            top_node = max((n for n in nodes_in_chunk if n in elev), key=elev.get, default=None)
        except Exception:
            top_node = None
    elif req.top:
        nodes = {n for u, v, _ in chunk for n in (u, v)}
        top_node = min(nodes, key=lambda n: (Gu.nodes[n]["x"] - req.top["lon"]) ** 2
                                             + (Gu.nodes[n]["y"] - req.top["lat"]) ** 2)
    walk_coords, order, walk_m = build_walk(Gu, chunk, top_node, set(req.allowed))
    # the walk's actual start point may have moved (edges added/removed) even when a
    # top_node was requested — eulerize()/circuit-building can pick a different source
    # node than requested, so report back where it really starts, not where it was asked to
    new_top = ({"lon": walk_coords[0][0][0], "lat": walk_coords[0][0][1]}
               if walk_coords and walk_coords[0] else None)
    return {"order": order,
            "walk": {"type": "MultiLineString", "coordinates": walk_coords},
            "walk_km": round(walk_m / 1000, 1),
            "top": new_top}


_wardmap_cache: dict[str, dict] = {}


@app.post("/wardmap")
def wardmap(req: PolyReq):
    """Vector-map source data for the whole planning shape, fetched ONCE per ward:
    every OSM building + every road from the (already cached) ward street graph. The
    client slices this per route-footprint bbox when rendering each document's SVG —
    one Overpass call instead of one per route."""
    poly = clean_poly(req.polygon)
    key = hashlib.md5(poly.wkb).hexdigest()
    if key in _wardmap_cache:
        return _wardmap_cache[key]

    buildings = []
    try:
        gdf = ox.features_from_polygon(poly, tags={"building": True})
        for geom in gdf.geometry:
            polys = geom.geoms if geom.geom_type == "MultiPolygon" else (
                [geom] if geom.geom_type == "Polygon" else [])
            for p in polys:
                buildings.append([[round(x, 6), round(y, 6)] for x, y in p.exterior.coords])
    except Exception:
        pass  # no buildings mapped here (or Overpass hiccup) — map still renders

    roads = []
    try:
        Gu, _ = get_graph(poly)
        for u, v, k, d in Gu.edges(keys=True, data=True):
            if "geometry" in d:
                cs = [[round(x, 6), round(y, 6)] for x, y in d["geometry"].coords]
            else:
                cs = [[Gu.nodes[u]["x"], Gu.nodes[u]["y"]], [Gu.nodes[v]["x"], Gu.nodes[v]["y"]]]
            roads.append({"coords": cs, "name": first(d.get("name")) or "",
                          "highway": first(d.get("highway")) or ""})
    except Exception:
        pass

    # Orientation POIs: stations, bus stations and place centres in a ~3 km fringe
    # around the ward — the nearest ones get direction markers at each route map's edge.
    pois = []
    try:
        area = poly.convex_hull.buffer(0.03)  # ~3 km in degrees at UK latitudes
        gdf = ox.features_from_polygon(area, tags={
            "railway": ["station", "halt"], "amenity": ["bus_station"],
            "place": ["city", "town", "village", "suburb", "neighbourhood"]})
        for _, row in gdf.iterrows():
            name = row.get("name")
            if not isinstance(name, str) or not name:
                continue
            c = row.geometry.centroid
            kind = ("station" if isinstance(row.get("railway"), str) else
                    "bus station" if isinstance(row.get("amenity"), str) else row.get("place"))
            pois.append({"lon": round(c.x, 6), "lat": round(c.y, 6),
                         "name": name, "kind": kind})
    except Exception:
        pass

    result = {"buildings": buildings, "roads": roads, "pois": pois}
    _wardmap_cache[key] = result
    return result


@app.post("/plan")
def plan(req: PlanReq):
    def gen():
        try:
            yield from _steps(req)
        except Exception as e:
            yield json.dumps({"error": f"Planning failed: {e}"}) + "\n"
    return StreamingResponse(gen(), media_type="application/x-ndjson")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, port=8000)
