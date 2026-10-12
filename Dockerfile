FROM python:3.12-slim

WORKDIR /app

ARG PIP_INDEX=https://pypi.org/simple
RUN pip install --no-cache-dir --index-url $PIP_INDEX "requests[socks]"

COPY app/bot.py .

CMD ["python", "-u", "bot.py"]
