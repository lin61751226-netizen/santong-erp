from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DataValues(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class FinanceValues(DataValues):
    entry_date: date
    entry_type: Literal["收入", "支出"]
    category: str = Field(min_length=1, max_length=120)
    summary: str = Field(default="", max_length=4000)
    vendor_name: str = Field(default="", max_length=200)
    income_amount: float = Field(default=0, ge=0, le=1e12, allow_inf_nan=False)
    expense_amount: float = Field(default=0, ge=0, le=1e12, allow_inf_nan=False)
    project_name: str = Field(default="", max_length=200)
    payment_method: str = Field(default="", max_length=120)
    payment_status: str = Field(default="", max_length=120)
    invoice_number: str = Field(default="", max_length=120)
    invoice_date: date | None = None
    handled_by: str = Field(default="", max_length=120)
    account_name: str = Field(default="", max_length=200)
    voucher_type: str = Field(default="", max_length=120)
    tag: str = Field(default="", max_length=200)
    note: str = Field(default="", max_length=4000)

    @model_validator(mode="after")
    def valid_amounts(self):
        income, expense = self.income_amount, self.expense_amount
        if (self.entry_type == "收入" and not (income > 0 and expense == 0)) or (
            self.entry_type == "支出" and not (expense > 0 and income == 0)
        ):
            raise ValueError("收入只能填收入金額；支出只能填支出金額，且必須大於零")
        for amount in (income, expense):
            if abs(amount - round(amount, 2)) > 0.00001:
                raise ValueError("金額最多兩位小數")
        return self


class ContactValues(DataValues):
    name: str = Field(min_length=1, max_length=200)
    tax_id: str = Field(default="", max_length=40)
    category: str = Field(default="未分類", min_length=1, max_length=120)
    department: str = Field(default="", max_length=200)
    title: str = Field(default="", max_length=200)
    contact_person: str = Field(default="", max_length=200)
    phone: str = Field(default="", max_length=100)
    mobile: str = Field(default="", max_length=100)
    email: str = Field(default="", max_length=254)
    address: str = Field(default="", max_length=2000)
    note: str = Field(default="", max_length=4000)


class DocumentDataSave(DataValues):
    kind: Literal["finance", "contacts"]
    record_id: int | None = Field(default=None, gt=0)
    expected_revision: str | None = Field(default=None, min_length=64, max_length=64)
    request_id: UUID
    values: dict


class DocumentDataExportRequest(DataValues):
    kind: Literal["finance", "contacts"]
    expected_dataset: str = Field(min_length=64, max_length=64)
