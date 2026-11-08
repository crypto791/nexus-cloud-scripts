import os
import json
import time
import hashlib
import sqlite3
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


APP_VERSION = "4.7.0"

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv(
    "GEMINI_MODEL",
    "gemini-3.5-flash-lite"
).strip()

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)

DATABASE_PATH = os.getenv(
    "DATABASE_PATH",
    "/tmp/nexus_cloud.sqlite3"
)

MAX_EVENTS = 500
MAX_EVENT_SIZE = 20000
MAX_MESSAGE_LENGTH = 12000

app = FastAPI(
    title="Nexus Cloud API",
    version=APP_VERSION
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =========================================================
# BANCO DE DADOS
# =========================================================

def get_db():
    folder = os.path.dirname(DATABASE_PATH)

    if folder:
        os.makedirs(folder, exist_ok=True)

    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_database():
    db = get_db()

    db.execute("""
        CREATE TABLE IF NOT EXISTS events (
            event_hash TEXT PRIMARY KEY,
            category TEXT NOT NULL,
            event_json TEXT NOT NULL,
            created_at REAL NOT NULL
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS reports (
            report_id TEXT PRIMARY KEY,
            category TEXT NOT NULL,
            report_json TEXT NOT NULL,
            event_count INTEGER NOT NULL,
            created_at REAL NOT NULL
        )
    """)

    db.commit()
    db.close()


init_database()


# =========================================================
# UTILIDADES
# =========================================================

def current_time():
    return int(time.time())


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str
    )


def content_hash(value: Any) -> str:
    raw = canonical_json(value).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def safe_text(value: Any, limit=MAX_MESSAGE_LENGTH):
    if value is None:
        return ""

    return str(value).strip()[:limit]


def get_latest_report():
    db = get_db()

    row = db.execute("""
        SELECT *
        FROM reports
        ORDER BY created_at DESC
        LIMIT 1
    """).fetchone()

    db.close()

    if not row:
        return None

    try:
        report = json.loads(row["report_json"])
    except Exception:
        report = {}

    return {
        "report_id": row["report_id"],
        "category": row["category"],
        "event_count": row["event_count"],
        "created_at": row["created_at"],
        "events": report.get("events", [])
    }


# =========================================================
# MODELOS
# =========================================================

class RecordRequest(BaseModel):
    category: str = "Roblox"
    events: list[Any] = Field(default_factory=list)


class ChatRequest(BaseModel):
    message: str = ""
    prompt: str = ""
    history: list[dict[str, Any]] = Field(default_factory=list)
    context: Any = None


class AnalyzeRequest(BaseModel):
    report: Any = None
    events: list[Any] = Field(default_factory=list)
    category: str = "Roblox"
    question: str = ""


# =========================================================
# GEMINI
# =========================================================

async def ask_gemini(
    prompt: str,
    history=None,
    system_instruction=None
):
    if not GEMINI_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="GEMINI_API_KEY não configurada no Render."
        )

    prompt = safe_text(prompt)

    if not prompt:
        raise HTTPException(
            status_code=400,
            detail="Mensagem vazia."
        )

    contents = []

    for item in (history or [])[-20:]:
        role = str(item.get("role", "user")).lower()

        if role not in ("user", "model"):
            continue

        message = safe_text(
            item.get("text", item.get("message", ""))
        )

        if not message:
            continue

        contents.append({
            "role": role,
            "parts": [{"text": message}]
        })

    contents.append({
        "role": "user",
        "parts": [{"text": prompt}]
    })

    payload = {
        "contents": contents,
        "generationConfig": {
            "temperature": 0.35,
            "maxOutputTokens": 1800
        }
    }

    if system_instruction:
        payload["systemInstruction"] = {
            "parts": [
                {"text": safe_text(system_instruction, 5000)}
            ]
        }

    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": GEMINI_API_KEY
    }

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                GEMINI_URL,
                headers=headers,
                json=payload
            )

        if response.status_code != 200:
            try:
                error_data = response.json()
            except Exception:
                error_data = response.text[:1000]

            raise HTTPException(
                status_code=502,
                detail={
                    "message": "Erro retornado pelo Gemini.",
                    "status": response.status_code,
                    "error": error_data
                }
            )

        data = response.json()

        candidates = data.get("candidates", [])

        if not candidates:
            raise HTTPException(
                status_code=502,
                detail="Gemini não retornou uma resposta."
            )

        parts = (
            candidates[0]
            .get("content", {})
            .get("parts", [])
        )

        answer = "\n".join(
            part.get("text", "")
            for part in parts
            if part.get("text")
        ).strip()

        if not answer:
            raise HTTPException(
                status_code=502,
                detail="Resposta vazia do Gemini."
            )

        return answer

    except HTTPException:
        raise

    except httpx.TimeoutException:
        raise HTTPException(
            status_code=504,
            detail="Tempo limite de conexão com Gemini."
        )

    except Exception as error:
        raise HTTPException(
            status_code=502,
            detail=f"Falha na comunicação com Gemini: {str(error)}"
        )


