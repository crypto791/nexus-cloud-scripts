"""
NEXUS CLOUD - Roblox Public Image Search API
Version: 1.0.0

Install:
    pip install fastapi uvicorn httpx pydantic

Run:
    uvicorn main:app --host 0.0.0.0 --port 8000

Render:
    Build: pip install -r requirements.txt
    Start: uvicorn main:app --host 0.0.0.0 --port $PORT

Optional environment variable:
    GEMINI_API_KEY=...

Public Roblox data only. No Roblox cookies, account tokens, private inventories,
or authenticated account access are used.
"""

from __future__ import annotations

import os
import time
import uuid
from collections import deque
from typing import Any, Literal

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

APP_VERSION = "1.0.0"
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

app = FastAPI(
    title="NEXUS CLOUD API",
    version=APP_VERSION,
    description="Public Roblox thumbnails/search plus Nexus reports and Gemini chat.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

HTTP_TIMEOUT = httpx.Timeout(18.0, connect=8.0)
ROBLOX_HEADERS = {
    "User-Agent": "NexusCloudPublicAssetSearch/1.0",
    "Accept": "application/json",
}

# Small bounded in-memory buffers; they reset when the Render instance restarts.
REPORTS: deque[dict[str, Any]] = deque(maxlen=1000)
EVENTS: deque[dict[str, Any]] = deque(maxlen=2000)
REQUEST_CACHE: dict[str, tuple[float, Any]] = {}
CACHE_TTL = 90.0


class RecordPayload(BaseModel):
    category: str = Field(default="manual_report", max_length=80)
    batch_id: str | None = Field(default=None, max_length=100)
    events: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    message: str | None = Field(default=None, max_length=4000)
    player: str | None = Field(default=None, max_length=64)
    place_id: int | None = None


class ChatPayload(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    context: str = Field(default="", max_length=12000)


def _cache_get(key: str) -> Any | None:
    item = REQUEST_CACHE.get(key)
    if not item:
        return None
    expires, value = item
    if expires < time.time():
        REQUEST_CACHE.pop(key, None)
        return None
    return value


def _cache_set(key: str, value: Any) -> None:
    if len(REQUEST_CACHE) > 500:
        now = time.time()
        for old_key, (expires, _) in list(REQUEST_CACHE.items()):
            if expires < now:
                REQUEST_CACHE.pop(old_key, None)
        if len(REQUEST_CACHE) > 500:
            REQUEST_CACHE.pop(next(iter(REQUEST_CACHE)), None)
    REQUEST_CACHE[key] = (time.time() + CACHE_TTL, value)


async def roblox_get(url: str, params: dict[str, Any] | None = None) -> Any:
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers=ROBLOX_HEADERS) as client:
        response = await client.get(url, params=params)
        if response.status_code == 429:
            raise HTTPException(429, "Roblox limitou temporariamente as consultas. Aguarde e tente novamente.")
        if response.status_code >= 400:
            raise HTTPException(502, f"Roblox respondeu HTTP {response.status_code}.")
        try:
            return response.json()
        except ValueError:
            raise HTTPException(502, "A resposta pública do Roblox não veio em JSON.")


async def add_asset_thumbnails(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ids = []
    for item in items:
        value = item.get("id") or item.get("assetId") or item.get("asset_id")
        if value is not None and str(value).isdigit():
            ids.append(str(value))

    if not ids:
        return items

    unique_ids = list(dict.fromkeys(ids))[:50]
    data = await roblox_get(
        "https://thumbnails.roblox.com/v1/assets",
        {
            "assetIds": ",".join(unique_ids),
            "returnPolicy": "PlaceHolder",
            "size": "420x420",
            "format": "Png",
            "isCircular": "false",
        },
    )
    urls = {
        str(row.get("targetId")): row.get("imageUrl")
        for row in data.get("data", [])
        if row.get("imageUrl")
    }
    for item in items:
        value = item.get("id") or item.get("assetId") or item.get("asset_id")
        if value is not None:
            item["image_url"] = urls.get(str(value), item.get("image_url"))
    return items


@app.get("/")
async def root():
    return {
        "service": "NEXUS CLOUD",
        "version": APP_VERSION,
        "status": "online",
        "endpoints": [
            "/api/health",
            "/api/record",
            "/api/roblox/search",
            "/api/roblox/asset/{asset_id}",
            "/api/chat",
        ],
    }


@app.get("/api/health")
async def health():
    return {
        "success": True,
        "status": "online",
        "service": "NEXUS CLOUD",
        "version": APP_VERSION,
        "gemini_configured": bool(GEMINI_API_KEY),
        "public_roblox_search": True,
    }


@app.post("/api/record")
async def record(payload: RecordPayload):
    accepted = []
    now = time.time()

    if payload.message:
        entry = {
            "id": str(uuid.uuid4()),
            "received_at": now,
            "category": payload.category,
            "message": payload.message,
            "player": payload.player,
            "place_id": payload.place_id,
        }
        REPORTS.append(entry)
        accepted.append(entry["id"])

    for event in payload.events:
        safe_event = {
            "id": str(event.get("id") or uuid.uuid4()),
            "received_at": now,
            "batch_id": payload.batch_id,
            "category": str(event.get("category", payload.category))[:80],
            "action": str(event.get("action", ""))[:120],
            "detail": str(event.get("detail", event.get("message", "")))[:1500],
        }
        EVENTS.append(safe_event)
        accepted.append(safe_event["id"])

    if not accepted and not payload.message and not payload.events:
        raise HTTPException(400, "Nenhum relatório ou evento foi enviado.")

    return {
        "success": True,
        "Success": True,
        "accepted": len(accepted),
        "ids": accepted,
        "message": "Relatórios recebidos pelo servidor.",
    }


@app.get("/api/reports")
async def get_reports(limit: int = Query(50, ge=1, le=200)):
    return {
        "success": True,
        "reports": list(REPORTS)[-limit:],
        "events": list(EVENTS)[-limit:],
    }


@app.get("/api/roblox/search")
async def roblox_search(
    kind: Literal["icons", "users", "avatars", "games", "assets", "decals"] = "icons",
    query: str = Query(..., min_length=1, max_length=80),
    limit: int = Query(20, ge=1, le=30),
):
    """
    kinds:
      icons   -> Roblox public catalog item search (IconFinder remains client-side)
      users   -> public user search + headshot thumbnails
      avatars -> same public user search, with avatar headshots
      games   -> public experience search + game icon thumbnails
      assets  -> public avatar catalog results + asset thumbnails
      decals  -> requires a numeric public asset ID; use /api/roblox/asset/{id}
    """
    q = query.strip()
    if not q:
        raise HTTPException(400, "Digite algo para pesquisar.")

    cache_key = f"search:{kind}:{q.lower()}:{limit}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    if kind in ("users", "avatars"):
        data = await roblox_get(
            "https://users.roblox.com/v1/users/search",
            {"keyword": q, "limit": min(limit, 25)},
        )
        users = data.get("data", [])
        ids = [str(user["id"]) for user in users if user.get("id") is not None]
        thumb_data = {}
        if ids:
            thumbs = await roblox_get(
                "https://thumbnails.roblox.com/v1/users/avatar-headshot",
                {
                    "userIds": ",".join(ids),
                    "size": "150x150",
                    "format": "Png",
                    "isCircular": "true",
                },
            )
            thumb_data = {
                str(row.get("targetId")): row.get("imageUrl")
                for row in thumbs.get("data", [])
            }
        results = [
            {
                "id": user.get("id"),
                "name": user.get("name"),
                "display_name": user.get("displayName"),
                "type": "avatar",
                "image_url": thumb_data.get(str(user.get("id"))),
                "profile_url": f"https://www.roblox.com/users/{user.get('id')}/profile",
            }
            for user in users
        ]
        result = {"success": True, "kind": kind, "query": q, "results": results}

    elif kind == "games":
        data = await roblox_get(
            "https://games.roblox.com/v1/games/list",
            {
                "model.keyword": q,
                "model.maxRows": min(limit, 25),
                "model.startRows": 0,
                "model.sortOrder": "Asc",
            },
        )
        games = data.get("Games", data.get("games", []))
        ids = [str(game.get("UniverseId") or game.get("universeId")) for game in games]
        ids = [value for value in ids if value and value != "None"]
        thumb_data = {}
        if ids:
            thumbs = await roblox_get(
                "https://thumbnails.roblox.com/v1/games/icons",
                {
                    "universeIds": ",".join(ids[:25]),
                    "returnPolicy": "PlaceHolder",
                    "size": "512x512",
                    "format": "Png",
                    "isCircular": "false",
                },
            )
            thumb_data = {
                str(row.get("targetId")): row.get("imageUrl")
                for row in thumbs.get("data", [])
            }
        results = []
        for game in games:
            universe_id = game.get("UniverseId") or game.get("universeId")
            place_id = game.get("PlaceId") or game.get("rootPlaceId")
            results.append({
                "id": universe_id,
                "place_id": place_id,
                "name": game.get("Name") or game.get("name"),
                "creator": game.get("Creator", {}).get("Name") if isinstance(game.get("Creator"), dict) else game.get("creatorName"),
                "type": "game",
                "image_url": thumb_data.get(str(universe_id)),
                "url": f"https://www.roblox.com/games/{place_id}" if place_id else None,
            })
        result = {"success": True, "kind": kind, "query": q, "results": results}

    elif kind in ("icons", "assets"):
        # Marketplace catalog search. IconFinder's five Lua icon libraries
        # are loaded in the LocalScript and are not scraped by this endpoint.
        data = await roblox_get(
            "https://catalog.roblox.com/v1/search/items/details",
            {
                "Category": 1,
                "Keyword": q,
                "Limit": min(limit, 30),
                "SortType": 0,
            },
        )
        rows = data.get("data", [])
        results = []
        for row in rows:
            results.append({
                "id": row.get("id"),
                "name": row.get("name"),
                "type": "asset",
                "creator": (row.get("creator") or {}).get("name"),
                "price": row.get("price"),
                "image_url": None,
                "url": f"https://www.roblox.com/catalog/{row.get('id')}" if row.get("id") else None,
            })
        results = await add_asset_thumbnails(results)
        result = {"success": True, "kind": kind, "query": q, "results": results}

    else:  # decals
        if not q.isdigit():
            raise HTTPException(
                400,
                "Para Decals, informe o ID público do asset. A busca por palavra-chave de Decals não está disponível neste endpoint público.",
            )
        return await roblox_asset(int(q))

    _cache_set(cache_key, result)
    return result


@app.get("/api/roblox/asset/{asset_id}")
async def roblox_asset(asset_id: int):
    if asset_id < 1:
        raise HTTPException(400, "ID de asset inválido.")

    cache_key = f"asset:{asset_id}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    thumb_data = await roblox_get(
        "https://thumbnails.roblox.com/v1/assets",
        {
            "assetIds": str(asset_id),
            "returnPolicy": "PlaceHolder",
            "size": "420x420",
            "format": "Png",
            "isCircular": "false",
        },
    )
    rows = thumb_data.get("data", [])
    image_url = rows[0].get("imageUrl") if rows else None

    # Public metadata endpoint; it may reject some asset types.
    metadata = None
    try:
        metadata = await roblox_get(
            f"https://economy.roblox.com/v2/assets/{asset_id}/details"
        )
    except HTTPException:
        metadata = None

    result = {
        "success": True,
        "kind": "asset",
        "id": asset_id,
        "name": metadata.get("Name") if isinstance(metadata, dict) else None,
        "asset_type_id": metadata.get("AssetTypeId") if isinstance(metadata, dict) else None,
        "image_url": image_url,
        "url": f"https://www.roblox.com/library/{asset_id}",
    }
    _cache_set(cache_key, result)
    return result


@app.post("/api/chat")
async def gemini_chat(payload: ChatPayload):
    if not GEMINI_API_KEY:
        raise HTTPException(
            503,
            "Gemini não configurado. Adicione GEMINI_API_KEY nas variáveis de ambiente do Render.",
        )

    prompt = payload.message.strip()
    if not prompt:
        raise HTTPException(400, "Mensagem vazia.")

    contents = []
    if payload.context.strip():
        contents.append({
            "role": "user",
            "parts": [{"text": "Contexto de depuração da própria experiência:\n" + payload.context[:12000]}],
        })
    contents.append({"role": "user", "parts": [{"text": prompt}]})

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    )
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            response = await client.post(
                url,
                json={
                    "contents": contents,
                    "generationConfig": {
                        "temperature": 0.4,
                        "maxOutputTokens": 1200,
                    },
                },
            )
        if response.status_code >= 400:
            raise HTTPException(502, f"Gemini respondeu HTTP {response.status_code}.")
        data = response.json()
        candidates = data.get("candidates") or []
        parts = (
            candidates[0].get("content", {}).get("parts", [])
            if candidates else []
        )
        reply = "\n".join(
            part.get("text", "") for part in parts if isinstance(part, dict)
        ).strip()
        if not reply:
            raise HTTPException(502, "Gemini não retornou texto.")
        return {"success": True, "Success": True, "reply": reply, "model": GEMINI_MODEL}
    except httpx.TimeoutException:
        raise HTTPException(504, "Tempo limite ao consultar Gemini.")
    except httpx.RequestError as exc:
        raise HTTPException(502, f"Falha de conexão com Gemini: {str(exc)[:200]}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
