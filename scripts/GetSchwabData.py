from numpy import double
from schwab.auth import easy_client
from schwab.client import Client
import pandas as pd
import datetime
import json
import os
# Follow the instructions on the screen to authenticate your client.
# if starting anew, then it will take 60 minutes to get the API working
# client = easy_client(
#         api_key='i9h6jPuty2e0w7VdvJSqnn4JJc4AemGM',  #
#         app_secret='dg9G2OMG9DooAa6u',
#         callback_url='https://127.0.0.1:8182',
#         token_path='/tmp/token.json')




def getStockPrice():
    resp = client.get_price_history_every_day('AAPL')
    print (resp)
    # assert resp.status_code == httpx.codes.OK
    history = resp.json()


    # Convert JSON to pandas DataFrame
    df = pd.DataFrame(history)
    
    # Print the DataFrame
    print("\nDataFrame:")
    print(df)
    



def getOptionsData(ticker: str, client=None, countOfStrikesToGet: int = 50):
    """Fetch options chain using the same parameters as DailyIndexRangeFinder."""
    if client is None:
        from data_sources.schwab_client import _build_option_chain_kwargs, _get_client, _schwab_symbol

        schwab_symbol = _schwab_symbol(ticker.lstrip("$"))
        chain_kwargs = _build_option_chain_kwargs(
            ticker.lstrip("$"),
            strike_count=countOfStrikesToGet,
        )
        resp = _get_client().get_option_chain(schwab_symbol, **chain_kwargs)
        resp.raise_for_status()
        return resp.json()

    current_month = datetime.datetime.now().strftime("%B").upper()
    exp_month = getattr(Client.Options.ExpirationMonth, current_month)
    data = client.get_option_chain(
        symbol=ticker,
        contract_type=Client.Options.ContractType.ALL,
        strike_count=countOfStrikesToGet,
        strategy=Client.Options.Strategy.SINGLE if ticker.startswith("$") else Client.Options.Strategy.ANALYTICAL,
        interval=5.0,
        strike_range=Client.Options.StrikeRange.OUT_OF_THE_MONEY,
        from_date=datetime.datetime.now(),
        to_date=datetime.datetime.now() + datetime.timedelta(days=2 if ticker.startswith("$") else 42),
        entitlement=Client.Options.Entitlement.PAYING_PRO,
        exp_month=exp_month,
        option_type=Client.Options.Type.ALL,
    )
    return data.json()

def json_to_df(data_json):
    # Initialize lists to store option data
    options_data = []
    
    # Check if there are errors in the response
    if 'errors' in data_json:
        print("Error in API response:", data_json['errors'])
        return pd.DataFrame()  # Return empty DataFrame
        
    # Process call options
    if 'callExpDateMap' in data_json:
        for expiry_date in data_json['callExpDateMap']:
            for strike in data_json['callExpDateMap'][expiry_date]:
                for option in data_json['callExpDateMap'][expiry_date][strike]:
                    option['optionType'] = 'CALL'
                    option['expiryDate'] = expiry_date.split(':')[0]
                    option['strikePrice'] = float(strike)
                    # Convert gamma to float
                    if 'gamma' in option:
                        option['gamma'] = double(option['gamma'])
                    options_data.append(option)
    
    # Process put options
    if 'putExpDateMap' in data_json:
        for expiry_date in data_json['putExpDateMap']:
            for strike in data_json['putExpDateMap'][expiry_date]:
                for option in data_json['putExpDateMap'][expiry_date][strike]:
                    option['optionType'] = 'PUT'
                    option['expiryDate'] = expiry_date.split(':')[0]
                    option['strikePrice'] = float(strike)
                    # Convert gamma to float
                    if 'gamma' in option:
                        option['gamma'] = double(option['gamma'])
                    options_data.append(option)
    
    # Convert to DataFrame
    df = pd.DataFrame(options_data)
    
    # Add underlying price if available
    if 'underlyingPrice' in data_json:
        df['underlyingPrice'] = data_json['underlyingPrice']
    
    return df



def save_options_json_to_df(data):
    rows = []

    # Loop through the expiration dates in callExpDateMap
    for exp_key, strikes in data.get("callExpDateMap", {}).items():
        for strike, options_list in strikes.items():
            for option in options_list:
                flat_option = option.copy()
                # Convert gamma to float with full precision
                if 'gamma' in flat_option:
                    flat_option['gamma'] = float(flat_option['gamma'])
                flat_option["expirationDateKey"] = exp_key
                flat_option["strikePriceKey"] = strike
                flat_option["symbolRoot"] = data.get("symbol")
                flat_option["underlyingPrice"] = data.get("underlyingPrice")
                rows.append(flat_option)

    df = pd.DataFrame(rows)
    # Ensure gamma column maintains full float precision
    if 'gamma' in df.columns:
        df['gamma'] = df['gamma'].astype(float)
    return df




