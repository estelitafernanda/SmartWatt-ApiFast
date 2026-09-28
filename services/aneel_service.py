import json
import httpx
import os
from datetime import date
from dotenv import load_dotenv

load_dotenv()

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
            timeout=60,
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