import logging
import sqlite3
import asyncio
import os
from typing import Dict, Any, Optional
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.asyncio.transaction import submit_and_wait
from xrpl.asyncio.ledger import get_latest_validated_ledger_sequence
from xrpl.models.requests import (
    AccountInfo, AccountLines, AccountTx, BookOffers, Ledger, RipplePathFind,
    ServerInfo, Tx, Fee, AccountChannels, AccountCurrencies, AccountObjects, AccountOffers,
    LedgerEntry, GatewayBalances, NoRippleCheck, AMMInfo, NFTBuyOffers, NFTSellOffers
)
from xrpl.models.transactions import (
    Payment, OfferCreate, OfferCancel, TrustSet, AccountSet, CheckCash, CheckCreate,
    EscrowCreate, EscrowFinish, EscrowCancel, PaymentChannelCreate, PaymentChannelClaim,
    PaymentChannelFund, SignerListSet, TicketCreate, NFTokenMint, NFTokenBurn,
    NFTokenCreateOffer, NFTokenAcceptOffer, NFTokenCancelOffer,
    AMMCreate, AMMWithdraw, AMMDeposit, AMMBid, AMMVote
)
from xrpl.models.amounts import IssuedCurrencyAmount
from xrpl.wallet import Wallet
from xrpl.utils import xrp_to_drops, drops_to_xrp
from xrpl.core.addresscodec import is_valid_classic_address
from cryptography.fernet import Fernet
import base64
import hashlib
import json
from datetime import datetime, timedelta, UTC
import time
import matplotlib.pyplot as plt
import io
from statistics import mean, stdev
from collections import Counter

# Custom formatter to handle missing user_id
class CustomFormatter(logging.Formatter):
    def format(self, record):
        if not hasattr(record, 'user_id'):
            record.user_id = 'N/A'
        return super().format(record)

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
handler = logging.StreamHandler()
formatter = CustomFormatter('%(asctime)s - %(name)s - %(levelname)s - %(user_id)s - %(message)s')
handler.setFormatter(formatter)
logger.handlers = [handler]