def save_to_excel(df, ticker, folder="output"):
    # Create the output folder if it doesn't exist
    if not os.path.exists(folder):
        os.makedirs(folder)
        
    # Generate filename with timestamp
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{folder}/{ticker}_options_{timestamp}.xlsx"
    
    # Ensure gamma column maintains full float precision before saving
    if 'gamma' in df.columns:
        df['gamma'] = df['gamma'].astype('float64')
    
    # Save to Excel with float_format to preserve precision
    writer = pd.ExcelWriter(filename, engine='openpyxl')
    df.to_excel(writer, index=False, float_format='%.10f')
    writer.close()
    
    print(f"Saved options data to Excel file: {filename}")
    return filename


def save_to_json(json_data, ticker, folder="output"):
    # Create the output folder if it doesn't exist
    if not os.path.exists(folder):
        os.makedirs(folder)
        
    # Generate filename with timestamp
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{folder}/{ticker}_options_{timestamp}.json"
    
    # Save to JSON file
    with open(filename, 'w') as f:
        json.dump(json_data, f, indent=4)
    print(f"Saved options data to JSON file: {filename}")
    return filename

def compute_total_gex(data):
    """Compute dealers' total GEX"""
    contract_size = 100
    spot = data.underlyingPrice.iloc[1]  # Get the underlying price from the first row
    # Compute gamma exposure for each option
    data["GEX"] = spot * data.gamma * data.openInterest * contract_size * spot * 0.01

    # For put option we assume negative gamma, i.e. dealers sell puts and buy calls
    data["GEX"] = data.apply(lambda x: -x.GEX if x.type == "P" else x.GEX, axis=1)
    totalNotionalGex=round(data.GEX.sum() / 10 ** 9, 4)
    print(f"Total notional GEX: ${totalNotionalGex} Bn")
    return totalNotionalGex

def fix_option_data(data):
    """
    Fix option data columns.

    From the name of the option derive type of option, expiration and strike price
    """
    # data["type"] = data["putCall"].apply(lambda x: "C" if x == "CALL" else "P")
    # data["strike"] = data["strikePrice"]
    # data["expiration"] = data["expiryDate"]
    # # Convert expiration to datetime format
    # data["expiration"] = pd.to_datetime(data["expiration"], format="%y%m%d")
    return data

# def plot_gex_by_strike(df,totalGex):
#     """
#     Plot GEX vs Strike price as a bar graph for options expiring in 1 day
    
#     Args:
#         df: DataFrame containing 'strike', 'GEX' and 'daysToExpiration' columns
#     """

#     # Filter for options expiring in 1 day
#     df_filtered = df[df['daysToExpiration'] == 1]
#     # Extract expiry date from the first option in the filtered dataset
#     expiry_date = df_filtered['expiryDate'].iloc[0] if not df_filtered.empty else 'Unknown'
#     # print(f"Plotting GEX for options expiring on: {expiry_date}")
#     # Create the plot
#     plt.figure(figsize=(12, 6))
    
#     # Create bar colors based on GEX values
#     colors = ['red' if gex < 0 else 'blue' for gex in df_filtered['GEX']]
#     plt.bar(df_filtered['strike'], df_filtered['GEX'], color=colors)
    
#     # Add labels and title
#     plt.xlabel('Strike Price')
#     plt.ylabel('Gamma Exposure (GEX)')
#     plt.title(f"Option Gamma Exposure by Strike Price (1 Day to Expiration / {expiry_date} ) Total Gex : {totalGex}")
    
#     # Add grid
#     plt.grid(True, linestyle='--', alpha=0.7)
    
#     # Format y-axis to show billions
#     plt.gca().yaxis.set_major_formatter(plt.FuncFormatter(lambda x, p: f'${x/1e9:.2f}B'))
    
#     plt.tight_layout()
#     plt.show()

if __name__ == "__main__":
    # Example usage
    client = easy_client(
        api_key='i9h6jPuty2e0w7VdvJSqnn4JJc4AemGM',
        app_secret='dg9G2OMG9DooAa6u',
        callback_url='https://127.0.0.1:8182',
        token_path='/tmp/token.json')
    # ticker="SPY"
    ticker_SPX="$SPX"
    ticker_AAPL='AAPL'
    tickerToUse=ticker_SPX
    # getStockPrice()
    data_json=getOptionsData(ticker=tickerToUse)
    # Save the JSON data to a file named after the ticker
    filename = f"{tickerToUse}_options.json"
    save_to_json(data_json,tickerToUse)
    # with open(filename, 'w') as f:
    #     json.dump(data_json, f, indent=4)
    # print(f"Saved options data to {filename}")
    
    df=json_to_df(data_json)
    df=fix_option_data(df)
    # totalGex=compute_total_gex(data=df)
    print("\nColumn names in the DataFrame:")
    # plot_gex_by_strike(df,totalGex)
    # print(df.columns.tolist())
    # print(df)
    # save_to_excel(df=df, ticker=tickerToUse)
    
    
    
    
