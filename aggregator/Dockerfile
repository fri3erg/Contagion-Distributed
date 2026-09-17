# Build context: repo root (docker-compose.yml sets context: .)
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY aggregator/ aggregator/
COPY dashboard/ dashboard/
COPY topology/topology.json topology/topology.json

EXPOSE 8000/tcp

CMD ["python", "-m", "aggregator.main"]
