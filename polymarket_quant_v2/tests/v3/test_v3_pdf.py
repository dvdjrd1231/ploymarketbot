"""§5 — PDF extraction, and the three cases it must refuse.

The tests build real PDFs byte by byte rather than shipping fixtures, so the
input is known exactly and the assertions are about characters that were
definitely in the file. Both the uncompressed and the FlateDecode paths are
exercised, because a real document is always the compressed one.

The refusals matter more than the successes. A PDF that yields mojibake is
worse than one that yields nothing: the garbage flows into the claim extractor
and becomes candidate hypotheses built on characters that were never written.
"""

from __future__ import annotations

import zlib

from pqv3.agents import documents
from pqv3.agents.pdf import LEGIBILITY_FLOOR, extract


def build_pdf(content: bytes, *, compress: bool = False,
              encrypt: bool = False) -> bytes:
    """A minimal but structurally valid single-page PDF."""
    stream = zlib.compress(content) if compress else content
    filt = b"/Filter/FlateDecode" if compress else b""
    objs = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/Contents 4 0 R"
        b"/Resources<</Font<</F1 5 0 R>>>>>>",
        b"<</Length " + str(len(stream)).encode() + filt + b">>\nstream\n"
        + stream + b"\nendstream",
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    for i, body in enumerate(objs, 1):
        out += str(i).encode() + b" 0 obj " + body + b" endobj\n"
    trailer = b"<</Size 6/Root 1 0 R"
    if encrypt:
        trailer += b"/Encrypt 6 0 R"
    out += b"trailer " + trailer + b">>\n%%EOF\n"
    return bytes(out)


TEXT_OPS = (b"BT /F1 12 Tf 72 720 Td "
            b"(Wallets with a high rolling win rate tend to outperform.) Tj "
            b"0 -14 Td "
            b"(We assume larger position sizes indicate higher conviction.) Tj "
            b"ET")


# ------------------------------------------------------------------ reading
def test_reads_an_uncompressed_pdf():
    r = extract(build_pdf(TEXT_OPS))
    assert r.ok, r.reason
    assert "high rolling win rate" in r.text
    assert "higher conviction" in r.text
    assert r.legibility > 0.9


def test_reads_a_flate_compressed_pdf():
    """The path every real document takes."""
    r = extract(build_pdf(TEXT_OPS, compress=True))
    assert r.ok, r.reason
    assert "outperform" in r.text
    assert r.streams_decoded == 1


def test_kerning_gaps_become_spaces():
    """A TJ array with large negative kerns is how words are separated."""
    ops = (b"BT /F1 12 Tf [(Order) -300 (book) -300 (imbalance)] TJ ET")
    r = extract(build_pdf(ops))
    assert r.ok
    assert "Order book imbalance" in r.text, r.text


def test_hex_and_utf16_strings_decode():
    ops = (b"BT /F1 12 Tf <48656C6C6F20776F726C64> Tj ET")   # "Hello world"
    r = extract(build_pdf(ops))
    assert r.ok and "Hello world" in r.text


def test_escapes_in_literal_strings():
    ops = rb"BT /F1 12 Tf (a\(b\)c \\ d\101e) Tj ET"          # \101 = 'A'
    r = extract(build_pdf(ops))
    assert r.ok
    assert "a(b)c" in r.text and "dAe" in r.text


# ---------------------------------------------------------------- refusals
def test_an_encrypted_pdf_is_refused_by_name():
    r = extract(build_pdf(TEXT_OPS, encrypt=True))
    assert not r.ok
    assert "encrypted" in r.reason and "password" in r.reason


def test_a_scan_with_no_text_layer_is_refused():
    """An image-only page. OCR is the only route and OCR is a dependency."""
    r = extract(build_pdf(b"q 612 0 0 792 0 0 cm /Im1 Do Q"))
    assert not r.ok
    assert "no text layer" in r.reason and "OCR" in r.reason


def test_unmapped_subset_font_bytes_are_refused_not_returned():
    """The dangerous case: output that looks like text and is not.

    Identity-H glyph indices read as Latin-1 give accented and control
    characters. Returning them would feed invented words to the claim
    extractor, so the extractor measures legibility and refuses below a floor.
    """
    garbage = bytes(range(0xC0, 0xFF)) * 4
    ops = b"BT /F1 12 Tf (" + garbage.replace(b"\\", b"").replace(
        b"(", b"").replace(b")", b"") + b") Tj ET"
    r = extract(build_pdf(ops))
    assert not r.ok
    assert r.legibility < LEGIBILITY_FLOOR
    assert "Identity-H" in r.reason
    assert "refused" in r.reason


def test_a_non_pdf_is_rejected():
    assert not extract(b"just some text").ok


# ------------------------------------------------- through the document path
def test_a_pdf_reaches_the_claim_extractor(tmp_path):
    p = tmp_path / "note.pdf"
    p.write_bytes(build_pdf(TEXT_OPS, compress=True))
    d = documents.ingest(str(p))
    assert d.ok, d.error
    kinds = {c.kind for c in d.claims}
    assert "CLAIM" in kinds and "ASSUMPTION" in kinds
    assert any("w_win_rate" in c.features for c in d.claims)
    assert d.proposals, "a readable PDF must produce candidates like any other"


def test_a_scanned_pdf_reports_why_through_the_document_path(tmp_path):
    p = tmp_path / "scan.pdf"
    p.write_bytes(build_pdf(b"q /Im1 Do Q"))
    d = documents.ingest(str(p))
    assert not d.ok
    assert "OCR" in d.error


def test_pdf_is_no_longer_a_blanket_refusal():
    assert ".pdf" not in documents.REFUSED
    assert ".doc" in documents.REFUSED, "legacy binary formats still are"
