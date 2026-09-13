import pandas as pd
import datetime
import json
import os
from schwab.auth import easy_client
from schwab.client import Client

def getSchwabClient():
    client = easy_client(
        api_key='i9h6jPuty2e0w7VdvJSqnn4JJc4AemGM',
        app_secret='dg9G2OMG9DooAa6u',
        callback_url='https://127.0.0.1:8182',
        token_path='/tmp/token.json'
    )
    return client

#Returns the DF of stock data for a given date
_SCHWAB_TICKER_MAP = {"^SPX": "$SPX", "SPX": "$SPX", "^NDX": "$NDX", "NDX": "$NDX"}

def getStocData1d5mForDateInDf(ticker:str, date:str):
    ticker = _SCHWAB_TICKER_MAP.get(ticker, ticker)
    client = getSchwabClient()
    start_dt = pd.to_datetime(date).replace(hour=0, minute=0, second=0, microsecond=0)
    end_dt   = start_dt + datetime.timedelta(days=1) - datetime.timedelta(seconds=1)
    data = client.get_price_history_every_five_minutes(symbol=ticker, start_datetime=start_dt, end_datetime=end_dt)
    data_json = data.json()
    data_df = convert_json_to_df_stock_data(data_json)
    return data_df

def getStockPrice1d5m(ticker:str):
    ticker = _SCHWAB_TICKER_MAP.get(ticker, ticker)
    client = getSchwabClient()
    today = datetime.datetime.now()
    start_dt = today.replace(hour=0, minute=0, second=0, microsecond=0)
    end_dt   = today.replace(hour=23, minute=59, second=59, microsecond=0)
    data = client.get_price_history_every_five_minutes(symbol=ticker, start_datetime=start_dt, end_datetime=end_dt)
    data_json = data.json()
    return data_json

def convert_json_to_df_stock_data(data_json):
        # Convert JSON data to DataFrame
    if 'candles' in data_json:
        df_price = pd.DataFrame(data_json['candles'])
        # Convert datetime from milliseconds to datetime objects
        df_price['datetime'] = pd.to_datetime(df_price['datetime'], unit='ms')
        # Set datetime as index
        df_price.set_index('datetime', inplace=True)
        # Convert datetime index to Chicago timezone
        df_price.index = df_price.index.tz_localize('UTC').tz_convert('America/Chicago')
        # Convert datetime from milliseconds to readable format
        # df_price['datetime'] = pd.to_datetime(df_price['datetime'], unit='ms')
        
        # print("\n5-minute price history DataFrame:")
        # print(df_price)
        # Convert column names to uppercase
        # df_price.columns = df_price.columns.str.upper()
        return df_price
    else:
        print("No candle data found in JSON response")
        return None
    