class XRPLBotAgent:
    def __init__(self):
        self.client = None  # Will be initialized in init()
        self.current_network = None
        self.application = None
        self.pending_inputs = {}
        self.price_cache = {}  # {currency_issuer: [(timestamp, price), ...]}
        self.DB_DIR = "db"
        self.DB_NAME = os.path.join(self.DB_DIR, "xrpl_bot.db")
        self.TELEGRAM_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"  # Add your token here
        self.NETWORKS = {
            "ripple_s1": "https://s1.ripple.com:51234/",
            "ripple_s2": "https://s2.ripple.com:51234/"
        }
        self.SLIPPAGE_DISCLAIMER = "\nNote: Slippage may affect final amount received."
        self.init()

    def init(self):
        self.client = AsyncJsonRpcClient(self.NETWORKS["ripple_s1"])
        self.current_network = "ripple_s1"
        self.application = Application.builder().token(self.TELEGRAM_TOKEN).build()
        self.pending_inputs = {}
        self.price_cache = {}
        self._setup_handlers()
        self.init_db()

    # Database Functions
    def init_db(self):
        if not os.path.exists(self.DB_DIR):
            os.makedirs(self.DB_DIR)
            logger.info(f"Created database directory: {self.DB_DIR}")
        conn = sqlite3.connect(self.DB_NAME, check_same_thread=False)
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS users 
                     (user_id INTEGER, wallet_name TEXT, encrypted_seed TEXT, secret_name TEXT, is_current INTEGER DEFAULT 0, slippage REAL DEFAULT 5.0,
                      PRIMARY KEY (user_id, wallet_name))''')
        c.execute('''CREATE TABLE IF NOT EXISTS secrets 
                     (user_id INTEGER, secret_name TEXT, secret_value TEXT, is_default INTEGER DEFAULT 0,
                      PRIMARY KEY (user_id, secret_name))''')
        conn.commit()
        conn.close()

    def get_fernet_key(self, user_id: int, secret_value: str) -> bytes:
        key_material = f"{user_id}{secret_value}".encode()
        key = hashlib.sha256(key_material).digest()[:32]
        return base64.urlsafe_b64encode(key)

    def encrypt_seed(self, seed: str, user_id: int, secret_name: str) -> bytes:
        secret_value = self.get_secret(user_id, secret_name)
        fernet = Fernet(self.get_fernet_key(user_id, secret_value))
        return fernet.encrypt(seed.encode())

    def decrypt_seed(self, encrypted_seed: bytes, user_id: int, secret_name: str) -> str:
        secret_value = self.get_secret(user_id, secret_name)
        fernet = Fernet(self.get_fernet_key(user_id, secret_value))
        return fernet.decrypt(encrypted_seed).decode()

    def add_secret(self, user_id: int, secret_name: str, secret_value: str) -> bool:
        conn = sqlite3.connect(self.DB_NAME)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM secrets WHERE user_id = ?", (user_id,))
        is_first = c.fetchone()[0] == 0
        c.execute("INSERT OR REPLACE INTO secrets (user_id, secret_name, secret_value, is_default) VALUES (?, ?, ?, ?)",
                  (user_id, secret_name, secret_value, 1 if is_first else 0))
        conn.commit()
        conn.close()
        return True

    def get_secret(self, user_id: int, secret_name: Optional[str] = None) -> Optional[str]:
        conn = sqlite3.connect(self.DB_NAME)
        c = conn.cursor()
        if secret_name:
            c.execute("SELECT secret_value FROM secrets WHERE user_id = ? AND secret_name = ?", (user_id, secret_name))
        else:
            c.execute("SELECT secret_value FROM secrets WHERE user_id = ? AND is_default = 1", (user_id,))
        result = c.fetchone()
        conn.close()
        return result[0] if result else None

    def set_default_secret(self, user_id: int, secret_name: str) -> bool:
        conn = sqlite3.connect(self.DB_NAME)
        c = conn.cursor()
        c.execute("UPDATE secrets SET is_default = 0 WHERE user_id = ?", (user_id,))
        c.execute("UPDATE secrets SET is_default = 1 WHERE user_id = ? AND secret_name = ?", (user_id, secret_name))
        conn.commit()
        conn.close()
        return c.rowcount > 0

    def get_wallet(self, user_id: int, wallet_name: str = None) -> Optional[Wallet]:
        conn = sqlite3.connect(self.DB_NAME)
        c = conn.cursor()
        if wallet_name:
            c.execute("SELECT encrypted_seed, secret_name FROM users WHERE user_id = ? AND wallet_name = ?",
                      (user_id, wallet_name))
        else:
            c.execute("SELECT encrypted_seed, secret_name FROM users WHERE user_id = ? AND is_current = 1",
                      (user_id,))
        result = c.fetchone()
        conn.close()
        if result and result[0]:
            encrypted_seed, secret_name = result
            seed = self.decrypt_seed(encrypted_seed, user_id, secret_name)
            return Wallet.from_seed(seed)
        return None

    def get_all_wallets(self, user_id: int) -> list[tuple[str, Wallet]]:
        conn = sqlite3.connect(self.DB_NAME)
        c = conn.cursor()
        c.execute("SELECT wallet_name, encrypted_seed, secret_name FROM users WHERE user_id = ?", (user_id,))
        results = c.fetchall()
        conn.close()
        wallets = []
        for wallet_name, encrypted_seed, secret_name in results:
            seed = self.decrypt_seed(encrypted_seed, user_id, secret_name)
            wallets.append((wallet_name, Wallet.from_seed(seed)))
        return wallets

    def set_current_wallet(self, user_id: int, wallet_name: str) -> bool:
        conn = sqlite3.connect(self.DB_NAME)
        c = conn.cursor()
        c.execute("UPDATE users SET is_current = 0 WHERE user_id = ?", (user_id,))
        c.execute("UPDATE users SET is_current = 1 WHERE user_id = ? AND wallet_name = ?", (user_id, wallet_name))
        conn.commit()
        conn.close()
        return c.rowcount > 0

    def get_slippage(self, user_id: int, wallet_name: str = None) -> float:
        conn = sqlite3.connect(self.DB_NAME)
        c = conn.cursor()
        if wallet_name:
            c.execute("SELECT slippage FROM users WHERE user_id = ? AND wallet_name = ?", (user_id, wallet_name))
        else:
            c.execute("SELECT slippage FROM users WHERE user_id = ? AND is_current = 1", (user_id,))
        result = c.fetchone()
        conn.close()
        return result[0] if result else 5.0

    def set_slippage(self, user_id: int, slippage: float, wallet_name: str = None) -> bool:
        conn = sqlite3.connect(self.DB_NAME)
        c = conn.cursor()
        if wallet_name:
            c.execute("UPDATE users SET slippage = ? WHERE user_id = ? AND wallet_name = ?",
                      (slippage, user_id, wallet_name))
        else:
            c.execute("UPDATE users SET slippage = ? WHERE user_id = ? AND is_current = 1",
                      (slippage, user_id))
        affected = c.rowcount > 0
        conn.commit()
        conn.close()
        return affected

    # XRPL Utilities
    async def calculate_dynamic_slippage(self, currency: str, issuer: str) -> float:
        try:
            request = BookOffers(taker_pays="XRP",
                                 taker_gets=IssuedCurrencyAmount(currency=currency, issuer=issuer, value="1"))
            async with asyncio.timeout(10):
                response = await self.client.request(request)
            if response.is_successful() and response.result["offers"]:
                prices = [float(offer["TakerPays"]) / float(offer["TakerGets"]) for offer in response.result["offers"][-10:]]
                if len(prices) > 1:
                    volatility = (max(prices) - min(prices)) / min(prices) * 100
                    return min(max(2.0, volatility * 1.5), 30.0)
            return 5.0
        except Exception as e:
            logger.exception(f"Error calculating dynamic slippage: {e}")
            return 5.0

    async def get_fee(self) -> str:
        response = await self.client.request(Fee())
        return response.result["drops"]["base_fee"] if response.is_successful() else "10"

    async def get_ledger_index(self) -> int:
        return await get_latest_validated_ledger_sequence(self.client)

    # Data Fetching and Caching
    async def fetch_price_data(self, quote_currency: str, issuer: str, periods: int = 20) -> list[tuple[float, float]]:
        key = f"XRP_{quote_currency}_{issuer}"
        if key not in self.price_cache:
            self.price_cache[key] = []

        prices = []
        for network in ["ripple_s1", "ripple_s2"]:
            try:
                if network != self.current_network:
                    self.client = AsyncJsonRpcClient(self.NETWORKS[network])
                    self.current_network = network
                for _ in range(periods - len(prices)):
                    async with asyncio.timeout(10):
                        response = await self.client.request(BookOffers(
                            taker_pays="XRP",
                            taker_gets=IssuedCurrencyAmount(currency=quote_currency, issuer=issuer, value="1")
                        ))
                    if response.is_successful() and response.result["offers"]:
                        price = float(response.result["offers"][0]["TakerPays"]) / float(
                            response.result["offers"][0]["TakerGets"])
                        prices.append((time.time(), price))
                    else:
                        logger.warning(
                            f"No offers found for XRP/{quote_currency} with issuer {issuer} on {network}")
                        break
                    await asyncio.sleep(2)
                break
            except Exception as e:
                logger.error(f"Failed to fetch price data from {network}: {e}")
                if network == "ripple_s2":
                    logger.error("Both XRPL nodes failed; using cached/default data")

        if not prices:
            prices = self.price_cache[key][-periods:] if self.price_cache[key] else [(time.time(), 0.5)] * periods
        else:
            self.price_cache[key].extend(prices)
            prices = self.price_cache[key][-periods:]

        return prices

    # Technical Analysis Functions
    def calculate_fibonacci_levels(self, high: float, low: float) -> Dict[str, float]:
        diff = high - low
        return {
            "0%": low,
            "23.6%": low + (diff * 0.236),
            "38.2%": low + (diff * 0.382),
            "50%": low + (diff * 0.5),
            "61.8%": low + (diff * 0.618),
            "100%": high,
            "161.8%": high + (diff * 0.618)
        }

    def calculate_sma(self, prices: list[float], period: int) -> list[float]:
        sma = []
        for i in range(len(prices)):
            if i < period - 1:
                sma.append(None)
            else:
                sma.append(mean(prices[i - period + 1:i + 1]))
        return sma

    def calculate_ema(self, prices: list[float], period: int) -> list[float]:
        ema = [prices[0]] if prices else []
        multiplier = 2 / (period + 1)
        for i in range(1, len(prices)):
            ema.append((prices[i] * multiplier) + (ema[-1] * (1 - multiplier)))
        return [None] * (period - 1) + ema[-len(prices) + period - 1:] if len(prices) >= period else [None] * len(prices)

    def calculate_rsi(self, prices: list[float], period: int = 14) -> list[float]:
        rsi = [None] * (period - 1)
        if len(prices) < period + 1:
            return rsi + [50.0] * (len(prices) - period + 1)
        gains, losses = [], []
        for i in range(1, len(prices)):
            diff = prices[i] - prices[i - 1]
            gains.append(max(diff, 0))
            losses.append(max(-diff, 0))
        for i in range(period - 1, len(gains)):
            avg_gain = mean(gains[i - period + 1:i + 1]) if gains[i - period + 1:i + 1] else 0
            avg_loss = mean(losses[i - period + 1:i + 1]) if losses[i - period + 1:i + 1] else 0
            rs = avg_gain / avg_loss if avg_loss != 0 else float('inf')
            rsi.append(100 - (100 / (1 + rs)))
        return rsi

    def calculate_bollinger_bands(self, prices: list[float], period: int = 20) -> tuple[list[float], list[float], list[float]]:
        sma = self.calculate_sma(prices, period)
        upper, lower = [], []
        for i in range(len(prices)):
            if i < period - 1:
                upper.append(None)
                lower.append(None)
            else:
                window = prices[i - period + 1:i + 1]
                std = stdev(window) if len(window) > 1 else 0
                upper.append(sma[i] + 2 * std if sma[i] is not None else None)
                lower.append(sma[i] - 2 * std if sma[i] is not None else None)
        return upper, sma, lower

    def calculate_macd(self, prices: list[float], short_period: int = 12, long_period: int = 26,
                       signal_period: int = 9) -> tuple[list[float], list[float], list[float]]:
        ema_short = self.calculate_ema(prices, short_period)
        ema_long = self.calculate_ema(prices, long_period)
        macd_line = [None if s is None or l is None else s - l for s, l in zip(ema_short, ema_long)]
        signal_line = self.calculate_ema([m for m in macd_line if m is not None], signal_period)
        signal_line = [None] * (len(macd_line) - len(signal_line)) + signal_line
        histogram = [None if m is None or s is None else m - s for m, s in zip(macd_line, signal_line)]
        return macd_line, signal_line, histogram

    def calculate_support_resistance(self, prices: list[float], bins: int = 10) -> tuple[float, float]:
        hist, edges = [], []
        valid_prices = [p for p in prices if p is not None]
        if not valid_prices:
            return 0.5, 0.5
        for i in range(0, len(valid_prices), bins):
            chunk = valid_prices[i:i + bins]
            if chunk:
                counts = Counter([round(p, 2) for p in chunk])
                hist.extend(counts.values())
                edges.extend(counts.keys())
        if not edges:
            return min(valid_prices), max(valid_prices)
        support = edges[hist.index(max(hist))] if hist else min(valid_prices)
        resistance = edges[hist.index(max(hist))] if hist else max(valid_prices)
        return support, resistance

    async def calculate_volume(self, wallet: str, periods: int = 20) -> list[float]:
        response = await self.client.request(AccountTx(account=wallet, limit=periods))
        if not response.is_successful() or "transactions" not in response.result:
            return [0.0] * periods
        volumes = []
        for tx in response.result["transactions"]:
            if "Amount" in tx["tx"]:
                amount = tx["tx"]["Amount"]
                volumes.append(float(amount) / 1_000_000 if isinstance(amount, str) else float(amount["value"]))
        return volumes + [0.0] * (periods - len(volumes)) if len(volumes) < periods else volumes[-periods:]

    def calculate_stochastic(self, prices: list[float], period: int = 14) -> list[float]:
        stochastic = []
        for i in range(len(prices)):
            if i < period - 1:
                stochastic.append(None)
            else:
                window = [p for p in prices[i - period + 1:i + 1] if p is not None]
                if not window:
                    stochastic.append(None)
                else:
                    highest = max(window)
                    lowest = min(window)
                    stochastic.append(100 * (prices[i] - lowest) / (highest - lowest) if highest != lowest else 50)
        return stochastic

    def calculate_atr(self, prices: list[float], period: int = 14) -> list[float]:
        atr = []
        if len(prices) < 2:
            return [0.0] * len(prices)
        trs = []
        for i in range(1, len(prices)):
            if prices[i] is not None and prices[i - 1] is not None:
                tr = max(prices[i] - prices[i - 1], 0)
                trs.append(tr)
        for i in range(len(prices)):
            if i < period:
                atr.append(None)
            else:
                window = trs[i - period:i]
                atr.append(mean(window) if window else None)
        return atr

    async def generate_chart(self, user_id: int, quote_currency: str, issuer: str,
                             prices: list[tuple[float, float]],
                             fib_levels: Optional[Dict[str, float]] = None) -> io.BytesIO:
        times, price_values = zip(*prices)
        price_values = [float(p) for p in price_values]
        plt.figure(figsize=(12, 10))

        # Price Plot with Indicators
        plt.subplot(3, 1, 1)
        plt.plot(range(len(price_values)), price_values, label="Price", color="blue")
        if fib_levels:
            for level, value in fib_levels.items():
                plt.axhline(y=value, linestyle="--", label=f"Fib {level}", alpha=0.7)
        sma_10 = self.calculate_sma(price_values, 10)
        upper_bb, sma_20, lower_bb = self.calculate_bollinger_bands(price_values, 20)
        support, resistance = self.calculate_support_resistance(price_values)
        plt.plot(range(len(sma_10)), sma_10, label="SMA (10)", color="orange")
        plt.plot(range(len(upper_bb)), upper_bb, label="BB Upper", color="green", linestyle="--")
        plt.plot(range(len(lower_bb)), lower_bb, label="BB Lower", color="red", linestyle="--")
        plt.axhline(y=support, linestyle="-", color="purple", label="Support", alpha=0.5)
        plt.axhline(y=resistance, linestyle="-", color="pink", label="Resistance", alpha=0.5)
        plt.title(f"XRP/{quote_currency} Price Chart (Issuer: {issuer[:6]})")
        plt.xlabel("Time (Recent Trades)")
        plt.ylabel(f"Price ({quote_currency})")
        plt.legend()
        plt.grid(True)

        # RSI and Stochastic
        plt.subplot(3, 1, 2)
        rsi = self.calculate_rsi(price_values, 14)
        stochastic = self.calculate_stochastic(price_values, 14)
        plt.plot(range(len(rsi)), rsi, label="RSI (14)", color="purple")
        plt.plot(range(len(stochastic)), stochastic, label="Stochastic (14)", color="cyan")
        plt.axhline(y=70, linestyle="--", color="red", alpha=0.5)
        plt.axhline(y=30, linestyle="--", color="green", alpha=0.5)
        plt.title("RSI & Stochastic")
        plt.xlabel("Time (Recent Trades)")
        plt.ylabel("Value")
        plt.legend()
        plt.grid(True)

        # MACD and Volume
        plt.subplot(3, 1, 3)
        macd_line, signal_line, histogram = self.calculate_macd(price_values)
        wallet = self.get_wallet(user_id).classic_address if self.get_wallet(user_id) else "rDefault"
        volume = await self.calculate_volume(wallet, len(price_values))
        plt.plot(range(len(macd_line)), macd_line, label="MACD", color="blue")
        plt.plot(range(len(signal_line)), signal_line, label="Signal", color="orange")
        plt.bar(range(len(histogram)), histogram, label="Histogram", color="grey", alpha=0.5)
        plt.twinx()
        plt.plot(range(len(volume)), volume, label="Volume", color="green", alpha=0.3)
        plt.title("MACD & Volume")
        plt.xlabel("Time (Recent Trades)")
        plt.ylabel("MACD")
        plt.legend()
        plt.grid(True)

        buf = io.BytesIO()
        plt.tight_layout()
        plt.savefig(buf, format="png")
        buf.seek(0)
        plt.close()
        return buf

    # UI Functions
    def get_main_menu(self):
        keyboard = [
            [InlineKeyboardButton("👛 Wallet", callback_data="wallet_menu"),
             InlineKeyboardButton("💸 Trading", callback_data="trading_menu")],
            [InlineKeyboardButton("🔗 Payments", callback_data="payments_menu"),
             InlineKeyboardButton("📊 Info", callback_data="info_menu")],
            [InlineKeyboardButton("🔒 Advanced", callback_data="advanced_menu"),
             InlineKeyboardButton("⚙️ Settings", callback_data="settings_menu")],
            [InlineKeyboardButton("🏦 AMM", callback_data="amm_menu"),
             InlineKeyboardButton("🖼 NFTs", callback_data="nft_menu")],
            [InlineKeyboardButton("📈 Analysis", callback_data="analysis_menu")]
        ]
        return InlineKeyboardMarkup(keyboard)

    def get_analysis_menu(self):
        keyboard = [
            [InlineKeyboardButton("📏 Fibonacci Chart", callback_data="fibonacci_chart"),
             InlineKeyboardButton("📉 TA Chart", callback_data="ta_chart")],
            [InlineKeyboardButton("Back", callback_data="main_menu")]
        ]
        return InlineKeyboardMarkup(keyboard)

    def get_wallet_menu(self):
        keyboard = [
            [InlineKeyboardButton("✨ Create Wallet", callback_data="create_wallet"),
             InlineKeyboardButton("🎩 Switch Wallet", callback_data="switch_wallet")],
            [InlineKeyboardButton("🦄 Balance", callback_data="balance"),
             InlineKeyboardButton("📊 Portfolio", callback_data="portfolio")],
            [InlineKeyboardButton("Back", callback_data="main_menu")]
        ]
        return InlineKeyboardMarkup(keyboard)

    def get_trading_menu(self):
        keyboard = [
            [InlineKeyboardButton("💸 Buy", callback_data="buy"),
             InlineKeyboardButton("💰 Sell", callback_data="sell")],
            [InlineKeyboardButton("📈 Buy At Price", callback_data="buy_at"),
             InlineKeyboardButton("📉 Sell At Price", callback_data="sell_at")],
            [InlineKeyboardButton("❌ Cancel Order", callback_data="cancel_order"),
             InlineKeyboardButton("💲 Price Check", callback_data="price")],
            [InlineKeyboardButton("Back", callback_data="main_menu")]
        ]
        return InlineKeyboardMarkup(keyboard)

    def get_payments_menu(self):
        keyboard = [
            [InlineKeyboardButton("🎀 Send XRP", callback_data="send"),
             InlineKeyboardButton("🍭 Distribute XRP", callback_data="distribute")],
            [InlineKeyboardButton("🌈 Trustline", callback_data="trustline"),
             InlineKeyboardButton("🔗 Payment Path", callback_data="payment_path")],
            [InlineKeyboardButton("💸 Pay Channel Create", callback_data="pay_channel_create"),
             InlineKeyboardButton("✅ Pay Channel Claim", callback_data="pay_channel_claim")],
            [InlineKeyboardButton("Back", callback_data="main_menu")]
        ]
        return InlineKeyboardMarkup(keyboard)

    def get_info_menu(self):
        keyboard = [
            [InlineKeyboardButton("📜 History", callback_data="history"),
             InlineKeyboardButton("📋 Ledger Info", callback_data="ledger_info")],
            [InlineKeyboardButton("🏦 Gateway Balances", callback_data="gateway_balances"),
             InlineKeyboardButton("🔍 Server Info", callback_data="server_info")],
            [InlineKeyboardButton("Back", callback_data="main_menu")]
        ]
        return InlineKeyboardMarkup(keyboard)

    def get_advanced_menu(self):
        keyboard = [
            [InlineKeyboardButton("🔒 Escrow Create", callback_data="escrow_create"),
             InlineKeyboardButton("✅ Escrow Finish", callback_data="escrow_finish")],
            [InlineKeyboardButton("❌ Escrow Cancel", callback_data="escrow_cancel"),
             InlineKeyboardButton("📝 Check Create", callback_data="check_create")],
            [InlineKeyboardButton("💵 Check Cash", callback_data="check_cash"),
             InlineKeyboardButton("🎟 Ticket Create", callback_data="ticket_create")],
            [InlineKeyboardButton("Back", callback_data="main_menu")]
        ]
        return InlineKeyboardMarkup(keyboard)

    def get_settings_menu(self):
        keyboard = [
            [InlineKeyboardButton("🔮 Add Secret", callback_data="add_secret"),
             InlineKeyboardButton("🌟 Set Default Secret", callback_data="set_default_secret")],
            [InlineKeyboardButton("⚙️ Set Slippage", callback_data="set_slippage"),
             InlineKeyboardButton("🌐 Switch Network", callback_data="network")],
            [InlineKeyboardButton("💾 Backup", callback_data="backup"),
             InlineKeyboardButton("🔐 Account Set", callback_data="account_set")],
            [InlineKeyboardButton("Back", callback_data="main_menu")]
        ]
        return InlineKeyboardMarkup(keyboard)

    def get_amm_menu(self):
        keyboard = [
            [InlineKeyboardButton("🏦 Create AMM Pool", callback_data="amm_create"),
             InlineKeyboardButton("💧 Deposit to AMM", callback_data="amm_deposit")],
            [InlineKeyboardButton("🏦 Withdraw from AMM", callback_data="amm_withdraw"),
             InlineKeyboardButton("📈 AMM Bid", callback_data="amm_bid")],
            [InlineKeyboardButton("🗳 AMM Vote", callback_data="amm_vote"),
             InlineKeyboardButton("ℹ️ AMM Info", callback_data="amm_info")],
            [InlineKeyboardButton("Back", callback_data="main_menu")]
        ]
        return InlineKeyboardMarkup(keyboard)

    def get_nft_menu(self):
        keyboard = [
            [InlineKeyboardButton("🖼 Mint NFT", callback_data="nft_mint"),
             InlineKeyboardButton("🔥 Burn NFT", callback_data="nft_burn")],
            [InlineKeyboardButton("🤝 Create NFT Offer", callback_data="nft_offer_create"),
             InlineKeyboardButton("✅ Accept NFT Offer", callback_data="nft_offer_accept")],
            [InlineKeyboardButton("❌ Cancel NFT Offer", callback_data="nft_offer_cancel"),
             InlineKeyboardButton("📜 NFT Buy Offers", callback_data="nft_buy_offers")],
            [InlineKeyboardButton("Back", callback_data="main_menu")]
        ]
        return InlineKeyboardMarkup(keyboard)

    def get_wallet_selection(self, user_id: int, action: str, back_action: str = "wallet_menu"):
        wallets = self.get_all_wallets(user_id)
        if not wallets:
            return InlineKeyboardMarkup([[InlineKeyboardButton("No wallets! Back", callback_data=back_action)]])
        keyboard = [[InlineKeyboardButton(f"{name}", callback_data=f"{action}|{name}")] for name, _ in wallets]
        keyboard.append([InlineKeyboardButton("Back", callback_data=back_action)])
        return InlineKeyboardMarkup(keyboard)

    def get_amount_selection(self, action: str, currency: str = "", issuer: str = ""):
        amounts = ["1", "10", "50", "100", "500"]
        keyboard = [[InlineKeyboardButton(f"{amt}", callback_data=f"{action}|{amt}|{currency}|{issuer}")] for amt in amounts]
        keyboard.append([InlineKeyboardButton("Back",
                                              callback_data="trading_menu" if "buy" in action or "sell" in action else "payments_menu")])
        return InlineKeyboardMarkup(keyboard)

    def get_currency_selection(self, action: str, amount: str):
        currencies = ["USD", "EUR", "BTC"]
        keyboard = [[InlineKeyboardButton(curr, callback_data=f"{action}|{amount}|{curr}")] for curr in currencies]
        keyboard.append([InlineKeyboardButton("Back",
                                              callback_data="trading_menu" if "buy" in action or "sell" in action else "payments_menu")])
        return InlineKeyboardMarkup(keyboard)

    def get_issuer_selection(self, action: str, amount: str, currency: str):
        issuers = ["rvYAfWj5gh67oV6fW32ZzP3Aw4Eubs59B", "r456..."]
        keyboard = [[InlineKeyboardButton(issuer[:6], callback_data=f"{action}|{amount}|{currency}|{issuer}")] for issuer in issuers]
        keyboard.append([InlineKeyboardButton("Back",
                                              callback_data="trading_menu" if "buy" in action or "sell" in action else "payments_menu")])
        return InlineKeyboardMarkup(keyboard)

    def get_slippage_selection(self, action: str, amount: str, currency: str, issuer: str):
        keyboard = [
            [InlineKeyboardButton("Static (5%)", callback_data=f"{action}|{amount}|{currency}|{issuer}|static")],
            [InlineKeyboardButton("Dynamic (2-30%)", callback_data=f"{action}|{amount}|{currency}|{issuer}|dynamic")],
            [InlineKeyboardButton("Back", callback_data="trading_menu")]
        ]
        return InlineKeyboardMarkup(keyboard)

    def get_confirmation(self, action: str, details: str, back_action: str = "main_menu"):
        keyboard = [
            [InlineKeyboardButton("Yes", callback_data=f"{action}_confirm|{details}"),
             InlineKeyboardButton("No", callback_data=back_action)]
        ]
        return InlineKeyboardMarkup(keyboard)

    def get_fibonacci_trade_menu(self, levels: Dict[str, float]):
        keyboard = [
            [InlineKeyboardButton(f"Buy at {k} ({v:.2f})", callback_data=f"buy_at_fib|{k}|{v}") for k, v in list(levels.items())[:3]],
            [InlineKeyboardButton(f"Sell at {k} ({v:.2f})", callback_data=f"sell_at_fib|{k}|{v}") for k, v in list(levels.items())[4:6]],
            [InlineKeyboardButton("Back", callback_data="analysis_menu")]
        ]
        return InlineKeyboardMarkup(keyboard)

    # Telegram Handlers
    def _setup_handlers(self):
        self.application.add_handler(CommandHandler("start", self.start))
        self.application.add_handler(CallbackQueryHandler(self.button))
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text_input))

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = update.message.from_user.id
        logger.info("User started bot", extra={'user_id': user_id})
        await update.message.reply_text("Welcome to your XRP Bot! 🌌", reply_markup=self.get_main_menu())

    async def button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        user_id = query.from_user.id
        data = query.data.split("|")
        action = data[0]

        response = await self.handle_message({
            "user_id": user_id,
            "action": action,
            "data": data[1:] if len(data) > 1 else [],
            "telegram_query": query
        })

        if "telegram_response" in response:
            await query.edit_message_text(response["telegram_response"],
                                          reply_markup=response.get("reply_markup", self.get_main_menu()))
        elif "image" in response:
            await query.message.reply_photo(response["image"], caption=response.get("caption", ""),
                                            reply_markup=response.get("reply_markup", self.get_main_menu()))
            await query.delete()
        elif "error" in response:
            await query.edit_message_text(f"Error: {response['error']}", reply_markup=self.get_main_menu())

    async def handle_text_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = update.message.from_user.id
        text = update.message.text
        if user_id in self.pending_inputs:
            action, params = self.pending_inputs[user_id]
            del self.pending_inputs[user_id]
            response = await self.handle_message({
                "user_id": user_id,
                "action": action,
                "data": params + [text],
                "telegram_query": None
            })
            if "telegram_response" in response:
                await update.message.reply_text(response["telegram_response"],
                                                reply_markup=response.get("reply_markup", self.get_main_menu()))
            elif "image" in response:
                await update.message.reply_photo(response["image"], caption=response.get("caption", ""),
                                                 reply_markup=response.get("reply_markup", self.get_main_menu()))
            elif "error" in response:
                await update.message.reply_text(f"Error: {response['error']}", reply_markup=self.get_main_menu())

    # Core Agent Logic
    async def handle_message(self, args: Dict[str, Any]) -> Dict[str, Any]:
        user_id = args.get("user_id")
        action = args.get("action", "").lower()
        data = args.get("data", [])
        telegram_query = args.get("telegram_query")

        if not user_id:
            return {"error": "user_id is required"}

        try:
            # Main Menu Navigation
            if action == "main_menu":
                return {"telegram_response": "Pick an option! 🌌", "reply_markup": self.get_main_menu()}
            elif action == "wallet_menu":
                return {"telegram_response": "Wallet Options:", "reply_markup": self.get_wallet_menu()}
            elif action == "trading_menu":
                return {"telegram_response": "Trading Options:", "reply_markup": self.get_trading_menu()}
            elif action == "payments_menu":
                return {"telegram_response": "Payment Options:", "reply_markup": self.get_payments_menu()}
            elif action == "info_menu":
                return {"telegram_response": "Info Options:", "reply_markup": self.get_info_menu()}
            elif action == "advanced_menu":
                return {"telegram_response": "Advanced Options:", "reply_markup": self.get_advanced_menu()}
            elif action == "settings_menu":
                return {"telegram_response": "Settings Options:", "reply_markup": self.get_settings_menu()}
            elif action == "amm_menu":
                return {"telegram_response": "AMM Options:", "reply_markup": self.get_amm_menu()}
            elif action == "nft_menu":
                return {"telegram_response": "NFT Options:", "reply_markup": self.get_nft_menu()}
            elif action == "analysis_menu":
                return {"telegram_response": "Analysis Tools:", "reply_markup": self.get_analysis_menu()}

            # Wallet Management
            elif action == "create_wallet":
                new_wallet = Wallet.create()
                secret_name = self.get_secret(user_id) or "default"
                encrypted_seed = self.encrypt_seed(new_wallet.seed, user_id, secret_name)
                conn = sqlite3.connect(self.DB_NAME)
                c = conn.cursor()
                wallet_name = f"wallet_{time.time()}"
                c.execute(
                    "INSERT INTO users (user_id, wallet_name, encrypted_seed, secret_name, is_current, slippage) VALUES (?, ?, ?, ?, ?, ?)",
                    (user_id, wallet_name, encrypted_seed, secret_name, 1, 5.0))
                conn.commit()
                conn.close()
                logger.info(f"Wallet created for user", extra={'user_id': user_id})
                return {"telegram_response": f"✨ Wallet created: {new_wallet.classic_address}",
                        "result": {"address": new_wallet.classic_address, "wallet_name": wallet_name}}

            elif action == "switch_wallet":
                return {"telegram_response": "🎩 Select a wallet:",
                        "reply_markup": self.get_wallet_selection(user_id, "switch_wallet_select")}

            elif action == "switch_wallet_select":
                wallet_name = data[0]
                if self.set_current_wallet(user_id, wallet_name):
                    logger.info(f"Switched wallet for user", extra={'user_id': user_id})
                    return {"telegram_response": f"🎩 Switched to wallet '{wallet_name}'!"}
                return {"error": "Failed to switch wallet"}

            elif action == "balance":
                wallet = self.get_wallet(user_id)
                if not wallet:
                    return {"telegram_response": "🦄 No wallet set!", "error": "No wallet set"}
                response = await self.client.request(
                    AccountInfo(account=wallet.classic_address, ledger_index="validated"))
                if response.is_successful():
                    balance = drops_to_xrp(response.result["account_data"]["Balance"])
                    logger.info(f"Checked balance for user", extra={'user_id': user_id})
                    return {"telegram_response": f"🦄 XRP Balance: {balance}", "result": {"balance": balance}}
                return {"error": "Failed to fetch balance"}

            elif action == "portfolio":
                wallet = self.get_wallet(user_id)
                if not wallet:
                    return {"telegram_response": "📊 No wallet set!", "error": "No wallet set"}
                lines = await self.client.request(AccountLines(account=wallet.classic_address))
                xrp = await self.client.request(
                    AccountInfo(account=wallet.classic_address, ledger_index="validated"))
                balances = [f"🟢 XRP: {drops_to_xrp(xrp.result['account_data']['Balance'])}"]
                portfolio = [{"currency": "XRP", "balance": drops_to_xrp(xrp.result["account_data"]["Balance"])}]
                if lines.is_successful():
                    for line in lines.result["lines"]:
                        balances.append(f"🟢 {line['currency']} ({line['account'][:6]}): {line['balance']}")
                        portfolio.append({"currency": line["currency"], "issuer": line["account"],
                                          "balance": float(line["balance"])})
                logger.info(f"Retrieved portfolio for user", extra={'user_id': user_id})
                return {"telegram_response": f"📊 Portfolio:\n" + "\n".join(balances),
                        "result": {"portfolio": portfolio}}

            # Trading
            elif action == "buy":
                return {"telegram_response": "💸 Select amount to buy:",
                        "reply_markup": self.get_amount_selection("buy_amount")}

            elif action == "buy_amount":
                amount = data[0]
                return {"telegram_response": f"💸 Buying {amount} - Select currency:",
                        "reply_markup": self.get_currency_selection("buy_currency", amount)}

            elif action == "buy_currency":
                amount, currency = data[0], data[1]
                return {"telegram_response": f"💸 Buying {amount} {currency} - Select issuer:",
                        "reply_markup": self.get_issuer_selection("buy_issuer", amount, currency)}

            elif action == "buy_issuer":
                amount, currency, issuer = data[0], data[1], data[2]
                return {"telegram_response": f"💸 Buying {amount} {currency} ({issuer[:6]}) - Slippage:",
                        "reply_markup": self.get_slippage_selection("buy_slippage", amount, currency, issuer)}

            elif action == "buy_slippage":
                amount, currency, issuer, slippage_type = data[0], data[1], data[2], data[3]
                slippage = await self.calculate_dynamic_slippage(currency,
                                                                 issuer) if slippage_type == "dynamic" else self.get_slippage(user_id)
                details = f"{amount}|{currency}|{issuer}|{slippage}"
                return {
                    "telegram_response": f"💸 Confirm buy: {amount} {currency} ({issuer[:6]}) with {slippage}% slippage? {self.SLIPPAGE_DISCLAIMER}",
                    "reply_markup": self.get_confirmation("buy", details, "trading_menu")}

            elif action == "buy_confirm":
                amount, currency, issuer, slippage = data[0].split("|")
                wallet = self.get_wallet(user_id)
                if not wallet:
                    return {"error": "No wallet set"}
                slippage = float(slippage)
                token_amount = IssuedCurrencyAmount(currency=currency, issuer=issuer, value=amount)
                offer_tx = OfferCreate(account=wallet.classic_address, taker_pays=token_amount,
                                       taker_gets=xrp_to_drops(float(amount) * (1 + slippage / 100)))
                response = await submit_and_wait(offer_tx, self.client, wallet)
                if response.is_successful():
                    logger.info(f"Buy order placed for user", extra={'user_id': user_id})
                    return {
                        "telegram_response": f"💸 Bought {amount} {currency}! Sequence: {response.result['tx_json']['Sequence']}",
                        "result": {"sequence": response.result["tx_json"]["Sequence"]}}
                return {"error": "Buy failed"}

            elif action == "sell":
                return {"telegram_response": "💰 Select amount to sell:",
                        "reply_markup": self.get_amount_selection("sell_amount")}

            # Payments
            elif action == "send" or action == "distribute":
                return {"telegram_response": f"{'🎀' if action == 'send' else '🍭'} Send XRP - Select amount:",
                        "reply_markup": self.get_amount_selection(f"{action}_amount")}

            elif action in ["send_amount", "distribute_amount"]:
                amount = data[0]
                self.pending_inputs[user_id] = (f"{action.split('_')[0]}_dest", [amount])
                return {
                    "telegram_response": f"{'🎀' if action.startswith('send') else '🍭'} Sending {amount} XRP - Enter destination address (e.g., r123...):"}

            elif action in ["send_dest", "distribute_dest"]:
                amount, destination = data[0], data[1]
                wallet = self.get_wallet(user_id)
                if not wallet:
                    return {"error": "No wallet set"}
                if not is_valid_classic_address(destination):
                    return {"error": "Invalid destination address"}
                payment_tx = Payment(account=wallet.classic_address, destination=destination,
                                     amount=xrp_to_drops(float(amount)))
                response = await submit_and_wait(payment_tx, self.client, wallet)
                if response.is_successful():
                    logger.info(f"XRP sent/distributed for user", extra={'user_id': user_id})
                    return {
                        "telegram_response": f"{'🎀' if action.startswith('send') else '🍭'} Sent {amount} XRP to {destination[:6]}! Sequence: {response.result['tx_json']['Sequence']}",
                        "result": {"sequence": response.result["tx_json"]["Sequence"]}}
                return {"error": "Send failed"}

            elif action == "trustline":
                return {"telegram_response": "🌈 Set trustline - Select currency:",
                        "reply_markup": self.get_currency_selection("trustline_currency", "")}

            elif action == "trustline_currency":
                currency = data[1]
                return {"telegram_response": f"🌈 Setting trustline for {currency} - Select issuer:",
                        "reply_markup": self.get_issuer_selection("trustline_issuer", "", currency)}

            elif action == "trustline_issuer":
                currency, issuer = data[1], data[2]
                wallet = self.get_wallet(user_id)
                if not wallet:
                    return {"error": "No wallet set"}
                trust_tx = TrustSet(account=wallet.classic_address,
                                    limit_amount=IssuedCurrencyAmount(currency=currency, issuer=issuer,
                                                                      value="1000000"))
                response = await submit_and_wait(trust_tx, self.client, wallet)
                if response.is_successful():
                    logger.info(f"Trustline set for user", extra={'user_id': user_id})
                    return {
                        "telegram_response": f"🌈 Trustline set for {currency} from {issuer[:6]}! Sequence: {response.result['tx_json']['Sequence']}",
                        "result": {"sequence": response.result["tx_json"]["Sequence"]}}
                return {"error": "Trustline creation failed"}

            elif action == "payment_path":
                return {"telegram_response": "🔗 Payment Path - Select amount:",
                        "reply_markup": self.get_amount_selection("payment_path_amount")}

            elif action == "payment_path_amount":
                amount = data[0]
                self.pending_inputs[user_id] = ("payment_path_dest", [amount])
                return {
                    "telegram_response": f"🔗 Finding path for {amount} XRP - Enter destination address (e.g., r123...):"}

            elif action == "payment_path_dest":
                amount, destination = data[0], data[1]
                wallet = self.get_wallet(user_id)
                if not wallet:
                    return {"error": "No wallet set"}
                if not is_valid_classic_address(destination):
                    return {"error": "Invalid destination address"}
                path_request = RipplePathFind(
                    source_account=wallet.classic_address,
                    destination_account=destination,
                    destination_amount=xrp_to_drops(float(amount))
                )
                response = await self.client.request(path_request)
                if response.is_successful() and response.result.get("alternatives"):
                    paths = response.result["alternatives"]
                    msg = f"🔗 Found {len(paths)} payment paths to {destination[:6]}:\n"
                    for i, path in enumerate(paths[:3], 1):
                        msg += f"Path {i}: {json.dumps(path['paths_computed'][0][:2] if path['paths_computed'] else 'Direct', indent=2)}\n"
                    logger.info(f"Payment paths found for user", extra={'user_id': user_id})
                    return {"telegram_response": msg, "result": {"paths": paths}}
                return {"error": "No payment paths found"}

            # Info
            elif action == "ledger_info":
                response = await self.client.request(Ledger(ledger_index="validated"))
                if response.is_successful():
                    ledger = response.result["ledger"]
                    info = f"📋 Ledger Index: {ledger['ledger_index']}\nClosed: {ledger['close_time_human']}\nTotal XRP: {drops_to_xrp(ledger['total_coins'])}"
                    logger.info(f"Ledger info retrieved for user", extra={'user_id': user_id})
                    return {"telegram_response": info, "result": ledger}
                return {"error": "Failed to fetch ledger info"}

            elif action == "history":
                wallet = self.get_wallet(user_id)
                if not wallet:
                    return {"error": "No wallet set"}
                response = await self.client.request(AccountTx(account=wallet.classic_address, limit=5))
                if response.is_successful():
                    txs = [
                        f"📜 {tx['tx']['TransactionType']} - {tx['tx'].get('Amount', 'N/A')} - {tx['tx'].get('Destination', 'N/A')[:6]}"
                        for tx in response.result["transactions"]]
                    logger.info(f"Transaction history retrieved for user", extra={'user_id': user_id})
                    return {"telegram_response": "📜 Recent Transactions:\n" + "\n".join(txs),
                            "result": response.result["transactions"]}
                return {"error": "Failed to fetch transaction history"}

            # Advanced
            elif action == "escrow_create":
                return {"telegram_response": "🔒 Escrow Create - Select amount:",
                        "reply_markup": self.get_amount_selection("escrow_create_amount")}

            elif action == "escrow_create_amount":
                amount = data[0]
                self.pending_inputs[user_id] = ("escrow_create_dest", [amount])
                return {
                    "telegram_response": f"🔒 Creating escrow for {amount} XRP - Enter destination address (e.g., r123...):"}

            elif action == "escrow_create_dest":
                amount, destination = data[0], data[1]
                wallet = self.get_wallet(user_id)
                if not wallet:
                    return {"error": "No wallet set"}
                escrow_tx = EscrowCreate(
                    account=wallet.classic_address,
                    amount=xrp_to_drops(float(amount)),
                    destination=destination,
                    finish_after=int(time.time()) + 86400
                )
                response = await submit_and_wait(escrow_tx, self.client, wallet)
                if response.is_successful():
                    logger.info(f"Escrow created for user", extra={'user_id': user_id})
                    return {
                        "telegram_response": f"🔒 Escrow created for {amount} XRP! Sequence: {response.result['tx_json']['Sequence']}",
                        "result": {"sequence": response.result["tx_json"]["Sequence"]}}
                return {"error": "Escrow creation failed"}

            # Settings
            elif action == "set_slippage":
                return {"telegram_response": "⚙️ Set slippage - Select value (in %):",
                        "reply_markup": self.get_amount_selection("set_slippage_value")}

            elif action == "set_slippage_value":
                slippage = float(data[0])
                if self.set_slippage(user_id, slippage):
                    logger.info(f"Slippage set for user", extra={'user_id': user_id})
                    return {"telegram_response": f"⚙️ Slippage set to {slippage}%",
                            "result": {"slippage": slippage}}
                return {"error": "Failed to set slippage"}

            # Analysis
            elif action == "ta_chart":
                self.pending_inputs[user_id] = ("ta_chart_currency", [])
                return {"telegram_response": "📉 Enter the quote currency for the XRP TA chart (e.g., USD):"}

            elif action == "ta_chart_currency":
                quote_currency = data[0].upper()
                self.pending_inputs[user_id] = ("ta_chart_issuer", [quote_currency])
                return {
                    "telegram_response": f"📉 Enter the issuer address for XRP/{quote_currency} (e.g., rvYAfWj5gh67oV6fW32ZzP3Aw4Eubs59B for Bitstamp USD):"}

            elif action == "ta_chart_issuer":
                quote_currency, issuer = data[0], data[1]
                if not is_valid_classic_address(issuer):
                    return {"error": "Invalid issuer address"}
                prices = await self.fetch_price_data(quote_currency, issuer, 20)
                if len(prices) < 10 or all(p[1] == 0.5 for p in prices):
                    return {
                        "error": f"No sufficient offer data found for XRP/{quote_currency} with issuer {issuer}"}
                chart = await self.generate_chart(user_id, quote_currency, issuer, prices)
                logger.info(f"TA chart generated for user with XRP/{quote_currency} (issuer: {issuer})",
                            extra={'user_id': user_id})
                return {"image": chart, "caption": f"XRP/{quote_currency} TA Chart (Issuer: {issuer[:6]})"}

            elif action == "fibonacci_chart":
                self.pending_inputs[user_id] = ("fibonacci_high", [])
                return {"telegram_response": "📏 Enter the high price (e.g., 1.5):"}

            elif action == "fibonacci_high":
                high = float(data[0])
                self.pending_inputs[user_id] = ("fibonacci_low", [high])
                return {"telegram_response": f"📏 High set to {high}. Enter the low price (e.g., 1.0):"}

            elif action == "fibonacci_low":
                high, low = float(data[0]), float(data[1])
                levels = self.calculate_fibonacci_levels(high, low)
                prices = await self.fetch_price_data("USD", "rvYAfWj5gh67oV6fW32ZzP3Aw4Eubs59B", 20)
                if len(prices) < 10:
                    return {"error": "Insufficient price data for chart"}
                chart = await self.generate_chart(user_id, "USD", "rvYAfWj5gh67oV6fW32ZzP3Aw4Eubs59B", prices, levels)
                msg = "📈 Fibonacci Levels:\n" + "\n".join([f"{k}: {v:.2f}" for k, v in levels.items()])
                logger.info(f"Fibonacci chart generated for user", extra={'user_id': user_id})
                return {"image": chart, "caption": msg, "reply_markup": self.get_fibonacci_trade_menu(levels)}

            elif action in ["buy_at_fib", "sell_at_fib"]:
                level_key, price = data[0], float(data[1])
                wallet = self.get_wallet(user_id)
                if not wallet:
                    return {"error": "No wallet set"}
                amount = xrp_to_drops(1)
                tx = OfferCreate(
                    account=wallet.classic_address,
                    taker_gets=amount if action == "buy_at_fib" else IssuedCurrencyAmount(currency="USD",
                                                                                          issuer="rvYAfWj5gh67oV6fW32ZzP3Aw4Eubs59B",
                                                                                          value="1"),
                    taker_pays=IssuedCurrencyAmount(currency="USD", issuer="rvYAfWj5gh67oV6fW32ZzP3Aw4Eubs59B",
                                                    value=str(price)) if action == "buy_at_fib" else amount
                )
                response = await submit_and_wait(tx, self.client, wallet)
                if response.is_successful():
                    logger.info(f"Trade executed at Fibonacci level for user", extra={'user_id': user_id})
                    return {
                        "telegram_response": f"{'💸 Bought' if action == 'buy_at_fib' else '💰 Sold'} at {level_key} ({price:.2f})! Sequence: {response.result['tx_json']['Sequence']}"}
                return {"error": "Trade failed"}

            else:
                return {"error": f"Unknown action: {action}"}

        except ValueError as e:
            return {"error": f"Invalid input: {e}"}
        except Exception as e:
            logger.error(f"Error handling action {action}: {str(e)}", extra={'user_id': user_id})
            return {"error": str(e)}

    def run(self):
        logger.info("Starting XRPL Telegram bot...")
        self.application.run_polling()

def main():
    agent = XRPLBotAgent()
    agent.run()

if __name__ == "__main__":
    main()