# =========================================================
# HEALTH
# =========================================================

@app.get("/")
async def root():
    return {
        "name": "Nexus Cloud",
        "version": APP_VERSION,
        "status": "online",
        "health": "/api/health"
    }


@app.get("/api/health")
async def health():
    db = get_db()

    event_count = db.execute(
        "SELECT COUNT(*) FROM events"
    ).fetchone()[0]

    report_count = db.execute(
        "SELECT COUNT(*) FROM reports"
    ).fetchone()[0]

    db.close()

    return {
        "success": True,
        "status": "online",
        "version": APP_VERSION,
        "gemini": {
            "configured": bool(GEMINI_API_KEY),
            "model": GEMINI_MODEL
        },
        "database": {
            "available": True,
            "events": event_count,
            "reports": report_count
        },
        "latest_report": get_latest_report() is not None
    }


# =========================================================
# RECEBER RELATÓRIOS
# =========================================================

@app.post("/api/record")
async def record_report(data: RecordRequest):
    category = safe_text(data.category, 100) or "Roblox"

    if not data.events:
        raise HTTPException(
            status_code=400,
            detail="Nenhum evento foi enviado."
        )

    if len(data.events) > MAX_EVENTS:
        raise HTTPException(
            status_code=413,
            detail=f"Limite de {MAX_EVENTS} eventos por envio."
        )

    accepted = []
    duplicate_count = 0

    db = get_db()

    try:
        db.execute("BEGIN IMMEDIATE")

        for event in data.events:
            event_json = canonical_json(event)

            if len(event_json.encode("utf-8")) > MAX_EVENT_SIZE:
                continue

            event_id = content_hash(event)

            cursor = db.execute("""
                INSERT OR IGNORE INTO events (
                    event_hash,
                    category,
                    event_json,
                    created_at
                )
                VALUES (?, ?, ?, ?)
            """, (
                event_id,
                category,
                event_json,
                time.time()
            ))

            if cursor.rowcount == 1:
                accepted.append({
                    "id": event_id,
                    "event": event
                })
            else:
                duplicate_count += 1

        if not accepted:
            db.commit()

            latest = db.execute("""
                SELECT report_id
                FROM reports
                ORDER BY created_at DESC
                LIMIT 1
            """).fetchone()

            return {
                "success": True,
                "duplicate": True,
                "message": "Todos os eventos já foram recebidos.",
                "received": 0,
                "duplicates": duplicate_count,
                "report_id": latest["report_id"] if latest else None
            }

        report_events = [
            item["event"]
            for item in accepted
        ]

        report_id = content_hash(
            sorted(item["id"] for item in accepted)
        )

        report_data = {
            "category": category,
            "events": report_events
        }

        db.execute("""
            INSERT OR IGNORE INTO reports (
                report_id,
                category,
                report_json,
                event_count,
                created_at
            )
            VALUES (?, ?, ?, ?, ?)
        """, (
            report_id,
            category,
            canonical_json(report_data),
            len(report_events),
            time.time()
        ))

        db.commit()

        return {
            "success": True,
            "duplicate": False,
            "message": "Relatório recebido com sucesso.",
            "report_id": report_id,
            "received": len(accepted),
            "duplicates": duplicate_count,
            "total_submitted": len(data.events)
        }

    except Exception:
        db.rollback()
        raise

    finally:
        db.close()


