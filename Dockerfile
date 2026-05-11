FROM python:3.11-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml .

RUN uv sync --no-cache --no-install-project

COPY . .

ENV PYTHONPATH=/app/src
EXPOSE 8888

# Uruchamiamy za pomocą uv, aby mieć pewność, że używamy venv
CMD ["uv", "run", "python", "-m", "uvicorn", "api.server:app", "--host", "0.0.0.0", "--port", "8888"]