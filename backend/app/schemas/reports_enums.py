"""Shared closed enum atoms for the reports query AST and the saved-layout
JSON validator. Both surfaces draw dataset / dimension / measure-field /
aggregation from these closed enums so a value cannot drift between "what a
saved widget references" and "what the live compiler accepts"."""
from __future__ import annotations

import enum


class Dataset(str, enum.Enum):
    TRANSACTIONS = "transactions"


class Aggregation(str, enum.Enum):
    SUM = "sum"
    COUNT = "count"
    AVG = "avg"
    DISTINCT = "distinct"


class MeasureField(str, enum.Enum):
    AMOUNT = "amount"
    ID = "id"
    CATEGORY_ID = "category_id"
    ACCOUNT_ID = "account_id"


class Dimension(str, enum.Enum):
    CATEGORY = "category"
    CATEGORY_MASTER = "category_master"
    ACCOUNT = "account"
    TAG = "tag"
    TXN_TYPE = "txn_type"
    STATUS = "status"
    MONTH = "month"
    WEEK = "week"
    DAY = "day"
