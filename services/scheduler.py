from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from services.aneel_service import buscar_distribuidoras
from services.firebase_service import get_db
from datetime import datetime, timedelta, timezone
from google.cloud.firestore import Query

scheduler = AsyncIOScheduler()


def _sincronizado_recentemente(dias: int = 7) -> bool:
    """No plano gratuito o Render reinicia a API várias vezes por dia; sem esta
    checagem, cada reinício regravaria todas as distribuidoras."""
    try:
        docs = list(
            get_db().collection("distribuidoras")
                .order_by("atualizadoEm", direction=Query.DESCENDING)
                .limit(1)
                .stream()
        )
        if not docs:
            return False
        ultima = docs[0].to_dict().get("atualizadoEm")
        if not ultima:
            return False
        if ultima.tzinfo is None:
            ultima = ultima.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - ultima < timedelta(days=dias)
    except Exception as e:
        print(f"Não foi possível verificar a última sincronização: {e}")
        return False


async def atualizar_distribuidoras_se_necessario():
    if _sincronizado_recentemente():
        print("Distribuidoras sincronizadas há menos de 7 dias; pulando.")
        return
    await atualizar_distribuidoras()


async def atualizar_distribuidoras():
    """Busca distribuidoras na ANEEL e salva no Firestore."""
    print("Atualizando distribuidoras da ANEEL...")
    try:
        distribuidoras = await buscar_distribuidoras()
        if not distribuidoras:
            print("Nenhuma distribuidora encontrada.")
            return

        db = get_db()
        batch = db.batch()

        for d in distribuidoras:
            ref = db.collection("distribuidoras").document(d["sigla"])
            batch.set(ref, {
                **d,
                "atualizadoEm": datetime.now(timezone.utc),
            })

        batch.commit()
        print(f"{len(distribuidoras)} distribuidoras salvas no Firestore!")

    except Exception as e:
        print(f"Erro ao atualizar distribuidoras: {e}")


def iniciar_agendador():
    """Inicia o agendador para atualizar distribuidoras semanalmente."""

    # Roda toda segunda-feira às 3h da manhã
    scheduler.add_job(
        atualizar_distribuidoras,
        trigger=CronTrigger(day_of_week="mon", hour=3, minute=0),
        id="atualizar_distribuidoras",
        replace_existing=True,
    )

    # Roda também na inicialização para garantir dados frescos
    scheduler.add_job(
        atualizar_distribuidoras_se_necessario,
        id="atualizar_distribuidoras_inicio",
        replace_existing=True,
    )

    scheduler.start()
    print("Agendador configurado — distribuidoras atualizam toda segunda às 3h.")