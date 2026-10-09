"""Print the FBA labels and handling marks for a live Prep demo, so a real item can be prepped on camera.

    python scripts/print_labels.py                       # every FBA demo unit (UNIT-0014) -> out/print-labels.html
    python scripts/print_labels.py --unit UNIT-0014 --unit UNIT-0002 --out labels.html

Open the HTML file in a browser and print it at 100% (no "fit to page"). Each FNSKU label is the size of a standard
2.625 x 1 inch FBA label, with a Code 128 barcode of the FNSKU from the organisers' work order, the code printed under it,
the product title and "New". The handling marks the work order asks for (fragile, this way up) are printed beside it.

A second label for another unit is printed under "for the mislabel test": stick that one on instead and Prep should read
the wrong code and fail `fnsku_text_match`. These are props for a demo of the sample's fictional products: the FNSKUs
are the organisers' dummy values (X00DUMMY...), never real Amazon labels.
"""
from __future__ import annotations

import argparse
import csv
import html
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from shared.utils.console import utf8_console  # noqa: E402

# Code 128: bar/space module widths for values 0..105, then STOP (106). Every symbol is 11 modules wide (STOP 13).
CODE128 = (
    "212222 222122 222221 121223 121322 131222 122213 122312 132212 221213 221312 231212 112232 122132 122231 113222 "
    "123122 123221 223211 221132 221231 213212 223112 312131 311222 321122 321221 312212 322112 322211 212123 212321 "
    "232121 111323 131123 131321 112313 132113 132311 211313 231113 231311 112133 112331 132131 113123 113321 133121 "
    "313121 211331 231131 213113 213311 213131 311123 311321 331121 312113 312311 332111 314111 221411 431111 111224 "
    "111422 121124 121421 141122 141221 112214 112412 122114 122411 142112 142211 241211 221114 413111 241112 134111 "
    "111242 121142 121241 114212 124112 124211 411212 421112 421211 212141 214121 412121 111143 111341 131141 114113 "
    "114311 411113 411311 113141 114131 311141 411131 211412 211214 211232 2331112"
).split()
START_B, STOP = 104, 106
assert len(CODE128) == 107 and len(set(CODE128)) == 107
assert all(sum(map(int, p)) == 11 for p in CODE128[:106]) and sum(map(int, CODE128[STOP])) == 13


def code128_modules(text: str) -> list[int]:
    """Alternating bar/space widths (in modules) for `text` in Code 128 set B, with quiet zones left to the caller."""
    if not text or any(not 32 <= ord(c) <= 126 for c in text):
        raise ValueError("Code 128 B takes printable ASCII only")
    values = [ord(c) - 32 for c in text]
    check = (START_B + sum(i * v for i, v in enumerate(values, 1))) % 103
    return [int(w) for v in (START_B, *values, check, STOP) for w in CODE128[v]]


def barcode_svg(text: str, height_mm: float = 9.0, module_mm: float = 0.25) -> str:
    widths = code128_modules(text)
    x, rects = 10, []  # 10-module quiet zone each side
    for i, w in enumerate(widths):
        if i % 2 == 0:
            rects.append(f'<rect x="{x}" y="0" width="{w}" height="1"/>')
        x += w
    total = x + 10
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {total} 1" preserveAspectRatio="none" '
            f'style="width:{total * module_mm:.2f}mm;height:{height_mm}mm;display:block;margin:0 auto" '
            f'shape-rendering="crispEdges" fill="#000">{"".join(rects)}</svg>')


def barcode_png(text: str, module_px: int = 4, height_px: int = 120):
    """The same barcode as a PIL image (used by the tests to decode it with a real barcode reader)."""
    from PIL import Image, ImageDraw

    widths = code128_modules(text)
    img = Image.new("L", ((sum(widths) + 20) * module_px, height_px + 40), 255)
    draw, x = ImageDraw.Draw(img), 10 * module_px
    for i, w in enumerate(widths):
        if i % 2 == 0:
            draw.rectangle([x, 20, x + w * module_px - 1, 20 + height_px], fill=0)
        x += w * module_px
    return img


