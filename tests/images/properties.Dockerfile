FROM python:3.12-slim
RUN pip install --no-cache-dir hypothesis==6.170.16 pytest==8.3.5 coverage==7.17.3
