import json
import time
import httpx
import os
from datetime import date
from dotenv import load_dotenv

from services.firebase_service import get_db

load_dotenv()

TIMEOUT_TARIFA_S = 15

RESOURCE_ID = os.getenv("ANEEL_RESOURCE_ID", "fcf2906c-7c32-4b9b-a637-054e7a5234f4")
BASE_URL = "https://dadosabertos.aneel.gov.br/api/3/action/datastore_search"
HEADERS = {"User-Agent": "Mozilla/5.0"}
PAGE_SIZE = 5000
MAX_PAGINAS = 40


def _parse_valor(valor) -> float:
    """Converte '1,85', ',00' ou '1.234,56' em float."""
    try:
        return float(str(valor).replace(".", "").replace(",", ".").strip())
    except (ValueError, AttributeError):
        return 0.0


async def _consultar(params: dict, timeout: int = 90) -> list[dict]:
    """Uma chamada ao datastore_search. Sempre loga o motivo de falhas."""
    query = {"resource_id": RESOURCE_ID, "include_total": "false", **params}
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers=HEADERS,
    ) as client:
        resposta = await client.get(BASE_URL, params=query)

    if resposta.status_code != 200:
        print(f"[ANEEL] HTTP {resposta.status_code}: {resposta.text[:500]}")
        return []

    dados = resposta.json()
    if not dados.get("success"):
        print(f"[ANEEL] success=false: {dados.get('error') or dados}")
        return []

    return dados.get("result", {}).get("records", [])


async def _consultar_tudo(params: dict) -> list[dict]:
    """Percorre todas as páginas do resultado."""
    todos: list[dict] = []
    offset = 0
    for _ in range(MAX_PAGINAS):
        pagina = await _consultar({**params, "limit": PAGE_SIZE, "offset": offset})
        todos.extend(pagina)
        if len(pagina) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return todos


async def buscar_tarifa_vigente(
    distribuidora: str,
    modalidade: str,
    classe: str,
    subgrupo: str = "B1",
) -> dict | None:
    hoje = date.today().isoformat()

    filtros = {
        "SigAgente": distribuidora.strip(),
        "DscModalidadeTarifaria": modalidade.strip(),
        "DscClasse": classe.strip(),
        "DscSubGrupo": subgrupo.strip(),
        "DscBaseTarifaria": "Tarifa de Aplicação",
        "DscUnidadeTerciaria": "MWh",
        "DscDetalhe": "Não se aplica",
    }

    print(f"\n🔍 Buscando tarifa: {distribuidora} | {modalidade} | {classe} | {subgrupo}")

    try:
        registros = await _consultar(
            {
                "filters": json.dumps(filtros, ensure_ascii=False),
                "sort": "DatInicioVigencia desc",
                "limit": 100,
            },
            timeout=TIMEOUT_TARIFA_S,
        )
    except Exception as e:
        print(f"[ANEEL] erro ao buscar tarifa vigente: {e!r}")
        return None

    vigentes = [
        r for r in registros
        if (r.get("DatInicioVigencia") or "") <= hoje
        and (not r.get("DatFimVigencia") or r["DatFimVigencia"] >= hoje)
    ]

    if not vigentes:
        print("Nenhuma tarifa vigente encontrada!")
        return None

    registro = next(
        (r for r in vigentes if r.get("NomPostoTarifario") == "Não se aplica"),
        vigentes[0],
    )

    te = _parse_valor(registro.get("VlrTE", "0")) / 1000
    tusd = _parse_valor(registro.get("VlrTUSD", "0")) / 1000

    print(f"Tarifa encontrada: TE={te} TUSD={tusd} R$/kWh")

    return {
        "distribuidora": distribuidora,
        "modalidade": modalidade,
        "classe": classe,
        "subgrupo": subgrupo,
        "TE": te,
        "TUSD": tusd,
        "total": te + tusd,
        "vigenciaInicio": registro.get("DatInicioVigencia"),
        "vigenciaFim": registro.get("DatFimVigencia"),
    }


CACHE_TARIFA_SEGUNDOS = 6 * 60 * 60
_cache_tarifa: dict[tuple, tuple[float, dict]] = {}


def _id_tarifa(chave: tuple) -> str:
    """Id do documento em 'tarifas' (sem barras, que o Firestore não aceita)."""
    return "__".join(chave).replace("/", "-")


