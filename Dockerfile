FROM python:3.11-slim

WORKDIR /app

COPY README.md pyproject.toml ./
COPY src/ src/

RUN pip install --no-cache-dir .

CMD ["fw-context-mcp"]
