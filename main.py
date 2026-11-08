
import os
from typing import Any, Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# ============================================================
# CONFIG
# ============================================================

APP_VERSION = "2.2.0"

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

GEMINI_MODEL = os.getenv(
    "GEMINI_MODEL",
    "gemini-3.5-flash-lite"
)

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    + GEMINI_MODEL
    + ":generateContent"
)

MAX_MESSAGE_LENGTH = 12000

app = FastAPI(
    title="Nexus Gemini API",
    version=APP_VERSION
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"]
)

# ============================================================
# MODELS
# ============================================================

class ChatRequest(BaseModel):
    message: str = Field(
        min_length=1,
        max_length=MAX_MESSAGE_LENGTH
    )

    # Aceita texto, dicionário ou ausência de contexto.
    context: Optional[Any] = None


# ============================================================
# HOME
# ============================================================

@app.get("/")
async def home():
    return {
        "name": "Nexus Gemini API",
        "version": APP_VERSION,
        "status": "online"
    }


# ============================================================
# HEALTH
# ============================================================

@app.get("/api/health")
async def health():
    return {
        "success": True,
        "status": "online",
        "version": APP_VERSION,
        "model": GEMINI_MODEL,
        "gemini_configured": bool(GEMINI_API_KEY)
    }


# ============================================================
# GEMINI CHAT
# ============================================================

@app.post("/api/chat")
async def chat(data: ChatRequest):

    if not GEMINI_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="GEMINI_API_KEY não configurada no Render."
        )

    context_text = ""

    if data.context is not None:
        if isinstance(data.context, str):
            context_text = data.context[:12000]
        else:
            context_text = str(data.context)[:12000]

    system_instruction = """
Você é o assistente de programação do Nexus Chatbot.

Responda em português brasileiro.

Ajude com:
- Programação Luau.
- Interfaces Roblox.
- Depuração de scripts.
- Organização de projetos.
- Utilização de assets públicos.

Quando gerar código, use exatamente este formato:

<codigo>
-- código Luau completo
</codigo>

Não coloque explicações dentro das tags.

REGRAS:
- Nunca diga que executou um código quando apenas o gerou.
- Explique brevemente a finalidade do código.
- Não invente resultados de testes.
- Se não precisar gerar código, responda normalmente.
"""

    prompt = (
        "CONTEXTO DO PAINEL:\n"
        + context_text
        + "\n\nMENSAGEM DO USUÁRIO:\n"
        + data.message
    )

    payload = {
        "systemInstruction": {
            "parts": [
                {
                    "text": system_instruction
                }
            ]
        },
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": prompt
                    }
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 4096
        }
    }

    try:
        async with httpx.AsyncClient(timeout=60) as client:

            response = await client.post(
                GEMINI_URL,
                params={
                    "key": GEMINI_API_KEY
                },
                json=payload
            )

        if response.status_code != 200:
            raise HTTPException(
                status_code=502,
                detail={
                    "message": "Erro retornado pelo Gemini.",
                    "gemini_status": response.status_code,
                    "response": response.text[:2000]
                }
            )

        result = response.json()

        candidates = result.get("candidates", [])

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

        reply = "\n".join(
            part.get("text", "")
            for part in parts
            if part.get("text")
        )

        if not reply:
            raise HTTPException(
                status_code=502,
                detail="A resposta do Gemini veio vazia."
            )

        return {
            "success": True,
            "Success": True,
            "reply": reply,
            "response": reply,
            "answer": reply,
            "model": GEMINI_MODEL
        }

    except httpx.TimeoutException:
        raise HTTPException(
            status_code=504,
            detail="Tempo limite do Gemini excedido."
        )

    except httpx.RequestError:
        raise HTTPException(
            status_code=502,
            detail="Não foi possível conectar ao Gemini."
        )


# ============================================================
# SERVER
# ============================================================

if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port
    )
