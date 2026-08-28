"""Import chat sites from a spreadsheet into the UC site registry, and
optionally probe them all immediately.

Expects an .xlsx whose first sheet has header columns Domain | Brand |
Notes (extra columns ignored). Parsed with the stdlib (zip + XML) so no
extra dependencies are needed. Rows become kind="widget" registry entries
(brand-site chatbots are widget-class: launcher + iframe composer).

Usage:
  pixi run python scripts/import_sites_xlsx.py chatbot_domains.xlsx
  pixi run python scripts/import_sites_xlsx.py chatbot_domains.xlsx --probe
  pixi run python scripts/import_sites_xlsx.py chatbot_domains.xlsx \
      --probe --only klarna,lego
"""

from __future__ import annotations

import argparse
import re
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from uc_browser.health import HealthMonitor  # noqa: E402
from uc_browser.registry import get_registry  # noqa: E402

_M = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def read_rows(path: Path) -> list[dict]:
    z = zipfile.ZipFile(path)
    shared: list[str] = []
    if "xl/sharedStrings.xml" in z.namelist():
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
        for si in root.findall(f"{_M}si"):
            shared.append("".join(t.text or "" for t in si.iter(f"{_M}t")))

    def cell_text(c) -> str:
        if c.get("t") == "s":
            v = c.find(f"{_M}v")
            return shared[int(v.text)] if v is not None else ""
        return "".join(t.text or "" for t in c.iter(f"{_M}t")) or \
               "".join(v.text or "" for v in c.findall(f"{_M}v"))

    sheet = next(n for n in z.namelist()
                 if re.match(r"xl/worksheets/sheet1\.xml$", n))
    root = ET.fromstring(z.read(sheet))
    rows = [[cell_text(c) for c in row] for row in root.iter(f"{_M}row")]
    if not rows:
        return []
    header = [h.strip().lower() for h in rows[0]]
    out = []
    for raw in rows[1:]:
        rec = dict(zip(header, raw + [""] * (len(header) - len(raw))))
        if rec.get("domain", "").strip():
            out.append(rec)
    return out


def slugify(brand: str, domain: str) -> str:
    base = brand or domain.split(".")[0]
    slug = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")
    return slug[:40] or re.sub(r"[^a-z0-9]+", "-", domain.lower()).strip("-")[:40]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("xlsx", type=Path)
    ap.add_argument("--probe", action="store_true",
                    help="probe every imported site now (one browser sweep)")
    ap.add_argument("--only", help="comma-separated slugs to import/probe")
    ap.add_argument("--interval-minutes", type=int, default=30,
                    help="probe interval for imported sites (default 30)")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    rows = read_rows(args.xlsx)
    if not rows:
        print("No data rows found.")
        return 2

    registry = get_registry()
    wanted = {s.strip() for s in args.only.split(",")} if args.only else None
    entries = []
    for rec in rows:
        domain = rec["domain"].strip().lower()
        slug = slugify(rec.get("brand", ""), domain)
        if wanted and slug not in wanted:
            continue
        notes = " — ".join(x for x in (rec.get("brand", "").strip(),
                                       rec.get("notes", "").strip()) if x)
        existing = registry.get(slug)
        if existing and existing.source == "user":
            print(f"  = {slug:18s} already registered")
            entries.append(existing)
            continue
        entry = registry.add(
            slug, f"https://{domain}", kind="widget", notes=notes,
            probe_interval_s=args.interval_minutes * 60,
        )
        print(f"  + {slug:18s} https://{domain}")
        entries.append(entry)

    print(f"\n{len(entries)} site(s) in scope "
          f"(registry: {registry._path})")

    if args.probe and entries:
        print("\nProbing (detect level, no messages sent)...\n")
        monitor = HealthMonitor()
        results = monitor.probe_sites(entries)
        print(f"\n{'site':18s} {'status':10s} {'level':7s} {'ms':>6s}  detail")
        print("-" * 78)
        ok = 0
        for r in sorted(results, key=lambda r: (r.status, r.site)):
            ok += r.status == "ok"
            print(f"{r.site:18s} {r.status:10s} {r.level_reached:7s} "
                  f"{r.latency_ms:6d}  {r.detail[:44]}")
        print("-" * 78)
        print(f"{ok}/{len(results)} detect-level ok. Uptime page: "
              "pixi run status-server → http://127.0.0.1:4010/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
