FROM python:3.11.13-alpine

RUN addgroup -S atlas && adduser -S -G atlas -u 10001 atlas
WORKDIR /workspace
USER atlas
ENTRYPOINT ["python", "/workspace/.atlas_runner.py"]
