FROM python:3.12-slim

RUN apt-get update && apt-get install -y curl && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs && \
    rm -rf /var/lib/apt/lists/*

RUN npm install -g @rynfar/meridian

WORKDIR /app

COPY quiz_battle/solver_api/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY quiz_battle/solver_api/main.py ./main.py
COPY quiz_battle/solver_api/start.sh ./start.sh
RUN chmod +x start.sh

CMD ["./start.sh"]
