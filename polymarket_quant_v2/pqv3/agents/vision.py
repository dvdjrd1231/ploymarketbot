"""§5 — screenshots and scanned pages, through a vision-capable model.

The last refusal in the document pipeline was images: a screenshot of a
strategy note, or a PDF that is a photograph of paper. Both were declined on
the grounds that OCR is a third-party dependency. That was true and is now
beside the point — this installation already talks to an OpenAI-compatible
endpoint for the agent, and the same wire format carries an image. No new
dependency, no bundled OCR engine, and the operator has already chosen the
model.

WHAT MAKES THIS DIFFERENT FROM EVERY OTHER READER, and why it is labelled
everywhere it surfaces: a `.docx` or a text-layer PDF is DECODED. The bytes in
the file determine the characters that come out, and running it twice gives the
same answer. A transcription is GENERATED. A model reading a blurry figure can
produce a plausible number that is not the number on the page, and nothing in
the output distinguishes the two.

So §41 applies at full strength:

  * every `Document` produced this way carries `transcribed=True`
  * claims extracted from it are marked `evidence="TRANSCRIBED"`
  * the note says so, in the reply, every time
  * `documents.extract` refuses to promote a transcribed numeric claim into a
    candidate threshold — it never does that for any document, and here the
    reason is sharper

The right use is a screenshot of prose whose CLAIMS you want mapped onto the
engine's vocabulary. The wrong use is lifting a figure off an image and
treating it as measured.
"""

from __future__ import annotations

import base64
import mimetypes
from dataclasses import dataclass, field
from pathlib import Path

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
MAX_IMAGE_BYTES = 12 * 1024 * 1024

PROMPT = (
    "Transcribe every word of text visible in this image, verbatim and in "
    "reading order. Preserve numbers exactly as written. Do not summarise, "
    "do not explain, do not describe the layout, and do not add anything that "
    "is not legibly present. If part of the image is unreadable, write "
    "[unreadable] there rather than guessing. If there is no text at all, "
    "reply with exactly: NO TEXT.")


@dataclass
class Transcription:
    ok: bool = False
    text: str = ""
    model: str = ""
    reason: str = ""
    warnings: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def available(st) -> tuple[bool, str]:
    cfg = getattr(st, "agents", None)
    if not (cfg and cfg.llm_provider and cfg.llm_endpoint and cfg.llm_model):
        return False, (
            "reading an image needs a vision-capable model. Set "
            "PQV3_LLM_PROVIDER, PQV3_LLM_ENDPOINT and PQV3_LLM_MODEL to one "
            "that accepts images — llama3.2-vision or qwen2.5-vl under Ollama, "
            "or any OpenAI-compatible vision endpoint")
    return True, f"{cfg.llm_provider}/{cfg.llm_model}"


def data_url(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    return (f"data:{mime};base64,"
            + base64.b64encode(path.read_bytes()).decode("ascii"))


def transcribe(st, path: str) -> Transcription:
    """Read the text in an image. The result is GENERATED, never measured."""
    from .llm import LocalLLM

    r = Transcription()
    p = Path(path)
    if not p.exists():
        r.reason = f"no such file: {p}"
        return r
    if p.stat().st_size > MAX_IMAGE_BYTES:
        r.reason = (f"{p.stat().st_size:,} bytes exceeds the "
                    f"{MAX_IMAGE_BYTES:,} limit for an image")
        return r

    ok, detail = available(st)
    if not ok:
        r.reason = detail
        return r

    llm = LocalLLM(st)
    r.model = llm.cfg.llm_model
    msg = llm.chat([{
        "role": "user",
        "content": [
            {"type": "text", "text": PROMPT},
            {"type": "image_url", "image_url": {"url": data_url(p)}},
        ],
    }], max_tokens=3000)

    if msg.get("error"):
        err = str(msg["error"])
        hint = ""
        if "400" in err or "image" in err.lower():
            hint = (" — this model may not accept images. A text-only model "
                    "cannot read a screenshot; try a vision model")
        r.reason = f"the model could not read the image: {err}{hint}"
        return r

    text = (msg.get("content") or "").strip()
    if not text or text.upper().startswith("NO TEXT"):
        r.reason = ("the model reports no legible text in this image. If you "
                    "can read text in it yourself, the resolution is probably "
                    "too low for the model")
        return r

    r.ok = True
    r.text = text
    r.warnings.append(
        "TRANSCRIBED, NOT DECODED. A model read this image and produced these "
        "characters; a blurry figure can become a plausible wrong number and "
        "nothing in the text marks which. Check any figure against the image "
        "before relying on it, and treat claims from it as weaker evidence "
        "than the same claim in a text file (§41).")
    return r
