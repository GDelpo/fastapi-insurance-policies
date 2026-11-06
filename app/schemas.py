"""Request/Response schemas for the Policy Service.

Hierarchical structure:
    Policy
    ├── nro_pol, ramo, etc. (legacy DB field names)
    ├── insured (Insured)
    ├── producer (Producer)
    └── endorsements[] (Endorsement)
        ├── nro_endoso
        ├── emission_date
        └── files[] (PolicyFile)
            ├── name
            ├── file_type (TOASEG, TOPROD, etc.)
            ├── original_path
            └── local_path (transformed)
"""

import math
import re
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path, PureWindowsPath
from typing import Any, Generic, TypeVar
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from email_validator import EmailNotValidError, validate_email
from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator


def to_camel(string: str) -> str:
    """Convert snake_case to camelCase."""
    components = string.split("_")
    return components[0] + "".join(x.title() for x in components[1:])


class CamelModel(BaseModel):
    """Base model with camelCase serialization."""

    model_config = ConfigDict(
        from_attributes=True,
        populate_by_name=True,
        alias_generator=to_camel,
    )


# ============================================================================
# Enums
# ============================================================================


class FileType(str, Enum):
    """Policy file type."""

    TOASEG = "TOASEG"  # For insured
    TOASEG_CERT = "TOASEG_CERT"  # Certificate for insured
    TOPROD = "TOPROD"  # For producer
    TOORG = "TOORG"  # Organization
    UNKNOWN = "UNKNOWN"  # Unknown type


# ============================================================================
# Core Models - Hierarchical
# ============================================================================


def _decimal_to_str(v: Any) -> str:
    """Convert Decimal or numeric types to string."""
    if v is None:
        return ""
    if isinstance(v, Decimal):
        return str(int(v)) if v == int(v) else str(v)
    return str(v)


# ============================================================================
# Name & Email Sanitization
# ============================================================================

# Lowercase particles that should NOT be title-cased (Spanish/Italian common)
_NAME_PARTICLES = frozenset({"de", "del", "di", "da", "la", "las", "los", "el", "y", "e"})

# Legal suffixes that must stay UPPERCASE (matched case-insensitively)
_LEGAL_SUFFIXES = frozenset(
    {
        "sa",
        "srl",
        "sas",
        "se",
        "sac",
        "sci",  # plain
        "s.a.",
        "s.r.l.",
        "s.a.s.",
        "s.e.",
        "s.a.c.",
        "s.c.i.",  # dotted
        "ltda",
        "ltda.",
        "cia",
        "cia.",  # limited / compañía
        "inc",
        "inc.",
        "corp",
        "corp.",
        "llc",  # international
    }
)

# Patterns that indicate a non-real email in legacy DB data
_JUNK_EMAIL_RE = re.compile(
    r"^(no\s*tiene|sin\s*mail|sin\s*email|no\s*posee|n/?a|s/?d|xxx|no@|prueba@|test@|-+|\.\.+|\s+)$",
    re.IGNORECASE,
)


def sanitize_name(raw: str) -> str:
    """Normalize a person/company name from legacy DB.

    - Title-case from ALL CAPS
    - Fix spacing around commas:  "PEREZ , JUAN" → "Perez, Juan"
    - Preserve lowercase particles: "De La Rosa" stays as "de la Rosa"
    - Preserve legal suffixes in UPPERCASE: "SRL", "S.A.", "SA", etc.
    - Collapse multiple spaces
    """
    if not raw or not raw.strip():
        return raw

    # Fix commas: remove spaces before, ensure one space after
    name = re.sub(r"\s*,\s*", ", ", raw.strip())
    # Collapse multiple spaces
    name = re.sub(r"\s{2,}", " ", name)

    # Title-case each word, respecting particles and legal suffixes
    parts = []
    for i, word in enumerate(name.split()):
        # Handle comma attached to word (e.g. "Perez,")
        trailing = ""
        if word.endswith(","):
            trailing = ","
            word = word[:-1]

        lower = word.lower()
        if lower in _LEGAL_SUFFIXES:
            parts.append(word.upper() + trailing)
        elif i > 0 and lower in _NAME_PARTICLES:
            parts.append(lower + trailing)
        else:
            parts.append(word.capitalize() + trailing)

    return " ".join(parts)


def sanitize_email(raw: str | None) -> str | None:
    """Validate and normalize an email address.

    - Returns normalized (lowercased) email if valid
    - Returns None for junk/placeholder values or invalid addresses
    """
    if not raw or not raw.strip():
        return None

    cleaned = raw.strip()

    # Filter out known junk patterns from legacy DBs
    if _JUNK_EMAIL_RE.match(cleaned):
        return None

    try:
        result = validate_email(cleaned, check_deliverability=False)
        return result.normalized.lower()
    except EmailNotValidError:
        return None


