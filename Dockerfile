FROM python:3.13-slim

WORKDIR /app

RUN pip install --no-cache-dir aiogram openai aiosqlite aiohttp

COPY bot.py .

EXPOSE 8080

CMD ["python", "bot.py"]
