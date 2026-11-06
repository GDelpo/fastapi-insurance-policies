# Development Dockerfile
# Using bookworm for compatibility with older SQL Server (2008 R2 with TLS 1.0)
FROM python:3.12-slim-bookworm

WORKDIR /app

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Install FreeTDS driver for SQL Server (mejor compatibilidad con versiones antiguas)
RUN apt-get update && apt-get install -y --no-install-recommends \
    tdsodbc \
    freetds-dev \
    unixodbc \
    unixodbc-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Configure FreeTDS ODBC driver
RUN echo "[FreeTDS]\nDescription=FreeTDS Driver\nDriver=/usr/lib/x86_64-linux-gnu/odbc/libtdsodbc.so\nSetup=/usr/lib/x86_64-linux-gnu/odbc/libtdsS.so\nFileUsage=1" > /etc/odbcinst.ini

# Configure FreeTDS settings
RUN echo "[global]\ntds version = 7.3\ntext size = 64512\nencryption = off" > /etc/freetds/freetds.conf

# Install Python dependencies
COPY requirements.txt requirements-dev.txt ./
RUN pip install -r requirements.txt && \
    pip install -r requirements-dev.txt

# Copy application code
COPY . .

# Create non-root user
RUN mkdir -p /app/logs && \
    useradd -m -u 1000 appuser && \
    chown -R appuser:appuser /app

USER appuser

# Expose port
EXPOSE 8082

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8082/health || exit 1

# Development: Run with --reload for hot-reload
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8082", "--reload"]