def _rows(name: str) -> list[dict]:
    with open(ROOT / "data" / "sample" / name, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _titles() -> dict[str, str]:
    return {r["sku"]: r["product_title"] for r in _rows("receiving_sample.csv") if r.get("sku") and r.get("product_title")}


MARKS = {
    "fragile": ("FRAGILE", "Handle with care",  # the ISO 780 glass, black, drawn: an emoji prints grey and looks unlike a mark
                '<svg viewBox="0 0 40 60" style="height:13mm" fill="none" stroke="#000" stroke-width="4">'
                '<path d="M6 4h28l-2 16a12 12 0 0 1-24 0z" fill="#000"/><path d="M20 34v18M8 56h24"/></svg>'),
    "this_way_up": ("THIS WAY UP", "", "&#x2191;&#x2191;"),
}


def label_html(row: dict, title: str, note: str = "") -> str:
    fnsku = row["fnsku"]
    marks = [m for m in (row.get("wo_handling_marks") or "").split(";") if m]
    mark_html = "".join(
        f'<div class="mark"><div class="sym">{MARKS[m][2]}</div><b>{MARKS[m][0]}</b><small>{MARKS[m][1]}</small></div>'
        for m in marks if m in MARKS)
    return f"""
<section class="unit">
  <h2>{html.escape(row['unit_id'])} · {html.escape(row['work_order_id'])} · {html.escape(row['sku'])}{f' · <em>{html.escape(note)}</em>' if note else ''}</h2>
  <div class="row">
    <div class="label">{barcode_svg(fnsku)}<div class="code">{html.escape(fnsku)}</div>
      <div class="title">{html.escape(title)}</div><div class="cond">New</div></div>
    {mark_html}
  </div>
</section>"""


def main() -> int:
    utf8_console()
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--unit", action="append", help="FBA unit id (repeatable); default: the FBA demo unit UNIT-0014")
    ap.add_argument("--org", default="org_demo_alpha")
    ap.add_argument("--out", default=str(ROOT / "out" / "print-labels.html"))
    args = ap.parse_args()
    prep = {(r["org_id"], r["unit_id"]): r for r in _rows("prep_sample.csv")}
    titles = _titles()
    units = args.unit or ["UNIT-0014"]
    parts = []
    for unit in units:
        row = prep.get((args.org, unit))
        if row is None:
            print(f"{unit}: no FBA work order for {args.org} in prep_sample.csv", file=sys.stderr)
            return 2
        parts.append(label_html(row, titles.get(row["sku"], row["sku"])))
    wrong = next(r for (o, u), r in sorted(prep.items()) if o == args.org and u not in units)
    parts.append(label_html({**wrong, "wo_handling_marks": ""}, titles.get(wrong["sku"], wrong["sku"]),
                            note=f"for the mislabel test: stick this on {units[0]} instead, and Prep should fail fnsku_text_match"))
    page = f"""<!doctype html><html><head><meta charset="utf-8"><title>Pod 12 demo labels</title><style>
@page {{ size: A4; margin: 12mm; }}
body {{ font-family: Arial, Helvetica, sans-serif; color: #000; background: #fff; }}
h1 {{ font-size: 15px; margin: 0 0 4px; }} p.how {{ font-size: 11px; margin: 0 0 14px; color: #333; }}
h2 {{ font-size: 11px; font-weight: normal; margin: 14px 0 6px; color: #444; }}
.row {{ display: flex; gap: 8mm; align-items: center; flex-wrap: wrap; }}
.label {{ width: 66.7mm; height: 25.4mm; border: 0.3mm dashed #999; box-sizing: border-box; padding: 1.5mm 2mm; text-align: center; }}
.code {{ font: bold 3.4mm/1 "Courier New", monospace; margin-top: 0.8mm; letter-spacing: 0.3mm; }}
.title {{ font-size: 2.4mm; margin-top: 0.8mm; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.cond {{ font-size: 2.4mm; }}
.mark {{ width: 38mm; height: 38mm; border: 1.2mm solid #000; box-sizing: border-box; display: flex; flex-direction: column;
        align-items: center; justify-content: center; text-align: center; }}
.mark .sym {{ font-size: 13mm; line-height: 1; }} .mark b {{ font-size: 4.6mm; margin-top: 1mm; }} .mark small {{ font-size: 2.6mm; }}
</style></head><body>
<h1>Pod 12 · labels for the live Prep demo</h1>
<p class="how">Print at 100%. Cut along the dashed line; stick the FNSKU label flat on one face of the unit, not across a seam or
edge, covering the maker's own barcode. Put each handling mark on a side. Dummy codes from the organisers' sample, not real Amazon labels.</p>
{''.join(parts)}
</body></html>"""
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    print(f"wrote {out}  ({', '.join(units)} + one mislabel-test label for {wrong['unit_id']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
