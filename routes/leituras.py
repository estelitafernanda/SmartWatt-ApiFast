import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from datetime import datetime, timedelta, timezone
from firebase_admin import firestore, messaging
from services.firebase_service import get_db
from services.aneel_service import buscar_tarifa_com_cache

router = APIRouter()

FUSO_BR = timezone(timedelta(hours=-3))

INTERVALO_PADRAO_S = 10
INTERVALO_MAXIMO_S = 120

# O gasto do mês é recalculado no máximo a cada 5 min por usuário, para não
# somar os acumulados do mês inteiro a cada leitura.
VERIFICACAO_MENSAL_S = 5 * 60
_ultima_verificacao_mensal: dict[str, float] = {}


class LeituraInput(BaseModel):
    sensorId: str = Field(min_length=1)
    corrente: float = Field(ge=0, le=150)
    tensao: float = Field(ge=0, le=300)


def _segundos_desde_ultima_leitura(dispositivo: dict, agora: datetime) -> float:
    """Tempo real desde a leitura anterior, para o kWh não depender do intervalo do ESP32."""
    ultima = dispositivo.get("ultimaLeitura")
    if not ultima:
        return INTERVALO_PADRAO_S

    segundos = (agora - ultima).total_seconds()
    if segundos <= 0:
        return INTERVALO_PADRAO_S
    return min(segundos, INTERVALO_MAXIMO_S)


def _enviar_notificacao(token: str | None, titulo: str, corpo: str, dados: dict) -> None:
    if not token:
        return
    try:
        messaging.send(messaging.Message(
            notification=messaging.Notification(title=titulo, body=corpo),
            data={k: str(v) for k, v in dados.items()},
            android=messaging.AndroidConfig(
                priority="high",
                notification=messaging.AndroidNotification(
                    channel_id="alertas_consumo",
                ),
            ),
            token=token,
        ))
        print(f"Notificação enviada: {titulo}")
    except Exception as e:
        print(f"Erro ao enviar notificação FCM: {e}")


def _custo_do_mes(db, uid: str, agora: datetime) -> float:
    """Soma o gasto do mês de todos os dispositivos do usuário (menos os removidos)."""
    inicio_mes = agora.date().replace(day=1).isoformat()
    hoje = agora.date().isoformat()
    total = 0.0

    for doc in db.collection("dispositivos").where("uidUsuario", "==", uid).stream():
        dispositivo = doc.to_dict()
        sensor_id = dispositivo.get("sensorId", "")
        if not sensor_id or dispositivo.get("removido"):
            continue

        acumulados = (
            db.collection("leituras")
              .document(sensor_id)
              .collection("acumulado")
              .where("data", ">=", inicio_mes)
              .where("data", "<=", hoje)
              .stream()
        )
        total += sum(a.to_dict().get("custoTotal", 0) for a in acumulados)

    return total


def _verificar_limite_mensal(db, uid: str, usuario: dict, agora: datetime) -> None:
    """Envia um alerta por mês quando o gasto passa do limite mensal do usuário.

    Se o usuário mudar o limite depois do alerta, o novo limite pode gerar outro.
    """
    if not usuario.get("alertaMensal", True):
        return

    limite = float(usuario.get("limiteMensal", 200))
    mes = agora.strftime("%Y-%m")
    enviado = usuario.get("alertaMensalEnviado") or {}
    if enviado.get("mes") == mes and float(enviado.get("limite", -1)) == limite:
        return

    agora_s = time.monotonic()
    if agora_s - _ultima_verificacao_mensal.get(uid, -VERIFICACAO_MENSAL_S) < VERIFICACAO_MENSAL_S:
        return
    _ultima_verificacao_mensal[uid] = agora_s

    gasto = _custo_do_mes(db, uid, agora)
    if gasto <= limite:
        return

    mensagem = (
        f"O gasto deste mês chegou a R$ {gasto:.2f} e passou "
        f"do limite de R$ {limite:.2f}."
    )
    db.collection("alertas").add({
        "uid": uid,
        "sensorId": "",
        "tipo": "mensal",
        "nomeDispositivo": "Gasto do mês",
        "gastoMes": round(gasto, 2),
        "limiteMensal": limite,
        "mensagem": mensagem,
        "lido": False,
        "timestamp": agora,
    })
    db.collection("usuarios").document(uid).update({
        "alertaMensalEnviado": {"mes": mes, "limite": limite},
    })
    _enviar_notificacao(
        usuario.get("fcmToken"),
        "Limite mensal ultrapassado",
        mensagem,
        {"tipo": "mensal", "gastoMes": round(gasto, 2)},
    )


