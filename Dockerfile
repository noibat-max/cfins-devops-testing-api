# cfins-devops-testing-api — the QA Workbench REST API (FastAPI on uvicorn).
#
# Build once, region/env-agnostic. ALL config comes from the environment (the
# ECS task definition); NO secrets or AWS credentials are baked in — on ECS the
# task role is resolved via boto3's default credential chain, JWT_SECRET/Cognito
# come from the task def / Secrets Manager. Provisioning scripts (scripts/) and
# .env are excluded via .dockerignore.
#
# Build (linux/amd64 for Fargate x86):
#   docker build -t cfins-qaworkbench-api:dev .
#   # Apple Silicon: docker buildx build --platform linux/amd64 -t cfins-qaworkbench-api:dev --load .

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first, so this layer caches across app-code changes.
COPY requirements.txt ./
RUN pip install -r requirements.txt

# App code only. scripts/, .env, caches, docs, tests are excluded by .dockerignore.
COPY app ./app

# Run as a non-root user.
RUN useradd -m -u 10001 app
USER app

EXPOSE 8000

# Liveness for local `docker run` (ECS/ALB use their own health checks). No curl
# in slim, so probe /health with stdlib.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health').status==200 else 1)" || exit 1

# Production server — bind all interfaces on 8000, no --reload. Scale via ECS
# task count (one uvicorn worker per task keeps memory/logging simple).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
