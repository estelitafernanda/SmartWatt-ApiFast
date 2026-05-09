from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from services.aneel_service import buscar_distribuidoras
from services.firebase_service import get_db
from datetime import datetime

scheduler = AsyncIOScheduler()


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
                "atualizadoEm": datetime.now(),
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
        atualizar_distribuidoras,
        id="atualizar_distribuidoras_inicio",
        replace_existing=True,
    )

    scheduler.start()
    print("Agendador configurado — distribuidoras atualizam toda segunda às 3h.")