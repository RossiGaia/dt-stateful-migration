FROM python:3.12
WORKDIR /etc/kopf-operator
COPY cyberphysical-application-operator.py .
COPY requirements.txt .
RUN pip install -r requirements.txt
CMD ["kopf",  "run", "cyberphysical-application-operator.py"]