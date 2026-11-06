"""Tests for Pydantic schemas — serialization, computed fields, sanitization."""

from app.schemas import (
    CamelModel,
    DependencyHealth,
    Endorsement,
    ErrorResponse,
    HealthResponse,
    Insured,
    InsuredResponse,
    PaginatedResponse,
    PaginationMeta,
    Policy,
    PolicyFile,
    Producer,
    ProducerResponse,
    ServiceStatus,
    sanitize_email,
    sanitize_name,
)


class TestCamelModel:
    """Verify camelCase alias generation."""

    def test_snake_to_camel(self):
        class M(CamelModel):
            my_field: str = "x"

        data = M().model_dump(by_alias=True)
        assert "myField" in data


class TestPaginationMeta:
    """PaginationMeta.from_params computation."""

    def test_single_page(self):
        meta = PaginationMeta.from_params(limit=100, skip=0, total=50)
        assert meta.total == 50
        assert meta.total_pages == 1
        assert meta.has_next is False
        assert meta.has_previous is False

    def test_two_pages(self):
        meta = PaginationMeta.from_params(limit=10, skip=0, total=15)
        assert meta.total_pages == 2
        assert meta.has_next is True
        assert meta.has_previous is False

    def test_second_page(self):
        meta = PaginationMeta.from_params(limit=10, skip=10, total=15)
        assert meta.has_next is False
        assert meta.has_previous is True

    def test_zero_total(self):
        meta = PaginationMeta.from_params(limit=10, skip=0, total=0)
        assert meta.total_pages == 0
        assert meta.has_next is False


class TestPaginatedResponse:
    """Generic PaginatedResponse[T]."""

    def test_serializes_items_and_pagination(self):
        meta = PaginationMeta.from_params(limit=10, skip=0, total=1)
        resp = PaginatedResponse[str](data=["hello"], meta=meta)
        data = resp.model_dump(by_alias=True)
        assert data["data"] == ["hello"]
        assert data["meta"]["total"] == 1


class TestPolicyComputed:
    """Policy computed fields."""

    def test_has_files_true(self):
        p = Policy(
            nro_pol="1",
            insured=Insured(code="1", name="A"),
            producer=Producer(code="1", name="B"),
            endorsements=[
                Endorsement(
                    nro_endoso=0,
                    files=[PolicyFile(name="f.pdf", available=True)],
                )
            ],
        )
        assert p.has_files is True
        assert p.total_file_count == 1

    def test_has_files_false_when_empty(self):
        p = Policy(
            nro_pol="2",
            insured=Insured(code="1", name="A"),
            producer=Producer(code="1", name="B"),
            endorsements=[Endorsement(nro_endoso=0, files=[])],
        )
        assert p.has_files is False
        assert p.total_file_count == 0

    def test_unavailable_file_not_counted(self):
        p = Policy(
            nro_pol="3",
            insured=Insured(code="1", name="A"),
            producer=Producer(code="1", name="B"),
            endorsements=[
                Endorsement(
                    nro_endoso=0,
                    files=[PolicyFile(name="f.pdf", available=False)],
                )
            ],
        )
        assert p.has_files is False
        assert p.unavailable_file_count == 1


class TestErrorResponse:
    """ErrorResponse always has success=False."""

    def test_success_defaults_false(self):
        err = ErrorResponse(error="TestError", message="boom")
        assert err.success is False


class TestHealthSchemas:
    """ServiceStatus, DependencyHealth, HealthResponse."""

    def test_service_status_values(self):
        assert ServiceStatus.HEALTHY == "healthy"
        assert ServiceStatus.DEGRADED == "degraded"
        assert ServiceStatus.UNHEALTHY == "unhealthy"

    def test_dependency_health_defaults(self):
        dep = DependencyHealth(name="db", status=ServiceStatus.HEALTHY)
        assert dep.required is True
        assert dep.details == {}

    def test_health_response_fields(self):
        resp = HealthResponse(
            status=ServiceStatus.HEALTHY,
            version="1.0.0",
            environment="test",
            dependencies={
                "db": DependencyHealth(name="db", status=ServiceStatus.HEALTHY),
            },
        )
        assert len(resp.dependencies) == 1


# ============================================================================
# Name Sanitization
# ============================================================================


