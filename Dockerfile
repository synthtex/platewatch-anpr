FROM python:3.12-alpine

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py index.html app.js styles.css camera.css image.css README.md ./

ENV PORT=8022
EXPOSE 8022

CMD ["python", "app.py"]
