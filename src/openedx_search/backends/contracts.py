"""Narrow, typed document-search contracts with explicit engine limits."""

import re
from dataclasses import dataclass


def validate_field(value: str) -> None:
    """Reject syntax rather than interpolating arbitrary field expressions."""
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_.]*", value
    ):
        raise ValueError("Invalid field name")


@dataclass(frozen=True)
class FilterTerm:
    """One exact-match OR clause; multiple terms are combined with AND."""

    field: str
    values: tuple[str, ...]

    def __post_init__(self):
        validate_field(self.field)
        if not self.values:
            raise ValueError("Empty value set would broaden a search")
        for value in self.values:
            # Backticks delimit Typesense literals. Reject unsupported literals
            # consistently on both engines instead of guessing an escape rule.
            if (
                not isinstance(value, str)
                or not value
                or any(ord(char) < 32 or char in "`\\" for char in value)
            ):
                raise ValueError("Unsupported exact-filter literal")


@dataclass(frozen=True)
class IndexDefinition:
    """Explicit string document schema for the initial library-search slice."""

    name: str
    searchable_fields: tuple[str, ...]
    filterable_fields: tuple[str, ...] = ()

    def __post_init__(self):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", self.name):
            raise ValueError("Invalid index name")
        if not self.searchable_fields:
            raise ValueError("At least one searchable field is required")
        for field in self.searchable_fields + self.filterable_fields:
            validate_field(field)
            if "." in field:
                raise ValueError(
                    "Nested schema fields are outside this initial contract"
                )


@dataclass(frozen=True)
class SearchQuery:
    """One-based, bounded pagination shared by both engines."""

    text: str = ""
    filters: tuple[FilterTerm, ...] = ()
    page: int = 1
    page_size: int = 20

    def __post_init__(self):
        if type(self.page) is not int or self.page < 1:
            raise ValueError("page must be a positive integer")
        if type(self.page_size) is not int or not 1 <= self.page_size <= 250:
            raise ValueError("page_size must be between 1 and 250")
        if not isinstance(self.text, str):
            raise ValueError("query text must be a string")


@dataclass(frozen=True)
class SearchResult:
    """Documents and count accuracy; relevance order is engine-specific."""

    documents: tuple[dict, ...]
    total: int
    total_is_exact: bool


@dataclass(frozen=True)
class WriteReceipt:
    """Submission acknowledgment; completion is distinct from submission."""

    task_id: int | None = None
    complete: bool = False


class BackendError(RuntimeError):
    """Sanitized engine/transport failure, with explicit retry guidance."""

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable
