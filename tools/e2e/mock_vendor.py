"""Mock Mistral-compatible vendor server (localhost only).

Speaks the exact API dialect backend/app/providers/mistral_provider.py
expects: GET /v1/models and POST /v1/chat/completions with a
json_schema response_format. Corrections are deterministic OCR fixes so
the E2E result is verifiable by eye.
"""

import json
import re

from fastapi import FastAPI, Request

app = FastAPI()

# Deterministic "LLM": classic French OCR confusions found in sample.xml.
FIXES = [
    (r"\bFrauce\b", "France"),
    (r"\buue\b", "une"),
    (r"\bcitoyeus\b", "citoyens"),
    (r"\bsoulevèreut\b", "soulevèrent"),
    (r"\bpouvolr\b", "pouvoir"),
    (r"\bjouruée\b", "journée"),
    (r"\bsou\b", "son"),
    (r"\bbouleYerse-", "bouleverse-"),
    (r"\buationale\b", "nationale"),
    (r"\bhoinme\b", "homme"),
    (r"\bcitoyeu\b", "citoyen"),
    (r"\bpriucipes\b", "principes"),
    (r"\btrausformer\b", "transformer"),
    (r"\btronblée\b", "troublée"),
    (r"\bmeuts\b", "ments"),
]


def correct(text: str) -> str:
    for pat, rep in FIXES:
        text = re.sub(pat, rep, text)
    return text


@app.get("/v1/models")
async def models():
    return {
        "data": [
            {
                "id": "mock-mistral-small",
                "name": "Mock Mistral Small",
                "capabilities": {"completion_chat": True},
            }
        ]
    }


@app.post("/v1/chat/completions")
async def completions(request: Request):
    body = await request.json()
    user_payload = json.loads(body["messages"][1]["content"])
    lines_out = [
        {"line_id": ln["line_id"], "corrected_text": correct(ln.get("ocr_text", ""))}
        for ln in user_payload.get("lines", [])
    ]
    content = json.dumps({"lines": lines_out}, ensure_ascii=False)
    return {
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 80},
    }
