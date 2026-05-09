from fastapi import APIRouter, HTTPException
from services.firebase_service import get_db
from services.aneel_service import buscar_distribuidoras
from datetime import datetime

router = APIRouter()


@router.get("/")
async def listar_distribuidoras():
    """Retorna lista de distribuidoras do cache no Firestore."""
    try:
        db = get_db()
        docs = db.collection("distribuidoras")\
                 .where("ativo", "==", True)\
                 .order_by("sigla")\
                 .stream()

        distribuidoras = [doc.to_dict() for doc in docs]

        if not distribuidoras:
            raise HTTPException(
                status_code=404,
                detail="Nenhuma distribuidora encontrada. Aguarde a sincronização."
            )

        return {
            "total": len(distribuidoras),
            "distribuidoras": distribuidoras,
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/sincronizar")
async def sincronizar_distribuidoras():
    """Força sincronização manual com a API da ANEEL."""
    try:
        distribuidoras = await buscar_distribuidoras()
        if not distribuidoras:
            raise HTTPException(
                status_code=503,
                detail="Não foi possível buscar distribuidoras na ANEEL."
            )

        db = get_db()
        batch = db.batch()

        for d in distribuidoras:
            ref = db.collection("distribuidoras").document(d["sigla"])
            batch.set(ref, {
                **d,
                "atualizadoEm": datetime.now(),
            })

        batch.commit()

        return {
            "mensagem": f"{len(distribuidoras)} distribuidoras sincronizadas com sucesso!",
            "total": len(distribuidoras),
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))