FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py index.html get_uprn.py ./

# No UPRN address database is shipped in the image (data/ is 2GB+ and gitignored) —
# the app already falls back to estimated address counts when it's absent.
EXPOSE 8080
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8080}"]
