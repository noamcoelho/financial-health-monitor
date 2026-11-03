# Imagem do Airflow com as dependências e o código do Monitor de Saúde Financeira.
ARG AIRFLOW_VERSION=3.3.2
ARG PYTHON_VERSION=3.12

FROM apache/airflow:${AIRFLOW_VERSION}-python${PYTHON_VERSION}

# ARGs declarados antes do FROM precisam ser redeclarados para uso abaixo
ARG AIRFLOW_VERSION
ARG PYTHON_VERSION

# 1. Dependências, primeiro: esta camada só é refeita quando o requirements.txt muda
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir \
        "apache-airflow==${AIRFLOW_VERSION}" \
        -r /tmp/requirements.txt \
        --constraint "https://raw.githubusercontent.com/apache/airflow/constraints-${AIRFLOW_VERSION}/constraints-${PYTHON_VERSION}.txt"

# 2. Código do projeto, espelhando a estrutura do repositório
RUN mkdir -p /opt/airflow/project/data
COPY --chown=airflow:root src/ /opt/airflow/project/src/
COPY --chown=airflow:root config/ /opt/airflow/project/config/

# O pacote financial_monitor fica importável por qualquer processo do Airflow
ENV PYTHONPATH=/opt/airflow/project/src