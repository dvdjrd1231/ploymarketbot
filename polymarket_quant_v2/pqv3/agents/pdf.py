"""§5 — PDF text extraction, standard library only.

This was previously refused on the grounds that every viable parser is a
third-party dependency. That was half right. Extracting text from a PDF needs
`zlib`, which is stdlib, plus a content-stream tokeniser, which is a few
hundred lines — the dependency is real for LAYOUT-faithful extraction and for
the long tail of malformed files, not for pulling the words out of an ordinary
research note or paper.

So this reads what it can read and REFUSES WHAT IT CANNOT, by name. The
refusals are the important part, because a PDF that yields plausible-looking
mojibake is worse than one that yields nothing: mojibake flows into the claim
extractor and becomes candidate hypotheses built on characters that were never
in the document.

Three cases are detected and refused rather than guessed at:

    ENCRYPTED       the file carries /Encrypt. Even empty-password encryption
                    needs RC4 or AES key derivation to undo.
    NO TEXT LAYER   a scan. The page is an image and there are no text
                    operators at all; OCR is the only route and OCR is a
                    dependency.
    UNMAPPED GLYPHS the file uses subset fonts with Identity-H encoding and no
                    usable /ToUnicode map, so the bytes are glyph indices in a
                    private font rather than characters. Detected by measuring
                    how much of the output is ordinary language; below a floor
                    it is refused.

What comes out is words in reading order per content stream. Column layout,
tables and headers are not reconstructed — `documents.py` classifies sentences,
which survives losing the layout.
"""

from __future__ import annotations

import re
import zlib
from dataclasses import dataclass, field

# Below this share of "ordinary text" characters the extraction is treated as
# failed. Measured on correct extractions of ordinary prose, which run 0.93+.
LEGIBILITY_FLOOR = 0.72
MIN_CHARS_TO_JUDGE = 60

_STREAM = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.S)
_ENCRYPT = re.compile(rb"/Encrypt\b")

# Text-showing operators. `Tj` and `'` take one string, `TJ` an array of
# strings and kerning numbers, `"` two numbers then a string.
_TJ = re.compile(rb"\[(.*?)\]\s*TJ", re.S)
_TJ_ONE = re.compile(rb"(\((?:[^()\\]|\\.)*\)|<[0-9A-Fa-f\s]*>)\s*(Tj|')", re.S)
_STR = re.compile(rb"\((?:[^()\\]|\\.)*\)|<[0-9A-Fa-f\s]*>", re.S)
_NEWLINE_OP = re.compile(rb"\b(Td|TD|T\*|TL)\b")
_BT = re.compile(rb"\bBT\b")

_ESCAPES = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b",
            b"f": b"\f", b"(": b"(", b")": b")", b"\\": b"\\"}


@dataclass
class PdfText:
    ok: bool = False
    text: str = ""
    pages_seen: int = 0
    streams_decoded: int = 0
    legibility: float = 0.0
    reason: str = ""
    warnings: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def _decode_literal(raw: bytes) -> bytes:
    """PDF literal string: backslash escapes plus \\ddd octal."""
    out = bytearray()
    i, n = 0, len(raw)
    while i < n:
        c = raw[i:i + 1]
        if c != b"\\":
            out += c
            i += 1
            continue
        i += 1
        if i >= n:
            break
        e = raw[i:i + 1]
        if e in _ESCAPES:
            out += _ESCAPES[e]
            i += 1
        elif e.isdigit():
            oct_digits = b""
            while i < n and len(oct_digits) < 3 and raw[i:i + 1].isdigit():
                oct_digits += raw[i:i + 1]
                i += 1
            try:
                out.append(int(oct_digits, 8) & 0xFF)
            except ValueError:
                pass
        elif e in (b"\n", b"\r"):
            i += 1                      # line continuation
        else:
            out += e
            i += 1
    return bytes(out)


def _decode_string(tok: bytes) -> str:
    """One PDF string token -> text.

    Hex strings that are UTF-16BE (the usual shape when a /ToUnicode map is
    present) are decoded as such. Otherwise bytes are read as Latin-1, which
    is exactly WinAnsiEncoding for the printable range and is what ordinary
    documents use.
    """
    tok = tok.strip()
    if tok.startswith(b"<"):
        hexs = re.sub(rb"[^0-9A-Fa-f]", b"", tok[1:-1])
        if len(hexs) % 2:
            hexs += b"0"
        try:
            data = bytes.fromhex(hexs.decode("ascii"))
        except ValueError:
            return ""
        if data.startswith(b"\xfe\xff"):
            return data[2:].decode("utf-16-be", "replace")
        # A run of 2-byte codes with zero high bytes is UTF-16BE without the
        # byte-order mark; anything else is read a byte at a time.
        if len(data) >= 4 and len(data) % 2 == 0 and \
                all(data[i] == 0 for i in range(0, len(data), 2)):
            return data.decode("utf-16-be", "replace")
        return data.decode("latin-1", "replace")
    return _decode_literal(tok[1:-1]).decode("latin-1", "replace")


