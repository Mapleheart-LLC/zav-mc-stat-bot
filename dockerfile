FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py storage.py status.py dashboard.py ./
CMD ["python", "bot.py"]