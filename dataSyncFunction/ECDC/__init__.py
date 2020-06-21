import datetime
import logging
from sqlalchemy.sql import text as sa_text
import sqlalchemy
import urllib
from sqlalchemy import create_engine
import pandas as pd
import requests
import os
import azure.functions as func

from .. import shared


def main(mytimer: func.TimerRequest) -> None:
    utc_timestamp = datetime.datetime.utcnow().replace(
        tzinfo=datetime.timezone.utc).isoformat()

    if mytimer.past_due:
        logging.info('The timer is past due!')

    today = datetime.datetime.now().date()
    date = today
    try:
        df = pd.read_excel(
            "https://www.ecdc.europa.eu/sites/default/files/documents/COVID-19-geographic-disbtribution-worldwide-%s.xlsx" % date.strftime("%Y-%m-%d"))
        logging.info(f"date: {today}")
    except Exception as e:
        logging.info(e)
        logging.info(f"No data for date {today}, yet.")
        return
    change_column_names_date = datetime.date(2020, 3, 27)
    if date >= change_column_names_date:
        df = df.rename(columns={
                       'dateRep': 'DateRep', 'countriesAndTerritories': shared.helpers.country_col, 'cases': 'infections'})
    df['DateRep'] = pd.to_datetime(df['DateRep']).dt.date


    df = df.rename(columns={'Countries and territories': shared.helpers.country_col,
                            'DateRep': 'date', 'Deaths': 'deaths', 'Cases': 'infections'})

    df = df[['date', 'infections', 'deaths', shared.helpers.country_col]]

    df = df.sort_values(by=['date', shared.helpers.country_col])

    df[df[shared.helpers.country_col] == 'Germany'].groupby(by=[shared.helpers.country_col]).cumsum()

    df_cumsum = df.groupby(by=[shared.helpers.country_col]).cumsum()

    df_result = df[['date', shared.helpers.country_col]].join(df_cumsum)

    username = os.environ.get('keyvault_db_username')
    password = os.environ.get('keyvault_db_password')

    params = urllib.parse.quote_plus(
        'Driver={ODBC Driver 17 for SQL Server};Server=tcp:covid19dbserver.database.windows.net,1433;Database=covid19db;Uid='+username
        + '@covid19dbserver;Pwd='+password
        + ';Encrypt=yes;TrustServerCertificate=no;Connection Timeout=30;')
    conn_str = 'mssql+pyodbc:///?odbc_connect={}'.format(params)
    engine = create_engine(conn_str, echo=False)

    assert df_result.duplicated().sum() == 0

    table_name = "ECDC"
    table_name_updates = f"{table_name}_updates"

    try:
        df_temp = pd.read_sql("select Top(1) * from dbo.%s" %
                              table_name_updates, engine)
        engine.execute(sa_text('''TRUNCATE TABLE %s''' %
                               table_name_updates).execution_options(autocommit=True))
    except Exception as e:
        print(e)
        pass

    country_col = 'Country/Region'
    dtype_dict = {}
    for col in [country_col]:
        df_result[col] = df_result[col].str.slice(start=0, stop=99)
        dtype_dict[col] = sqlalchemy.types.NVARCHAR(length=100)

    df_result = df_result[['Country/Region', 'infections', 'deaths', 'date']]

    df_result.infections = df_result.infections.fillna(0)
    df_result.deaths = df_result.deaths.fillna(0)

    df_result.to_sql(table_name_updates,
                     engine,
                     if_exists='append', schema='dbo',
                     index=False, chunksize=100,
                     method='multi', dtype=dtype_dict)

    merge_statement = f'''
    MERGE INTO dbo.{table_name} AS Target 
    USING 
        (
            SELECT [Country/Region], infections, deaths, date 
            FROM dbo.{table_name_updates}
        ) AS Source 
    ON Target.[Country/Region] = Source.[Country/Region] 
        AND Target.date = Source.date 
    WHEN MATCHED THEN 
        UPDATE SET 
        Target.infections = Source.infections, 
        Target.deaths = Source.deaths
    WHEN NOT MATCHED BY TARGET THEN 
        INSERT ([Country/Region], infections, deaths, date)
        VALUES (Source.[Country/Region], Source.infections, Source.deaths, Source.date);

    '''
    engine.execute(sa_text(merge_statement).execution_options(autocommit=True))

    logging.info('Python timer trigger function ran at %s', utc_timestamp)
