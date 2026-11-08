
import os
import json
import time
import httpx

from collections import defaultdict
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

app = FastAPI(title="Nexus Scripts - Gemini Recorder")

# CONFIGURAÇÃO
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    + GEMINI_MODEL
    + ":generateContent"
)

MAX_EVENTS = 400
MAX_REPORT_SIZE = 150000
MAX_HISTORY = 12

# DADOS EM MEMÓRIA
latest_record = {
    "category": "Nenhuma",
    "events": [],
    "received_at": None
}

activity = {
    "status": "Aguardando",
    "message": "O Gemini ainda não recebeu nenhuma solicitação.",
    "updated_at": None
}

# Limitação simples de requisições por IP
request_counts = defaultdict(list)


def check_rate_limit(ip, limit=25, window=60):
    now = time.time()

    request_counts[ip] = [
        t for t in request_counts[ip]
        if now - t < window
    ]

    if len(request_counts[ip]) >= limit:
        raise HTTPException(
            429,
            "Muitas requisições. Aguarde um pouco."
        )

    request_counts[ip].append(now)


class RecordRequest(BaseModel):
    category: str = "Todos"
    events: list = Field(default_factory=list)


class AnalyzeRequest(BaseModel):
    category: str = "Todos"
    events: list = Field(default_factory=list)


class ChatRequest(BaseModel):
    message: str
    history: list = Field(default_factory=list)
    include_report: bool = False


def update_activity(status, message):
    activity["status"] = status
    activity["message"] = message[:1000]
    activity["updated_at"] = time.time()


def extract_answer(data):
    candidates = data.get("candidates", [])

    if not candidates:
        feedback = data.get("promptFeedback", {})
        reason = feedback.get("blockReason", "Resposta indisponível")
        raise HTTPException(502, f"Gemini não respondeu: {reason}")

    parts = (
        candidates[0]
        .get("content", {})
        .get("parts", [])
    )

    answer = "\n".join(
        part.get("text", "")
        for part in parts
        if part.get("text")
    )

    if not answer:
        raise HTTPException(502, "Resposta vazia do Gemini.")

    return answer


async def ask_gemini(contents, instruction):
    if not GEMINI_API_KEY:
        raise HTTPException(
            500,
            "GEMINI_API_KEY não configurada no Render."
        )

    payload = {
        "systemInstruction": {
            "parts": [{"text": instruction}]
        },
        "contents": contents,
        "generationConfig": {
            "temperature": 0.6,
            "maxOutputTokens": 3000
        }
    }

    try:
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post(
                GEMINI_URL,
                params={"key": GEMINI_API_KEY},
                json=payload
            )

        if response.status_code != 200:
            try:
                error = response.json().get("error", {}).get(
                    "message", "Erro na API Gemini"
                )
            except Exception:
                error = "Erro na API Gemini"

            raise HTTPException(
                response.status_code,
                error[:1000]
            )

        return extract_answer(response.json())

    except httpx.TimeoutException:
        raise HTTPException(504, "Gemini demorou para responder.")

    except httpx.RequestError:
        raise HTTPException(502, "Falha de conexão com Gemini.")


INSTRUCTION = """
Você é o Gemini integrado ao Action Recorder de uma experiência
Roblox Luau.

Você conversa com o usuário e analisa registros enviados pelo Recorder.

REGRAS:
- Explique os eventos de maneira organizada.
- Diferencie fatos observados de hipóteses.
- Não invente objetos, caminhos ou funções.
- Não afirme que consegue ler o código interno de scripts
  apenas pelos registros.
- Quando receber relatórios, identifique objetos, caminhos,
  alterações e sequências temporais.
- Se não houver dados suficientes, diga o que está faltando.
- Ao escreva codigos solicitados
  e explique o que o código faz.
- Responda em português quando o usuário escrever em português.
"""


