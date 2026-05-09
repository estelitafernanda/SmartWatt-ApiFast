from fastapi import APIRouter, HTTPException
from datetime import datetime, date, timedelta
from services.firebase_service import get_db
import calendar

router = APIRouter()


@router.get("/{uid}")
async def obter_previsao(uid: str):
    """
    Calcula previsão de gasto mensal com base no acumulado
    de cada dispositivo do usuário nos últimos dias.
    """
    db = get_db()

    try:
        # ── 1. Busca dispositivos do usuário ───────────────────
        dispositivos = (
            db.collection("dispositivos")
              .where("uidUsuario", "==", uid)
              .where("ativo", "==", True)
              .stream()
        )

        hoje = date.today()
        inicio_mes = date(hoje.year, hoje.month, 1)
        dias_passados = (hoje - inicio_mes).days + 1
        dias_no_mes = calendar.monthrange(hoje.year, hoje.month)[1]
        dias_restantes = dias_no_mes - dias_passados

        resultado = []
        total_mes_atual = 0.0
        total_previsao = 0.0
        total_kwh_mes = 0.0

        for doc in dispositivos:
            dispositivo = doc.to_dict()
            sensor_id = dispositivo.get("sensorId", "")
            nome = dispositivo.get("nome", "Dispositivo")

            if not sensor_id:
                continue

            # ── 2. Busca acumulados do mês atual ────────────────
            acumulados = (
                db.collection("leituras")
                  .document(sensor_id)
                  .collection("acumulado")
                  .where("data", ">=", inicio_mes.isoformat())
                  .where("data", "<=", hoje.isoformat())
                  .stream()
            )

            custo_mes = 0.0
            kwh_mes = 0.0
            dias_com_dados = 0

            for ac in acumulados:
                d = ac.to_dict()
                custo_mes += d.get("custoTotal", 0)
                kwh_mes += d.get("kwhTotal", 0)
                dias_com_dados += 1

            # ── 3. Calcula média diária e previsão ───────────────
            if dias_com_dados > 0:
                media_diaria = custo_mes / dias_com_dados
                kwh_media_diaria = kwh_mes / dias_com_dados
            else:
                media_diaria = 0
                kwh_media_diaria = 0

            previsao_dispositivo = custo_mes + (media_diaria * dias_restantes)
            previsao_kwh = kwh_mes + (kwh_media_diaria * dias_restantes)

            total_mes_atual += custo_mes
            total_previsao += previsao_dispositivo
            total_kwh_mes += kwh_mes

            resultado.append({
                "sensorId": sensor_id,
                "nome": nome,
                "custoMesAtual": round(custo_mes, 2),
                "mediaDiaria": round(media_diaria, 4),
                "previsaoMensal": round(previsao_dispositivo, 2),
                "kwhMes": round(kwh_mes, 3),
                "previsaoKwh": round(previsao_kwh, 3),
                "diasComDados": dias_com_dados,
            })

        # ── 4. Busca gasto do dia atual (todos os dispositivos) ─
        custo_hoje = 0.0
        for item in resultado:
            acumulado_hoje = (
                db.collection("leituras")
                  .document(item["sensorId"])
                  .collection("acumulado")
                  .document(hoje.isoformat())
                  .get()
            )
            if acumulado_hoje.exists:
                custo_hoje += acumulado_hoje.to_dict().get("custoTotal", 0)

        return {
            "uid": uid,
            "mes": hoje.strftime("%B/%Y"),
            "diasPassados": dias_passados,
            "diasNoMes": dias_no_mes,
            "diasRestantes": dias_restantes,
            "resumo": {
                "custoMesAtual": round(total_mes_atual, 2),
                "custoHoje": round(custo_hoje, 2),
                "previsaoMensal": round(total_previsao, 2),
                "kwhMes": round(total_kwh_mes, 3),
            },
            "porDispositivo": resultado,
        }

    except Exception as e:
        print(f"❌ Erro ao calcular previsão: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/hoje/{uid}")
async def gasto_hoje(uid: str):
    """Retorna gasto acumulado de hoje por dispositivo."""
    db = get_db()
    hoje = date.today().isoformat()

    try:
        dispositivos = (
            db.collection("dispositivos")
              .where("uidUsuario", "==", uid)
              .where("ativo", "==", True)
              .stream()
        )

        resultado = []
        total = 0.0

        for doc in dispositivos:
            d = doc.to_dict()
            sensor_id = d.get("sensorId", "")

            acumulado = (
                db.collection("leituras")
                  .document(sensor_id)
                  .collection("acumulado")
                  .document(hoje)
                  .get()
            )

            custo = 0.0
            kwh = 0.0
            if acumulado.exists:
                dados = acumulado.to_dict()
                custo = dados.get("custoTotal", 0)
                kwh = dados.get("kwhTotal", 0)

            total += custo
            resultado.append({
                "sensorId": sensor_id,
                "nome": d.get("nome", "Dispositivo"),
                "custoHoje": round(custo, 2),
                "kwhHoje": round(kwh, 3),
            })

        return {
            "data": hoje,
            "custoTotal": round(total, 2),
            "dispositivos": resultado,
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))