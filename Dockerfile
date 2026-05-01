FROM public.ecr.aws/docker/library/python:3.11-slim
COPY agent/app.py /app/app.py
EXPOSE 8080
CMD ["python", "/app/app.py"]
