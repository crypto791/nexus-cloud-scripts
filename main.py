
import os
import time
import uuid
import secrets
from collections import deque
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# ============================================================
# CONFIG
# ============================================================

APP_VERSION = "2.1.0"

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

MAX_CODE_LENGTH = 200000
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
    allow_headers=["*"]
)

# ============================================================
# MEMORY
# ============================================================

reports = deque(maxlen=1000)
tickets = {}
search_cache = {}


def now():
    return int(time.time())


def create_ticket_id():
    for _ in range(100):
        ticket_id = "ticket_" + str(
            secrets.randbelow(90000000) + 10000000
        )

        if ticket_id not in tickets:
            return ticket_id

    return "ticket_" + uuid.uuid4().hex


# ============================================================
# MODELS
# ============================================================

class ChatRequest(BaseModel):
    message: str = Field(
        min_length=1,
        max_length=MAX_MESSAGE_LENGTH
    )
    context: Optional[dict] = None


class RecordRequest(BaseModel):
    type: str = "event"
    message: str = ""
    data: Optional[dict] = None
    place_id: Optional[int] = None
    player: Optional[str] = None


class TicketCreate(BaseModel):
    code: str = Field(
        min_length=1,
        max_length=MAX_CODE_LENGTH
    )
    name: str = Field(
        default="Código gerado",
        max_length=100
    )
    place_id: Optional[int] = None
    owner_tag: Optional[str] = None


class TicketUpdate(BaseModel):
    code: Optional[str] = Field(
        default=None,
        max_length=MAX_CODE_LENGTH
    )
    name: Optional[str] = Field(
        default=None,
        max_length=100
    )


# ============================================================
# HOME / HEALTH
# ============================================================

@app.get("/")
async def home():
    return {
        "name": "Nexus Cloud API",
        "version": APP_VERSION,
        "status": "online"
    }


@app.get("/api/health")
async def health():
    return {
        "success": True,
        "status": "online",
        "version": APP_VERSION,
        "model": GEMINI_MODEL,
        "gemini_configured": bool(GEMINI_API_KEY),
        "tickets": len(tickets),
        "reports": len(reports),
        "time": now()
    }


# ============================================================
# REPORTS
# ============================================================

@app.post("/api/record")
async def record_event(data: RecordRequest):
    item = {
        "id": uuid.uuid4().hex[:12],
        "type": data.type,
        "message": data.message[:5000],
        "data": data.data or {},
        "place_id": data.place_id,
        "player": data.player,
        "timestamp": now()
    }

    reports.appendleft(item)

    return {
        "success": True,
        "id": item["id"]
    }


@app.get("/api/reports")
async def get_reports(
    limit: int = Query(default=100, ge=1, le=500),
    event_type: Optional[str] = None
):
    items = list(reports)

    if event_type:
        items = [
            item for item in items
            if item["type"] == event_type
        ]

    return {
        "success": True,
        "count": len(items[:limit]),
        "reports": items[:limit]
    }


@app.delete("/api/reports")
async def clear_reports():
    reports.clear()

    return {
        "success": True,
        "message": "Relatórios apagados."
    }


# ============================================================
# GEMINI
# ============================================================

