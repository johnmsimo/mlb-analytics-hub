"""Immutable lookup indexes for process-cached pandas dataframes."""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable

import pandas as pd


def normalize_name(value) -> str:
    """Normalize common ``First Last`` and ``Last, First`` name formats."""
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    normalized = re.sub(r"\s+", " ", str(value).lower().strip())
    if "," in normalized:
        last, first = [part.strip() for part in normalized.split(",", 1)]
        normalized = f"{first} {last}".strip()
    return normalized


def normalize_id(value) -> str:
    """Return one stable key for integer-like IDs from CSVs and request args."""
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return str(int(value))
    key = str(value).strip()
    if key.endswith(".0") and key[:-2].lstrip("-").isdigit():
        return key[:-2]
    return key


class DataFrameLookupIndex:
    """Index dataframe rows by ID, normalized name, and last-name token.

    Index construction is intentionally separate from lookup so loaders can
    rebuild once when their 24-hour dataframe snapshot refreshes. Duplicate
    keys retain the first row, matching pandas' previous ``iloc[0]`` behavior.
    """

    def __init__(
        self,
        dataframe: pd.DataFrame,
        *,
        id_columns: Iterable[str] = (),
        name_columns: Iterable[str] = (),
        name_normalizer: Callable[[object], str] = normalize_name,
    ):
        self._by_id: dict[str, pd.Series] = {}
        self._by_name: dict[str, pd.Series] = {}
        self._by_last_name: dict[str, pd.Series] = {}
        self._ordered_names: list[tuple[str, pd.Series]] = []
        self._name_normalizer = name_normalizer

        if dataframe is None or dataframe.empty:
            return

        ids = [column for column in id_columns if column in dataframe.columns]
        names = [column for column in name_columns if column in dataframe.columns]
        for _, row in dataframe.iterrows():
            for column in ids:
                key = normalize_id(row.get(column))
                if key:
                    self._by_id.setdefault(key, row)
            seen_names: set[str] = set()
            for column in names:
                key = name_normalizer(row.get(column))
                if not key or key in seen_names:
                    continue
                seen_names.add(key)
                self._by_name.setdefault(key, row)
                self._by_last_name.setdefault(key.split()[-1], row)
                self._ordered_names.append((key, row))

    def find(
        self,
        *,
        player_id=None,
        name=None,
        last_name_fallback: bool = False,
        contains_fallback: bool = True,
    ) -> pd.Series | None:
        """Return the first matching row without scanning dataframe columns."""
        player_key = normalize_id(player_id)
        if player_key:
            row = self._by_id.get(player_key)
            if row is not None:
                return row

        name_key = self._name_normalizer(name)
        if not name_key:
            return None
        row = self._by_name.get(name_key)
        if row is not None:
            return row
        if last_name_fallback:
            row = self._by_last_name.get(name_key.split()[-1])
            if row is not None:
                return row
        if contains_fallback:
            needle = name_key.split()[-1] if last_name_fallback else name_key
            for candidate, candidate_row in self._ordered_names:
                if needle in candidate:
                    return candidate_row
        return None