class Producer(CamelModel):
    """Insurance producer/agent."""

    code: str = Field(..., description="Producer code (cod_agente)")
    name: str = Field(..., description="Full name")
    email: str | None = Field(None, description="Producer email")

    @field_validator("code", mode="before")
    @classmethod
    def convert_code(cls, v: Any) -> str:
        return _decimal_to_str(v)

    @field_validator("name", mode="before")
    @classmethod
    def clean_name(cls, v: Any) -> str:
        return sanitize_name(_decimal_to_str(v))

    @field_validator("email", mode="before")
    @classmethod
    def clean_email(cls, v: Any) -> str | None:
        return sanitize_email(v)


class Insured(CamelModel):
    """Insured person."""

    code: str = Field(..., description="Insured code (cod_aseg)")
    name: str = Field(..., description="Full name")
    email: str | None = Field(None, description="Insured email")

    @field_validator("code", mode="before")
    @classmethod
    def convert_code(cls, v: Any) -> str:
        return _decimal_to_str(v)

    @field_validator("name", mode="before")
    @classmethod
    def clean_name(cls, v: Any) -> str:
        return sanitize_name(_decimal_to_str(v))

    @field_validator("email", mode="before")
    @classmethod
    def clean_email(cls, v: Any) -> str | None:
        return sanitize_email(v)


# Marker prefix in DB for files that are no longer available
UNAVAILABLE_MARKER = "ARCHIVO NO DISPONIBLE"

# Excluded path prefixes (shares we don't have access to)
# Loaded from settings at module level; updated by app startup
_excluded_path_prefixes: list[str] = []


