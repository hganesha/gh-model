FROM python:3.12-slim
COPY . /srv
RUN pip install /srv
CMD ["uvicorn", "catalog.main:app"]
