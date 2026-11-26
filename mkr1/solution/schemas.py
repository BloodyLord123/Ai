from typing import Literal
from pydantic import BaseModel, Field


class ExtractedRequirement(BaseModel):
    requirement_id: str = Field(description="Номер пункту (напр. '1.1', 'Вар-3', 'Висн')")
    requirement_text: str = Field(description="Чітке формулювання вимоги з методички")


class GuidelinesPlan(BaseModel):
    variant_checked: str = Field(description="Перевірений варіант")
    requirements: list[ExtractedRequirement] = Field(
        description="Повний список усіх загальних вимог та вимог конкретного варіанта"
    )


class RequirementCheck(BaseModel):
    requirement_id: str = Field(description="Номер пункту вимоги")
    requirement_text: str = Field(description="Суть вимоги з методички")
    status: Literal["PASS", "FAIL", "WARNING"] = Field(
        description="PASS - виконано; FAIL - не виконано/помилка; WARNING - частково або потрібна візуальна перевірка графіки"
    )
    evidence_from_work: str = Field(
        description="Цитата, фрагмент коду або назва файлу зі звіту студента (або 'Відсутньо')"
    )
    recommendation: str = Field(
        description="Конкретна порада, що виправити (для PASS — 'Виправлень не потрібно')"
    )


class BatchAuditResponse(BaseModel):
    checks: list[RequirementCheck]