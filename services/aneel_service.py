import httpx
import os
from datetime import date
from dotenv import load_dotenv

load_dotenv()

RESOURCE_ID = os.getenv("ANEEL_RESOURCE_ID", "fcf2906c-7c32-4b9b-a637-054e7a5234f4")
BASE_URL = "https://dadosabertos.aneel.gov.br/api/3/action/datastore_search_sql"


def _parse_valor(valor: str) -> float:
    try:
        return float(str(valor).replace(",", ".").strip())
    except (ValueError, AttributeError):
        return 0.0


async def buscar_tarifa_vigente(
    distribuidora: str,
    modalidade: str,
    classe: str,
    subgrupo: str = "B1",
) -> dict | None:
    hoje = date.today().isoformat()

    def _sanitizar(valor: str) -> str:
        return valor.replace("'", "''").strip()

    sql = f"""
        SELECT "SigAgente", "DscModalidadeTarifaria", "DscClasse",
               "DscSubGrupo", "DscDetalhe",
               "DscBaseTarifaria", "DscUnidadeTerciaria",
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
        ORDER BY "VlrTE" DESC, "DatInicioVigencia" DESC
        LIMIT 3
    """

    print(f"\n🔍 Buscando tarifa: {distribuidora} | {modalidade} | {classe} | {subgrupo}")

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resposta = await client.get(BASE_URL, params={"sql": sql})
            dados = resposta.json()

        registros = dados.get("result", {}).get("records", [])

        if not registros:
            print(f"Nenhuma tarifa vigente encontrada!")
            return None

        registro = registros[0]
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

    except Exception as e:
        print(f"Erro ao buscar tarifa vigente: {e}")
        return None


async def buscar_distribuidoras() -> list[dict]:
    try:
        sql = f"""
            SELECT DISTINCT "SigAgente"
            FROM "{RESOURCE_ID}"
            WHERE "SigAgente" IS NOT NULL
              AND LOWER("SigAgente") NOT LIKE '%informado%'
              AND LOWER("SigAgente") NOT LIKE '%n/a%'
              AND "SigAgente" != ''
            ORDER BY "SigAgente"
            LIMIT 200
        """
        async with httpx.AsyncClient(timeout=30) as client:
            resposta = await client.get(BASE_URL, params={"sql": sql})
            dados = resposta.json()

        registros = dados.get("result", {}).get("records", [])

        return [
            {
                "sigla": r["SigAgente"].strip(),
                "nome": r["SigAgente"].strip(),
                "ativo": True,
            }
            for r in registros
            if r.get("SigAgente", "").strip()
            and "informado" not in r["SigAgente"].lower()
        ]

    except Exception as e:
        print(f"Erro ao buscar distribuidoras: {e}")
        return []