"""SQL queries and row mappers for the Policy domain.

Single source of truth for:
  - Column aliases (constants — prevent silent bugs on schema changes)
  - SQL query strings (base query + filters + lookup queries)
  - Row mappers (pure functions: list[dict] → domain objects)

Design:
  - All SQL in one place → easy to audit, test, and adjust for DB version quirks
  - Mappers are pure functions → no side effects, trivially unit-testable
  - Repository imports from here; nothing here imports from repository or service

SQL Server 2008 R2 compatibility notes:
  - ROW_NUMBER() OVER (...) used for pagination (OFFSET/FETCH requires 2012+)
  - No correlated subqueries in SELECT (error 8180 via FreeTDS prepared statements)
  - CAST(date AS DATE) works on 2008 R2 for date truncation
"""

from typing import Any

# ============================================================================
# Column name constants
# Aliases defined in POLICY_BASE SELECT — referenced in map_policies()
# ============================================================================

# pv_header
COL_NRO_POL = "nro_pol"
COL_NRO_ENDOSO = "nro_endoso"
COL_FECHA_EMISION = "fecha_emision"
COL_COD_AGENTE = "cod_agente"
COL_COD_ASEG = "cod_aseg"

# Computed / aliased columns
COL_RAMO = "ramo"
COL_NOMBRE_ASEGURADO = "nombre_asegurado"
COL_MAIL_ASEGURADO = "mail_asegurado"
COL_NOMBRE_AGENTE = "nombre_agente"
COL_MAIL_AGENTE = "mail_agente"
COL_ORIGINAL_PATH = "original_path"
COL_FECHA_CREACION = "fecha_creacion"

# Producer / insured lookup columns
COL_CODE = "code"
COL_NAME = "name"
COL_EMAIL = "email"


# ============================================================================
# SQL — Policies
#
# POLICY_BASE is the canonical join query equivalent to the production report:
#
#   pv_header (core)
#   ├── timpresion_pdf     (PDF file records)
#   ├── maseg_header       (insured link)
#   │   └── mpersona       (insured name)
#   │       └── mpersona_telef cod=99  (insured email)
#   ├── magente            (producer)
#   │   └── mpersona_telef cod=74  (producer email)
#   └── tramo              (policy type / ramo)
# ============================================================================

POLICY_BASE = """
    SELECT
        r.txt_desc                              AS ramo,
        pv.nro_pol,
        pv.nro_endoso,
        CAST(pv.fec_emi AS DATE)                AS fecha_emision,
        mh.cod_aseg,
        RTRIM(CONVERT(VARCHAR(64),
            ISNULL(per.txt_apellido1, '') + ' ' + ISNULL(per.txt_nombre, '')
        ))                                      AS nombre_asegurado,
        telAseg.txt_telefono                    AS mail_asegurado,
        pv.cod_agente,
        ag.txt_cheque_a_nom                     AS nombre_agente,
        telAgente.txt_telefono                  AS mail_agente,
        pdf.txt_path                            AS original_path,
        pdf.fecha                               AS fecha_creacion
    FROM pv_header pv
    LEFT JOIN timpresion_pdf   pdf     ON pv.id_pv       = pdf.id_pv
    JOIN       maseg_header    mh      ON pv.cod_aseg     = mh.cod_aseg
    JOIN       mpersona        per     ON mh.id_persona   = per.id_persona
    JOIN       magente         ag      ON pv.cod_agente   = ag.cod_agente
    JOIN       tramo           r       ON pv.cod_ramo     = r.cod_ramo
    LEFT JOIN  mpersona_telef  telAseg
                ON telAseg.id_persona   = per.id_persona
               AND telAseg.cod_tipo_telef  = 99
    LEFT JOIN  mpersona_telef  telAgente
                ON telAgente.id_persona  = ag.id_persona
               AND telAgente.cod_tipo_telef = 74
    WHERE 1=1
"""

# Get a single policy by number (no pagination needed)
POLICY_BY_NUMBER = (
    POLICY_BASE
    + """
    AND pv.nro_pol = ?
    ORDER BY pv.nro_endoso, pdf.fecha
"""
)

# Load all policies since a date — used by cache sync loop
POLICY_SINCE_DATE = (
    POLICY_BASE
    + """
    AND CAST(pdf.fecha AS DATE) >= ?
    ORDER BY pv.nro_pol, pv.nro_endoso, pdf.fecha
"""
)

# ── WHERE clause fragments ─────────────────────────────────────────────────
# Each fragment is used with build_paginated_query() below.
# Always start with AND so they compose cleanly into "WHERE 1=1 {fragment}".

