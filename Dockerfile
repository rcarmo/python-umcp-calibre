FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends calibre \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
ENV PYTHONPATH=/app/src \
    CALIBRE_LIBRARIES=default=/books \
    CALIBRE_DEFAULT_LIBRARY=default
EXPOSE 9000
CMD ["python", "-m", "calibre_umcp.server", "--host", "0.0.0.0", "--port", "9000", "--http"]
