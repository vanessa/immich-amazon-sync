FROM alpine:3.21

RUN apk add --no-cache python3

WORKDIR /app
COPY src/immich_sync.py entrypoint.sh ./
RUN chmod +x entrypoint.sh

VOLUME ["/data"]

ENTRYPOINT ["./entrypoint.sh"]
