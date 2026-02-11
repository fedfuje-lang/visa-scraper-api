FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

WORKDIR /app

# Requirements installieren
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App Code kopieren
COPY . .

# Port von Railway
ENV PORT=8000
EXPOSE 8000

# Start Command
CMD uvicorn discovery_api:app --host 0.0.0.0 --port $PORT
