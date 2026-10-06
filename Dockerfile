FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 DRIFTWATCH_HOME=/app
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .
COPY config ./config
COPY db ./db
ENTRYPOINT ["driftwatch"]
