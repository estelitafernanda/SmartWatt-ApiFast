from fastapi import APIRouter, Depends, HTTPException
from datetime import datetime, date, timedelta, timezone
from services.firebase_service import get_db
from services.auth import usuario_autenticado
import calendar

router = APIRouter()

FUSO_BR = timezone(timedelta(hours=-3))

JANELA_MEDIA_DIAS = 7


def _inicio_monitoramento(db, dispositivo: dict, sensor_id: str) -> date | None:
    """Data em que o dispositivo começou a ser monitorado (cadastro ou primeiro acumulado)."""
    candidatos = []

    cadastro = dispositivo.get("dataCadastro")
    if cadastro:
        try:
            candidatos.append(cadastro.astimezone(FUSO_BR).date())
        except (AttributeError, ValueError):
            pass

    primeiro = (
        db.collection("leituras")
          .document(sensor_id)
          .collection("acumulado")
          .order_by("data")
          .limit(1)
          .stream()
    )
    for ac in primeiro:
        data = ac.to_dict().get("data") or ac.id
        try:
            candidatos.append(date.fromisoformat(data))
        except (TypeError, ValueError):
            pass

    return min(candidatos) if candidatos else None


def _media_ultimos_dias(db, sensor_id: str, hoje: date, dias: int) -> tuple[float, float]:
    """Média diária de custo e kWh dos últimos `dias` dias completos (sem contar hoje).

    Dias sem registro entram como zero (aparelho desligado).
    """
    inicio = hoje - timedelta(days=dias)
    fim = hoje - timedelta(days=1)
    acumulados = (
        db.collection("leituras")
          .document(sensor_id)
          .collection("acumulado")
          .where("data", ">=", inicio.isoformat())
          .where("data", "<=", fim.isoformat())
          .stream()
    )
    custo = 0.0
    kwh = 0.0
    for ac in acumulados:
        d = ac.to_dict()
        custo += d.get("custoTotal", 0)
        kwh += d.get("kwhTotal", 0)
    return custo / dias, kwh / dias


def _dias_monitorados(dispositivo: dict, primeiro_dia: str | None, inicio_mes: date, hoje: date) -> int:
    """Dias (incluindo hoje) desde que o dispositivo começou a ser monitorado no mês.

    O início é o dia do cadastro (ou o 1º dia do mês, se foi cadastrado antes) ou
    o primeiro dia com consumo registrado, o que vier primeiro.
    """
    candidatos = []

    cadastro = dispositivo.get("dataCadastro")
    if cadastro:
        try:
            candidatos.append(max(inicio_mes, cadastro.astimezone(FUSO_BR).date()))
        except (AttributeError, ValueError):
            pass

    if primeiro_dia:
        try:
            candidatos.append(date.fromisoformat(primeiro_dia))
        except ValueError:
            pass

    if not candidatos:
        return 0

    inicio = min(candidatos)
    return max(1, (hoje - inicio).days + 1)


@router.get("/{uid}")
async def obter_previsao(uid: str, uid_logado: str = Depends(usuario_autenticado)):
    """
    Calcula previsão de gasto mensal com base no acumulado
    de cada dispositivo do usuário nos últimos dias.
    """
    if uid != uid_logado:
        raise HTTPException(
            status_code=403,
            detail="Você não tem permissão para ver a previsão de outro usuário.",
        )

    db = get_db()

    try:
        dispositivos = (
            db.collection("dispositivos")
              .where("uidUsuario", "==", uid)
              .stream()
        )

        hoje = datetime.now(FUSO_BR).date()
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
            # Pausados continuam contando no gasto do mês; removidos não.
            if dispositivo.get("removido"):
                continue
            sensor_id = dispositivo.get("sensorId", "")
            nome = dispositivo.get("nome", "Dispositivo")

            if not sensor_id:
                continue

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
            primeiro_dia = None

            for ac in acumulados:
                d = ac.to_dict()
                custo_mes += d.get("custoTotal", 0)
                kwh_mes += d.get("kwhTotal", 0)
                dias_com_dados += 1
                data = d.get("data") or ac.id
                if primeiro_dia is None or data < primeiro_dia:
                    primeiro_dia = data

            # A média usa todos os dias desde o início do monitoramento no mês, e não
            # só os dias com consumo: dias com o aparelho desligado contam como zero.
            dias_monitorados = _dias_monitorados(
                dispositivo, primeiro_dia, inicio_mes, hoje
            )

            inicio_monit = _inicio_monitoramento(db, dispositivo, sensor_id)
            dias_completos = (hoje - inicio_monit).days if inicio_monit else 0

            if dias_completos >= JANELA_MEDIA_DIAS:
                # Uma semana completa disponível: média móvel dos últimos 7 dias
                # completos, mesmo que parte deles seja do mês anterior.
                media_diaria, kwh_media_diaria = _media_ultimos_dias(
                    db, sensor_id, hoje, JANELA_MEDIA_DIAS
                )
                metodo_media = "ultimos7dias"
            elif dias_monitorados > 0:
                media_diaria = custo_mes / dias_monitorados
                kwh_media_diaria = kwh_mes / dias_monitorados
                metodo_media = "diasMonitorados"
            else:
                media_diaria = 0
                kwh_media_diaria = 0
                metodo_media = "semDados"

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
                "diasMonitorados": dias_monitorados,
                "metodoMedia": metodo_media,
            })

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
        print(f"Erro ao calcular previsão: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/hoje/{uid}")
async def gasto_hoje(uid: str, uid_logado: str = Depends(usuario_autenticado)):
    """Retorna gasto acumulado de hoje por dispositivo."""
    if uid != uid_logado:
        raise HTTPException(
            status_code=403,
            detail="Você não tem permissão para ver os dados de outro usuário.",
        )

    db = get_db()
    hoje = datetime.now(FUSO_BR).date().isoformat()

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