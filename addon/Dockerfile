FROM python:3.12-alpine

WORKDIR /app
COPY app.py index.html app.js styles.css camera.css README.md ./

ENV PORT=8022
ENV ANPR_DB=/data/anpr.sqlite3
VOLUME ["/data"]
EXPOSE 8022

CMD ["python", "app.py"]
