"""§5 — images, and the label that must travel with everything they produce.

A vision model reading a screenshot is the only reader in this system whose
output is GENERATED rather than DECODED. Every other format is a function of
the bytes; run it twice and the characters are identical. A model looking at a
blurry figure can produce a plausible number that was never on the page, and
nothing in the resulting string marks which is which.

So most of this file is about the label, not the transcription. The
transcription itself is tested through a mock endpoint that speaks the same
wire format the real one does.
"""

from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from pqv3.agents import documents, vision

# A 1x1 PNG. Enough to exercise the encode/transport path.
PNG = base64.b64decode(
    b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    b"YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


class MockVision:
    """An OpenAI-compatible endpoint that records what it was sent."""

    def __init__(self, reply: str = "") -> None:
        self.reply = reply
        self.requests: list = []

    def __enter__(self):
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):                                # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                outer.requests.append(json.loads(self.rfile.read(n) or b"{}"))
                out = json.dumps({"choices": [{"message": {
                    "role": "assistant", "content": outer.reply}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"


@pytest.fixture
def png(tmp_path):
    p = tmp_path / "shot.png"
    p.write_bytes(PNG)
    return p


def wire(st, mock):
    st.agents.llm_provider = "mock"
    st.agents.llm_endpoint = mock.endpoint
    st.agents.llm_model = "mock-vision"
    return st


# ------------------------------------------------------------- unavailable
def test_without_a_model_an_image_is_refused_with_the_way_forward(st, png):
    st.agents.llm_provider = st.agents.llm_endpoint = st.agents.llm_model = ""
    ok, detail = vision.available(st)
    assert not ok and "vision-capable" in detail
    d = documents.read(str(png), st)
    assert not d.ok and "vision-capable" in d.error


def test_a_text_only_model_failing_says_it_may_not_accept_images(st, png):
    st.agents.llm_provider = "mock"
    st.agents.llm_endpoint = "http://127.0.0.1:1/v1"
    st.agents.llm_model = "text-only"
    st.agents.llm_timeout_secs = 2.0
    t = vision.transcribe(st, str(png))
    assert not t.ok and "could not read the image" in t.reason


# ------------------------------------------------------------- transcribing
def test_the_image_goes_on_the_wire_as_a_data_url(st, png):
    with MockVision("Wallets with a high win rate outperform.") as m:
        wire(st, m)
        t = vision.transcribe(st, str(png))
    assert t.ok and "high win rate" in t.text
    content = m.requests[0]["messages"][0]["content"]
    kinds = {c["type"] for c in content}
    assert kinds == {"text", "image_url"}
    url = [c for c in content if c["type"] == "image_url"][0]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == PNG


def test_the_prompt_forbids_guessing(st, png):
    with MockVision("x") as m:
        wire(st, m)
        vision.transcribe(st, str(png))
    prompt = [c for c in m.requests[0]["messages"][0]["content"]
              if c["type"] == "text"][0]["text"]
    assert "verbatim" in prompt
    assert "[unreadable]" in prompt and "rather than guessing" in prompt


def test_no_text_in_the_image_is_reported_not_invented(st, png):
    with MockVision("NO TEXT") as m:
        wire(st, m)
        t = vision.transcribe(st, str(png))
    assert not t.ok and "no legible text" in t.reason


# ------------------------------------------------------------------ labels
def test_a_transcription_is_labelled_everywhere_it_surfaces(st, png):
    """The point of the whole module. §41."""
    note = ("Wallets with a long track record and a high rolling win rate "
            "tend to outperform the market.")
    with MockVision(note) as m:
        wire(st, m)
        d = documents.ingest(str(png), st)
    assert d.ok
    assert d.transcribed is True
    assert "TRANSCRIBED, NOT DECODED" in d.note
    assert "Check any figure against the image" in d.note
    assert d.claims and all(c.evidence == "TRANSCRIBED" for c in d.claims)


def test_a_decoded_document_is_not_labelled_transcribed(tmp_path, st):
    p = tmp_path / "note.md"
    p.write_text("Wallets with a high rolling win rate tend to outperform.",
                 encoding="utf-8")
    d = documents.ingest(str(p), st)
    assert d.transcribed is False
    assert all(c.evidence == "DECODED" for c in d.claims)


def test_a_screenshot_still_produces_ordinary_candidates(st, png):
    """Transcribed or not, a claim clears the same bar as any other."""
    with MockVision("Wallets with a high rolling win rate outperform.") as m:
        wire(st, m)
        d = documents.ingest(str(png), st)
    assert d.proposals
    for pr in d.proposals:
        assert "<threshold>" in pr["statement"], (
            "no threshold is adopted from a document, least of all one a "
            "model read off an image")


def test_the_console_routes_an_image_to_the_document_pipeline(st, png):
    from pqv3.agents.console import Console
    from pqv3.core.store import Store
    with MockVision("A high win rate tends to outperform the market.") as m:
        wire(st, m)
        r = Console(st, Store(st)).ask(f"read {png}", narrate=False)
    assert r["mode"] == "DOCUMENT"
    assert r["document"]["transcribed"] is True
