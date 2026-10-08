"""Standalone batch processing of externally-supplied return records with image URLs.

No database or worker queue involved: this reads two seller-supplied CSV files (a
"before" file per unit and a "returned" file per return event), downloads the referenced
photos, runs the real judgment session and deterministic pipeline per return, and writes
one output CSV in the same shape as `data/returns_sample.csv`.
"""