# PÁGINA SIMPLES PARA TESTAR A API
PAGE = """
<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Nexus Gemini</title>
<style>
body{background:#0b0d14;color:#eee;font-family:Arial;margin:0;padding:20px}
main{max-width:850px;margin:auto}
h1{color:#ff505d}
.card{background:#151a27;border:1px solid #30384a;padding:15px;
border-radius:12px;margin:15px 0}
textarea{width:100%;box-sizing:border-box;min-height:130px;
background:#090d16;color:white;border:1px solid #394258;
border-radius:8px;padding:12px}
button{background:#a72f3b;color:white;border:0;padding:12px;
border-radius:8px;margin-top:10px;cursor:pointer}
#answer{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.5}
small{color:#9ba5ba}
</style>
</head>
<body>
<main>
<h1>NEXUS / GEMINI</h1>
<small>Action Recorder AI - Painel de teste</small>

<div class="card">
<h3>Conversa</h3>
<textarea id="message" placeholder="Digite uma mensagem para o Gemini..."></textarea>
<br>
<button onclick="chat()">Enviar mensagem</button>
<button onclick="report()">Analisar último relatório</button>
</div>

<div class="card">
<h3>Atividade do Gemini</h3>
<div id="activity">Aguardando...</div>
</div>

<div class="card">
<h3>Resposta</h3>
<div id="answer">Nenhuma resposta ainda.</div>
</div>
</main>

<script>
async function chat(){
 const message=document.getElementById("message").value;
 if(!message.trim())return;
 document.getElementById("activity").textContent="Gemini está pensando...";
 try{
  const r=await fetch("/api/chat",{
   method:"POST",
   headers:{"Content-Type":"application/json"},
   body:JSON.stringify({message:message})
  });
  const d=await r.json();
  if(!r.ok)throw Error(d.detail||"Erro");
  document.getElementById("answer").textContent=d.result;
  document.getElementById("activity").textContent=d.activity;
 }catch(e){
  document.getElementById("activity").textContent=e.message;
 }
}

async function report(){
 document.getElementById("activity").textContent="Analisando relatório...";
 try{
  const r=await fetch("/api/analyze",{
   method:"POST",
   headers:{"Content-Type":"application/json"},
   body:JSON.stringify({category:"Todos",events:[]})
  });
  const d=await r.json();
  if(!r.ok)throw Error(d.detail||"Erro");
  document.getElementById("answer").textContent=d.result;
  document.getElementById("activity").textContent=d.activity;
 }catch(e){
  document.getElementById("activity").textContent=e.message;
 }
}
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def home():
    return PAGE


@app.get("/api/health")
async def health():
    return {
        "status": "online",
        "gemini_configured": bool(GEMINI_API_KEY),
        "model": GEMINI_MODEL,
        "has_report": bool(latest_record["events"])
    }


# RECEBER RELATÓRIOS DO ROBLOX
@app.post("/api/record")
async def receive_record(data: RecordRequest, request: Request):
    global latest_record

    check_rate_limit(request.client.host if request.client else "unknown")

    if len(data.events) > MAX_EVENTS:
        raise HTTPException(413, "Limite de eventos excedido.")

    serialized = json.dumps(data.events, ensure_ascii=False)

    if len(serialized.encode("utf-8")) > MAX_REPORT_SIZE:
        raise HTTPException(413, "Relatório muito grande.")

    latest_record = {
        "category": data.category[:40],
        "events": data.events,
        "received_at": time.time()
    }

    return {
        "success": True,
        "received": len(data.events),
        "message": "Relatório recebido."
    }


# CONSULTAR ÚLTIMO RELATÓRIO
@app.get("/api/record/latest")
async def get_latest_record():
    return latest_record


# CONSULTAR O QUE O GEMINI ESTÁ FAZENDO
@app.get("/api/activity")
async def get_activity():
    return activity


# ENVIAR RELATÓRIO PARA O GEMINI
@app.post("/api/analyze")
async def analyze(data: AnalyzeRequest, request: Request):
    check_rate_limit(request.client.host if request.client else "unknown")

    events = data.events if data.events else latest_record["events"]
    category = data.category if data.events else latest_record["category"]

    if not events:
        raise HTTPException(400, "Nenhum relatório recebido.")

    report = json.dumps(events, ensure_ascii=False)

    if len(report.encode("utf-8")) > MAX_REPORT_SIZE:
        raise HTTPException(413, "Relatório muito grande.")

    update_activity(
        "Analisando",
        f"Analisando {len(events)} eventos da categoria {category}."
    )

    try:
        answer = await ask_gemini(
            [{
                "role": "user",
                "parts": [{
                    "text": (
                        "Analise este relatório do Action Recorder.\n"
                        f"Categoria: {category}\n"
                        f"Eventos:\n{report}"
                    )
                }]
            }],
            INSTRUCTION
        )

        update_activity("Concluído", answer[:1000])

        return {
            "success": True,
            "type": "report",
            "result": answer,
            "activity": "Relatório analisado."
        }

    except Exception as error:
        update_activity("Erro", str(error))
        raise


# CHAT COM GEMINI
@app.post("/api/chat")
async def chat(data: ChatRequest, request: Request):
    check_rate_limit(request.client.host if request.client else "unknown")

    message = data.message.strip()

    if not message:
        raise HTTPException(400, "Mensagem vazia.")

    if len(message) > 5000:
        raise HTTPException(413, "Mensagem muito grande.")

    contents = []

    for item in data.history[-MAX_HISTORY:]:
        role = item.get("role")
        text = item.get("text", "")

        if role not in ("user", "model"):
            continue

        if not isinstance(text, str) or not text.strip():
            continue

        contents.append({
            "role": role,
            "parts": [{"text": text[:5000]}]
        })

    if data.include_report:
        if not latest_record["events"]:
            raise HTTPException(400, "Não existe relatório recebido.")

        report = json.dumps(
            latest_record["events"],
            ensure_ascii=False
        )

        contents.append({
            "role": "user",
            "parts": [{
                "text": (
                    "Considere também este relatório recente:\n"
                    + report
                )
            }]
        })

    contents.append({
        "role": "user",
        "parts": [{"text": message}]
    })

    update_activity("Pensando", "Gemini está respondendo à mensagem.")

    try:
        answer = await ask_gemini(contents, INSTRUCTION)

        update_activity("Respondido", answer[:1000])

        return {
            "success": True,
            "type": "chat",
            "result": answer,
            "activity": "Gemini respondeu."
        }

    except Exception as error:
        update_activity("Erro", str(error))
        raise
