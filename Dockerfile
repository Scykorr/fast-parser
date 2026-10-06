FROM python:3.14-slim
WORKDIR /app
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock
COPY fast_parser ./fast_parser
ENV FAST_PARSER_DB=/app/data/football.sqlite3
EXPOSE 8000
# Local binding is the default; Docker explicitly uses its own container interface.
CMD ["python", "-m", "uvicorn", "fast_parser.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
