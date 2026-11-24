from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator
try:
    from backend.database import get_connection, init_db
except ModuleNotFoundError:
    from database import get_connection, init_db
from pathlib import Path
from fastapi.responses import FileResponse

app = FastAPI(
    title="SafeBite API",
    description="Check interactions, risk levels, timing gaps, and daily food/symptom logs across diseases and items.",
    version="0.2.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DISCLAIMER = (
    "General informational guidance only. Not a medical diagnosis or prescription; "
    "always confirm diet and supplement changes with a qualified doctor or dietitian."
)

RISK_RANK = {
    "low": 1,
    "safe": 1,
    "moderate": 2,
    "medium": 2,
    "high": 3,
    "avoid": 3,
    "severe": 4,
}
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from fastapi import Depends, Header
from pydantic import EmailStr

AUTH_SECRET = os.getenv("AUTH_SECRET", "dev-change-this-secret-in-prod")
TOKEN_TTL_SECONDS = int(os.getenv("TOKEN_TTL_SECONDS", str(60 * 60 * 24 * 7)))  # 7 din


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000)
    return f"{salt}${dk.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        salt, expected_hex = stored_hash.split("$", 1)
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000)
    return hmac.compare_digest(dk.hex(), expected_hex)


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode((s + pad).encode("ascii"))


def create_access_token(user_id: int, email: str) -> str:
    payload = {"sub": user_id, "email": email, "exp": int(time.time()) + TOKEN_TTL_SECONDS}
    body = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(AUTH_SECRET.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64url_encode(sig)}"


def decode_access_token(token: str) -> dict:
    try:
        body, sig_b64 = token.split(".", 1)
        expected_sig = hmac.new(AUTH_SECRET.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
        actual_sig = _b64url_decode(sig_b64)
        if not hmac.compare_digest(expected_sig, actual_sig):
            raise ValueError("Invalid signature")
        payload = json.loads(_b64url_decode(body).decode("utf-8"))
        if int(payload.get("exp", 0)) < int(time.time()):
            raise ValueError("Token expired")
        if "sub" not in payload:
            raise ValueError("Missing subject")
        return payload
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid or expired token.") from exc


def get_current_user(authorization: str | None = Header(default=None)) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token.")
    token = authorization.split(" ", 1)[1].strip()
    payload = decode_access_token(token)
    return {"id": int(payload["sub"]), "email": payload.get("email", "")}

class AuthSignupRequest(BaseModel):
    email: str = Field(..., min_length=5, max_length=255)
    password: str = Field(..., min_length=8, max_length=128)
    full_name: str = Field(default="", max_length=120)


class AuthLoginRequest(BaseModel):
    email: str = Field(..., min_length=5, max_length=255)
    password: str = Field(..., min_length=1, max_length=128)


@app.post("/auth/signup", status_code=201)
def auth_signup(payload: AuthSignupRequest):
    email_norm = payload.email.strip().lower()
    if "@" not in email_norm or "." not in email_norm.split("@")[-1]:
        raise HTTPException(status_code=422, detail="Please enter a valid email address.")

    pw_hash = hash_password(payload.password)
    with get_connection() as conn:
        existing = conn.execute("SELECT id FROM users WHERE email = ?", (email_norm,)).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail="Email is already registered.")

        cur = conn.execute(
            "INSERT INTO users (email, password_hash, full_name) VALUES (?, ?, ?)",
            (email_norm, pw_hash, payload.full_name.strip()),
        )
        user_id = cur.lastrowid

    token = create_access_token(user_id=user_id, email=email_norm)
    return {"access_token": token, "token_type": "bearer", "user": {"id": user_id, "email": email_norm}}


@app.post("/auth/login")
def auth_login(payload: AuthLoginRequest):
    email_norm = payload.email.strip().lower()
    with get_connection() as conn:
        user = conn.execute(
            "SELECT id, email, password_hash, full_name FROM users WHERE email = ?",
            (email_norm,),
        ).fetchone()

    if not user or not verify_password(payload.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    token = create_access_token(user_id=user["id"], email=user["email"])
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {"id": user["id"], "email": user["email"], "full_name": user["full_name"]},
    }

ALLOWED_SEVERITIES = {"none", "mild", "moderate", "severe"}