def _content_text(content: bytes) -> str:
    """Pull the shown strings out of one decoded content stream, in order."""
    out: list[str] = []
    pos = 0
    for m in re.finditer(rb"(\[(?:[^\]\\]|\\.)*\]\s*TJ)|"
                         rb"((?:\((?:[^()\\]|\\.)*\)|<[0-9A-Fa-f\s]*>)\s*(?:Tj|'))|"
                         rb"(\b(?:Td|TD|T\*)\b)", content, re.S):
        if _NEWLINE_OP.fullmatch(m.group(0).strip()):
            out.append("\n")
            continue
        chunk = m.group(0)
        parts = [_decode_string(s) for s in _STR.findall(chunk)]
        if not parts:
            continue
        # Inside a TJ array a large negative kern is a word gap. Without this
        # every line comes back as onelongrunofwords.
        if chunk.rstrip().endswith(b"TJ"):
            joined = ""
            tokens = re.findall(
                rb"(\((?:[^()\\]|\\.)*\)|<[0-9A-Fa-f\s]*>)|(-?\d+(?:\.\d+)?)",
                chunk, re.S)
            for s_tok, num in tokens:
                if s_tok:
                    joined += _decode_string(s_tok)
                elif num:
                    try:
                        if float(num) <= -120:
                            joined += " "
                    except ValueError:
                        pass
            out.append(joined)
        else:
            out.append("".join(parts))
        pos = m.end()
    text = "".join(out)
    return re.sub(r"[ \t]{2,}", " ", text)


def _legibility(text: str) -> float:
    """Share of characters that belong to ordinary written language.

    The mojibake detector. A correct extraction of prose scores above 0.9; a
    subset font read as Latin-1 produces accented and control characters and
    scores far below.
    """
    if len(text) < MIN_CHARS_TO_JUDGE:
        return 1.0
    good = sum(1 for c in text
               if c.isalnum() and ord(c) < 128
               or c in " \n\r\t.,;:!?()[]{}-–—'\"/%$&*+=<>@#_|\\~^`")
    return good / len(text)


def extract(data: bytes) -> PdfText:
    """Text out of a PDF, or a named reason why not."""
    r = PdfText()
    if not data.startswith(b"%PDF"):
        r.reason = "not a PDF: the file does not begin with %PDF"
        return r
    if _ENCRYPT.search(data):
        r.reason = ("the PDF is encrypted (/Encrypt). Undoing even an "
                    "empty-password encryption needs RC4 or AES key "
                    "derivation, which is a dependency this project does not "
                    "take. Re-save it without a password")
        return r

    chunks: list[str] = []
    streams = _STREAM.findall(data)
    r.pages_seen = data.count(b"/Type/Page") + data.count(b"/Type /Page")
    for raw in streams:
        content = raw
        for attempt in (lambda b: zlib.decompress(b),
                        lambda b: zlib.decompressobj().decompress(b),
                        lambda b: zlib.decompress(b, -15)):
            try:
                content = attempt(raw)
                break
            except zlib.error:
                continue
        if not _BT.search(content):
            continue                    # not a text stream
        piece = _content_text(content)
        if piece.strip():
            chunks.append(piece)
            r.streams_decoded += 1

    if not chunks:
        r.reason = ("the PDF has no text layer — no content stream contains "
                    "text operators. This is a scan or an export of images, "
                    "and reading it needs OCR, which is a dependency this "
                    "project does not take")
        return r

    text = "\n".join(chunks)
    text = re.sub(r"\n{3,}", "\n\n", text)
    r.legibility = round(_legibility(text), 4)
    if r.legibility < LEGIBILITY_FLOOR:
        r.reason = (
            f"text was extracted but only {r.legibility:.0%} of it is ordinary "
            f"language, below the {LEGIBILITY_FLOOR:.0%} floor. The file "
            f"almost certainly uses subset fonts with Identity-H encoding and "
            f"no /ToUnicode map, so its bytes are glyph indices in a private "
            f"font rather than characters. Returning this would feed invented "
            f"words into the claim extractor, so it is refused instead. Export "
            f"the document to .docx, .txt or .md")
        r.text = text[:400]
        return r

    r.ok = True
    r.text = text
    if r.legibility < 0.9:
        r.warnings.append(
            f"legibility {r.legibility:.0%} — some characters may not have "
            f"survived the font encoding. Check the extraction before relying "
            f"on a figure taken from it")
    return r
