FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY data/puzzles.json ./data/puzzles.json
COPY data/knowledge_base.json ./data/knowledge_base.json

CMD ["python", "src/bot.py"]
