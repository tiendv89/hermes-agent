from airflow import DAG
from airflow.operators.python import PythonOperator
from datetime import datetime

def extract():
    pass

def transform():
    pass

def load():
    pass

default_args = {
    "owner": "data_engineer",
}

with DAG(
    dag_id="example_pipeline",
    start_date=datetime(2024, 1, 1),
    schedule="@daily",
    catchup=False,
    default_args=default_args,
    tags=["data"],
) as dag:
    t1 = PythonOperator(task_id="extract", python_callable=extract)
    t2 = PythonOperator(task_id="transform", python_callable=transform)
    t3 = PythonOperator(task_id="load", python_callable=load)

    t1 >> t2 >> t3
