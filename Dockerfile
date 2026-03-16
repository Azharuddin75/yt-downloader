FROM python:3.11-slim

# ffmpeg install karo
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Requirements install karo
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App files copy karo
COPY . .

# Port expose karo
EXPOSE 8080

# Server run karo
CMD gunicorn server:app --bind 0.0.0.0:8080 --timeout 300 --workers 2