WHERE_NO_FILTER = ""  # no additional filter — returns all policies
WHERE_EMISSION_DATE = "AND CAST(pv.fec_emi  AS DATE) = ?"

# Direct count of policies emitted on a date — bypasses timpresion_pdf join.
# This is the STABLE baseline: only looks at pv_header, no file dependency.
# Use this for readiness checks so policies without PDFs yet are never missed.
COUNT_EMITTED_BY_DATE = """
    SELECT COUNT(DISTINCT pv.nro_pol) AS total
    FROM pv_header pv
    WHERE CAST(pv.fec_emi AS DATE) = ?
"""

# Policies emitted on a date that have NO entry in timpresion_pdf at all.
# These are the ones whose PDFs haven't been generated yet by the backend system.
# Uses NOT EXISTS (correlated subquery in WHERE — safe with FreeTDS, unlike SELECT).
POLICIES_NO_FILES_BY_DATE = """
    SELECT
        pv.nro_pol,
        r.txt_desc                              AS ramo,
        mh.cod_aseg,
        RTRIM(CONVERT(VARCHAR(64),
            ISNULL(per.txt_apellido1, '') + ' ' + ISNULL(per.txt_nombre, '')
        ))                                      AS nombre_asegurado,
        pv.cod_agente,
        ag.txt_cheque_a_nom                     AS nombre_agente
    FROM pv_header pv
    JOIN maseg_header mh ON pv.cod_aseg   = mh.cod_aseg
    JOIN mpersona    per ON mh.id_persona  = per.id_persona
    JOIN magente      ag ON pv.cod_agente  = ag.cod_agente
    JOIN tramo         r ON pv.cod_ramo    = r.cod_ramo
    WHERE CAST(pv.fec_emi AS DATE) = ?
      AND NOT EXISTS (
          SELECT 1 FROM timpresion_pdf pdf WHERE pdf.id_pv = pv.id_pv
      )
    ORDER BY pv.nro_pol
"""
WHERE_CREATION_DATE = "AND CAST(pdf.fecha   AS DATE) = ?"
WHERE_EMISSION_RANGE = "AND CAST(pv.fec_emi  AS DATE) BETWEEN ? AND ?"
WHERE_CREATION_RANGE = "AND CAST(pdf.fecha   AS DATE) BETWEEN ? AND ? AND pdf.txt_path IS NOT NULL"
WHERE_CREATION_DATE_FILES = (
    "AND CAST(pdf.fecha   AS DATE) = ?          AND pdf.txt_path IS NOT NULL"
)


# ============================================================================
# SQL — Producers
# ============================================================================

PRODUCER_BY_CODE = """
    SELECT
        ag.cod_agente       AS code,
        ag.txt_cheque_a_nom AS name,
        tel.txt_telefono    AS email
    FROM magente ag
    LEFT JOIN mpersona_telef tel
           ON tel.id_persona    = ag.id_persona
          AND tel.cod_tipo_telef = 74
    WHERE ag.cod_agente = ?
"""


# ROW_NUMBER() for SQL Server 2008 R2 pagination compatibility.
# Use build_producers_query() instead of this constant directly.
def build_producers_query(search: str | None = None, order_dir: str = "ASC") -> tuple[str, list]:
    """Return (sql, extra_params) for producers list.

    Params order: [*extra_params, offset, offset+limit]
    Optional search filters by name OR email (LIKE, case-insensitive on SQL Server).
    """
    direction = "DESC" if order_dir.upper() == "DESC" else "ASC"
    if search:
        where = "(ag.txt_cheque_a_nom LIKE ? OR tel.txt_telefono LIKE ?)"
        extra_params: list = [f"%{search}%", f"%{search}%"]
    else:
        where = "ag.txt_cheque_a_nom IS NOT NULL"
        extra_params = []

    sql = f"""
        SELECT code, name, email
        FROM (
            SELECT
                ag.cod_agente       AS code,
                ag.txt_cheque_a_nom AS name,
                tel.txt_telefono    AS email,
                ROW_NUMBER() OVER (ORDER BY ag.txt_cheque_a_nom {direction}) AS rn
            FROM magente ag
            LEFT JOIN mpersona_telef tel
                   ON tel.id_persona    = ag.id_persona
                  AND tel.cod_tipo_telef = 74
            WHERE {where}
        ) AS paged
        WHERE rn > ? AND rn <= ?
    """
    return sql, extra_params


# ============================================================================
# SQL — Insureds
# ============================================================================

