FROM python:3.11-slim
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    iproute2 procps sudo util-linux openssh-client \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN python3 -m pip install --no-cache-dir -r requirements.txt

COPY bot.py .

ENV SSH_KEY_PATH=/run/secrets/mybot_ssh_key

CMD ["python3", "bot.py"]
