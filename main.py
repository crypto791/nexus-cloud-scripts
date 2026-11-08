
import os
import json
import httpx

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

app = FastAPI(title="Action Recorder AI")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")

latest_record = {
    "category": "Nenhuma",
    "events": []
}

PAGE = r"""
<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Action Recorder AI</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:#090b12;color:#e9eaf2;font-family:Arial,sans-serif}
header{padding:20px;background:#111522;border-bottom:1px solid #252b3b}
h1{margin:0;font-size:22px;color:#ff4545}
header p{color:#9299ad;font-size:13px;margin-bottom:0}
main{max-width:950px;margin:25px auto;padding:0 15px}
.tabs{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:15px}
button{background:#1b2030;color:#ddd;border:1px solid #30384c;padding:11px 15px;border-radius:8px;cursor:pointer}
button.active{background:#8d252c;border-color:#ff4545;color:white}
button:hover{filter:brightness(1.2)}
.panel{background:#111522;border:1px solid #252b3b;border-radius:12px;padding:17px;margin-bottom:15px}
h2{font-size:16px;margin-top:0}
textarea{width:100%;min-height:230px;resize:vertical;background:#090d16;color:#dce3f5;border:1px solid #30384c;border-radius:8px;padding:13px;font-family:monospace;font-size:13px}
input{width:100%;padding:12px;background:#090d16;color:white;border:1px solid #30384c;border-radius:8px;margin:8px 0}
.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
.primary{background:#a52c35;border-color:#d84952}
#status{font-size:13px;color:#aab2c7;margin-top:12px}
#result{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.5}
.small{color:#8992a8;font-size:12px}
</style>
</head>
<body>
<header>
<h1>ACTION RECORDER AI</h1>
<p>Registro de eventos Roblox + análise com Gemini</p>
</header>

<main>
<div class="tabs">
<button class="active" onclick="tab('PlayerGui',this)">PlayerGui</button>
<button onclick="tab('Workspace',this)">Workspace</button>
<button onclick="tab('Character',this)">Character</button>
<button onclick="tab('Input',this)">Input</button>
<button onclick="tab('Todos',this)">Todos</button>
</div>

<section class="panel">
<h2>Registros capturados</h2>
<p class="small">Cole aqui os registros do seu Action Recorder ou receba-os pela API.</p>
<textarea id="logs" placeholder="Os registros aparecerão aqui..."></textarea>
<div class="actions">
<button class="primary" onclick="analyze()">Analisar com Gemini</button>
<button onclick="copyLogs()">Copiar registros</button>
<button onclick="loadLatest()">Carregar últimos registros</button>
<button onclick="clearLogs()">Limpar</button>
</div>
<div id="status">Aguardando registros.</div>
</section>

<section class="panel">
<h2>Resultado da análise</h2>
<div id="result">A análise aparecerá aqui.</div>
<div class="actions">
<button onclick="copyResult()">Copiar resultado</button>
</div>
</section>

<section class="panel">
<h2>Como conectar o Recorder</h2>
<p class="small">
O endpoint para enviar registros é:
</p>
<input id="endpoint" readonly>
<p class="small">
Envie um POST com JSON contendo "category" e "events".
A integração automática deve ser feita pelo servidor da sua própria
experiência Roblox Studio.
</p>
</section>
</main>

<script>
let currentTab = "PlayerGui";

document.getElementById("endpoint").value =
location.origin + "/api/record";

function tab(name, button){
    currentTab = name;
    document.querySelectorAll(".tabs button").forEach(b =>
        b.classList.remove("active")
    );
    button.classList.add("active");
    document.getElementById("status").textContent =
        "Categoria selecionada: " + name;
}

async function analyze(){
    const logs = document.getElementById("logs").value.trim();

    if(!logs){
        alert("Adicione registros primeiro.");
        return;
    }

    const status = document.getElementById("status");
    const result = document.getElementById("result");

    status.textContent = "Analisando com Gemini...";
    result.textContent = "Aguarde...";

    try{
        const response = await fetch("/api/analyze",{
            method:"POST",
            headers:{"Content-Type":"application/json"},
            body:JSON.stringify({
                category:currentTab,
                events:logs
            })
        });

        const data = await response.json();

        if(!response.ok){
            throw new Error(data.detail || "Erro na análise");
        }

        result.textContent = data.result;
        status.textContent = "Análise concluída.";
    }catch(error){
        status.textContent = "Erro: " + error.message;
        result.textContent = "Não foi possível analisar os registros.";
    }
}

async function loadLatest(){
    const status = document.getElementById("status");

    try{
        const response = await fetch("/api/session/latest");
        const data = await response.json();

        if(!data.events || data.events.length === 0){
            status.textContent = "Nenhum registro recebido ainda.";
            return;
        }

        document.getElementById("logs").value =
            JSON.stringify(data.events,null,2);

        currentTab = data.category || "Todos";
        status.textContent = "Últimos registros carregados.";
    }catch(error){
        status.textContent = "Erro ao carregar registros.";
    }
}

function copyLogs(){
    navigator.clipboard.writeText(
        document.getElementById("logs").value
    );
}

function copyResult(){
    navigator.clipboard.writeText(
        document.getElementById("result").innerText
    );
}

function clearLogs(){
    document.getElementById("logs").value = "";
    document.getElementById("result").textContent =
        "A análise aparecerá aqui.";
    document.getElementById("status").textContent =
        "Registros limpos.";
}
</script>
</body>
</html>
"""


