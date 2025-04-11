import logging
import sqlite3
import asyncio
import os
from typing import Dict, Any, Optional
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from xrpl.asyncio.clients import AsyncWebsocketClient
from xrpl.asyncio.transaction import submit_and_wait
from xrpl.asyncio.ledger import get_latest_validated_ledger_sequence
from xrpl.models.requests import AccountInfo, BookOffers, AMMInfo, Fee
from xrpl.models.transactions import Payment, OfferCreate, AMMCreate, AMMDeposit, AMMWithdraw, AMMBid, AMMVote, EscrowCreate, EscrowFinish, EscrowCancel, PaymentChannelCreate, PaymentChannelClaim
from xrpl.wallet import Wallet
from xrpl.utils import xrp_to_drops, drops_to_xrp
from xrpl.core.addresscodec import is_valid_classic_address
import matplotlib.pyplot as plt
import io
from statistics import mean, stdev
import time
from datetime import datetime

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class XRPLBot:
    def __init__(self):
        self.client = AsyncWebsocketClient("wss://xrplcluster.com/")
        self.application = Application.builder().token("8102741853:AAHraWG0DUmFnRhzLL043tBcchSMYNNmItk").build()  # Replace with your token
        self.pending_inputs = {}
        self.DB_NAME = "xrpl_bot.db"
        self.init_db()
        self._setup_handlers()
        self.application.job_queue.run_repeating(self.fetch_all_user_tokens, interval=60, first=10)

    ### Database Setup
    def init_db(self):
        """Initialize SQLite database for wallets, price history, tokens, and settings."""
        conn = sqlite3.connect(self.DB_NAME, check_same_thread=False)
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS users 
                     (user_id INTEGER, wallet_name TEXT, seed TEXT, is_current INTEGER DEFAULT 0, 
                      PRIMARY KEY (user_id, wallet_name))''')
        c.execute('''CREATE TABLE IF NOT EXISTS price_history 
                     (id INTEGER PRIMARY KEY AUTOINCREMENT, currency TEXT, issuer TEXT, timestamp INTEGER, 
                      bid_price REAL, ask_price REAL)''')
        c.execute('''CREATE TABLE IF NOT EXISTS user_tokens 
                     (user_id INTEGER, currency TEXT, issuer TEXT, PRIMARY KEY (user_id, currency, issuer))''')
        c.execute('''CREATE TABLE IF NOT EXISTS user_settings 
                     (user_id INTEGER PRIMARY KEY, default_currency TEXT, default_issuer TEXT, slippage_tolerance REAL)''')
        conn.commit()
        conn.close()

    ### Wallet Management
    def get_wallet(self, user_id: int, wallet_name: str = None) -> Optional[Wallet]:
        """Retrieve a user's current or specified wallet."""
        conn = sqlite3.connect(self.DB_NAME)
        c = conn.cursor()
        if wallet_name:
            c.execute("SELECT seed FROM users WHERE user_id = ? AND wallet_name = ?", (user_id, wallet_name))
        else:
            c.execute("SELECT seed FROM users WHERE user_id = ? AND is_current = 1", (user_id,))
        result = c.fetchone()
        conn.close()
        return Wallet.from_seed(result[0]) if result else None

    def set_current_wallet(self, user_id: int, wallet_name: str) -> bool:
        """Set a wallet as the current one for a user."""
        conn = sqlite3.connect(self.DB_NAME)
        c = conn.cursor()
        c.execute("UPDATE users SET is_current = 0 WHERE user_id = ?", (user_id,))
        c.execute("UPDATE users SET is_current = 1 WHERE user_id = ? AND wallet_name = ?", (user_id, wallet_name))
        conn.commit()
        conn.close()
        return c.rowcount > 0

    ### XRPL Utilities
    async def get_fee(self) -> str:
        """Get the current network fee."""
        async with self.client:
            response = await self.client.request(Fee())
            return response.result["drops"]["base_fee"] if response.is_successful() else "10"

    async def get_ledger_index(self) -> int:
        """Get the latest validated ledger index."""
        async with self.client:
            return await get_latest_validated_ledger_sequence(self.client)

    async def get_balance(self, wallet: Wallet) -> float:
        """Get XRP balance for a wallet."""
        async with self.client:
            response = await self.client.request(AccountInfo(account=wallet.classic_address, ledger_index="validated"))
            return drops_to_xrp(response.result["account_data"]["Balance"]) if response.is_successful() else 0.0

    ### Trading and Price Fetching
    async def fetch_price_data(self, currency: str, issuer: str) -> Dict[str, float]:
        """Fetch real-time bid and ask prices from the DEX order book."""
        async with self.client:
            bid_request = BookOffers(taker_gets={"currency": "XRP"}, taker_pays={"currency": currency, "issuer": issuer}, limit=1)
            bid_response = await self.client.request(bid_request)
            bid_offers = bid_response.result.get("offers", [])
            best_bid = float(bid_offers[0]["quality"]) if bid_offers else 0.0

            ask_request = BookOffers(taker_gets={"currency": currency, "issuer": issuer}, taker_pays={"currency": "XRP"}, limit=1)
            ask_response = await self.client.request(ask_request)
            ask_offers = ask_response.result.get("offers", [])
            best_ask = 1 / float(ask_offers[0]["quality"]) if ask_offers else 0.0

            if best_bid and best_ask:
                conn = sqlite3.connect(self.DB_NAME)
                c = conn.cursor()
                c.execute("INSERT INTO price_history (currency, issuer, timestamp, bid_price, ask_price) VALUES (?, ?, ?, ?, ?)",
                          (currency, issuer, int(time.time()), best_bid, best_ask))
                conn.commit()
                conn.close()
            return {"bid": best_bid, "ask": best_ask}

    async def fetch_all_user_tokens(self, context: ContextTypes.DEFAULT_TYPE):
        """Fetch price data for all user tokens periodically."""
        conn = sqlite3.connect(self.DB_NAME)
        c = conn.cursor()
        c.execute("SELECT DISTINCT currency, issuer FROM user_tokens")
        tokens = c.fetchall()
        conn.close()
        for currency, issuer in tokens:
            await self.fetch_price_data(currency, issuer)

    ### Technical Analysis
    def calculate_sma(self, prices: list[float], period: int) -> list[float]:
        """Calculate Simple Moving Average."""
        return [mean(prices[max(0, i-period+1):i+1]) for i in range(len(prices))]

    def calculate_ema(self, prices: list[float], period: int) -> list[float]:
        """Calculate Exponential Moving Average."""
        if not prices:
            return []
        ema = [prices[0]]
        multiplier = 2 / (period + 1)
        for price in prices[1:]:
            ema.append(price * multiplier + ema[-1] * (1 - multiplier))
        return ema

    def calculate_bollinger_bands(self, prices: list[float], period: int) -> tuple[list[float], list[float], list[float]]:
        """Calculate Bollinger Bands."""
        sma = self.calculate_sma(prices, period)
        upper, lower = [], []
        for i in range(len(prices)):
            window = prices[max(0, i-period+1):i+1]
            std = stdev(window) if len(window) > 1 else 0
            upper.append(sma[i] + 2 * std)
            lower.append(sma[i] - 2 * std)
        return upper, sma, lower

    def calculate_fibonacci_levels(self, prices: list[float]) -> Dict[str, float]:
        """Calculate Fibonacci retracement levels."""
        high, low = max(prices), min(prices)
        diff = high - low
        return {
            "0%": low,
            "23.6%": low + diff * 0.236,
            "38.2%": low + diff * 0.382,
            "50%": low + diff * 0.5,
            "61.8%": low + diff * 0.618,
            "100%": high
        }

    async def generate_chart(self, currency: str, issuer: str) -> tuple[io.BytesIO, float]:
        """Generate a technical analysis chart with current price highlighted."""
        conn = sqlite3.connect(self.DB_NAME)
        c = conn.cursor()
        c.execute("SELECT timestamp, (bid_price + ask_price) / 2 FROM price_history WHERE currency = ? AND issuer = ? ORDER BY timestamp LIMIT 50",
                  (currency, issuer))
        data = c.fetchall()
        conn.close()

        if len(data) < 10:
            raise ValueError("Insufficient data for chart")

        times, prices = zip(*data)
        prices = list(prices)
        current_price = prices[-1]
        sma = self.calculate_sma(prices, 10)
        upper_bb, _, lower_bb = self.calculate_bollinger_bands(prices, 20)
        fib_levels = self.calculate_fibonacci_levels(prices)

        plt.figure(figsize=(10, 6))
        plt.plot(range(len(prices)), prices, label="Price", color="blue")
        plt.plot(range(len(sma)), sma, label="SMA (10)", color="orange")
        plt.plot(range(len(upper_bb)), upper_bb, label="BB Upper", color="green", linestyle="--")
        plt.plot(range(len(lower_bb)), lower_bb, label="BB Lower", color="red", linestyle="--")
        plt.axhline(y=current_price, color='purple', linestyle='-', label='Current Price')
        for level, value in fib_levels.items():
            plt.axhline(y=value, linestyle=":", label=f"Fib {level}", color="gray")
        plt.legend()
        plt.title(f"{currency} Price Analysis")
        plt.xlabel("Time")
        plt.ylabel("Price (XRP)")

        buf = io.BytesIO()
        plt.savefig(buf, format="png")
        buf.seek(0)
        plt.close()
        return buf, current_price

    ### Telegram UI - Menu System with Emojis
    def get_main_menu(self):
        """Main menu keyboard with emojis."""
        keyboard = [
            [InlineKeyboardButton("👛 Wallet", callback_data="wallet_menu"),
             InlineKeyboardButton("💸 Trade", callback_data="trade_menu")],
            [InlineKeyboardButton("💰 Pay", callback_data="pay_menu"),
             InlineKeyboardButton("🏦 AMM", callback_data="amm_menu")],
            [InlineKeyboardButton("🔒 Escrow", callback_data="escrow_menu"),
             InlineKeyboardButton("💳 Payment Channels", callback_data="payment_channel_menu")],
            [InlineKeyboardButton("📊 Analysis", callback_data="analysis_menu"),
             InlineKeyboardButton("⚙️ Settings", callback_data="settings_menu")]
        ]
        return InlineKeyboardMarkup(keyboard)

    def get_wallet_menu(self):
        """Wallet menu keyboard with emojis."""
        keyboard = [
            [InlineKeyboardButton("✨ Create Wallet", callback_data="create_wallet"),
             InlineKeyboardButton("🔄 Switch Wallet", callback_data="switch_wallet")],
            [InlineKeyboardButton("📥 Import Wallet", callback_data="import_wallet"),
             InlineKeyboardButton("💰 Check Balance", callback_data="check_balance")],
            [InlineKeyboardButton("⬅️ Back", callback_data="main_menu")]
        ]
        return InlineKeyboardMarkup(keyboard)

    def get_trade_menu(self):
        """Trade menu keyboard with emojis."""
        keyboard = [
            [InlineKeyboardButton("📈 Buy", callback_data="buy"),
             InlineKeyboardButton("📉 Sell", callback_data="sell")],
            [InlineKeyboardButton("⬅️ Back", callback_data="main_menu")]
        ]
        return InlineKeyboardMarkup(keyboard)

    def get_amm_menu(self):
        """AMM menu keyboard with emojis."""
        keyboard = [
            [InlineKeyboardButton("🏦 Create Pool", callback_data="amm_create"),
             InlineKeyboardButton("📥 Deposit", callback_data="amm_deposit")],
            [InlineKeyboardButton("📤 Withdraw", callback_data="amm_withdraw"),
             InlineKeyboardButton("💵 Bid", callback_data="amm_bid")],
            [InlineKeyboardButton("🗳️ Vote", callback_data="amm_vote"),
             InlineKeyboardButton("⬅️ Back", callback_data="main_menu")]
        ]
        return InlineKeyboardMarkup(keyboard)

    def get_escrow_menu(self):
        """Escrow menu keyboard with emojis."""
        keyboard = [
            [InlineKeyboardButton("🔒 Create Escrow", callback_data="escrow_create"),
             InlineKeyboardButton("✅ Finish Escrow", callback_data="escrow_finish")],
            [InlineKeyboardButton("❌ Cancel Escrow", callback_data="escrow_cancel"),
             InlineKeyboardButton("⬅️ Back", callback_data="main_menu")]
        ]
        return InlineKeyboardMarkup(keyboard)

    def get_payment_channel_menu(self):
        """Payment Channel menu keyboard with emojis."""
        keyboard = [
            [InlineKeyboardButton("💳 Create Payment Channel", callback_data="payment_channel_create"),
             InlineKeyboardButton("💸 Claim from Payment Channel", callback_data="payment_channel_claim")],
            [InlineKeyboardButton("⬅️ Back", callback_data="main_menu")]
        ]
        return InlineKeyboardMarkup(keyboard)

    def get_analysis_menu(self):
        """Analysis menu keyboard with emojis."""
        keyboard = [
            [InlineKeyboardButton("📊 Analyze Token", callback_data="analyze_token"),
             InlineKeyboardButton("🔍 Add Token Pairing", callback_data="add_token_pairing")],
            [InlineKeyboardButton("⬅️ Back", callback_data="main_menu")]
        ]
        return InlineKeyboardMarkup(keyboard)

    def get_settings_menu(self):
        """Settings menu keyboard with emojis."""
        keyboard = [
            [InlineKeyboardButton("🔗 Set Default Token Pairing", callback_data="set_default_pairing"),
             InlineKeyboardButton("📉 Set Slippage Tolerance", callback_data="set_slippage")],
            [InlineKeyboardButton("⬅️ Back", callback_data="main_menu")]
        ]
        return InlineKeyboardMarkup(keyboard)

    ### Handlers
    def _setup_handlers(self):
        """Set up Telegram handlers."""
        self.application.add_handler(CommandHandler("start", self.start))
        self.application.add_handler(CallbackQueryHandler(self.button))
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text))

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command."""
        user_id = update.message.from_user.id
        logger.info(f"User {user_id} started the bot")
        await update.message.reply_text("Welcome to XRPL Bot! 🌟", reply_markup=self.get_main_menu())

    async def button(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle button clicks for all menu options."""
        query = update.callback_query
        await query.answer()
        user_id = query.from_user.id
        data = query.data.split("|")
        action = data[0]

        # Main Menu Navigation
        if action == "main_menu":
            await query.edit_message_text("🌟 Main Menu", reply_markup=self.get_main_menu())
        elif action == "wallet_menu":
            await query.edit_message_text("👛 Wallet Options", reply_markup=self.get_wallet_menu())
        elif action == "trade_menu":
            await query.edit_message_text("💸 Trade Options", reply_markup=self.get_trade_menu())
        elif action == "pay_menu":
            self.pending_inputs[user_id] = ("pay_address", [])
            await query.edit_message_text("💰 Enter destination address:")
        elif action == "amm_menu":
            await query.edit_message_text("🏦 AMM Options", reply_markup=self.get_amm_menu())
        elif action == "escrow_menu":
            await query.edit_message_text("🔒 Escrow Options", reply_markup=self.get_escrow_menu())
        elif action == "payment_channel_menu":
            await query.edit_message_text("💳 Payment Channel Options", reply_markup=self.get_payment_channel_menu())
        elif action == "analysis_menu":
            await query.edit_message_text("📊 Analysis Options", reply_markup=self.get_analysis_menu())
        elif action == "settings_menu":
            await query.edit_message_text("⚙️ Settings", reply_markup=self.get_settings_menu())

        # Wallet Functions
        elif action == "create_wallet":
            wallet = Wallet.create()
            wallet_name = f"wallet_{int(time.time())}"
            conn = sqlite3.connect(self.DB_NAME)
            c = conn.cursor()
            c.execute("INSERT INTO users (user_id, wallet_name, seed, is_current) VALUES (?, ?, ?, ?)",
                      (user_id, wallet_name, wallet.seed, 1))
            conn.commit()
            conn.close()
            await query.edit_message_text(f"✨ Wallet created: {wallet.classic_address}", reply_markup=self.get_wallet_menu())

        elif action == "switch_wallet":
            conn = sqlite3.connect(self.DB_NAME)
            c = conn.cursor()
            c.execute("SELECT wallet_name FROM users WHERE user_id = ?", (user_id,))
            wallets = c.fetchall()
            conn.close()
            if not wallets:
                await query.edit_message_text("🔴 No wallets found.", reply_markup=self.get_wallet_menu())
            else:
                keyboard = [[InlineKeyboardButton(w[0], callback_data=f"switch_select|{w[0]}")] for w in wallets]
                keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="wallet_menu")])
                await query.edit_message_text("🔄 Select a wallet:", reply_markup=InlineKeyboardMarkup(keyboard))

        elif action == "switch_select":
            wallet_name = data[1]
            if self.set_current_wallet(user_id, wallet_name):
                await query.edit_message_text(f"🔄 Switched to {wallet_name}", reply_markup=self.get_wallet_menu())
            else:
                await query.edit_message_text("🔴 Switch failed.", reply_markup=self.get_wallet_menu())

        elif action == "check_balance":
            wallet = self.get_wallet(user_id)
            if wallet:
                balance = await self.get_balance(wallet)
                await query.edit_message_text(f"💰 Balance: {balance} XRP", reply_markup=self.get_wallet_menu())
            else:
                await query.edit_message_text("🔴 No wallet selected.", reply_markup=self.get_wallet_menu())

        elif action == "import_wallet":
            self.pending_inputs[user_id] = ("import_wallet_seed", [])
            await query.edit_message_text("📥 Enter your wallet seed to import:")

        # Trade Functions
        elif action == "buy":
            self.pending_inputs[user_id] = ("buy_currency", ["buy"])
            await query.edit_message_text("📈 Enter token currency code to buy (e.g., USD):")
        elif action == "sell":
            self.pending_inputs[user_id] = ("sell_currency", ["sell"])
            await query.edit_message_text("📉 Enter token currency code to sell (e.g., USD):")

        # AMM Functions
        elif action == "amm_create":
            self.pending_inputs[user_id] = ("amm_create_currency", [])
            await query.edit_message_text("🏦 Enter token currency code for AMM pool:")
        elif action == "amm_deposit":
            self.pending_inputs[user_id] = ("amm_deposit_currency", [])
            await query.edit_message_text("📥 Enter token currency code for AMM deposit:")
        elif action == "amm_withdraw":
            self.pending_inputs[user_id] = ("amm_withdraw_currency", [])
            await query.edit_message_text("📤 Enter token currency code for AMM withdrawal:")
        elif action == "amm_bid":
            self.pending_inputs[user_id] = ("amm_bid_currency", [])
            await query.edit_message_text("💵 Enter token currency code for AMM bid:")
        elif action == "amm_vote":
            self.pending_inputs[user_id] = ("amm_vote_currency", [])
            await query.edit_message_text("🗳️ Enter token currency code for AMM vote:")

        # Escrow Functions
        elif action == "escrow_create":
            self.pending_inputs[user_id] = ("escrow_amount", [])
            await query.edit_message_text("🔒 Enter amount for escrow:")
        elif action == "escrow_finish":
            self.pending_inputs[user_id] = ("escrow_finish_sequence", [])
            await query.edit_message_text("✅ Enter escrow sequence to finish:")
        elif action == "escrow_cancel":
            self.pending_inputs[user_id] = ("escrow_cancel_sequence", [])
            await query.edit_message_text("❌ Enter escrow sequence to cancel:")

        # Payment Channel Functions
        elif action == "payment_channel_create":
            self.pending_inputs[user_id] = ("payment_channel_amount", [])
            await query.edit_message_text("💳 Enter amount for payment channel:")
        elif action == "payment_channel_claim":
            self.pending_inputs[user_id] = ("payment_channel_claim_channel", [])
            await query.edit_message_text("💸 Enter channel ID to claim:")

        # Analysis Functions
        elif action == "analyze_token":
            self.pending_inputs[user_id] = ("analyze_token_currency", [])
            await query.edit_message_text("📊 Enter token currency code for analysis (e.g., USD):")
        elif action == "add_token_pairing":
            self.pending_inputs[user_id] = ("add_token_pairing_currency", [])
            await query.edit_message_text("🔍 Enter token currency code to add (e.g., USD):")

        # Settings Functions
        elif action == "set_default_pairing":
            self.pending_inputs[user_id] = ("set_default_currency", [])
            await query.edit_message_text("🔗 Enter default token currency code (e.g., USD):")
        elif action == "set_slippage":
            self.pending_inputs[user_id] = ("set_slippage_value", [])
            await query.edit_message_text("📉 Enter slippage tolerance percentage (e.g., 5):")

    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle text input for all operations."""
        user_id = update.message.from_user.id
        text = update.message.text
        if user_id not in self.pending_inputs:
            return

        action, params = self.pending_inputs[user_id]
        del self.pending_inputs[user_id]

        # Wallet Importing
        if action == "import_wallet_seed":
            seed = text
            try:
                wallet = Wallet.from_seed(seed)
                wallet_name = f"imported_{int(time.time())}"
                conn = sqlite3.connect(self.DB_NAME)
                c = conn.cursor()
                c.execute("INSERT INTO users (user_id, wallet_name, seed, is_current) VALUES (?, ?, ?, ?)",
                          (user_id, wallet_name, seed, 0))
                conn.commit()
                conn.close()
                await update.message.reply_text(f"📥 Wallet imported: {wallet.classic_address}", reply_markup=self.get_wallet_menu())
            except Exception as e:
                await update.message.reply_text(f"🔴 Invalid seed: {e}", reply_markup=self.get_wallet_menu())

        # Trading Handlers
        elif action == "buy_currency" or action == "sell_currency":
            trade_type = params[0]
            self.pending_inputs[user_id] = (f"{trade_type}_issuer", [trade_type, text.upper()])
            await update.message.reply_text(f"Enter issuer address for {text.upper()}:")

        elif action == "buy_issuer" or action == "sell_issuer":
            trade_type, currency = params[0], params[1]
            issuer = text
            if not is_valid_classic_address(issuer):
                await update.message.reply_text("🔴 Invalid issuer address.", reply_markup=self.get_trade_menu())
                return
            self.pending_inputs[user_id] = (f"{trade_type}_amount", [trade_type, currency, issuer])
            await update.message.reply_text(f"Enter amount to {trade_type}:")

        elif action == "buy_amount" or action == "sell_amount":
            trade_type, currency, issuer = params[0], params[1], params[2]
            amount = float(text)
            wallet = self.get_wallet(user_id)
            if not wallet:
                await update.message.reply_text("🔴 No wallet selected.", reply_markup=self.get_trade_menu())
                return
            prices = await self.fetch_price_data(currency, issuer)
            if not prices["bid"] or not prices["ask"]:
                await update.message.reply_text("🔴 No market data available.", reply_markup=self.get_trade_menu())
                return
            if trade_type == "buy":
                xrp_amount = amount * prices["ask"]
                tx = OfferCreate(account=wallet.classic_address, taker_gets={"currency": currency, "issuer": issuer, "value": str(amount)},
                                 taker_pays=xrp_to_drops(xrp_amount))
            else:
                xrp_amount = amount * prices["bid"]
                tx = OfferCreate(account=wallet.classic_address, taker_gets=xrp_to_drops(xrp_amount),
                                 taker_pays={"currency": currency, "issuer": issuer, "value": str(amount)})
            async with self.client:
                response = await submit_and_wait(tx, self.client, wallet)
                if response.is_successful():
                    await update.message.reply_text(f"✅ {trade_type.capitalize()} successful: {amount} {currency}", reply_markup=self.get_trade_menu())
                else:
                    await update.message.reply_text(f"🔴 {trade_type.capitalize()} failed.", reply_markup=self.get_trade_menu())

        # Payment Handler
        elif action == "pay_address":
            destination = text
            if not is_valid_classic_address(destination):
                await update.message.reply_text("🔴 Invalid address.", reply_markup=self.get_main_menu())
                return
            self.pending_inputs[user_id] = ("pay_amount", [destination])
            await update.message.reply_text("💰 Enter amount in XRP:")

        elif action == "pay_amount":
            destination = params[0]
            amount = float(text)
            wallet = self.get_wallet(user_id)
            if not wallet:
                await update.message.reply_text("🔴 No wallet selected.", reply_markup=self.get_main_menu())
                return
            tx = Payment(account=wallet.classic_address, destination=destination, amount=xrp_to_drops(amount))
            async with self.client:
                response = await submit_and_wait(tx, self.client, wallet)
                if response.is_successful():
                    await update.message.reply_text(f"✅ Payment of {amount} XRP sent to {destination}", reply_markup=self.get_main_menu())
                else:
                    await update.message.reply_text("🔴 Payment failed.", reply_markup=self.get_main_menu())

        # AMM Handlers
        elif action == "amm_create_currency":
            currency = text.upper()
            self.pending_inputs[user_id] = ("amm_create_issuer", [currency])
            await update.message.reply_text(f"🏦 Enter issuer address for {currency}:")

        elif action == "amm_create_issuer":
            currency, issuer = params[0], text
            if not is_valid_classic_address(issuer):
                await update.message.reply_text("🔴 Invalid issuer address.", reply_markup=self.get_amm_menu())
                return
            self.pending_inputs[user_id] = ("amm_create_amounts", [currency, issuer])
            await update.message.reply_text("🏦 Enter XRP amount and token amount (e.g., '10 100'):")

        elif action == "amm_create_amounts":
            currency, issuer = params[0], params[1]
            xrp_amount, token_amount = map(float, text.split())
            wallet = self.get_wallet(user_id)
            if not wallet:
                await update.message.reply_text("🔴 No wallet selected.", reply_markup=self.get_amm_menu())
                return
            tx = AMMCreate(account=wallet.classic_address, amount=xrp_to_drops(xrp_amount),
                           amount2={"currency": currency, "issuer": issuer, "value": str(token_amount)},
                           trading_fee=500)  # 0.5% fee
            async with self.client:
                response = await submit_and_wait(tx, self.client, wallet)
                if response.is_successful():
                    await update.message.reply_text(f"✅ AMM pool created: {xrp_amount} XRP, {token_amount} {currency}", reply_markup=self.get_amm_menu())
                else:
                    await update.message.reply_text("🔴 AMM creation failed.", reply_markup=self.get_amm_menu())

        elif action == "amm_deposit_currency":
            currency = text.upper()
            self.pending_inputs[user_id] = ("amm_deposit_issuer", [currency])
            await update.message.reply_text(f"📥 Enter issuer address for {currency}:")

        elif action == "amm_deposit_issuer":
            currency, issuer = params[0], text
            if not is_valid_classic_address(issuer):
                await update.message.reply_text("🔴 Invalid issuer address.", reply_markup=self.get_amm_menu())
                return
            self.pending_inputs[user_id] = ("amm_deposit_amounts", [currency, issuer])
            await update.message.reply_text("📥 Enter XRP amount and token amount to deposit (e.g., '5 50'):")

        elif action == "amm_deposit_amounts":
            currency, issuer = params[0], params[1]
            xrp_amount, token_amount = map(float, text.split())
            wallet = self.get_wallet(user_id)
            if not wallet:
                await update.message.reply_text("🔴 No wallet selected.", reply_markup=self.get_amm_menu())
                return
            tx = AMMDeposit(account=wallet.classic_address, amount=xrp_to_drops(xrp_amount),
                            amount2={"currency": currency, "issuer": issuer, "value": str(token_amount)})
            async with self.client:
                response = await submit_and_wait(tx, self.client, wallet)
                if response.is_successful():
                    await update.message.reply_text(f"✅ Deposited {xrp_amount} XRP and {token_amount} {currency} to AMM", reply_markup=self.get_amm_menu())
                else:
                    await update.message.reply_text("🔴 AMM deposit failed.", reply_markup=self.get_amm_menu())

        elif action == "amm_withdraw_currency":
            currency = text.upper()
            self.pending_inputs[user_id] = ("amm_withdraw_issuer", [currency])
            await update.message.reply_text(f"📤 Enter issuer address for {currency}:")

        elif action == "amm_withdraw_issuer":
            currency, issuer = params[0], text
            if not is_valid_classic_address(issuer):
                await update.message.reply_text("🔴 Invalid issuer address.", reply_markup=self.get_amm_menu())
                return
            self.pending_inputs[user_id] = ("amm_withdraw_amount", [currency, issuer])
            await update.message.reply_text("📤 Enter LP token amount to withdraw:")

        elif action == "amm_withdraw_amount":
            currency, issuer = params[0], params[1]
            lp_amount = float(text)
            wallet = self.get_wallet(user_id)
            if not wallet:
                await update.message.reply_text("🔴 No wallet selected.", reply_markup=self.get_amm_menu())
                return
            tx = AMMWithdraw(account=wallet.classic_address, amount={"currency": "XRP"}, amount2={"currency": currency, "issuer": issuer},
                             lp_token_in={"currency": "XXX", "issuer": wallet.classic_address, "value": str(lp_amount)})
            async with self.client:
                response = await submit_and_wait(tx, self.client, wallet)
                if response.is_successful():
                    await update.message.reply_text(f"✅ Withdrew {lp_amount} LP tokens from AMM", reply_markup=self.get_amm_menu())
                else:
                    await update.message.reply_text("🔴 AMM withdrawal failed.", reply_markup=self.get_amm_menu())

        elif action == "amm_bid_currency":
            currency = text.upper()
            self.pending_inputs[user_id] = ("amm_bid_issuer", [currency])
            await update.message.reply_text(f"💵 Enter issuer address for {currency}:")

        elif action == "amm_bid_issuer":
            currency, issuer = params[0], text
            if not is_valid_classic_address(issuer):
                await update.message.reply_text("🔴 Invalid issuer address.", reply_markup=self.get_amm_menu())
                return
            self.pending_inputs[user_id] = ("amm_bid_amount", [currency, issuer])
            await update.message.reply_text("💵 Enter bid amount in XRP:")

        elif action == "amm_bid_amount":
            currency, issuer = params[0], params[1]
            amount = float(text)
            wallet = self.get_wallet(user_id)
            if not wallet:
                await update.message.reply_text("🔴 No wallet selected.", reply_markup=self.get_amm_menu())
                return
            tx = AMMBid(account=wallet.classic_address, amount=xrp_to_drops(amount),
                        asset={"currency": "XRP"}, asset2={"currency": currency, "issuer": issuer})
            async with self.client:
                response = await submit_and_wait(tx, self.client, wallet)
                if response.is_successful():
                    await update.message.reply_text(f"✅ Bid {amount} XRP on AMM", reply_markup=self.get_amm_menu())
                else:
                    await update.message.reply_text("🔴 AMM bid failed.", reply_markup=self.get_amm_menu())

        elif action == "amm_vote_currency":
            currency = text.upper()
            self.pending_inputs[user_id] = ("amm_vote_issuer", [currency])
            await update.message.reply_text(f"🗳️ Enter issuer address for {currency}:")

        elif action == "amm_vote_issuer":
            currency, issuer = params[0], text
            if not is_valid_classic_address(issuer):
                await update.message.reply_text("🔴 Invalid issuer address.", reply_markup=self.get_amm_menu())
                return
            self.pending_inputs[user_id] = ("amm_vote_fee", [currency, issuer])
            await update.message.reply_text("🗳️ Enter trading fee to vote for (in basis points, e.g., 500 for 0.5%):")

        elif action == "amm_vote_fee":
            currency, issuer = params[0], params[1]
            fee = int(text)
            wallet = self.get_wallet(user_id)
            if not wallet:
                await update.message.reply_text("🔴 No wallet selected.", reply_markup=self.get_amm_menu())
                return
            tx = AMMVote(account=wallet.classic_address, asset={"currency": "XRP"},
                         asset2={"currency": currency, "issuer": issuer}, trading_fee=fee)
            async with self.client:
                response = await submit_and_wait(tx, self.client, wallet)
                if response.is_successful():
                    await update.message.reply_text(f"✅ Voted for {fee/10000}% fee on AMM", reply_markup=self.get_amm_menu())
                else:
                    await update.message.reply_text("🔴 AMM vote failed.", reply_markup=self.get_amm_menu())

        # Escrow Handlers
        elif action == "escrow_amount":
            amount = float(text)
            self.pending_inputs[user_id] = ("escrow_create_destination", [amount])
            await update.message.reply_text("🔒 Enter destination address for escrow:")

        elif action == "escrow_create_destination":
            amount, destination = params[0], text
            if not is_valid_classic_address(destination):
                await update.message.reply_text("🔴 Invalid destination address.", reply_markup=self.get_escrow_menu())
                return
            self.pending_inputs[user_id] = ("escrow_create_condition", [amount, destination])
            await update.message.reply_text("🔒 Enter condition for escrow (e.g., time in seconds):")

        elif action == "escrow_create_condition":
            amount, destination, condition = params[0], params[1], text
            wallet = self.get_wallet(user_id)
            if not wallet:
                await update.message.reply_text("🔴 No wallet selected.", reply_markup=self.get_escrow_menu())
                return
            tx = EscrowCreate(account=wallet.classic_address, amount=xrp_to_drops(amount),
                              destination=destination, condition=condition)
            async with self.client:
                response = await submit_and_wait(tx, self.client, wallet)
                if response.is_successful():
                    await update.message.reply_text(f"✅ Escrow created for {amount} XRP to {destination}", reply_markup=self.get_escrow_menu())
                else:
                    await update.message.reply_text("🔴 Escrow creation failed.", reply_markup=self.get_escrow_menu())

        elif action == "escrow_finish_sequence":
            sequence = int(text)
            wallet = self.get_wallet(user_id)
            if not wallet:
                await update.message.reply_text("🔴 No wallet selected.", reply_markup=self.get_escrow_menu())
                return
            tx = EscrowFinish(account=wallet.classic_address, offer_sequence=sequence)
            async with self.client:
                response = await submit_and_wait(tx, self.client, wallet)
                if response.is_successful():
                    await update.message.reply_text(f"✅ Escrow {sequence} finished", reply_markup=self.get_escrow_menu())
                else:
                    await update.message.reply_text("🔴 Failed to finish escrow.", reply_markup=self.get_escrow_menu())

        elif action == "escrow_cancel_sequence":
            sequence = int(text)
            wallet = self.get_wallet(user_id)
            if not wallet:
                await update.message.reply_text("🔴 No wallet selected.", reply_markup=self.get_escrow_menu())
                return
            tx = EscrowCancel(account=wallet.classic_address, offer_sequence=sequence)
            async with self.client:
                response = await submit_and_wait(tx, self.client, wallet)
                if response.is_successful():
                    await update.message.reply_text(f"✅ Escrow {sequence} canceled", reply_markup=self.get_escrow_menu())
                else:
                    await update.message.reply_text("🔴 Failed to cancel escrow.", reply_markup=self.get_escrow_menu())

        # Payment Channel Handlers
        elif action == "payment_channel_amount":
            amount = float(text)
            self.pending_inputs[user_id] = ("payment_channel_destination", [amount])
            await update.message.reply_text("💳 Enter destination address for payment channel:")

        elif action == "payment_channel_destination":
            amount, destination = params[0], text
            if not is_valid_classic_address(destination):
                await update.message.reply_text("🔴 Invalid destination address.", reply_markup=self.get_payment_channel_menu())
                return
            self.pending_inputs[user_id] = ("payment_channel_expiration", [amount, destination])
            await update.message.reply_text("💳 Enter expiration time for payment channel (in seconds):")

        elif action == "payment_channel_expiration":
            amount, destination, expiration = params[0], params[1], text
            wallet = self.get_wallet(user_id)
            if not wallet:
                await update.message.reply_text("🔴 No wallet selected.", reply_markup=self.get_payment_channel_menu())
                return
            tx = PaymentChannelCreate(account=wallet.classic_address, amount=xrp_to_drops(amount),
                                      destination=destination, settle_delay=int(expiration))
            async with self.client:
                response = await submit_and_wait(tx, self.client, wallet)
                if response.is_successful():
                    await update.message.reply_text(f"✅ Payment channel created for {amount} XRP to {destination}", reply_markup=self.get_payment_channel_menu())
                else:
                    await update.message.reply_text("🔴 Payment channel creation failed.", reply_markup=self.get_payment_channel_menu())

        elif action == "payment_channel_claim_channel":
            channel_id = text
            wallet = self.get_wallet(user_id)
            if not wallet:
                await update.message.reply_text("🔴 No wallet selected.", reply_markup=self.get_payment_channel_menu())
                return
            tx = PaymentChannelClaim(channel=channel_id, account=wallet.classic_address)
            async with self.client:
                response = await submit_and_wait(tx, self.client, wallet)
                if response.is_successful():
                    await update.message.reply_text(f"✅ Claimed from payment channel {channel_id}", reply_markup=self.get_payment_channel_menu())
                else:
                    await update.message.reply_text("🔴 Failed to claim from payment channel.", reply_markup=self.get_payment_channel_menu())

        # Analysis Handlers
        elif action == "analyze_token_currency":
            currency = text.upper()
            self.pending_inputs[user_id] = ("analyze_token_issuer", [currency])
            await update.message.reply_text(f"📊 Enter issuer address for {currency}:")

        elif action == "analyze_token_issuer":
            currency, issuer = params[0], text
            if not is_valid_classic_address(issuer):
                await update.message.reply_text("🔴 Invalid issuer address.", reply_markup=self.get_analysis_menu())
                return
            try:
                chart, current_price = await self.generate_chart(currency, issuer)
                caption = f"📊 {currency} Analysis\n💡 Current Price: {current_price:.4f} XRP"
                await update.message.reply_photo(chart, caption=caption, reply_markup=self.get_analysis_menu())
            except ValueError as e:
                await update.message.reply_text(f"🔴 {str(e)}", reply_markup=self.get_analysis_menu())

        elif action == "add_token_pairing_currency":
            currency = text.upper()
            self.pending_inputs[user_id] = ("add_token_pairing_issuer", [currency])
            await update.message.reply_text(f"🔍 Enter issuer address for {currency}:")

        elif action == "add_token_pairing_issuer":
            currency, issuer = params[0], text
            if not is_valid_classic_address(issuer):
                await update.message.reply_text("🔴 Invalid issuer address.", reply_markup=self.get_analysis_menu())
                return
            conn = sqlite3.connect(self.DB_NAME)
            c = conn.cursor()
            c.execute("INSERT OR IGNORE INTO user_tokens (user_id, currency, issuer) VALUES (?, ?, ?)", (user_id, currency, issuer))
            conn.commit()
            conn.close()
            await update.message.reply_text(f"🔍 Added {currency} from {issuer} for analysis.", reply_markup=self.get_analysis_menu())

        # Settings Handlers
        elif action == "set_default_currency":
            currency = text.upper()
            self.pending_inputs[user_id] = ("set_default_issuer", [currency])
            await update.message.reply_text(f"🔗 Enter default issuer address for {currency}:")

        elif action == "set_default_issuer":
            currency, issuer = params[0], text
            if not is_valid_classic_address(issuer):
                await update.message.reply_text("🔴 Invalid issuer address.", reply_markup=self.get_settings_menu())
                return
            conn = sqlite3.connect(self.DB_NAME)
            c = conn.cursor()
            c.execute("INSERT OR REPLACE INTO user_settings (user_id, default_currency, default_issuer) VALUES (?, ?, ?)",
                      (user_id, currency, issuer))
            conn.commit()
            conn.close()
            await update.message.reply_text(f"🔗 Default token pairing set to {currency} from {issuer}", reply_markup=self.get_settings_menu())

        elif action == "set_slippage_value":
            try:
                slippage = float(text)
                conn = sqlite3.connect(self.DB_NAME)
                c = conn.cursor()
                c.execute("UPDATE user_settings SET slippage_tolerance = ? WHERE user_id = ?", (slippage, user_id))
                if c.rowcount == 0:
                    c.execute("INSERT INTO user_settings (user_id, slippage_tolerance) VALUES (?, ?)", (user_id, slippage))
                conn.commit()
                conn.close()
                await update.message.reply_text(f"📉 Slippage tolerance set to {slippage}%", reply_markup=self.get_settings_menu())
            except ValueError:
                await update.message.reply_text("🔴 Invalid slippage value.", reply_markup=self.get_settings_menu())

    ### Run the Bot
    def run(self):
        """Start the bot."""
        logger.info("Starting XRPL Bot...")
        self.application.run_polling()

if __name__ == "__main__":
    bot = XRPLBot()
    bot.run()