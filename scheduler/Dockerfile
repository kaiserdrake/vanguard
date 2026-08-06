FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY scheduler.py .

# Drop root. The scheduler only reads a read-only schedule mount and publishes
# to MQTT, so it needs no write access and no privileged port.
RUN useradd --system --uid 10001 --no-create-home scheduler
USER scheduler

CMD ["python", "-u", "scheduler.py"]