class TestSanitizeName:
    """sanitize_name normalizes legacy DB name formats."""

    def test_all_caps_to_title(self):
        assert sanitize_name("GOMEZ MARIA") == "Gomez Maria"

    def test_fixes_comma_spacing(self):
        assert sanitize_name("PEREZ , JUAN CARLOS") == "Perez, Juan Carlos"

    def test_preserves_particles(self):
        assert sanitize_name("DE LA ROSA MARIA") == "De la Rosa Maria"

    def test_particle_at_start_is_capitalized(self):
        assert sanitize_name("DE MARCO LUIS") == "De Marco Luis"

    def test_collapses_spaces(self):
        assert sanitize_name("PEREZ   JUAN") == "Perez Juan"

    def test_empty_passthrough(self):
        assert sanitize_name("") == ""
        assert sanitize_name("  ") == "  "

    def test_already_clean(self):
        assert sanitize_name("Juan Pérez") == "Juan Pérez"

    def test_comma_no_extra_space(self):
        assert sanitize_name("LOPEZ,MARIA") == "Lopez, Maria"

    def test_company_srl(self):
        assert sanitize_name("ACME SEGUROS SRL") == "Acme Seguros SRL"

    def test_company_sa(self):
        assert sanitize_name("EMPRESA NACIONAL SA") == "Empresa Nacional SA"

    def test_company_dotted(self):
        assert sanitize_name("ORGANIZACION ACME S.R.L.") == "Organizacion Acme S.R.L."

    def test_company_sa_dotted(self):
        assert sanitize_name("COOPERATIVA DE SEGUROS S.A.") == "Cooperativa de Seguros S.A."

    def test_company_with_comma(self):
        assert sanitize_name("MARTINEZ Y ASOCIADOS SRL") == "Martinez y Asociados SRL"


# ============================================================================
# Email Sanitization
# ============================================================================


class TestSanitizeEmail:
    """sanitize_email validates and normalizes email addresses."""

    def test_valid_email_lowercased(self):
        assert sanitize_email("ACME.BROKERS@EXAMPLE.COM") == "acme.brokers@example.com"

    def test_valid_email_preserved(self):
        assert sanitize_email("maria.gomez@example.com") == "maria.gomez@example.com"

    def test_none_returns_none(self):
        assert sanitize_email(None) is None

    def test_empty_returns_none(self):
        assert sanitize_email("") is None
        assert sanitize_email("   ") is None

    def test_junk_no_tiene(self):
        assert sanitize_email("NO TIENE") is None
        assert sanitize_email("no tiene") is None

    def test_junk_sin_mail(self):
        assert sanitize_email("SIN MAIL") is None
        assert sanitize_email("sin email") is None

    def test_junk_na(self):
        assert sanitize_email("N/A") is None
        assert sanitize_email("S/D") is None

    def test_junk_dashes(self):
        assert sanitize_email("---") is None

    def test_invalid_format(self):
        assert sanitize_email("not-an-email") is None
        assert sanitize_email("just@") is None
        assert sanitize_email("@nothing.com") is None

    def test_invalid_no_domain(self):
        assert sanitize_email("user@") is None


# ============================================================================
# Schema Integration (validators applied on models)
# ============================================================================


class TestProducerSanitization:
    """Producer/ProducerResponse apply sanitization via validators."""

    def test_producer_name_sanitized(self):
        p = Producer(code="100", name="PEREZ , JUAN CARLOS", email="JUAN.PEREZ@EXAMPLE.COM")
        assert p.name == "Perez, Juan Carlos"
        assert p.email == "juan.perez@example.com"

    def test_producer_response_sanitized(self):
        p = ProducerResponse(code="100", name="GARCIA LOPEZ", email="NO TIENE")
        assert p.name == "Garcia Lopez"
        assert p.email is None

    def test_insured_name_sanitized(self):
        i = Insured(code="200", name="GOMEZ MARIA", email="maria.gomez@example.com")
        assert i.name == "Gomez Maria"
        assert i.email == "maria.gomez@example.com"

    def test_insured_response_junk_email(self):
        i = InsuredResponse(code="200", name="PEREZ , MARIA", email="SIN MAIL")
        assert i.name == "Perez, Maria"
        assert i.email is None
