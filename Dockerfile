FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000

# 单进程运行：全部状态仅存于该进程内存
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