class PolicyFile(CamelModel):
    """File associated with an endorsement.

    Original path comes from SQL Server and is transformed
    to a locally accessible path via configuration.

    The core system marks files it no longer has by replacing the path with a
    literal Spanish marker, 'ARCHIVO NO DISPONIBLE' ("file not available"),
    followed by a free-text note and the file name. Only the prefix is matched;
    these files are flagged with available=False.
    """

    name: str = Field(..., description="File name")
    file_type: FileType = Field(default=FileType.UNKNOWN, description="File type")
    original_path: str | None = Field(
        None, description="Original network path from DB", exclude=True
    )
    local_path: str | None = Field(None, description="Transformed local path (mount point)")
    available: bool = Field(True, description="False if file marked as unavailable in DB")
    unavailable_reason: str | None = Field(
        None,
        description="'excluded' (inaccessible share) or 'unavailable' (marked in DB) when available=False",
    )
    created_at: datetime | None = Field(None, description="Creation date")

    # Storage metadata (populated from Redis — written by dispatcher via PATCH)
    in_storage: bool = Field(False, description="File exists in cloud storage")
    storage_id: str | None = Field(None, description="Cloud storage file ID", exclude=True)
    storage_link: str | None = Field(None, description="Cloud storage view/download link")

    @staticmethod
    def detect_type(filename: str) -> FileType:
        """Detect file type by filename."""
        name_upper = filename.upper()

        if "TOASEG_CERT" in name_upper or "_CERT" in name_upper:
            return FileType.TOASEG_CERT
        elif "TOASEG" in name_upper:
            return FileType.TOASEG
        elif "TOPROD" in name_upper:
            return FileType.TOPROD
        elif "TOORG" in name_upper:
            return FileType.TOORG

        return FileType.UNKNOWN

    @staticmethod
    def is_unavailable(path: str) -> bool:
        """Check if the DB path is marked as unavailable.

        Matches the UNAVAILABLE_MARKER prefix the core system writes in place
        of the path (e.g. 'ARCHIVO NO DISPONIBLE - <note>\\filename.pdf').
        """
        if not path:
            return False
        return path.upper().startswith(UNAVAILABLE_MARKER)

    @staticmethod
    def is_excluded(path: str) -> bool:
        """Check if the path belongs to an excluded/inaccessible share.

        Some files exist on shares we don't have mounted
        (e.g. \\\\fileserver\\policies-urgent, c:\\PolicyPDF, E:\\policies-urgent).
        These are detected via configurable prefix list.
        """
        if not path:
            return False
        path_upper = path.upper().replace("/", "\\")
        return any(
            path_upper.startswith(prefix.upper().replace("/", "\\"))
            for prefix in _excluded_path_prefixes
        )

    @staticmethod
    def escapes_mount(path: str) -> bool:
        """True if the path has a '..' segment (it could resolve outside the mount)."""
        if not path:
            return False
        return ".." in path.replace("\\", "/").split("/")

    @staticmethod
    def transform_path(
        original_path: str,
        local_prefix: str,
    ) -> str | None:
        """Transform network path (UNC) to local mount path.

        Mount point maps directly to the UNC share root:
          \\\\fileserver\\policies\\  →  /media/polizas/

        If local_prefix is empty, returns original_path as-is.
        Returns None for paths marked as unavailable.

        Args:
            original_path: E.g.: \\\\fileserver\\policies\\file.pdf
            local_prefix: E.g.: /media/polizas

        Returns:
            Transformed path, original if no prefix, or None if unavailable
        """
        if not original_path:
            return None

        # Detect unavailable files
        if PolicyFile.is_unavailable(original_path):
            return None

        # Detect excluded paths (inaccessible shares)
        if PolicyFile.is_excluded(original_path):
            return None

        # If no local_prefix, use original path as-is
        if not local_prefix:
            return original_path

        # Normalize to forward slashes
        path_norm = original_path.replace("\\", "/")

        # Detect if UNC path (//server/share/...)
        if not path_norm.startswith("//"):
            # Not UNC, return as-is
            return original_path

        # Extract UNC parts: //server/share/rest
        parts = path_norm[2:].split("/")  # Remove initial //

        if len(parts) < 3:
            # Not enough components, return original
            return original_path

        # server = parts[0], share = parts[1], rest = parts[2:]
        relative_parts = parts[2:]  # Everything after server/share

        # A ".." segment would resolve outside the mount; such a path is never
        # handed out as a local path.
        if PolicyFile.escapes_mount(original_path):
            return None

        # Build local path: /media/polizas/subdir/file.pdf
        return str(Path(local_prefix).joinpath(*relative_parts))

    @classmethod
    def from_db_row(
        cls,
        txt_path: str | None,
        created_at: datetime | None,
        local_prefix: str = "",
    ) -> "PolicyFile | None":
        """Create from database row.

        Handles three cases:
        1. txt_path is None/empty → returns None (no file)
        2. txt_path starts with 'ARCHIVO NO DISPONIBLE' → unavailable file
        3. Normal UNC path → transforms to local mount path
        """
        if not txt_path:
            return None

        # Detect unavailable or excluded files
        unavailable = cls.is_unavailable(txt_path)
        # A path that could escape the mount is treated like an inaccessible share.
        excluded = (
            (cls.is_excluded(txt_path) or cls.escapes_mount(txt_path)) if not unavailable else False
        )
        is_accessible = not unavailable and not excluded

        name = PureWindowsPath(txt_path).name
        file_type = cls.detect_type(name)
        local_path = cls.transform_path(txt_path, local_prefix) if is_accessible else None

        unavailable_reason: str | None
        if excluded:
            unavailable_reason = "excluded"
        elif unavailable:
            unavailable_reason = "unavailable"
        else:
            unavailable_reason = None

        return cls(
            name=name,
            file_type=file_type,
            original_path=txt_path,
            local_path=local_path,
            available=is_accessible,
            unavailable_reason=unavailable_reason,
            created_at=created_at,
        )


class Endorsement(CamelModel):
    """Policy endorsement.

    nro_endoso = 0 is the original policy.
    nro_endoso > 0 are modifications/renewals.
    """

    nro_endoso: int = Field(..., description="Endorsement number (0 = original)")
    emission_date: date | None = Field(None, description="Emission date")
    files: list[PolicyFile] = Field(default_factory=list, description="Endorsement files")

    @property
    def file_count(self) -> int:
        return len(self.files)


