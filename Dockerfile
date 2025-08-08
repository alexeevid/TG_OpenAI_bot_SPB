FROM python:3.11-slim

# Install dependencies
RUN pip install --no-cache-dir --upgrade pip

WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir -r requirements.txt

# Set locale environment
ENV LANG=C.UTF-8

CMD ["bash", "start.sh"]
