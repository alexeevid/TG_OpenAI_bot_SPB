FROM python:3.11-slim
WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir -r requirements.txt
ENV LANG=C.UTF-8
CMD ["python","-m","bot.main"]
