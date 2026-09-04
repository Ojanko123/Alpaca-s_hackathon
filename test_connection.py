"""
Quick connection test - run this first, before main.py.

Confirms your .env credentials are correct and can reach your
dedicated competition paper account.

Usage:
    python test_connection.py
"""

from alpaca.trading.client import TradingClient
from config import ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_PAPER, ALPACA_ACCOUNT_ID

def main():
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        print("ERROR: ALPACA_API_KEY / ALPACA_SECRET_KEY not found. Check your .env file exists and is named exactly '.env'.")
        return

    print(f"Connecting to Alpaca paper account (paper={ALPACA_PAPER})...")
    client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=ALPACA_PAPER)

    account = client.get_account()

    print("\n--- Connection successful ---")
    print(f"Account ID:      {account.id}")
    print(f"Status:          {account.status}")
    print(f"Equity:          ${float(account.equity):,.2f}")
    print(f"Buying power:    ${float(account.buying_power):,.2f}")
    print(f"Cash:            ${float(account.cash):,.2f}")

    print(f"Account number:  {account.account_number}")

    if ALPACA_ACCOUNT_ID and str(account.account_number) != ALPACA_ACCOUNT_ID:
        print(f"\nWARNING: connected account number ({account.account_number}) does not match "
              f"ALPACA_ACCOUNT_ID in your .env ({ALPACA_ACCOUNT_ID}). "
              f"Double check you're pointing at your dedicated competition account.")
    else:
        print("\nAccount number matches your .env ALPACA_ACCOUNT_ID. You're good to go.")


if __name__ == "__main__":
    main()