class Policy(CamelModel):
    """Complete policy with full hierarchy.

    Central domain model — the ONE response type for all policy queries.

    Hierarchy:
        Policy → Endorsement → PolicyFile
        Policy → Producer (agent)
        Policy → Insured (tomador/asegurado)

    Storage enrichment is optional: when requested, each PolicyFile
    includes in_storage, storage_id, storage_link fields.
    """

    nro_pol: str = Field(..., description="Policy number")
    ramo: str = Field(default="", description="Policy type/branch")
    emission_date: date | None = Field(None, description="Emission date (from endorsement 0)")
    insured: Insured
    producer: Producer
    endorsements: list[Endorsement] = Field(default_factory=list)

    @field_validator("nro_pol", "ramo", mode="before")
    @classmethod
    def convert_to_str(cls, v: Any) -> str:
        return _decimal_to_str(v)

    @computed_field
    @property
    def endorsement_count(self) -> int:
        """Number of endorsements."""
        return len(self.endorsements)

    @computed_field
    @property
    def total_file_count(self) -> int:
        """Total files across all endorsements."""
        return sum(e.file_count for e in self.endorsements)

    @computed_field
    @property
    def has_files(self) -> bool:
        """True if at least one endorsement has available files."""
        return any(f.available for f in self.all_files())

    @computed_field
    @property
    def unavailable_file_count(self) -> int:
        """Number of files marked as unavailable in DB."""
        return sum(1 for f in self.all_files() if not f.available)

    @computed_field
    @property
    def all_in_storage(self) -> bool:
        """True if all AVAILABLE files are in cloud storage.

        Unavailable/excluded files are ignored — they can never be uploaded.
        A policy is storage-ready when every available file has been uploaded.
        """
        avail = [f for f in self.all_files() if f.available]
        return len(avail) > 0 and all(f.in_storage for f in avail)

    def all_files(self) -> list[PolicyFile]:
        """Get all files from all endorsements."""
        files = []
        for endorsement in self.endorsements:
            files.extend(endorsement.files)
        return files


# ============================================================================
# Response Models - Entity endpoints
# ============================================================================


class ProducerResponse(CamelModel):
    """Producer information."""

    code: str
    name: str
    email: str | None = None

    @field_validator("name", mode="before")
    @classmethod
    def clean_name(cls, v: Any) -> str:
        return sanitize_name(str(v)) if v else ""

    @field_validator("email", mode="before")
    @classmethod
    def clean_email(cls, v: Any) -> str | None:
        return sanitize_email(v)


class InsuredResponse(CamelModel):
    """Insured information."""

    code: str
    name: str
    email: str | None = None

    @field_validator("name", mode="before")
    @classmethod
    def clean_name(cls, v: Any) -> str:
        return sanitize_name(str(v)) if v else ""

    @field_validator("email", mode="before")
    @classmethod
    def clean_email(cls, v: Any) -> str | None:
        return sanitize_email(v)


# ============================================================================
# Readiness (computed view over policies)
# ============================================================================


class MarkSentRequest(CamelModel):
    """Request body for marking policies as sent in timpresion_pdf."""

    nro_pol_list: list[str] = Field(..., min_length=1, description="Policy numbers to mark as sent")
    sent_date: date = Field(..., description="Date on which the policies were sent")


class MarkSentResponse(CamelModel):
    """Response after marking policies as sent."""

    updated_rows: int = Field(..., description="Number of timpresion_pdf rows updated")
    policy_count: int = Field(..., description="Number of distinct policies processed")


def _normalize_storage_url(url: str | None) -> str | None:
    """Normaliza un storage_link de GCS asegurando que el object name esté
    correctamente encodado con %20 para espacios.

    Decodea completamente (cubre espacios crudos, %20, y doble-encoding %2520)
    antes de re-encodear, garantizando un resultado canónico sin importar
    qué vino del caller.
    """
    if not url:
        return url
    try:
        parsed = urlsplit(url)
        parts = parsed.path.split("/", 2)
        if len(parts) < 3:
            return url
        bucket, object_name = parts[1], parts[2]
        # Decodear hasta valor estable (cubre %20, %2520, espacios crudos)
        raw = object_name
        while True:
            decoded = unquote(raw)
            if decoded == raw:
                break
            raw = decoded
        encoded = quote(raw, safe="")
        if encoded == object_name:
            return url  # ya está bien, no tocar
        fixed_path = f"/{bucket}/{encoded}"
        return urlunsplit((parsed.scheme, parsed.netloc, fixed_path, parsed.query, parsed.fragment))
    except Exception:
        return url  # si algo falla, devolver el original sin romper nada


class StorageMetadataUpdate(CamelModel):
    """Request body for updating storage metadata on a policy file.

    Sent by the dispatcher service after uploading a file to storage.
    Stored in Redis and served transparently with policy data.
    """

    storage_id: str = Field(..., description="Cloud storage file ID")
    storage_link: str = Field(..., description="Cloud storage view/download link")

    @field_validator("storage_link", mode="before")
    @classmethod
    def normalize_storage_link(cls, v: Any) -> str | None:
        return _normalize_storage_url(v)


class StorageMetadataResponse(CamelModel):
    """Response after saving storage metadata."""

    policy_id: str
    file_id: str
    storage_id: str
    storage_link: str
    saved: bool = True


class MissingPolicyItem(CamelModel):
    """Policy that is missing files or not yet in storage."""

    nro_pol: str
    ramo: str = ""
    insured_name: str = ""
    producer_name: str = ""
    reason: str = Field(
        default="NO_FILES",
        description="Reason: NO_FILES, UNAVAILABLE, EXCLUDED, NOT_IN_STORAGE",
    )
    file_count: int | None = Field(None, description="Total available files (for NOT_IN_STORAGE)")
    files_in_storage: int | None = Field(
        None, description="Files already uploaded (for NOT_IN_STORAGE)"
    )


