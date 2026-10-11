FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py config.yaml entrypoint.sh ./
COPY defaults/ ./defaults/
VOLUME /app/data
ENTRYPOINT ["sh", "/app/entrypoint.sh"]
CMD ["python", "main.py"]
