FROM python:3.12-slim
WORKDIR /app
# procps: the ops daemon checks the bot process with pgrep
RUN apt-get update && apt-get install -y --no-install-recommends procps && rm -rf /var/lib/apt/lists/*
COPY requirements_bot.txt .
RUN pip install --no-cache-dir -r requirements_bot.txt
COPY . .
RUN chmod +x easy/run_all.sh
CMD ["easy/run_all.sh"]
