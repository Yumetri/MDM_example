"""Framework-independent Dimension values and immutable state."""

import re
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

_CODE_PATTERN = re.compile(r"^[A-Z0-9]{1,32}$", re.ASCII)
_STRING_VALUE_PATTERN = re.compile(r"^[A-Z0-9]+(?:_[A-Z0-9]+)*$", re.ASCII)
_CONTROL_PATTERN = re.compile(r"[\x00-\x1f\x7f]")


class DimensionValidationError(ValueError):
    """One public Dimension input or stored invariant is invalid."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message


@dataclass(frozen=True, slots=True)
class DimensionCode:
    """The normalized representative code shared by all Dimension types."""

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str):
            raise DimensionValidationError("code", "문자열이어야 합니다.")
        if not self.value.isascii():
            raise DimensionValidationError(
                "code", "ASCII 영문 대문자와 숫자만 사용해 1~32자로 입력해야 합니다."
            )
        if self.value.strip(" ") != self.value or " " in self.value:
            raise DimensionValidationError("code", "공백을 포함할 수 없습니다.")
        normalized = self.value.upper()
        if not _CODE_PATTERN.fullmatch(normalized):
            raise DimensionValidationError(
                "code", "ASCII 영문 대문자와 숫자만 사용해 1~32자로 입력해야 합니다."
            )
        if set(normalized) == {"N"}:
            raise DimensionValidationError(
                "code", "N으로만 이루어진 예약 코드는 사용할 수 없습니다."
            )
        object.__setattr__(self, "value", normalized)


@dataclass(frozen=True, slots=True)
class CompanyValue:
    """The normalized immutable value of a Company Dimension."""

    value: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _normalize_string_value(self.value))


@dataclass(frozen=True, slots=True)
class ModelValue:
    """The normalized immutable value of a Model Dimension."""

    value: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _normalize_string_value(self.value))


@dataclass(frozen=True, slots=True)
class BrandValue:
    """The normalized immutable value of a Brand Dimension."""

    value: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _normalize_string_value(self.value))


@dataclass(frozen=True, slots=True)
class CountryValue:
    """The normalized immutable value of a Country Dimension."""

    value: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _normalize_string_value(self.value))


@dataclass(frozen=True, slots=True)
class CategoryValue:
    """The normalized immutable value of a Category Dimension."""

    value: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _normalize_string_value(self.value))


def _normalize_string_value(value: str) -> str:
    if not isinstance(value, str):
        raise DimensionValidationError("value", "문자열이어야 합니다.")
    if _CONTROL_PATTERN.search(value):
        raise DimensionValidationError("value", "제어 문자를 포함할 수 없습니다.")
    if not value.isascii():
        raise DimensionValidationError(
            "value",
            "ASCII 영문 대문자, 숫자와 구분용 밑줄만 사용해 1~128자로 입력해야 합니다.",
        )
    normalized = value.strip(" ").upper()
    normalized = re.sub(r" +", "_", normalized)
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    if not 1 <= len(normalized) <= 128 or not _STRING_VALUE_PATTERN.fullmatch(normalized):
        raise DimensionValidationError(
            "value",
            "ASCII 영문 대문자, 숫자와 구분용 밑줄만 사용해 1~128자로 입력해야 합니다.",
        )
    return normalized


@dataclass(frozen=True, slots=True)
class Dimension[ValueT]:
    """Immutable current state shared while retaining the concrete value type."""

    id: UUID
    code: DimensionCode
    value: ValueT
    version: int
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None

    def __post_init__(self) -> None:
        if not isinstance(self.id, UUID):
            raise DimensionValidationError("id", "UUID여야 합니다.")
        if not isinstance(self.code, DimensionCode):
            raise DimensionValidationError("code", "유효한 DimensionCode여야 합니다.")
        if type(self.version) is not int or self.version < 1:
            raise DimensionValidationError("version", "1 이상의 정수여야 합니다.")
        if self.created_at.utcoffset() is None or self.updated_at.utcoffset() is None:
            raise DimensionValidationError("timestamp", "시간대가 포함된 시각이어야 합니다.")
        if self.updated_at < self.created_at:
            raise DimensionValidationError("updated_at", "created_at보다 빠를 수 없습니다.")
        if self.version == 1 and self.updated_at != self.created_at:
            raise DimensionValidationError("updated_at", "최초 생성 시각과 같아야 합니다.")
        if self.deleted_at is not None:
            if self.deleted_at.utcoffset() is None:
                raise DimensionValidationError("deleted_at", "시간대가 포함된 시각이어야 합니다.")
            if not self.created_at <= self.deleted_at <= self.updated_at:
                raise DimensionValidationError("deleted_at", "Dimension 시각 범위 안이어야 합니다.")

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None
