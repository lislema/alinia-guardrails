import os
import time
from typing import Dict, Any, List, Optional

import requests
from fastapi import FastAPI, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

ALINIA_API_URL = "https://api.alinia.ai/moderations/"
TIMEOUT_S = float(os.getenv("HTTP_TIMEOUT_S", "15"))
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# --- FastAPI app ---
app = FastAPI(title="Alinea Moderation Proxy", version="1.0.0")

# --- CORS (adjust via env) ---
allow_origins = os.getenv("CORS_ALLOW_ORIGINS", "*").split(",")
allow_headers = os.getenv("CORS_ALLOW_HEADERS", "*").split(",")
allow_methods = os.getenv("CORS_ALLOW_METHODS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in allow_origins],
    allow_credentials=False,
    allow_methods=[m.strip() for m in allow_methods],
    allow_headers=[h.strip() for h in allow_headers],
)

# --- Models ---
class DetectionSecurity(BaseModel):
    adversarial: bool = True

class DetectionSafety(BaseModel):
    wrongdoing: bool = True

class DetectionConfig(BaseModel):
    security: DetectionSecurity = Field(default_factory=DetectionSecurity)
    safety: DetectionSafety = Field(default_factory=DetectionSafety)

class ModerateRequest(BaseModel):
    input: str = Field(..., description="Text to moderate (single string).")
    detection_config: DetectionConfig = Field(default_factory=DetectionConfig)

class ModerateBatchRequest(BaseModel):
    inputs: List[str] = Field(..., description="List of texts to moderate.")
    detection_config: DetectionConfig = Field(default_factory=DetectionConfig)

class ModerateResponse(BaseModel):
    input: str
    flagged_categories: List[str]
    raw: Dict[str, Any]

class ModerateBatchItem(BaseModel):
    input: str
    flagged_categories: List[str]
    raw: Dict[str, Any]

class ModerateBatchResponse(BaseModel):
    items: List[ModerateBatchItem]

# --- Helpers ---
_session = requests.Session()

def _get_api_key() -> str:
    api_key = os.getenv("ALINIA_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="Missing ALINIA_API_KEY environment variable")
    return api_key

def _headers(api_key: str) -> Dict[str, str]:
    return {
        "accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

def call_alinea_single(input_text: str, detection_cfg: Dict[str, Any], *, attempts: int = 3) -> Dict[str, Any]:
    """
    Alinea expects 'input' to be a single string.
    We implement retries for transient errors.
    """
    api_key = _get_api_key()
    payload = {"input": input_text, "detection_config": detection_cfg}
    last_err = ""

    for i in range(attempts):
        resp = _session.post(ALINIA_API_URL, headers=_headers(api_key), json=payload, timeout=TIMEOUT_S)
        if resp.status_code == 200:
            return resp.json()
        last_err = resp.text
        if resp.status_code in RETRYABLE_STATUS and i < attempts - 1:
            time.sleep(0.8 * (2 ** i))
            continue
        raise HTTPException(status_code=resp.status_code, detail=last_err)

    raise HTTPException(status_code=502, detail=last_err or "Upstream error")

def extract_flagged(result_json: Dict[str, Any]) -> List[str]:
    res = result_json.get("result")
    if isinstance(res, list):
        res = res[0] if res else {}
    if not isinstance(res, dict):
        return []
    return res.get("flagged_categories", []) or []

# --- Routes ---
@app.get("/", response_class=None)
def index():
    # Lightweight inline UI you can replace later
    return (
        "<!doctype html><meta charset='utf-8'>"
        "<title>Alinea Moderation Proxy</title>"
        "<style>body{font-family:system-ui,Segoe UI,Roboto,Helvetica,Arial;margin:2rem;max-width:900px}"
        "textarea{width:100%;height:8rem}button{padding:.6rem 1rem;margin-top:.5rem}</style>"
        "<h1>Alinea Moderation Proxy</h1>"
        "<p>POST <code>/moderate</code> with JSON or <code>/moderate/plain</code> with text/plain."
        " For arrays, use <code>/moderate/batch</code> (server loops each item).</p>"
        "<form method='post' action='/moderate/plain'>"
        "<textarea name='text' placeholder='Type text to moderate...'></textarea><br>"
        "<button type='submit'>Moderate</button>"
        "</form>"
        "<p>Health: <a href='/healthz'>/healthz</a> • Ready: <a href='/readyz'>/readyz</a></p>"
    )

@app.get("/healthz")
def healthz():
    return {"ok": True}

@app.get("/readyz")
def readyz():
    # Optionally check env here
    return {"ok": True, "has_api_key": bool(os.getenv("ALINIA_API_KEY"))}

@app.post("/moderate", response_model=ModerateResponse)
def moderate(req: ModerateRequest):
    j = call_alinea_single(req.input, req.detection_config.model_dump())
    return ModerateResponse(
        input=req.input,
        flagged_categories=extract_flagged(j),
        raw=j,
    )

@app.post("/moderate/plain", response_model=ModerateResponse)
def moderate_plain(text: str = Body(..., media_type="text/plain", embed=False)):
    req = ModerateRequest(input=text)
    return moderate(req)

@app.post("/moderate/batch", response_model=ModerateBatchResponse)
def moderate_batch(req: ModerateBatchRequest):
    items: List[ModerateBatchItem] = []
    for t in req.inputs:
        j = call_alinea_single(t, req.detection_config.model_dump())
        items.append(
            ModerateBatchItem(
                input=t,
                flagged_categories=extract_flagged(j),
                raw=j,
            )
        )
    return ModerateBatchResponse(items=items)