# =========================================================
# ÚLTIMO RELATÓRIO
# =========================================================

@app.get("/api/record/latest")
async def latest_record():
    report = get_latest_report()

    if not report:
        return {
            "success": True,
            "exists": False,
            "report": None
        }

    return {
        "success": True,
        "exists": True,
        "report": report
    }


# =========================================================
# ANALISAR RELATÓRIO COM GEMINI
# =========================================================

@app.post("/api/analyze")
async def analyze_report(data: AnalyzeRequest):
    report = data.report

    if report is None and data.events:
        report = {
            "category": data.category,
            "events": data.events
        }

    if report is None:
        report = get_latest_report()

    if not report:
        raise HTTPException(
            status_code=404,
            detail="Nenhum relatório disponível para análise."
        )

    report_text = canonical_json(report)

    if len(report_text) > 30000:
        report_text = report_text[:30000]

    question = safe_text(data.question)

    prompt = f"""
Analise o relatório de depuração abaixo.

Objetivos:
- Explicar o que os eventos indicam.
- Identificar erros e comportamentos incomuns.
- Diferenciar fatos observados de hipóteses.
- Sugerir verificações seguras para o desenvolvedor.
- Não inventar eventos que não aparecem no relatório.

Pergunta adicional:
{question or "Faça uma análise geral."}

RELATÓRIO:
{report_text}
"""

    answer = await ask_gemini(
        prompt,
        system_instruction=(
            "Você é um assistente de depuração para desenvolvedores "
            "Roblox. Analise apenas os dados fornecidos e não presuma "
            "acesso ao jogo ou ao dispositivo."
        )
    )

    return {
        "success": True,
        "analysis": answer,
        "model": GEMINI_MODEL
    }


# =========================================================
# CHAT COM GEMINI
# =========================================================

@app.post("/api/chat")
async def chat(data: ChatRequest):
    message = safe_text(data.message or data.prompt)

    if not message:
        raise HTTPException(
            status_code=400,
            detail="Digite uma mensagem."
        )

    context = ""

    if data.context is not None:
        context = (
            "\n\nContexto fornecido:\n"
            + canonical_json(data.context)[:12000]
        )

    answer = await ask_gemini(
        message + context,
        history=data.history,
        system_instruction=(
            "Você é o Gemini integrado ao Nexus Cloud. "
            "Ajude com programação, depuração e interpretação "
            "de relatórios de uma experiência Roblox própria. "
            "Não afirme ter executado ações no jogo. "
            "Se não houver dados suficientes, explique isso."
        )
    )

    return {
        "success": True,
        "response": answer,
        "message": answer,
        "model": GEMINI_MODEL
    }


# =========================================================
# ATIVIDADE
# =========================================================

@app.get("/api/activity")
async def activity():
    db = get_db()

    events = db.execute("""
        SELECT category, event_json, created_at
        FROM events
        ORDER BY created_at DESC
        LIMIT 50
    """).fetchall()

    total_events = db.execute(
        "SELECT COUNT(*) FROM events"
    ).fetchone()[0]

    total_reports = db.execute(
        "SELECT COUNT(*) FROM reports"
    ).fetchone()[0]

    db.close()

    result = []

    for row in events:
        try:
            event = json.loads(row["event_json"])
        except Exception:
            event = row["event_json"]

        result.append({
            "category": row["category"],
            "event": event,
            "created_at": row["created_at"]
        })

    return {
        "success": True,
        "total_events": total_events,
        "total_reports": total_reports,
        "recent_events": result
    }


# =========================================================
# EXECUÇÃO LOCAL
# =========================================================

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        reload=False
    )