INSURED_BY_CODE = """
    SELECT TOP 1
        mh.cod_aseg      AS code,
        RTRIM(CONVERT(VARCHAR(64),
            ISNULL(per.txt_apellido1, '') + ' ' + ISNULL(per.txt_nombre, '')
        ))               AS name,
        tel.txt_telefono AS email
    FROM maseg_header mh
    JOIN       mpersona        per ON mh.id_persona   = per.id_persona
    LEFT JOIN  mpersona_telef  tel ON tel.id_persona   = per.id_persona
                                  AND tel.cod_tipo_telef = 99
    WHERE mh.cod_aseg = ?
"""


def build_insureds_query(search: str | None = None, order_dir: str = "ASC") -> tuple[str, list]:
    """Return (sql, extra_params) for the insureds list.

    Queries maseg_header directly — no join to pv_header or timpresion_pdf,
    which avoids duplicate rows caused by multiple policies/files per insured.

    Optional search filters by name OR email (LIKE, case-insensitive on SQL Server).

    Params order: [*extra_params, offset, offset+limit]
    """
    direction = "DESC" if order_dir.upper() == "DESC" else "ASC"
    if search:
        where = (
            "(ISNULL(per.txt_apellido1, '') + ' ' + ISNULL(per.txt_nombre, '') LIKE ?"
            " OR tel.txt_telefono LIKE ?)"
        )
        extra_params: list = [f"%{search}%", f"%{search}%"]
    else:
        where = "mh.cod_aseg IS NOT NULL"
        extra_params = []

    sql = f"""
        SELECT code, name, email
        FROM (
            SELECT
                mh.cod_aseg      AS code,
                RTRIM(CONVERT(VARCHAR(64),
                    ISNULL(per.txt_apellido1, '') + ' ' + ISNULL(per.txt_nombre, '')
                ))               AS name,
                tel.txt_telefono AS email,
                ROW_NUMBER() OVER (
                    ORDER BY per.txt_apellido1 {direction}, per.txt_nombre {direction}
                ) AS rn
            FROM maseg_header mh
            JOIN      mpersona       per ON mh.id_persona = per.id_persona
            LEFT JOIN mpersona_telef tel ON tel.id_persona = per.id_persona
                                       AND tel.cod_tipo_telef = 99
            WHERE {where}
        ) AS paged
        WHERE rn > ? AND rn <= ?
    """
    return sql, extra_params


# ============================================================================
# SQL — Mark sent (write operation)
# ============================================================================


def build_mark_sent_query(count: int) -> tuple[str, ...]:
    """Return the UPDATE query for marking timpresion_pdf rows as sent.

    Marks ALL pdf rows for the given policy numbers (both TOPROD and TOASEG files),
    but only if they have not been marked yet (sn_envio IS NULL OR sn_envio = 0).

    Parameters order: [sent_date, nro_pol_1, nro_pol_2, ...]
    Returns the SQL string.
    """
    placeholders = ", ".join(["?"] * count)
    sql = f"""
        UPDATE timpresion_pdf
        SET sn_envio = 1, fec_envio = ?
        WHERE id_pv IN (
            SELECT id_pv FROM pv_header WHERE nro_pol IN ({placeholders})
        )
        AND (sn_envio IS NULL OR sn_envio = 0)
    """
    return sql


# ============================================================================
# Query builder
# ============================================================================

_SORT_FIELD_MAP = {
    "emission_date": "fec_emi",
    "nro_pol": "nro_pol",
}


def build_paginated_query(
    where_clause: str,
    order_dir: str = "DESC",
    sort_by: str = "emission_date",
) -> str:
    """Build a paginated policy query for the given WHERE clause fragment.

    Uses ROW_NUMBER() CTE pattern (SQL Server 2008 R2 compatible).
    Parameters expected: [...filter params..., offset, offset + limit]

    Args:
        where_clause: A WHERE fragment starting with AND, e.g. WHERE_EMISSION_DATE
        order_dir:    "DESC" (default, most recent first) or "ASC" (oldest first)
        sort_by:      "emission_date" (default) or "nro_pol"
    """
    _dir = "DESC" if order_dir.upper() == "DESC" else "ASC"
    _col = _SORT_FIELD_MAP.get(sort_by, "fec_emi")
    _secondary = "nro_pol" if _col == "fec_emi" else "fec_emi"
    # Guard: _col and _dir are constrained to known safe values
    assert _col in ("fec_emi", "nro_pol"), f"Unexpected sort column: {_col}"
    assert _dir in ("ASC", "DESC"), f"Unexpected sort direction: {_dir}"
    return f"""
        WITH distinct_policies AS (
            SELECT pv.nro_pol, MAX(pv.fec_emi) AS fec_emi
            FROM pv_header pv
            LEFT JOIN timpresion_pdf pdf ON pv.id_pv = pdf.id_pv
            WHERE 1=1 {where_clause}
            GROUP BY pv.nro_pol
        ),
        ranked_policies AS (
            SELECT nro_pol, ROW_NUMBER() OVER (ORDER BY {_col} {_dir}, {_secondary} {_dir}) AS rn
            FROM distinct_policies
        ),
        paginated_policies AS (
            SELECT nro_pol FROM ranked_policies WHERE rn > ? AND rn <= ?
        )
        {POLICY_BASE}
        AND pv.nro_pol IN (SELECT nro_pol FROM paginated_policies)
        ORDER BY pv.{_col} {_dir}, pv.nro_pol {_dir}, pv.nro_endoso, pdf.fecha
    """


