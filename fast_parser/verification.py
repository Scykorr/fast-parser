from typing import Literal

from pydantic import BaseModel, Field, model_validator


class CoverageVerification(BaseModel):
    status: Literal["unverified", "partial", "verified"] = "unverified"
    fixtures: bool = False
    results: bool = False
    live: bool = False
    scope: str = Field(default="", max_length=300)
    evidence: str = Field(default="", max_length=1000)
    note: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def require_evidence(self):
        self.scope, self.evidence, self.note = self.scope.strip(), self.evidence.strip(), self.note.strip()
        if self.status != "unverified":
            if not self.scope or not self.evidence:
                raise ValueError("Укажите проверенный период/выборку и основание сверки")
            if not any((self.fixtures, self.results, self.live)):
                raise ValueError("Отметьте хотя бы один проверенный вид данных")
        if self.status == "verified" and not (self.fixtures and self.results):
            raise ValueError("Для верификации лиги должны быть проверены расписание и результаты")
        if self.status == "unverified" and any((self.fixtures, self.results, self.live)):
            raise ValueError("Для непроверенной лиги снимите отметки проверенных данных")
        return self
