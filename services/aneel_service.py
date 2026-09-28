import httpx
import os
from datetime import date
from dotenv import load_dotenv

load_dotenv()

RESOURCE_ID = os.getenv("ANEEL_RESOURCE_ID", "fcf2906c-7c32-4b9b-a637-054e7a5234f4")
BASE_URL = "https://dadosabertos.aneel.gov.br/api/3/action/datastore_search_sql"
HEADERS = {"User-Agent": "Mozilla/5.0"}


def _parse_valor(valor) -> float:
    """Converte '1,85', ',00' ou '1.234,56' em float."""
    try:
        return float(str(valor).replace(".", "").replace(",", ".").strip())
    except (ValueError, AttributeError):
        return 0.0


def _sanitizar(valor: str) -> str:
    return str(valor).replace("'", "''").strip()


async def _consultar_sql(sql: str, timeout: int = 90) -> list[dict]:
    """Executa a consulta SQL na API da ANEEL e sempre loga o motivo de falhas."""
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers=HEADERS,
    ) as client:
        resposta = await client.get(BASE_URL, params={"sql": sql})

    if resposta.status_code != 200:
        print(f"[ANEEL] HTTP {resposta.status_code}: {resposta.text[:500]}")
        return []

    dados = resposta.json()
    if not dados.get("success"):
        print(f"[ANEEL] success=false: {dados.get('error') or dados}")
        return []

    return dados.get("result", {}).get("records", [])


async def buscar_tarifa_vigente(
    distribuidora: str,
    modalidade: str,
    classe: str,
    subgrupo: str = "B1",
) -> dict | None:
    hoje = date.today().isoformat()

    sql = f"""
        SELECT "SigAgente", "DscModalidadeTarifaria", "DscClasse",
               "DscSubGrupo", "DscDetalhe",
               "DscBaseTarifaria", "DscUnidadeTerciaria",
               "NomPostoTarifario",
               "VlrTE", "VlrTUSD",
               "DatInicioVigencia", "DatFimVigencia"
        FROM "{RESOURCE_ID}"
        WHERE "SigAgente" = '{_sanitizar(distribuidora)}'
          AND "DscModalidadeTarifaria" = '{_sanitizar(modalidade)}'
          AND "DscClasse" = '{_sanitizar(classe)}'
          AND "DscSubGrupo" = '{_sanitizar(subgrupo)}'
          AND "DscBaseTarifaria" = 'Tarifa de Aplicação'
          AND "DscUnidadeTerciaria" = 'MWh'
          AND "DscDetalhe" = 'Não se aplica'
          AND "DatInicioVigencia" <= '{hoje}'
          AND ("DatFimVigencia" >= '{hoje}' OR "DatFimVigencia" IS NULL)
        ORDER BY "DatInicioVigencia" DESC
        LIMIT 10
    """

    print(f"\n Buscando tarifa: {distribuidora} | {modalidade} | {classe} | {subgrupo}")

    try:
        registros = await _consultar_sql(sql, timeout=60)
    except Exception as e:
        print(f"[ANEEL] erro ao buscar tarifa vigente: {e!r}")
        return None

    if not registros:
        print("Nenhuma tarifa vigente encontrada!")
        return None

    registro = next(
        (r for r in registros if r.get("NomPostoTarifario") == "Não se aplica"),
        registros[0],
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

    sql = f"""
        SELECT DISTINCT "SigAgente"
        FROM "{RESOURCE_ID}"
        WHERE "SigAgente" IS NOT NULL
          AND "SigAgente" != ''
          AND LOWER("SigAgente") NOT LIKE '%informado%'
          AND LOWER("SigAgente") NOT LIKE '%n/a%'
          AND "DatFimVigencia" >= '{corte}'
        ORDER BY "SigAgente"
        LIMIT 300
    """

    try:
        registros = await _consultar_sql(sql)
    except Exception as e:
        print(f"[ANEEL] erro ao buscar distribuidoras: {e!r}")
        return []

    return [
        {
            "sigla": r["SigAgente"].strip(),
            "nome": r["SigAgente"].strip(),
            "ativo": True,
        }
        for r in registros
        if r.get("SigAgente", "").strip()
    ]