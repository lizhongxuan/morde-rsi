FROM python:3.12-slim
WORKDIR /app
COPY . /app
EXPOSE 8765
CMD ["python3", "service.py", "serve"]
