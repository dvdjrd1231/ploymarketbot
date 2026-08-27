"""§5 / §29 — reading documents, and refusing to over-read them.

The failure this file guards against is not "the parser crashed". It is the
parser producing a confident extraction of a claim it did not actually
understand — a sentence about order-book imbalance mapped onto price columns
because it contained the word "price", then reported as testable. That is the
document equivalent of fabricating evidence, and §29's rule against copying a
document's conclusions is precisely about it.
"""

from __future__ import annotations

import zipfile

import pytest

from pqv3.agents import documents
from pqv3.agents.console import Console
from pqv3.core.store import Store
from pqv3.server.api import Api

NOTE = """\
Wallets with a long track record and a high rolling win rate tend to
outperform when they enter early.

We assume that larger position sizes indicate higher conviction.

Order-book imbalance above 0.6 predicts short-term price movement, and news
sentiment leads the market by four minutes.

However, this result excludes transaction costs and assumes perfect fills.

Kelly = edge / odds, capped at one quarter.
"""


@pytest.fixture
def note(tmp_path):
    p = tmp_path / "note.md"
    p.write_text(NOTE, encoding="utf-8")
    return p


# ------------------------------------------------------------------ reading
def test_reads_plain_text(note):
    d = documents.read(str(note))
    assert d.ok and d.words > 40 and "Kelly" in d.text


def test_reads_csv_into_rows(tmp_path):
    p = tmp_path / "t.csv"
    p.write_text("wallet,roi\n0xa,0.14\n0xb,-0.02\n", encoding="utf-8")
    d = documents.read(str(p))
    assert d.ok and d.tables[0] == ["wallet", "roi"] and len(d.tables) == 3


def test_reads_docx(tmp_path):
    """DOCX is a zip of XML, so it is readable without a dependency."""
    p = tmp_path / "t.docx"
    xml = ('<?xml version="1.0"?><w:document '
           'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           '<w:body><w:p><w:r><w:t>Wallets with a high win rate </w:t></w:r>'
           '<w:r><w:t>tend to outperform.</w:t></w:r></w:p>'
           '<w:p><w:r><w:t>We assume larger size means conviction.</w:t></w:r>'
           '</w:p></w:body></w:document>')
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("word/document.xml", xml)
    d = documents.read(str(p))
    assert d.ok
    # Runs inside one paragraph are joined, paragraphs are not.
    assert "Wallets with a high win rate tend to outperform." in d.text
    assert d.text.count("\n") == 1


def test_reads_xlsx(tmp_path):
    p = tmp_path / "t.xlsx"
    shared = ('<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
              '<si><t>wallet</t></si><si><t>0xabc</t></si></sst>')
    sheet = ('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
             '<sheetData>'
             '<row><c t="s"><v>0</v></c><c><v>0.14</v></c></row>'
             '<row><c t="s"><v>1</v></c><c><v>0.02</v></c></row>'
             '</sheetData></worksheet>')
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("xl/sharedStrings.xml", shared)
        z.writestr("xl/worksheets/sheet1.xml", sheet)
    d = documents.read(str(p))
    assert d.ok and d.tables == [["wallet", "0.14"], ["0xabc", "0.02"]]


def test_pdf_is_read_now_and_a_broken_one_says_why(tmp_path):
    """PDFs are extracted (see test_v3_pdf.py); malformed ones still refuse.

    This test previously asserted a blanket refusal. That refusal was wrong —
    text extraction needs `zlib`, which is stdlib — and it is now only the
    genuinely unreadable cases that are declined, each by name.
    """
    p = tmp_path / "paper.pdf"
    p.write_bytes(b"%PDF-1.4\n...no streams at all...")
    d = documents.read(str(p))
    assert not d.ok
    assert "no text layer" in d.error and "OCR" in d.error


def test_missing_file_says_so(tmp_path):
    d = documents.read(str(tmp_path / "nope.md"))
    assert not d.ok and "no such file" in d.error


def test_oversized_file_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(documents, "MAX_BYTES", 10)
    p = tmp_path / "big.txt"
    p.write_text("x" * 100, encoding="utf-8")
    d = documents.read(str(p))
    assert not d.ok and "exceeds" in d.error


# --------------------------------------------------------------- extraction
def test_claims_are_classified(note):
    d = documents.ingest(str(note))
    kinds = {c.kind for c in d.claims}
    assert {"CLAIM", "ASSUMPTION", "LIMITATION", "FORMULA"} <= kinds


def test_claims_map_onto_the_engines_real_vocabulary(note):
    from pqv3.research.matrix import FEATURES
    d = documents.ingest(str(note))
    used = {f for c in d.claims for f in c.features}
    assert used, "a document about win rates must reach the win-rate columns"
    assert used <= set(FEATURES), (
        "a proposal naming a column the matrix does not have is untestable "
        "and would fail silently inside discovery")
    assert "w_win_rate" in used