@router.post("/")
async def receber_leitura(dados: LeituraInput):
    db = get_db()

    try:
        ref_dispositivo = db.collection("dispositivos").document(dados.sensorId)
        doc_dispositivo = ref_dispositivo.get()
        if not doc_dispositivo.exists:
            raise HTTPException(
                status_code=404,
                detail=f"Sensor '{dados.sensorId}' não encontrado. "
                       f"Cadastre o dispositivo no app antes de enviar leituras."
            )

        dispositivo = doc_dispositivo.to_dict()

        if dispositivo.get("removido") or not dispositivo.get("ativo", True):
            return {
                "sucesso": False,
                "ignorada": True,
                "motivo": "Dispositivo pausado ou removido no app. Leitura descartada.",
            }

        uid = dispositivo.get("uidUsuario", "")
        if not uid:
            raise HTTPException(
                status_code=400,
                detail="Dispositivo sem usuário vinculado. "
                       "Recadastre o dispositivo no app."
            )

        doc_usuario = db.collection("usuarios").document(uid).get()
        if not doc_usuario.exists:
            raise HTTPException(
                status_code=404,
                detail="Usuário não encontrado no banco de dados."
            )

        usuario = doc_usuario.to_dict()

        distribuidora_raw = usuario.get("distribuidora", "")
        distribuidora = distribuidora_raw.get("sigla", "") \
            if isinstance(distribuidora_raw, dict) else distribuidora_raw
        modalidade = usuario.get("modalidade", "")
        classe = usuario.get("classe", "")
        subgrupo = usuario.get("subgrupo", "")

        if not distribuidora or not modalidade or not classe or not subgrupo:
            raise HTTPException(
                status_code=400,
                detail="Perfil tarifário do usuário incompleto. "
                       "Atualize distribuidora, modalidade, classe e subgrupo no app."
            )

        fator_potencia = dispositivo.get("fatorPotencia")
        if not fator_potencia:
            raise HTTPException(
                status_code=400,
                detail="Fator de potência não informado. "
                       "Recadastre o dispositivo no app."
            )

        tarifa = await buscar_tarifa_com_cache(
            distribuidora, modalidade, classe, subgrupo
        )
        if not tarifa:
            raise HTTPException(
                status_code=503,
                detail="Não foi possível obter a tarifa vigente da ANEEL."
            )

        agora = datetime.now(FUSO_BR)
        hoje = agora.date().isoformat()
        hora_chave = agora.strftime("%Y-%m-%d-%H")

        potencia_aparente_w = dados.corrente * dados.tensao
        potencia_ativa_w = potencia_aparente_w * fator_potencia
        potencia_kw = potencia_ativa_w / 1000
        custo_hora = potencia_kw * tarifa["total"]

        horas = _segundos_desde_ultima_leitura(dispositivo, agora) / 3600
        kwh_leitura = potencia_kw * horas
        custo_leitura = custo_hora * horas

        ref_leituras = db.collection("leituras").document(dados.sensorId)

        if potencia_ativa_w > 0:
            ref_leituras.collection("historico").add({
                "corrente": dados.corrente,
                "tensao": dados.tensao,
                "potenciaAparenteW": potencia_aparente_w,
                "potenciaAtivaW": potencia_ativa_w,
                "fatorPotencia": fator_potencia,
                "custoHora": custo_hora,
                "custoLeitura": custo_leitura,
                "kwhLeitura": kwh_leitura,
                "TE": tarifa["TE"],
                "TUSD": tarifa["TUSD"],
                "timestamp": agora,
            })

            ref_leituras.collection("acumulado").document(hoje).set({
                "data": hoje,
                "sensorId": dados.sensorId,
                "uid": uid,
                "custoTotal": firestore.Increment(custo_leitura),
                "kwhTotal": firestore.Increment(kwh_leitura),
                "ultimaAtualizacao": agora,
                "ultimaPotenciaAtivaW": potencia_ativa_w,
                "distribuidora": distribuidora,
                "subgrupo": subgrupo,
                "TE": tarifa["TE"],
                "TUSD": tarifa["TUSD"],
            }, merge=True)

            ref_leituras.collection("tempo_real").document(hora_chave).set({
                "hora": hora_chave,
                "kwh": firestore.Increment(kwh_leitura),
                "timestamp": agora,
            }, merge=True)

        ref_dispositivo.update({
            "potenciaW": potencia_ativa_w,
            "custoHora": custo_hora,
            "fatorPotencia": fator_potencia,
            "ultimaLeitura": agora,
        })

        limite_w = usuario.get("limiteDispositivo", 500)
        alerta_ativo = usuario.get("alertaDispositivo", True)

        if alerta_ativo and potencia_ativa_w > limite_w:
            alertas_existentes = (
                db.collection("alertas")
                  .where("uid", "==", uid)
                  .where("sensorId", "==", dados.sensorId)
                  .where("lido", "==", False)
                  .limit(1)
                  .stream()
            )

            if not any(True for _ in alertas_existentes):
                nome_dispositivo = dispositivo.get("nome", "Dispositivo")

                db.collection("alertas").add({
                    "uid": uid,
                    "sensorId": dados.sensorId,
                    "nomeDispositivo": nome_dispositivo,
                    "potenciaW": round(potencia_ativa_w, 2),
                    "limiteW": limite_w,
                    "mensagem": f"{nome_dispositivo} está acima do limite configurado ({limite_w}W).",
                    "lido": False,
                    "timestamp": agora,
                })

                _enviar_notificacao(
                    usuario.get("fcmToken"),
                    "Consumo elevado",
                    f"{nome_dispositivo} está acima do limite!",
                    {"sensorId": dados.sensorId, "potenciaW": round(potencia_ativa_w, 2)},
                )

        if potencia_ativa_w > 0:
            try:
                _verificar_limite_mensal(db, uid, usuario, agora)
            except Exception as e:
                print(f"Erro ao verificar limite mensal: {e}")

        return {
            "sucesso": True,
            "potenciaAparenteW": round(potencia_aparente_w, 2),
            "potenciaAtivaW": round(potencia_ativa_w, 2),
            "fatorPotencia": fator_potencia,
            "custoHora": round(custo_hora, 4),
            "custoLeitura": round(custo_leitura, 6),
            "kwhLeitura": round(kwh_leitura, 6),
            "tarifa": {
                "TE": tarifa["TE"],
                "TUSD": tarifa["TUSD"],
                "total": tarifa["total"],
            },
        }

    except HTTPException:
        raise
    except Exception as e:
        print(f"Erro ao processar leitura: {e}")
        raise HTTPException(status_code=500, detail=str(e))