@app.post("/api/chat")
async def chat(data: ChatRequest):

    if not GEMINI_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="GEMINI_API_KEY não configurada no Render."
        )

    context_text = ""

    if data.context:
        context_text = str(data.context)[:12000]

    system_instruction = """
Você é o assistente de programação do Nexus Chatbot.

Responda em português brasileiro.

Ajude com:
- Programação Luau.
- Interfaces Roblox.
- Sistemas para Roblox Studio.
- Depuração de scripts.
- Organização de projetos.
- Busca e utilização de assets públicos.

Quando gerar código, use exatamente este formato:

<codigo>
-- código Luau completo
</codigo>

Não coloque explicações dentro das tags.

REGRAS:
- Nunca diga que executou um código quando apenas o gerou.
- Explique brevemente a finalidade do código.
- Se não precisar gerar código, responda normalmente.
- Não invente resultados de testes.
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
# TICKETS
# ============================================================

@app.post("/api/tickets")
async def create_ticket(data: TicketCreate):

    ticket_id = create_ticket_id()

    ticket = {
        "id": ticket_id,
        "name": data.name,
        "code": data.code,
        "place_id": data.place_id,
        "owner_tag": data.owner_tag,
        "created_at": now()
    }

    tickets[ticket_id] = ticket

    return {
        "success": True,
        "ticket": ticket_id,
        "name": ticket["name"],
        "created_at": ticket["created_at"]
    }


@app.get("/api/tickets")
async def list_tickets():

    items = []

    for ticket in tickets.values():
        items.append({
            "id": ticket["id"],
            "name": ticket["name"],
            "place_id": ticket["place_id"],
            "created_at": ticket["created_at"]
        })

    return {
        "success": True,
        "count": len(items),
        "tickets": items
    }


@app.get("/api/tickets/{ticket_id}")
async def get_ticket(ticket_id: str):

    ticket = tickets.get(ticket_id)

    if not ticket:
        raise HTTPException(
            status_code=404,
            detail="Ticket não encontrado."
        )

    return {
        "success": True,
        "ticket": ticket["id"],
        "name": ticket["name"],
        "code": ticket["code"],
        "place_id": ticket["place_id"],
        "created_at": ticket["created_at"]
    }


@app.put("/api/tickets/{ticket_id}")
async def update_ticket(
    ticket_id: str,
    data: TicketUpdate
):

    ticket = tickets.get(ticket_id)

    if not ticket:
        raise HTTPException(
            status_code=404,
            detail="Ticket não encontrado."
        )

    if data.code is not None:
        ticket["code"] = data.code

    if data.name is not None:
        ticket["name"] = data.name

    return {
        "success": True,
        "ticket": ticket_id,
        "message": "Ticket atualizado."
    }


@app.delete("/api/tickets/{ticket_id}")
async def delete_ticket(ticket_id: str):

    if ticket_id not in tickets:
        raise HTTPException(
            status_code=404,
            detail="Ticket não encontrado."
        )

    del tickets[ticket_id]

    return {
        "success": True,
        "message": "Ticket excluído."
    }


# ============================================================
# ROBLOX PUBLIC SEARCH
# ============================================================

async def roblox_get(url, params=None):

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                url,
                params=params,
                headers={
                    "User-Agent": "NexusCloud/2.1"
                }
            )

        if response.status_code != 200:
            raise HTTPException(
                status_code=502,
                detail={
                    "message": "Erro na API pública do Roblox.",
                    "status": response.status_code
                }
            )

        return response.json()

    except httpx.TimeoutException:
        raise HTTPException(
            status_code=504,
            detail="Tempo limite da busca excedido."
        )

    except httpx.RequestError:
        raise HTTPException(
            status_code=502,
            detail="Não foi possível acessar o Roblox."
        )


@app.get("/api/roblox/search")
async def roblox_search(
    kind: str = "icons",
    query: str = Query(
        default="",
        max_length=100
    ),
    limit: int = Query(
        default=20,
        ge=1,
        le=50
    )
):

    if not query.strip():
        raise HTTPException(
            status_code=400,
            detail="Informe um termo de busca."
        )

    kind = kind.lower().strip()

    cache_key = (
        f"{kind}:{query.lower()}:{limit}"
    )

    cached = search_cache.get(cache_key)

    if cached and cached["expires"] > now():
        return cached["data"]

    if kind in ("icons", "assets", "decals"):

        url = (
            "https://catalog.roblox.com/v1/search/items/details"
        )

        result = await roblox_get(
            url,
            {
                "Category": 11,
                "Keyword": query,
                "Limit": limit
            }
        )

        items = []

        for item in result.get("data", []):

            asset_id = item.get("id")

            if not asset_id:
                continue

            items.append({
                "id": asset_id,
                "name": item.get("name", "Asset"),
                "description": item.get("description", ""),
                "creator": item.get("creatorName", ""),
                "asset_type": item.get("assetType", ""),
                "url": (
                    f"https://www.roblox.com/catalog/{asset_id}"
                ),
                "thumbnail": (
                    "https://www.roblox.com/asset-thumbnail/"
                    f"image?assetId={asset_id}&width=420&height=420"
                )
            })

        output = {
            "success": True,
            "kind": kind,
            "query": query,
            "count": len(items),
            "results": items
        }

    elif kind == "users":

        result = await roblox_get(
            "https://users.roblox.com/v1/users/search",
            {
                "keyword": query,
                "limit": limit
            }
        )

        output = {
            "success": True,
            "kind": kind,
            "results": result.get("data", [])
        }

    elif kind == "games":

        result = await roblox_get(
            "https://games.roblox.com/v1/games/list",
            {
                "model.keyword": query,
                "model.maxRows": limit
            }
        )

        output = {
            "success": True,
            "kind": kind,
            "results": result.get("games", [])
        }

    else:
        raise HTTPException(
            status_code=400,
            detail="Tipo de busca inválido."
        )

    search_cache[cache_key] = {
        "expires": now() + 90,
        "data": output
    }

    return output


# ============================================================
# ROBLOX ASSET DETAILS
# ============================================================

@app.get("/api/roblox/asset/{asset_id}")
async def roblox_asset(asset_id: int):

    if asset_id <= 0:
        raise HTTPException(
            status_code=400,
            detail="ID inválido."
        )

    result = await roblox_get(
        "https://thumbnails.roblox.com/v1/assets",
        {
            "assetIds": asset_id,
            "size": "420x420",
            "format": "Png",
            "isCircular": "false"
        }
    )

    return {
        "success": True,
        "asset_id": asset_id,
        "url": (
            f"https://www.roblox.com/catalog/{asset_id}"
        ),
        "thumbnail": result.get("data", [])
    }


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