def test_partially_mapped_claims_carry_the_caveat(note):
    """The dangerous case, guarded explicitly.

    "Order-book imbalance predicts price movement" reaches the price columns
    through the word "price" while its actual condition has no column at all.
    Testing the mapped half and calling the claim tested answers a different
    question.
    """
    d = documents.ingest(str(note))
    ob = [c for c in d.claims if "imbalance" in c.text.lower()]
    assert ob, "the order-book sentence should have been classified"
    assert ob[0].caveat and "no observation column" in ob[0].caveat
    assert any(b["concept"] == "order-book depth" for b in ob[0].blocked_by)


def test_unavailable_concepts_are_reported_as_data_requirements(note):
    d = documents.ingest(str(note))
    concepts = {m["concept"] for m in d.missing_data}
    assert "order-book depth" in concepts
    assert "news and sentiment" in concepts
    for m in d.missing_data:
        assert m["why"], "a data requirement must say what would satisfy it"


def test_no_threshold_is_adopted_from_the_document(note):
    """§29 / §18. A number lifted from prose has no denominator behind it."""
    d = documents.ingest(str(note))
    assert d.proposals
    for p in d.proposals:
        assert "<threshold>" in p["statement"]
        assert "0.6" not in p["statement"], (
            "the document's own 0.6 must not become a threshold")
        assert "quantile grid" in p["threshold"]


def test_unstated_direction_costs_both_tests(note):
    d = documents.ingest(str(note))
    unstated = [p for p in d.proposals if "unstated" in p["direction_source"]]
    assert unstated
    assert "denominator" in unstated[0]["direction_source"], (
        "testing both directions doubles the search and must say so")


def test_a_document_about_nothing_we_observe_says_so(tmp_path):
    p = tmp_path / "off.md"
    p.write_text("The weather in Lisbon tends to be pleasant and it works "
                 "well for outdoor dining in the summer months.",
                 encoding="utf-8")
    d = documents.ingest(str(p))
    assert not d.proposals
    assert any("legitimate outcome" in s for s in d.next_steps)


# ----------------------------------------------------------------- console
def test_console_routes_a_path_to_the_document_pipeline(st, note):
    con = Console(st, Store(st))
    r = con.ask(f"read {note} and incorporate the strategy", narrate=False)
    assert r["mode"] == "DOCUMENT"
    assert r["document"]["ok"] is True
    assert r["document"]["proposals"]
    assert any("Nothing here has been tested" in f for f in r["finding"])


def test_quoted_path_with_spaces_is_found(st, tmp_path):
    p = tmp_path / "my research note.md"
    p.write_text(NOTE, encoding="utf-8")
    con = Console(st, Store(st))
    assert con.find_path(f'analyse "{p}"') == str(p)


def test_naming_a_file_to_create_is_not_a_document_to_read(st):
    """"add a module called scanner.py" writes a file; it does not read one."""
    con = Console(st, Store(st))
    assert con.classify("add a module called scanner.py")[0] == "ENGINEERING"
    assert con.classify("create tests/test_new.py")[0] == "ENGINEERING"
    assert con.classify("rewrite pqv3/scanner/signals.py")[0] == "ENGINEERING"
    # A path with no engineering verb is still an ingestion, even when the
    # file is missing, so a typo reports "could not read" instead of vanishing
    # into a research answer.
    assert con.classify("look at notes/strategy.md")[0] == "DOCUMENT"


def test_trailing_punctuation_is_not_part_of_the_path(st):
    con = Console(st, Store(st))
    assert con.find_path("please read notes/alpha.md.") == "notes/alpha.md"
    assert con.find_path("look at data.csv, then stop") == "data.csv"


def test_ingestion_is_remembered(st, note):
    store = Store(st)
    Console(st, store).ask(f"analyse {note}", narrate=False)
    rows = store.query("SELECT * FROM documents ORDER BY id DESC LIMIT 1")
    assert rows and rows[0]["ok"] == 1 and rows[0]["path"].endswith("note.md")


def test_engine_prose_is_not_eaten_by_the_seed_phrase_redactor(st, note):
    """A twelve-word English sentence is not a BIP-39 phrase.

    `secrets.redact` cannot tell the difference, and that trade is correct for
    untrusted text. It is not correct for sentences this codebase wrote, and
    applying it there deleted explanations out of the middle of replies.
    """
    con = Console(st, Store(st))
    r = con.ask(f"analyse {note}", narrate=False)
    caveats = [c["caveat"] for c in r["document"]["claims"] if c["caveat"]]
    assert caveats
    assert all("[REDACTED]" not in c for c in caveats), caveats
    assert all("[REDACTED]" not in f for f in r["finding"])


def test_foreign_text_is_still_scrubbed(st, tmp_path):
    """The other half: a key-shaped string in a document must not survive."""
    key = "0x" + "ab" * 32
    p = tmp_path / "leak.md"
    p.write_text(f"The wallet key is {key} and it predicts profitable trades.",
                 encoding="utf-8")
    con = Console(st, Store(st))
    r = con.ask(f"read {p}", narrate=False)
    blob = str(r["document"])
    assert key not in blob and "[REDACTED]" in blob


def test_document_reply_survives_the_api_boundary(st, note):
    store = Store(st)
    api = Api(st, store)
    r = api.chat(f"analyse {note}", narrate=False)
    import json
    json.dumps(r, default=str)
    assert r["document"]["claims"]
