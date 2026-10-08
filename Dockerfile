FROM python:3.10-slim

# FFmpeg kurulumu
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .

RUN pip install --no-cache-dir -r requirements.txt

# Render portunu çevre değişkeninden alır
CMD ["python", "main.py"] 
