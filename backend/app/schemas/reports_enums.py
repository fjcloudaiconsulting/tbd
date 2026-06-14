"""Shared closed enum atoms for the reports query AST and the saved-layout
JSON validator. Both surfaces draw dataset / dimension / measure-field /
aggregation from these closed enums so a value cannot drift between "what a
saved widget references" and "what the live compiler accepts"."""
from __future__ import annotations

import enum


class Dataset(str, enum.Enum):
    """Closed source set. Adding a value here EXPANDS what data the reports
    AST can reach and is a security-review event — every new dataset must
    have a registered ReportSource that org-scopes its own queries."""

    TRANSACTIONS = "transactions"
    ACCOUNTS = "accounts"


class Aggregation(str, enum.Enum):
    """Closed aggregation set. ``distinct`` is the short form of
    ``count_distinct`` (distinct-count of the targeted field)."""

    SUM = "sum"
    COUNT = "count"
    AVG = "avg"
    DISTINCT = "distinct"


class MeasureField(str, enum.Enum):
    AMOUNT = "amount"
    BALANCE = "balance"
    ID = "id"
    CATEGORY_ID = "category_id"
    ACCOUNT_ID = "account_id"


class Dimension(str, enum.Enum):
    CATEGORY = "category"
    CATEGORY_MASTER = "category_master"
    ACCOUNT = "account"
    ACCOUNT_TYPE = "account_type"
    CURRENCY = "currency"
    ACCOUNT_ACTIVE = "account_active"
    TAG = "tag"
    TXN_TYPE = "txn_type"
    STATUS = "status"
    MONTH = "month"
    WEEK = "week"
    DAY = "day"
