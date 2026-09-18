from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from app.errors import AnalysisError
from app.models import ColumnSchema, DatasetInfo, DatasetSchema, QueryPlan
from app.storage import Storage

log = logging.getLogger(__name__)


SUPPORTED_TABLE_TYPES = {"csv", "xlsx", "xls"}

MAX_SCHEMA_SAMPLE_VALUES = 8
MAX_STRING_SAMPLE_LENGTH = 120


@dataclass
class LoadedDataset:
    """
    Runtime representation of one structured document.

    dataframe:
        Complete Pandas DataFrame loaded from the uploaded document.

    table_name:
        Safe internal table name exposed to DuckDB/Gemini, for example:
            dataset_1
            dataset_2

    Gemini never needs to know the physical upload path.
    """

    document_id: str
    filename: str
    file
