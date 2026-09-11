"""Framework-independent Dimension values and immutable state."""

import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum, EnumType, StrEnum
from typing import Any
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


@dataclass(frozen=True, slots=True)
class YearValue:
    """A strict calendar-year value supported by the Year Dimension."""

    value: int

    def __post_init__(self) -> None:
        if type(self.value) is not int or not 2000 <= self.value <= 2999:
            raise DimensionValidationError("value", "2000 이상 2999 이하의 정수여야 합니다.")


class _StrictIntegerEnumType(EnumType):
    def __call__(
        cls,
        value: Any,
        names: Any = None,
        *values: Any,
        **kwargs: Any,
    ) -> Any:
        if names is not None:
            return super().__call__(value, names, *values, **kwargs)
        if type(value) is not int:
            raise DimensionValidationError("value", "1 이상 5 이하의 정수여야 합니다.")
        try:
            return super().__call__(value)  # type: ignore[no-matching-overload]
        except ValueError:
            raise DimensionValidationError("value", "1 이상 5 이하의 정수여야 합니다.") from None


class NetworkGeneration(Enum, metaclass=_StrictIntegerEnumType):
    """A non-arithmetic Network generation from the closed 1 through 5 set."""

    GENERATION_1 = 1
    GENERATION_2 = 2
    GENERATION_3 = 3
    GENERATION_4 = 4
    GENERATION_5 = 5


class MemoryUnit(StrEnum):
    """A decimal SI unit accepted by the Memory Dimension."""

    MB = "MB"
    GB = "GB"
    TB = "TB"
    PB = "PB"


_MEMORY_UNIT_MULTIPLIERS = {
    MemoryUnit.MB: 1,
    MemoryUnit.GB: 1_000,
    MemoryUnit.TB: 1_000_000,
    MemoryUnit.PB: 1_000_000_000,
}


@dataclass(frozen=True, slots=True)
class MemoryValue:
    """An immutable Memory amount and unit with derived decimal MB capacity."""

    amount: int
    unit: MemoryUnit

    def __post_init__(self) -> None:
        if type(self.amount) is not int or not 1 <= self.amount <= 2_147_483_647:
            raise DimensionValidationError(
                "value.amount", "1 이상 2147483647 이하의 정수여야 합니다."
            )
        if not isinstance(self.unit, MemoryUnit):
            raise DimensionValidationError("value.unit", "MB, GB, TB, PB 중 하나여야 합니다.")

    @classmethod
    def create(cls, *, amount: int, unit: str) -> "MemoryValue":
        if not isinstance(unit, str) or _CONTROL_PATTERN.search(unit):
            raise DimensionValidationError("value.unit", "MB, GB, TB, PB 중 하나여야 합니다.")
        normalized_unit = unit.strip(" ").upper()
        try:
            memory_unit = MemoryUnit(normalized_unit)
        except ValueError:
            raise DimensionValidationError(
                "value.unit", "MB, GB, TB, PB 중 하나여야 합니다."
            ) from None
        return cls(amount=amount, unit=memory_unit)

    @property
    def capacity_mb(self) -> int:
        return self.amount * _MEMORY_UNIT_MULTIPLIERS[self.unit]


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

    def change_value(self, value: ValueT, *, changed_at: datetime) -> "Dimension[ValueT]":
        """Return the next immutable state for one logical value change."""
        return self.change(code=None, value=value, changed_at=changed_at)

    def change(
        self,
        *,
        code: DimensionCode | None,
        value: ValueT | None,
        changed_at: datetime,
    ) -> "Dimension[ValueT]":
        """Return one next state for an optional code and value mutation."""
        next_code = self.code if code is None else code
        next_value = (
            self.value if value is None or _dimension_values_equal(self.value, value) else value
        )
        if next_code == self.code and _dimension_values_equal(self.value, next_value):
            return self
        if changed_at.utcoffset() is None:
            raise DimensionValidationError("updated_at", "시간대가 포함된 시각이어야 합니다.")
        if changed_at < self.updated_at:
            raise DimensionValidationError("updated_at", "현재 updated_at보다 빠를 수 없습니다.")
        return Dimension(
            id=self.id,
            code=next_code,
            value=next_value,
            version=self.version + 1,
            created_at=self.created_at,
            updated_at=changed_at,
            deleted_at=self.deleted_at,
        )


def _dimension_values_equal(current: object, proposed: object) -> bool:
    if isinstance(current, MemoryValue) and isinstance(proposed, MemoryValue):
        return current.capacity_mb == proposed.capacity_mb
    return current == proposed
