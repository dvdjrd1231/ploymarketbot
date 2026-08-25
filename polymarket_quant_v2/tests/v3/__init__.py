"""Makes this a package so pytest names its conftest `v3.conftest`.

Without it, both `tests/conftest.py` and `tests/v3/conftest.py` are importable
as top-level `conftest`, and V2's `from conftest import SCHEMA` resolves to
whichever pytest imported first. The V3 suite must not be able to break the V2
suite by existing.
"""