def _ler_tarifa_salva(chave: tuple) -> tuple[float, dict] | None:
    """Última tarifa gravada no Firestore: sobrevive aos reinícios do Render."""
    try:
        doc = get_db().collection("tarifas").document(_id_tarifa(chave)).get()
        if doc.exists:
            dados = doc.to_dict()
            return float(dados.get("salvaEm", 0)), dados["tarifa"]
    except Exception as e:
        print(f"[ANEEL] erro ao ler tarifa salva: {e!r}")
    return None


def _salvar_tarifa(chave: tuple, tarifa: dict) -> None:
    try:
        get_db().collection("tarifas").document(_id_tarifa(chave)).set({
            "tarifa": tarifa,
            "salvaEm": time.time(),
        })
    except Exception as e:
        print(f"[ANEEL] erro ao salvar tarifa: {e!r}")


def usar_tarifa_reserva(
    distribuidora: str,
    modalidade: str,
    classe: str,
    subgrupo: str,
    tarifa: dict,
) -> None:
    """Guarda uma tarifa de reserva (vinda de leituras anteriores) na memória e no
    Firestore, para as próximas leituras não ficarem esperando a ANEEL.
    Ela é marcada como antiga: a ANEEL é consultada de novo em 30 min."""
    chave = (distribuidora.strip(), modalidade.strip(), classe.strip(), subgrupo.strip())
    tarifa = {
        "distribuidora": distribuidora, "modalidade": modalidade,
        "classe": classe, "subgrupo": subgrupo, **tarifa,
    }
    marcada_em = time.time() - CACHE_TARIFA_SEGUNDOS + 30 * 60
    _cache_tarifa[chave] = (marcada_em, tarifa)
    try:
        get_db().collection("tarifas").document(_id_tarifa(chave)).set({
            "tarifa": tarifa,
            "salvaEm": marcada_em,
            "origem": "leituras anteriores",
        })
    except Exception as e:
        print(f"[ANEEL] erro ao salvar tarifa de reserva: {e!r}")


async def buscar_tarifa_com_cache(
    distribuidora: str,
    modalidade: str,
    classe: str,
    subgrupo: str = "B1",
) -> dict | None:
    """Tarifa vigente, sem deixar a lentidão da ANEEL travar o envio das leituras.

    Ordem: cache em memória -> cópia salva no Firestore -> ANEEL (tempo curto).
    Se a ANEEL falhar, usa a última tarifa conhecida, mesmo que antiga.
    """
    chave = (distribuidora.strip(), modalidade.strip(), classe.strip(), subgrupo.strip())
    agora = time.time()

    em_memoria = _cache_tarifa.get(chave)
    if em_memoria and agora - em_memoria[0] < CACHE_TARIFA_SEGUNDOS:
        return em_memoria[1]

    salva = em_memoria or _ler_tarifa_salva(chave)
    if salva and agora - salva[0] < CACHE_TARIFA_SEGUNDOS:
        _cache_tarifa[chave] = salva
        return salva[1]

    tarifa = await buscar_tarifa_vigente(*chave)
    if tarifa:
        _cache_tarifa[chave] = (agora, tarifa)
        _salvar_tarifa(chave, tarifa)
        return tarifa

    if salva:
        print("[ANEEL] falha ao atualizar a tarifa; usando a última tarifa conhecida")
        _cache_tarifa[chave] = (agora - CACHE_TARIFA_SEGUNDOS + 30 * 60, salva[1])
        return salva[1]

    return None

async def buscar_distribuidoras() -> list[dict]:
    corte = f"{date.today().year - 2}-01-01"

    try:
        registros = await _consultar_tudo(
            {"fields": "SigAgente,DatFimVigencia", "distinct": "true"}
        )
    except Exception as e:
        print(f"[ANEEL] erro ao buscar distribuidoras: {e!r}")
        return []

    ultima_vigencia: dict[str, str] = {}
    for r in registros:
        sigla = (r.get("SigAgente") or "").strip()
        if not sigla:
            continue
        baixa = sigla.lower()
        if "informado" in baixa or "n/a" in baixa:
            continue
        fim = r.get("DatFimVigencia") or "9999-12-31"  
        if fim > ultima_vigencia.get(sigla, ""):
            ultima_vigencia[sigla] = fim

    return [
        {"sigla": s, "nome": s, "ativo": True}
        for s in sorted(ultima_vigencia)
        if ultima_vigencia[s] >= corte
    ]