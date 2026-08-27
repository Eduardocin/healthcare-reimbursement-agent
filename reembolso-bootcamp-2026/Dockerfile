FROM python:3.11-slim

# poppler e tesseract: o anexo pode ser foto de recibo.
# As dependências Python fixadas têm wheels para CPython 3.11; compiladores não
# são necessários na imagem final.
RUN apt-get update && apt-get install -y --no-install-recommends \
        poppler-utils tesseract-ocr tesseract-ocr-por \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# O índice entra pronto. Não construa índice no build nem no start.
COPY storage/ ./storage/
COPY kb/ ./kb/
COPY app/ ./app/

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