class DiaryCreate(BaseModel):
    disease_id: int | None = None
    item_id: int | None = None
    meal_note: str = Field(..., min_length=1, max_length=250)
    intake_time: str = Field(..., min_length=3, max_length=20)
    gap_minutes: int = Field(default=0, ge=0, le=1440)
    symptom: str = Field(default="", max_length=200)
    severity: str = Field(default="None", max_length=40)
    notes: str = Field(default="", max_length=400)

    @field_validator("severity")
    @classmethod
    def validate_severity(cls, v: str) -> str:
        cleaned = (v or "None").strip()
        if cleaned.lower() not in ALLOWED_SEVERITIES:
            raise ValueError("Severity must be one of: None, Mild, Moderate, Severe.")
        return "None" if cleaned.lower() == "none" else cleaned.capitalize()


def _risk_score(level: str | None) -> int:
    return RISK_RANK.get((level or "").strip().lower(), 2)


@app.on_event("startup")
def on_startup() -> None:
    init_db()


@app.get("/health")
def health_check():
    return {"status": "ok", "app": "SafeBite API", "version": "0.2.0"}


@app.get("/diseases")
def get_diseases(q: str | None = Query(default=None)):
    with get_connection() as conn:
        if q and q.strip():
            term = f"%{q.strip()}%"
            rows = conn.execute(
                """
                SELECT disease_id, disease_name, category, notes
                FROM diseases
                WHERE LOWER(disease_name) LIKE LOWER(?)
                   OR LOWER(category) LIKE LOWER(?)
                ORDER BY disease_name ASC
                """,
                (term, term),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT disease_id, disease_name, category, notes
                FROM diseases
                ORDER BY disease_name ASC
                """
            ).fetchall()
    return {"count": len(rows), "diseases": [dict(r) for r in rows]}


@app.get("/items")
def get_items(
    q: str | None = Query(default=None),
    category: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
):
    query = "SELECT item_id, name_en, name_hi, category FROM items WHERE 1=1"
    params: list = []

    if q and q.strip():
        term = f"%{q.strip()}%"
        query += " AND (LOWER(name_en) LIKE LOWER(?) OR LOWER(name_hi) LIKE LOWER(?))"
        params.extend([term, term])

    if category and category.strip():
        query += " AND LOWER(category) = LOWER(?)"
        params.append(category.strip())

    query += " ORDER BY item_id ASC LIMIT ?"
    params.append(limit)

    with get_connection() as conn:
        rows = conn.execute(query, params).fetchall()
    return {"count": len(rows), "items": [dict(r) for r in rows]}


def _fetch_interaction(conn, disease_id: int, item_id: int) -> dict | None:
    row = conn.execute(
        """
        SELECT
            i.interaction_id,
            i.disease_id,
            d.disease_name,
            i.item_id,
            it.name_en AS item_name_en,
            it.name_hi AS item_name_hi,
            it.category AS item_category,
            i.risk_level,
            i.gap_minutes,
            i.advice,
            i.source
        FROM interactions i
        JOIN diseases d ON i.disease_id = d.disease_id
        JOIN items it ON i.item_id = it.item_id
        WHERE i.disease_id = ? AND i.item_id = ?
        """,
        (disease_id, item_id),
    ).fetchone()
    return dict(row) if row else None


@app.get("/check")
def check_interaction(
    disease_id: int = Query(...),
    item_id: int = Query(...),
):
    with get_connection() as conn:
        data = _fetch_interaction(conn, disease_id, item_id)

    if not data:
        raise HTTPException(status_code=404, detail="Interaction not found.")

    data["disclaimer"] = DISCLAIMER
    return data


@app.get("/check-multi")
def check_multi_interactions(
    disease_id: int = Query(...),
    item_ids: str = Query(..., description="Comma-separated item IDs, e.g. 1,2,5"),
):
    parsed_ids: list[int] = []
    for raw_id in item_ids.split(","):
        cleaned = raw_id.strip()
        if not cleaned:
            continue
        if not cleaned.isdigit():
            raise HTTPException(status_code=400, detail=f"Invalid item_id: {cleaned}")
        val = int(cleaned)
        if val not in parsed_ids:
            parsed_ids.append(val)

    if not parsed_ids:
        raise HTTPException(status_code=400, detail="At least one item_id is required.")

    results: list[dict] = []
    with get_connection() as conn:
        disease_row = conn.execute(
            "SELECT disease_id, disease_name FROM diseases WHERE disease_id = ?",
            (disease_id,),
        ).fetchone()
        if not disease_row:
            raise HTTPException(status_code=404, detail="Disease not found.")

        for iid in parsed_ids:
            entry = _fetch_interaction(conn, disease_id, iid)
            if entry:
                results.append(entry)

    if not results:
        raise HTTPException(status_code=404, detail="No interactions found for selected items.")

    highest = max(results, key=lambda r: _risk_score(r.get("risk_level")))
    max_gap = max(int(r.get("gap_minutes") or 0) for r in results)

    return {
        "disease_id": disease_id,
        "disease_name": disease_row["disease_name"],
        "count": len(results),
        "highest_risk": highest.get("risk_level", "Moderate"),
        "max_gap_minutes": max_gap,
        "results": results,
        "disclaimer": DISCLAIMER,
    }

FRONTEND_INDEX = Path(__file__).resolve().parent.parent / "frontend" / "index.html"


@app.get("/", include_in_schema=False)
def serve_frontend():
    if not FRONTEND_INDEX.exists():
        raise HTTPException(status_code=404, detail="Frontend index.html not found.")
    return FileResponse(FRONTEND_INDEX)

@app.get("/diary")
def get_diary_logs(
    disease_id: int | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    current_user: dict = Depends(get_current_user),
):
    query = """
        SELECT
            l.log_id,
            l.disease_id,
            d.disease_name,
            l.item_id,
            it.name_en AS item_name_en,
            it.name_hi AS item_name_hi,
            l.meal_note,
            l.intake_time,
            l.gap_minutes,
            l.symptom,
            l.severity,
            l.notes,
            l.created_at
        FROM diary_logs l
        LEFT JOIN diseases d ON l.disease_id = d.disease_id
        LEFT JOIN items it ON l.item_id = it.item_id
        WHERE l.user_id = ?
    """
    params: list = [current_user["id"]]

    if disease_id is not None:
        query += " AND l.disease_id = ?"
        params.append(disease_id)

    query += " ORDER BY l.log_id DESC LIMIT ?"
    params.append(limit)

    with get_connection() as conn:
        rows = conn.execute(query, params).fetchall()

    return {"count": len(rows), "logs": [dict(r) for r in rows]}


@app.post("/diary", status_code=201)
def create_diary_log(payload: DiaryCreate, current_user: dict = Depends(get_current_user)):
    with get_connection() as conn:
        if payload.disease_id is not None:
            d_row = conn.execute(
                "SELECT disease_id FROM diseases WHERE disease_id = ?",
                (payload.disease_id,),
            ).fetchone()
            if not d_row:
                raise HTTPException(status_code=404, detail="Selected disease not found.")

        if payload.item_id is not None:
            i_row = conn.execute(
                "SELECT item_id FROM items WHERE item_id = ?",
                (payload.item_id,),
            ).fetchone()
            if not i_row:
                raise HTTPException(status_code=404, detail="Selected food item not found.")
        cur = conn.execute(    
            """
            INSERT INTO diary_logs (
                user_id, disease_id, item_id, meal_note, intake_time,
                gap_minutes, symptom, severity, notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                current_user["id"],
                payload.disease_id,
                payload.item_id,
                payload.meal_note.strip(),
                payload.intake_time.strip(),
                payload.gap_minutes,
                payload.symptom.strip(),
                payload.severity.strip() or "None",
                payload.notes.strip(),
            ),
        )
        conn.commit()
        new_id = cur.lastrowid
        row = conn.execute(
            """
            SELECT
                l.log_id,
                l.disease_id,
                d.disease_name,
                l.item_id,
                it.name_en AS item_name_en,
                it.name_hi AS item_name_hi,
                l.meal_note,
                l.intake_time,
                l.gap_minutes,
                l.symptom,
                l.severity,
                l.notes,
                l.created_at
            FROM diary_logs l
            LEFT JOIN diseases d ON l.disease_id = d.disease_id
            LEFT JOIN items it ON l.item_id = it.item_id
            WHERE l.log_id = ? AND l.user_id = ?
            """,
            (new_id,current_user["id"]),
        ).fetchone()

    return {"status": "created", "log": dict(row)}


@app.delete("/diary/{log_id}")
def delete_diary_log(log_id: int, current_user: dict = Depends(get_current_user)):
    with get_connection() as conn:
        cur = conn.execute(
            "DELETE FROM diary_logs WHERE log_id = ?, AND user_id = ?",
            (log_id, current_user["id"]),
            )
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Diary log not found.")
    return {"status": "deleted", "log_id": log_id}