# Build context: repo root (docker-compose.yml sets context: .)
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY node/ node/

# UDP heartbeat, TCP high-trust sync, HTTP control plane.
EXPOSE 7000/udp 7001/tcp 8000/tcp

CMD ["python", "-m", "node.main"]