# ============================================================================
# Row mappers — pure functions
#
# Input:  raw list[dict] from sql_client.execute_query()
# Output: domain objects (Policy, ProducerResponse, InsuredResponse)
#
# Schemas are imported locally to keep this module free of circular imports
# and importable without a running app context (useful for unit tests).
# ============================================================================


def map_policies(rows: list[dict], local_prefix: str) -> list[Any]:
    """Map flat DB rows → list[Policy] (Policy → Endorsement → Files).

    Single pass grouping:
        1. Group rows by nro_pol → endorsements → files
        2. Build Policy objects from grouped data

    Args:
        rows:         Raw rows from POLICY_BASE query
        local_prefix: Mount prefix for UNC path transformation (e.g. /media/polizas)

    Returns:
        list[Policy] preserving DB ordering within each policy
    """
    from app.schemas import Endorsement, Insured, Policy, PolicyFile, Producer

    if not rows:
        return []

    # ── Step 1: group flat rows ───────────────────────────────────────────
    grouped: dict[str, dict] = {}

    for row in rows:
        nro_pol = row[COL_NRO_POL]
        nro_endoso = row[COL_NRO_ENDOSO]

        if nro_pol not in grouped:
            grouped[nro_pol] = {"info": row, "endorsements": {}}

        endorsements = grouped[nro_pol]["endorsements"]

        if nro_endoso not in endorsements:
            endorsements[nro_endoso] = {
                "emission_date": row[COL_FECHA_EMISION],
                "files": [],
            }

        if row.get(COL_ORIGINAL_PATH):
            file = PolicyFile.from_db_row(
                txt_path=row[COL_ORIGINAL_PATH],
                created_at=row.get(COL_FECHA_CREACION),
                local_prefix=local_prefix,
            )
            if file:
                endorsements[nro_endoso]["files"].append(file)

    # ── Step 2: build Policy objects ──────────────────────────────────────
    policies: list[Policy] = []

    for nro_pol, data in grouped.items():
        info = data["info"]

        endorsement_list = sorted(
            [
                Endorsement(
                    nro_endoso=nro_endoso,
                    emission_date=ed["emission_date"],
                    files=ed["files"],
                )
                for nro_endoso, ed in data["endorsements"].items()
            ],
            key=lambda e: e.nro_endoso,
        )

        # Emission date from endorsement 0 (original policy); fallback to first
        emission_date = next(
            (e.emission_date for e in endorsement_list if e.nro_endoso == 0),
            endorsement_list[0].emission_date if endorsement_list else None,
        )

        policies.append(
            Policy(
                nro_pol=nro_pol,
                ramo=info[COL_RAMO] or "",
                emission_date=emission_date,
                insured=Insured(
                    code=str(info[COL_COD_ASEG]),
                    name=info[COL_NOMBRE_ASEGURADO] or "",
                    email=info.get(COL_MAIL_ASEGURADO),
                ),
                producer=Producer(
                    code=str(info[COL_COD_AGENTE]),
                    name=info[COL_NOMBRE_AGENTE] or "",
                    email=info.get(COL_MAIL_AGENTE),
                ),
                endorsements=endorsement_list,
            )
        )

    return policies


def map_producer(row: dict) -> dict:
    """Map a producer DB row to ProducerResponse constructor kwargs."""
    return {
        "code": str(row[COL_CODE]),
        "name": row[COL_NAME] or "",
        "email": row.get(COL_EMAIL),
    }


def map_insured(row: dict) -> dict:
    """Map an insured DB row to InsuredResponse constructor kwargs."""
    return {
        "code": str(row[COL_CODE]),
        "name": row[COL_NAME] or "",
        "email": row.get(COL_EMAIL),
    }
