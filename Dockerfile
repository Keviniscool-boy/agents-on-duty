FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt .

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY agents ./agents
COPY api ./api
COPY auth ./auth
COPY mcp_client ./mcp_client
COPY mcp_tools ./mcp_tools
COPY memory ./memory
COPY tools ./tools
COPY tracing ./tracing
COPY knowledge_base ./knowledge_base
COPY vector_store ./vector_store
COPY main.py pyproject.toml ./

EXPOSE 8000

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
