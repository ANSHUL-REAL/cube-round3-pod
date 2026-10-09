"""Tools for the live demo: the printable Prep labels and the separate demo data folder.

A printed label is a physical prop that Prep's real model reads; if its barcode does not scan, or its code is not the work
order's FNSKU, the demo would show the wrong thing. The barcode is checked with a real barcode reader (zxing-cpp).
"""
from __future__ import annotations

import csv
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import print_labels  # noqa: E402


@pytest.mark.parametrize("text", ["X00DUMMY014", "X00DUMMY002", "Mixed case 0123 ~!"])
def test_the_printed_barcode_scans_back_to_the_same_code(text):
    zxingcpp = pytest.importorskip("zxingcpp")
    found = zxingcpp.read_barcodes(print_labels.barcode_png(text))
    assert [r.text for r in found] == [text]
    assert "128" in str(found[0].format)


def test_a_wrong_table_entry_would_not_scan():
    """The reader test above is only worth something if a broken barcode fails it."""
    zxingcpp = pytest.importorskip("zxingcpp")
    saved = print_labels.CODE128[56]  # the symbol for "X" in set B
    try:
        print_labels.CODE128[56] = "312311"  # a neighbour's pattern
        assert [r.text for r in zxingcpp.read_barcodes(print_labels.barcode_png("X00DUMMY014"))] != ["X00DUMMY014"]
    finally:
        print_labels.CODE128[56] = saved


def test_non_printable_text_is_refused():
    with pytest.raises(ValueError):
        print_labels.code128_modules("")
    with pytest.raises(ValueError):
        print_labels.code128_modules("é")


def test_the_label_sheet_carries_the_work_orders_fnsku_and_marks(tmp_path):
    out = tmp_path / "labels.html"
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "print_labels.py"), "--out", str(out)],
                       capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stderr
    page = out.read_text(encoding="utf-8")
    with open(ROOT / "data" / "sample" / "prep_sample.csv", newline="", encoding="utf-8") as fh:
        row = next(x for x in csv.DictReader(fh) if x["unit_id"] == "UNIT-0014")
    assert row["fnsku"] in page and "LED Desk Lamp" in page
    for mark in row["wo_handling_marks"].split(";"):
        assert print_labels.MARKS[mark][0] in page
    assert "for the mislabel test" in page and page.count("class=\"label\"") == 2


def test_an_unknown_unit_is_refused(tmp_path):
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "print_labels.py"), "--unit", "UNIT-9999",
                        "--out", str(tmp_path / "x.html")], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 2 and not (tmp_path / "x.html").exists()
