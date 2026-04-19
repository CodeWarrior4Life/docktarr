FROM python:3.12-alpine AS builder

WORKDIR /app
COPY pyproject.toml .
COPY src/ src/

RUN pip install --no-cache-dir .

FROM python:3.12-alpine

WORKDIR /app

RUN apk add --no-cache openssh-client

COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --from=builder /app/src /app/src

VOLUME /config
EXPOSE 8080
ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "doctarr"]
