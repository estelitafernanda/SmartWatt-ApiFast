from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from dotenv import load_dotenv
import os

from services.firebase_service import inicializar_firebase
from services.scheduler import iniciar_agendador
from services.auth import verificar_api_key
from routes import distribuidoras, leituras, previsao

load_dotenv()

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("Iniciando SmartWatt Backend...")

    inicializar_firebase()
    print("Firebase conectado!")

    iniciar_agendador()
    print("Agendador iniciado!")

    yield

    print("Encerrando SmartWatt Backend...")


app = FastAPI(
    title="SmartWatt API",
    description="Backend IoT para monitoramento inteligente de consumo de energia residencial.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(
    distribuidoras.router,
    prefix="/distribuidoras",
    tags=["Distribuidoras"],
)
app.include_router(
    leituras.router,
    prefix="/leitura",
    tags=["Leituras"],
    dependencies=[Depends(verificar_api_key)],  
)
app.include_router(
    previsao.router,
    prefix="/previsao",
    tags=["Previsão"],
)

@app.api_route("/", methods=["GET", "HEAD"], tags=["Status"])
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


@app.api_route("/health", methods=["GET", "HEAD"], tags=["Status"])
async def health():
    return {"status": "healthy"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", 8000)),
        reload=os.getenv("RENDER") is None,
)