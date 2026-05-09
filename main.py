from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from dotenv import load_dotenv
import os

from services.firebase_service import inicializar_firebase
from services.scheduler import iniciar_agendador
from routes import distribuidoras, leituras, previsao

load_dotenv()

# ─── Inicialização ─────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🚀 Iniciando SmartWatt Backend...")

    # Inicializa Firebase
    inicializar_firebase()
    print("🔥 Firebase conectado!")

    # Inicia agendador (atualiza distribuidoras semanalmente)
    iniciar_agendador()
    print("⏰ Agendador iniciado!")

    yield

    print("🛑 Encerrando SmartWatt Backend...")


# ─── App ───────────────────────────────────────────────────────────
app = FastAPI(
    title="SmartWatt API",
    description="Backend IoT para monitoramento inteligente de consumo de energia residencial.",
    version="1.0.0",
    lifespan=lifespan,
)

# ─── CORS ──────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Rotas ─────────────────────────────────────────────────────────
app.include_router(
    distribuidoras.router,
    prefix="/distribuidoras",
    tags=["Distribuidoras"],
)
app.include_router(
    leituras.router,
    prefix="/leitura",
    tags=["Leituras"],
)
app.include_router(
    previsao.router,
    prefix="/previsao",
    tags=["Previsão"],
)

# ─── Rota raiz ─────────────────────────────────────────────────────
@app.get("/", tags=["Status"])
async def raiz():
    return {
        "status": "online",
        "sistema": "SmartWatt API",
        "versao": "1.0.0",
        "descricao": "Plataforma IoT para monitoramento inteligente de consumo de energia residencial.",
        "rotas": {
            "distribuidoras": "/distribuidoras",
            "leitura": "/leitura",
            "previsao": "/previsao",
            "documentacao": "/docs",
        },
    }


# ─── Rota de saúde ─────────────────────────────────────────────────
@app.get("/health", tags=["Status"])
async def health():
    return {"status": "healthy"}


# ─── Rodar localmente ──────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", 8000)),
        reload=True,
)