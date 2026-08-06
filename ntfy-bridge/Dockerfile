FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bridge.py .

# Drop root. The bridge only reads a read-only config mount and makes outbound
# MQTT/HTTP calls, so it needs no write access and no privileged port.
RUN useradd --system --uid 10001 --no-create-home bridge
USER bridge

CMD ["python", "-u", "bridge.py"]
