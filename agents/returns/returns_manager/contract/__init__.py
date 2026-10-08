"""Cross-pod evidence contract (P10 / §14).

Evidence records are built by `contract.build` from database state and validated
against the Pydantic models here.  The canonical JSON schema is generated from
those models with `contract.schema.generate()`.
"""
