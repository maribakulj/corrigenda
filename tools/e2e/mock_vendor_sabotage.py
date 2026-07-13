"""Adversarial mock vendor: returns DELIBERATELY invariant-violating
corrections. If the pipeline's guards are real, none of these must reach
the output XML; the lines must fall back to OCR and the job must end
completed_with_fallbacks (or retry+downgrade first).

Sabotages:
  - TL4 (PART1 'dénon-'): fused full word 'dénonçait' → hyphen fusion.
  - TL7 ('ments.'): absorbs the next line's words → lines-never-merge.
  - TL10: empty string → empty-correction guard.
Other lines get legitimate fixes (same table as the honest mock).
"""

import json
import re

from fastapi import FastAPI, Request

app = FastAPI()

FIXES = [
    (r"\bFrauce\b", "France"),
    (r"\buue\b", "une"),
    (r"\bcitoyeus\b", "citoyens"),
    (r"\bsoulevèreut\b", "soulevèrent"),
    (r"\bpouvolr\b", "pouvoir"),
    (r"\bjouruée\b", "journée"),
    (r"\buationale\b", "nationale"),
    (r"\bhoinme\b", "homme"),
    (r"\bcitoyeu\b", "citoyen"),
    (r"\btronblée\b", "troublée"),
]


def correct(line_id: str, text: str) -> str:
    if line_id == "TL4":
        # Fusion: PART1 swallows PART2's fragment — ends with the FULL
        # logical word 'dénonçait' instead of 'dénon-'.
        return "Le peuple réclamait la liberté et dénonçait"
    if line_id == "TL7":
        # Absorption: pulls the next physical line's words into this one.
        return "ments. L'assemblée nationale proclama"
    if line_id == "TL10":
        return ""  # emptied line
    for pat, rep in FIXES:
        text = re.sub(pat, rep, text)
    return text


@app.get("/v1/models")
async def models():
    return {
        "data": [
            {
                "id": "mock-sabotage",
                "name": "Mock Sabotage",
                "capabilities": {"completion_chat": True},
            }
        ]
    }


@app.post("/v1/chat/completions")
async def completions(request: Request):
    body = await request.json()
    user_payload = json.loads(body["messages"][1]["content"])
    lines_out = [
        {
            "line_id": ln["line_id"],
            "corrected_text": correct(ln["line_id"], ln.get("ocr_text", "")),
        }
        for ln in user_payload.get("lines", [])
    ]
    content = json.dumps({"lines": lines_out}, ensure_ascii=False)
    return {
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 80},
    }
