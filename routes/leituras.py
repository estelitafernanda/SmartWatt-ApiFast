from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from datetime import datetime, date, timedelta, timezone
from firebase_admin import messaging
from services.firebase_service import get_db
from services.aneel_service import buscar_tarifa_vigente

router = APIRouter()

FUSO_BR = timezone(timedelta(hours=-3))

FATORES_POTENCIA = {
    "resistencia": 1.0,
    "arcondicionado": 0.92,
    "eletronico": 0.97,
    "iluminacao": 0.95,
    "outro": 0.90,
}


class LeituraInput(BaseModel):
    sensorId: str
    corrente: float
    tensao: float
    uid: str


@router.post("/")
async def receber_leitura(dados: LeituraInput):
    db = get_db()

    try:
        doc_usuario = db.collection("usuarios").document(dados.uid).get()
        if not doc_usuario.exists:
            raise HTTPException(status_code=404, detail="Usuário não encontrado.")

        usuario = doc_usuario.to_dict()

        distribuidora_raw = usuario.get("distribuidora", "")
        distribuidora = distribuidora_raw.get("sigla", "") if isinstance(distribuidora_raw, dict) else distribuidora_raw
        modalidade = usuario.get("modalidade", "")
        classe = usuario.get("classe", "")
        subgrupo = usuario.get("subgrupo", "B1")

        if not distribuidora or not modalidade or not classe:
            raise HTTPException(status_code=400, detail="Perfil tarifário incompleto.")

        doc_dispositivo = db.collection("dispositivos").document(dados.sensorId).get()
        fator_potencia = 0.92

        if doc_dispositivo.exists:
            disp = doc_dispositivo.to_dict()
            tipo_aparelho = disp.get("tipoAparelho", "outro")
            fator_potencia = FATORES_POTENCIA.get(tipo_aparelho, 0.92)
        else:
            disp = {}

        tarifa = await buscar_tarifa_vigente(
            distribuidora, modalidade, classe, subgrupo
        )

        if not tarifa:
            raise HTTPException(status_code=503, detail="Não foi possível obter a tarifa vigente.")

        potencia_aparente_w = dados.corrente * dados.tensao
        potencia_ativa_w = potencia_aparente_w * fator_potencia
        potencia_kw = potencia_ativa_w / 1000
        custo_hora = potencia_kw * tarifa["total"]
        custo_leitura = custo_hora / 360

        agora = datetime.now(FUSO_BR)
        hoje = agora.date().isoformat()

        db.collection("leituras") \
          .document(dados.sensorId) \
          .collection("historico") \
          .add({
              "corrente": dados.corrente,
              "tensao": dados.tensao,
              "potenciaAparenteW": potencia_aparente_w,
              "potenciaAtivaW": potencia_ativa_w,
              "fatorPotencia": fator_potencia,
              "custoHora": custo_hora,
              "custoLeitura": custo_leitura,
              "TE": tarifa["TE"],
              "TUSD": tarifa["TUSD"],
              "timestamp": agora,
          })

        ref_acumulado = (
            db.collection("leituras")
            .document(dados.sensorId)
            .collection("acumulado")
            .document(hoje)
        )

        doc_acumulado = ref_acumulado.get()

        if doc_acumulado.exists:
            atual = doc_acumulado.to_dict()
            ref_acumulado.update({
                "custoTotal": atual.get("custoTotal", 0) + custo_leitura,
                "kwhTotal": atual.get("kwhTotal", 0) + (potencia_kw / 360),
                "ultimaAtualizacao": agora,
                "ultimaPotenciaAtivaW": potencia_ativa_w,
            })
        else:
            ref_acumulado.set({
                "data": hoje,
                "sensorId": dados.sensorId,
                "uid": dados.uid,
                "custoTotal": custo_leitura,
                "kwhTotal": potencia_kw / 360,
                "ultimaAtualizacao": agora,
                "ultimaPotenciaAtivaW": potencia_ativa_w,
                "distribuidora": distribuidora,
                "subgrupo": subgrupo,
                "TE": tarifa["TE"],
                "TUSD": tarifa["TUSD"],
            })

        hora_chave = agora.strftime("%Y-%m-%d-%H")

        ref_hora = (
            db.collection("leituras")
            .document(dados.sensorId)
            .collection("tempo_real")
            .document(hora_chave)
        )

        doc_hora = ref_hora.get()
        kwh_incremento = potencia_kw / 360

        if doc_hora.exists:
            atual = doc_hora.to_dict()
            ref_hora.update({
                "kwh": atual.get("kwh", 0) + kwh_incremento,
                "timestamp": agora,
            })
        else:
            ref_hora.set({
                "hora": hora_chave,
                "kwh": kwh_incremento,
                "timestamp": agora,
            })

        db.collection("dispositivos") \
            .document(dados.sensorId) \
            .update({
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
                  .where("uid", "==", dados.uid)
                  .where("sensorId", "==", dados.sensorId)
                  .where("lido", "==", False)
                  .stream()
            )

            if not any(True for _ in alertas_existentes):
                nome_dispositivo = disp.get("nome", "Dispositivo")

                db.collection("alertas").add({
                    "uid": dados.uid,
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
                            token=token,
                        )
                        messaging.send(message)
                        print(f"Notificação enviada para {dados.uid}")
                    except Exception as e:
                        print(f"Erro ao enviar notificação FCM: {e}")

        return {
            "sucesso": True,
            "potenciaAparenteW": round(potencia_aparente_w, 2),
            "potenciaAtivaW": round(potencia_ativa_w, 2),
            "fatorPotencia": fator_potencia,
            "custoHora": round(custo_hora, 4),
            "custoLeitura": round(custo_leitura, 6),
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