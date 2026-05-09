from pydantic import BaseModel
from typing import Optional


class TarifaRequest(BaseModel):
    distribuidora: str
    modalidade: str
    classe: str
    subgrupo: str
    posto_tarifario: Optional[str] = None


class TarifaResponse(BaseModel):
    distribuidora: str
    modalidade: str
    classe: str
    subgrupo: str
    posto_tarifario: Optional[str]
    vigencia_inicio: Optional[str]
    vigencia_fim: Optional[str]
    TE: float
    TUSD: float
    tarifa_total_mwh: float
    tarifa_total_kwh: float