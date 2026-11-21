FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

# 問題データ（puzzles.json / knowledge_base.json）はイメージに含めない。
# 実行時にホストのディレクトリを /app/data に読み取り専用でマウントする:
#   docker run -v <dataディレクトリ>:/app/data:ro ...
RUN useradd --create-home --uid 10001 bot
USER bot

CMD ["python", "src/bot.py"]
