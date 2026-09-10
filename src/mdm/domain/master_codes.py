"""Framework-independent MasterCode composition and representation state."""

import hashlib
import json
from dataclasses import dataclass, fields
from datetime import datetime
from typing import ClassVar
from uuid import UUID

from mdm.domain.dimensions import (
    BrandValue,
    CategoryValue,
    CompanyValue,
    CountryValue,
    Dimension,
    MemoryValue,
    ModelValue,
    NetworkGeneration,
    YearValue,
)


class MasterCodeValidationError(ValueError):
    """A MasterCode state or one typed Dimension slot is invalid."""


MasterCodeDimension = Dimension[
    CompanyValue
    | BrandValue
    | ModelValue
    | CategoryValue
    | YearValue
    | MemoryValue
    | NetworkGeneration
    | CountryValue
]


@dataclass(frozen=True, slots=True)
class MasterCodeDimensions:
    """The eight fixed typed slots in canonical composition order."""

    company: MasterCodeDimension | None = None
    brand: MasterCodeDimension | None = None
    model: MasterCodeDimension | None = None
    category: MasterCodeDimension | None = None
    year: MasterCodeDimension | None = None
    memory: MasterCodeDimension | None = None
    network: MasterCodeDimension | None = None
    country: MasterCodeDimension | None = None

    ORDER: ClassVar[tuple[str, ...]] = (
        "company",
        "brand",
        "model",
        "category",
        "year",
        "memory",
        "network",
        "country",
    )
    TYPE_NAMES: ClassVar[tuple[str, ...]] = (
        "COMPANY",
        "BRAND",
        "MODEL",
        "CATEGORY",
        "YEAR",
        "MEMORY",
        "NETWORK",
        "COUNTRY",
    )
    _VALUE_TYPES: ClassVar[dict[str, type[object]]] = {
        "company": CompanyValue,
        "brand": BrandValue,
        "model": ModelValue,
        "category": CategoryValue,
        "year": YearValue,
        "memory": MemoryValue,
        "network": NetworkGeneration,
        "country": CountryValue,
    }

    def __post_init__(self) -> None:
        if tuple(field.name for field in fields(self)) != self.ORDER:
            raise MasterCodeValidationError("MasterCode Dimension order is invalid")
        for slot in self.ORDER:
            dimension = getattr(self, slot)
            if dimension is None:
                continue
            if not isinstance(dimension, Dimension):
                raise MasterCodeValidationError(f"{slot} must be a Dimension")
            if not isinstance(dimension.value, self._VALUE_TYPES[slot]):
                display_name = slot.title()
                raise MasterCodeValidationError(f"{slot} must contain a {display_name} value")
            if dimension.is_deleted:
                raise MasterCodeValidationError(f"{slot} Dimension must be active")

    def ordered(self) -> tuple[MasterCodeDimension | None, ...]:
        """Return the slots in the one canonical composition order."""
        return tuple(getattr(self, slot) for slot in self.ORDER)


@dataclass(frozen=True, slots=True)
class MasterCode:
    """Immutable current MasterCode state with aggregate representation ETag."""

    NOT_APPLICABLE_CODE: ClassVar[str] = "NNN"
    ETAG_ALGORITHM_VERSION: ClassVar[int] = 1

    id: UUID
    dimensions: MasterCodeDimensions
    code: str
    version: int
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None

    def __post_init__(self) -> None:
        if not isinstance(self.id, UUID):
            raise MasterCodeValidationError("id must be a UUID")
        if not isinstance(self.dimensions, MasterCodeDimensions):
            raise MasterCodeValidationError("dimensions are invalid")
        if self.code != self.compose(self.dimensions):
            raise MasterCodeValidationError("code does not match the current Dimension references")
        if type(self.version) is not int or self.version < 1:
            raise MasterCodeValidationError("version must be a positive integer")
        if self.created_at.utcoffset() is None or self.updated_at.utcoffset() is None:
            raise MasterCodeValidationError("timestamps must be timezone-aware")
        if self.updated_at < self.created_at:
            raise MasterCodeValidationError("updated_at must not precede created_at")
        if self.version == 1 and self.updated_at != self.created_at:
            raise MasterCodeValidationError("initial timestamps must match")
        if self.deleted_at is not None:
            if self.deleted_at.utcoffset() is None:
                raise MasterCodeValidationError("deleted_at must be timezone-aware")
            if not self.created_at <= self.deleted_at <= self.updated_at:
                raise MasterCodeValidationError("deleted_at must be within the state timestamps")

    @classmethod
    def create(
        cls,
        *,
        id: UUID,
        dimensions: MasterCodeDimensions,
        created_at: datetime,
    ) -> "MasterCode":
        """Create version one from the canonical pure composition method."""
        return cls(
            id=id,
            dimensions=dimensions,
            code=cls.compose(dimensions),
            version=1,
            created_at=created_at,
            updated_at=created_at,
            deleted_at=None,
        )

    @classmethod
    def compose(cls, dimensions: MasterCodeDimensions) -> str:
        """Compose exactly eight already-validated codes without re-normalizing them."""
        if not isinstance(dimensions, MasterCodeDimensions):
            raise MasterCodeValidationError("dimensions are invalid")
        return "-".join(
            cls.NOT_APPLICABLE_CODE if dimension is None else dimension.code.value
            for dimension in dimensions.ordered()
        )

    def etag(self) -> str:
        """Return the canonical aggregate strong ETag for the current representation."""
        dimension_vector = []
        for type_name, dimension in zip(
            MasterCodeDimensions.TYPE_NAMES,
            self.dimensions.ordered(),
            strict=True,
        ):
            dimension_vector.append(
                [
                    type_name,
                    None if dimension is None else str(dimension.id),
                    None if dimension is None else dimension.version,
                ]
            )
        payload = json.dumps(
            [self.ETAG_ALGORITHM_VERSION, self.version, dimension_vector],
            separators=(",", ":"),
        ).encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        return f'"mc-{self.ETAG_ALGORITHM_VERSION}-{digest}"'
