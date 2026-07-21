"""One-time setup: download OS Open UPRN (every GB address point, free OGL licence)
and build data/uprn.sqlite for fast bbox lookups.  Run:  python get_uprn.py
Needs ~4 GB disk during the build, ~1.5 GB after.
"""
import csv
import io
import sqlite3
import sys
import urllib.request
import zipfile
from pathlib import Path

URL = "https://api.os.uk/downloads/v1/products/OpenUPRN/downloads?area=GB&format=CSV&redirect"
DATA = Path(__file__).parent / "data"
DATA.mkdir(exist_ok=True)
ZIP = DATA / "uprn.zip"
DB = DATA / "uprn.sqlite"


def progress(blocks, bs, total):
    done = blocks * bs
    sys.stdout.write(f"\r  downloading... {done / 1e6:.0f} / {total / 1e6:.0f} MB")
    sys.stdout.flush()


if DB.exists():
    print(f"{DB} already exists - delete it to rebuild.")
    sys.exit(0)

if not ZIP.exists():
    print("Downloading OS Open UPRN (~600 MB)...")
    urllib.request.urlretrieve(URL, ZIP, reporthook=progress)
    print()

print("Building uprn.sqlite (a few minutes)...")
con = sqlite3.connect(DB)
con.execute("PRAGMA journal_mode=OFF")
con.execute("PRAGMA synchronous=OFF")
con.execute("CREATE TABLE uprn (lat REAL, lon REAL)")
with zipfile.ZipFile(ZIP) as z:
    name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
    with z.open(name) as f:
        rdr = csv.DictReader(io.TextIOWrapper(f, "utf-8"))
        batch, n = [], 0
        for row in rdr:
            try:
                batch.append((float(row["LATITUDE"]), float(row["LONGITUDE"])))
            except (KeyError, ValueError):
                continue
            if len(batch) >= 200_000:
                con.executemany("INSERT INTO uprn VALUES (?,?)", batch)
                n += len(batch)
                batch.clear()
                sys.stdout.write(f"\r  {n / 1e6:.1f} M rows")
                sys.stdout.flush()
        con.executemany("INSERT INTO uprn VALUES (?,?)", batch)
        n += len(batch)
print(f"\n  {n / 1e6:.1f} M rows total - indexing...")
con.execute("CREATE INDEX idx_latlon ON uprn(lat, lon)")
con.commit()
con.close()
ZIP.unlink()  # reclaim the 600 MB
print(f"Done -> {DB}")
