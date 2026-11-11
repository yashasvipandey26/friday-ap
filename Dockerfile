FROM python:3.12-slim

WORKDIR /srv/friday
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app app
COPY data data
COPY samples samples
COPY static static

ENV FRIDAY_DATA_DIR=/srv/friday/var
EXPOSE 8000
CMD ["sh", "-c", "uvicorn app.main:create_app --factory --host 0.0.0.0 --port ${PORT:-8000}"]
