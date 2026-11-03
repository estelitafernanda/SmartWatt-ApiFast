import os
import secrets

from fastapi import HTTPException, Security, status
from fastapi.security import APIKeyHeader

API_KEY_HEADER = APIKeyHeader(
    name="X-API-Key",
    description="Chave do dispositivo (variável de ambiente SMARTWATT_API_KEY).",
    auto_error=False,
)


async def verificar_api_key(api_key: str | None = Security(API_KEY_HEADER)) -> None:
    """Libera a requisição só quando o header X-API-Key bate com SMARTWATT_API_KEY."""
    chave_esperada = os.getenv("SMARTWATT_API_KEY")

    if not chave_esperada:
        print("[AUTH] SMARTWATT_API_KEY não configurada no servidor.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Autenticação não configurada no servidor.",
        )

    # compare_digest evita ataques de tempo na comparação da chave.
    if not api_key or not secrets.compare_digest(api_key, chave_esperada):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API Key inválida ou ausente.",
        )


from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from firebase_admin import auth as firebase_auth

from services.firebase_service import get_db

BEARER = HTTPBearer(
    description="ID token do Firebase do usuário logado (getIdToken() no app).",
    auto_error=False,
)


async def usuario_autenticado(
    credenciais: HTTPAuthorizationCredentials | None = Security(BEARER),
) -> str:
    """Valida o ID token do Firebase e devolve o uid do usuário logado."""
    if credenciais is None or not credenciais.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token de autenticação ausente.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    get_db()  
    
    try:
        token = firebase_auth.verify_id_token(credenciais.credentials)
    except Exception as e:
        print(f"[AUTH] Token do Firebase inválido: {type(e).__name__}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token inválido ou expirado. Faça login novamente.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return token["uid"]
