FROM python:3.11-slim

# GnuGo 리눅스 패키지 설치
RUN apt-get update && apt-get install -y gnugo && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]