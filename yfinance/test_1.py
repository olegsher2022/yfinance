import yfinance as yf
import pytest
import pandas as pd
import matplotlib.pyplot as plt

def test_6():
  print('Quotes:\n')
  df = yf.download('FNGU FANG SOXL BNKU YINN', period = '3mo', threads=False)
  df.index = pd.to_datetime(df.index)
  print(type(df))
  print(df.Close)


  df['Close'].plot(title='Stock Prices', xlabel='Date', ylabel='Close Price')
  plt.show()