class AnalyzeRequest(BaseModel):
    category: str = "Todos"
    events: str


class RecordRequest(BaseModel):
    category: str = "Todos"
    events: list


@app.get("/", response_class=HTMLResponse)
async def home():
    return PAGE


@app.get("/api/health")
async def health():
    return {
        "status": "online",
        "gemini_configured": bool(GEMINI_API_KEY)
    }


@app.post("/api/record")
async def receive_record(data: RecordRequest):
    global latest_record

    if len(json.dumps(data.events, ensure_ascii=False)) > 200000:
        raise HTTPException(413, "Registro muito grande.")

    latest_record = {
        "category": data.category[:40],
        "events": data.events[-2000:]
    }

    return {
        "success": True,
        "received": len(latest_record["events"])
    }


@app.get("/api/session/latest")
async def get_latest():
    return latest_record


@app.post("/api/analyze")
async def analyze(data: AnalyzeRequest):
    if not GEMINI_API_KEY:
        raise HTTPException(
            500,
            "Configure GEMINI_API_KEY nas variáveis de ambiente."
        )

    if len(data.events) > 100000:
        raise HTTPException(413, "Texto muito grande. Limite: 100 KB.")

    prompt = f"""
Você é um assistente especializado em análise de registros
de interfaces e eventos de Roblox Luau.

Categoria: {data.category}

REGISTROS:
{data.events}

Faça uma análise organizada:

1. Explique o que aconteceu.
2. Identifique os objetos e caminhos envolvidos.
3. Organize os eventos em ordem.
4. Diferencie fatos observados de hipóteses.
5. Se possível, apresente um exemplo Luau seguro
   para reproduzir os eventos em uma experiência própria.
6. Não invente propriedades, objetos ou funções que
   não estejam nos registros.

Não afirme que descobriu o código interno de scripts.
Os registros representam apenas eventos observados.
"""

    url = (
        "https://generativelanguage.googleapis.com/"
        f"v1beta/models/{GEMINI_MODEL}:generateContent"
    )

    payload = {
        "contents": [{
            "parts": [{"text": prompt}]
        }]
    }

    try:
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post(
                url,
                params={"key": GEMINI_API_KEY},
                json=payload
            )

        if response.status_code != 200:
            raise HTTPException(
                response.status_code,
                "Erro retornado pela API Gemini: " +
                response.text[:1000]
            )

        result_data = response.json()

        candidates = result_data.get("candidates", [])
        if not candidates:
            raise HTTPException(502, "Gemini não retornou uma resposta.")

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

        return {"result": answer}

    except httpx.TimeoutException:
        raise HTTPException(504, "Gemini demorou demais para responder.")
    except httpx.RequestError:
        raise HTTPException(502, "Não foi possível conectar ao Gemini.")
