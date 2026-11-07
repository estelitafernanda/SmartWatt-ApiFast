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

                token = usuario.get("fcmToken")
                if token:
                    try:
                        message = messaging.Message(
                            notification=messaging.Notification(
                                title="Consumo elevado",
                                body=f"{nome_dispositivo} está acima do limite!",
                            ),
                            data={
                                "sensorId": dados.sensorId,
                                "potenciaW": str(round(potencia_ativa_w, 2)),
                            },
                            android=messaging.AndroidConfig(
                                priority="high",
                                notification=messaging.AndroidNotification(
                                    channel_id="alertas_consumo",
                                ),
                            ),
                            token=token,
                        )
                        messaging.send(message)
                        print(f"Notificação enviada para {dados.sensorId}")
                    except Exception as e:
                        print(f"Erro ao enviar notificação FCM: {e}")

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
