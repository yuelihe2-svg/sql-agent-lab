"""Source-specific definition for the paid order total."""
import json
import re
from dataclasses import dataclass

from sqlglot import exp

PAID_TOTAL_TITLE = "指标口径：已付款订单总金额"
_FIELDS = {"table", "amount_column", "year_column", "status_column", "paid_value"}
_QUESTION = re.compile(
    r"(?:新问题[：:，,]\s*)?(?:请)?(?:统计|计算)\s*([12][0-9]{3})\s*年\s*已付款订单(?:的)?总金额[。？?！!]?")


def paid_total_year(question):
    match = _QUESTION.fullmatch(question.strip())
    return match[1] if match else None


@dataclass(frozen=True)
class PaidTotal:
    table: str
    amount_column: str
    year_column: str
    status_column: str
    paid_value: str | int

    @classmethod
    def from_document(cls, document_format, body, profile):
        if document_format != "json":
            raise ValueError("指标口径请选择 JSON 格式。")
        try:
            data = json.loads(body)
        except (TypeError, ValueError):
            raise ValueError("指标口径正文需要是有效的 JSON。") from None
        if not isinstance(data, dict) or data.keys() != _FIELDS:
            raise ValueError("请填写表名、金额字段、年份字段、付款状态字段和已付款值。")
        names = [data[key] for key in ("table", "amount_column", "year_column", "status_column")]
        if any(not isinstance(name, str) or not name.strip() or "\0" in name for name in names):
            raise ValueError("表名和字段名必须是非空文本。")
        table, *fields = names
        if len({field.casefold() for field in fields}) != len(fields):
            raise ValueError("金额、年份和付款状态需要使用不同字段。")
        columns = next(({column["name"] for column in entry["columns"]}
                        for entry in profile["tables"] if entry["name"] == table), set())
        if not set(fields) <= columns:
            raise ValueError("在当前数据库中找不到指标配置里的表或字段。")
        value = data["paid_value"]
        if type(value) is int:
            if not -(2**63) <= value < 2**63:
                raise ValueError("付款状态整数超出 SQLite 的范围。")
        elif not isinstance(value, str) or not value.strip() or "\0" in value:
            raise ValueError("已付款值必须是非空文本或整数。")
        return cls(**data)

    def sql(self, year):
        from sql_agent_lab.macsql.tools import quote_identifier
        table, amount, year_column, status = map(
            quote_identifier, (self.table, self.amount_column, self.year_column, self.status_column))
        paid = exp.convert(self.paid_value).sql(dialect="sqlite")
        return (f'SELECT SUM({amount}) AS total_paid_amount FROM {table} '
                f'WHERE {year_column} = {int(year)} AND {status} = {paid}')