class ReadinessResponse(CamelModel):
    """Readiness status for a given emission date.

    Answers: 'Are all policies for this date ready to be dispatched?'

    Fields breakdown:
      total          — direct COUNT(DISTINCT nro_pol) from pv_header (stable, no file dependency)
      with_files     — total - no_files_yet (have at least one timpresion_pdf row)
      complete       — have at least one AVAILABLE file for the target-date endorsement
      in_storage     — complete AND all available files for the target-date endorsement in Cloud
      not_in_storage — have available files but NOT all uploaded yet  (complete - in_storage)
      no_files_yet   — confirmed by NOT EXISTS: target-date endorsement has zero timpresion_pdf rows
      missing        — truly undeliverable: no_files_yet + unavailable/excluded-only (from cache)
      ready          — True only when total > 0 AND missing == 0 AND not_in_storage == 0
    """

    date: date
    total: int = Field(
        ..., description="Policies emitted this date — direct pv_header count, stable baseline"
    )
    with_files: int = Field(
        0, description="Policies with at least one file record in DB (available or not)"
    )
    no_files_yet: int = Field(
        0, description="Policies with no file records at all — PDF not generated yet"
    )
    complete: int = Field(..., description="Policies with at least one available file")
    missing: int = Field(
        ..., description="Truly undeliverable: no PDF yet + files all unavailable/excluded"
    )
    in_storage: int = Field(0, description="Policies with ALL available files in cloud storage")
    not_in_storage: int = Field(
        0, description="Policies with available files but not fully uploaded"
    )
    ready: bool = Field(
        ..., description="True only when total > 0, missing == 0, not_in_storage == 0"
    )
    missing_policies: list[MissingPolicyItem] = Field(
        default_factory=list,
        description="Policies with issues: no files or not in storage (when include_missing=true)",
    )


# ============================================================================
# Pagination — matches identidad/mailsender pattern
# ============================================================================


T = TypeVar("T")


class PaginationMeta(CamelModel):
    """Pagination metadata — aligned with identidad/mailsender standard."""

    total: int = Field(..., description="Total items matching the query")
    page: int = Field(..., description="Current page (1-indexed)")
    page_size: int = Field(..., description="Items per page")
    total_pages: int = Field(..., description="Total number of pages")
    has_next: bool = Field(..., description="Whether there is a next page")
    has_previous: bool = Field(..., description="Whether there is a previous page")

    @classmethod
    def from_params(
        cls,
        limit: int,
        skip: int,
        total: int,
    ) -> "PaginationMeta":
        """Build from pagination params and total count."""
        total_pages = math.ceil(total / limit) if limit > 0 else 0
        page = (skip // limit) + 1 if limit > 0 else 1
        return cls(
            total=total,
            page=page,
            page_size=limit,
            total_pages=total_pages,
            has_next=skip + limit < total,
            has_previous=skip > 0,
        )


class PaginatedResponse(CamelModel, Generic[T]):
    """Generic paginated response — consistent across all microservices."""

    data: list[T]
    meta: PaginationMeta


# ============================================================================
# Health — matches identidad/mailsender dependency-check pattern
# ============================================================================


class ServiceStatus(str, Enum):
    """Overall service health status."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


class DependencyHealth(CamelModel):
    """Health status for a single dependency."""

    name: str
    status: ServiceStatus
    required: bool = True
    latency_ms: float | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class HealthResponse(CamelModel):
    """Health check response with per-dependency detail."""

    status: ServiceStatus
    version: str = ""
    environment: str = ""
    dependencies: dict[str, DependencyHealth] = Field(default_factory=dict)


class CacheWarmupResponse(CamelModel):
    """Response for cache warmup."""

    success: bool
    policies_cached: int = 0
    duration_seconds: float = 0.0


# ============================================================================
# Error & Auth
# ============================================================================


class ErrorResponse(BaseModel):
    """Standard error response — consistent across all microservices."""

    success: bool = False
    error: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


# ============================================================================
# Module initialization: load excluded paths from config
# ============================================================================


def _init_excluded_paths() -> None:
    """Load excluded path prefixes from settings into module-level variable."""
    global _excluded_path_prefixes
    try:
        from app.config import settings

        _excluded_path_prefixes = list(settings.excluded_paths)
    except Exception:
        _excluded_path_prefixes = []


_init_excluded_paths()
