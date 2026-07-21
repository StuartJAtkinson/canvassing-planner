# Canvassing Route Planner

A tool for planning leafleting/canvassing routes across an electoral ward: pick a
ward, load its addresses, cut out land you don't want to walk, and get back a set
of balanced walking routes — each with a suggested door-to-door order, a vector
map, and a printable PDF pack.

FastAPI backend (`app.py`) + a single-page frontend (`index.html`), no build step.

## Setup

```bash
pip install -r requirements.txt
python app.py
```

Then open **http://localhost:8000**.

## How it works

The sidebar walks through a step-by-step wizard:

1. **Ward** — search for and select an electoral ward.
2. **Addresses** — every address in the ward is loaded and coloured by type
   (commercial/industrial addresses are excluded from route counts automatically).
3. **Area cutout** — cut out land that shouldn't be leafleted (industrial estates,
   open fields, etc.), by hand or via an automatic suggestion.
4. **Snap & tidy** — snaps the shape to the street network and straightens it.
5. **Plan** — set a target addresses-per-route and run the planner. Routes are
   built as Chinese-postman circuits over the street network, so the suggested
   walk order actually covers every street with minimal backtracking.
6. **Routes** — a list of the resulting routes. Each one opens as a document page
   with a vector map, street table, walk order, and editing tools.

### Route editing

From a route's page you can:
- Mark streets **not to visit** or **for another route**, then apply the changes
  in one recalculation.
- **Take** streets from another route into this one.
- **Split** a route into two along a hand-drawn line.
- See streets ranked **sparsest-first** (length per address) to judge what's
  worth cutting.
- **Choose route** — jump between routes by clicking them directly on the map.
- Click-erase individual segments, with a **Reset** to restore the original.

Two whole-plan maintenance tools live on the Routes step:
- **Label routes** — renumbers, recolours, renames (to each route's largest
  street), and recomputes each route's start point from terrain.
- **Fix overlapping roads** — finds any road counted on more than one route and
  keeps it only on the larger of the two.

### Output

- **Print this route** — a single route's document, browser-printed.
- **Routes Pack PDF** — a generated PDF (not browser print, for exact control
  over page breaks) covering every route: a ward-wide index page, then per-route
  pages with a minimap, instructions, the full map, walk order, streets, and
  notes.

## Project files

| File | Purpose |
|---|---|
| `app.py` | FastAPI backend — ward search, address/street-network fetching, route planning, walk-order recalculation |
| `index.html` | Frontend — wizard, Leaflet map, route document pages, PDF generation |
| `test_plan.py` | Backend test suite |
| `get_uprn.py` | Address (UPRN) lookup helper |
| `ISSUES.md` | Running log of known issues and fixes |

## Testing

```bash
python test_plan.py
```
