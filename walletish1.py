import logging
import os
import ssl
import base64
import time
from typing import Dict, Any, Optional, List, Tuple
from statistics import mean, stdev
from datetime import datetime
import matplotlib.pyplot as plt
import io
from dotenv import load_dotenv  # Add this import

from argon2 import PasswordHasher, exceptions
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlcipher3 import dbapi2 as sqlcipher
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from xrpl.asyncio.clients import AsyncWebsocketClient
from xrpl.asyncio.transaction import submit_and_wait
from xrpl.asyncio.ledger import get_latest_validated_ledger_sequence
from xrpl.models.requests import AccountInfo, BookOffers, AMMInfo, Fee
from xrpl.models.transactions import (
    Payment, OfferCreate, AMMCreate, AMMDeposit, AMMWithdraw, AMMBid, AMMVote,
    EscrowCreate, EscrowFinish, EscrowCancel, PaymentChannelCreate, PaymentChannelClaim, SignerListSet
)
from xrpl.wallet import Wallet
from xrpl.utils import xrp_to_drops, drops_to_xrp
from xrpl.core.addresscodec import is_valid_classic_address
from aiohttp import ClientSession, TCPConnector

# Load environment variables from .env file
load_dotenv()

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class XRPLBot:
    def __init__(self):
        self.nodes = [
            "wss://s1.ripple.com",
            "wss://s2.ripple.com",
            "wss://xrplcluster.com"
        ]
        self.ssl_context = ssl.create_default_context()
        self.ssl_context.verify_mode = ssl.CERT_REQUIRED
        self.ssl_context.check_hostname = True
        self.client: Optional[AsyncWebsocketClient] = None  # Defer initialization
        self.application = Application.builder().token(os.getenv("raddwall")).build()
        if not os.getenv("raddwall"):
            raise ValueError("raddwall environment variable not set")
        self.pending_inputs: Dict[int, Tuple[str, List[Any]]] = {}
        self.DB_NAME = "xrpl_bot.db"
        self.DB_KEY = os.getenv("SQLCIPHER_KEY")
        if not self.DB_KEY:
            raise ValueError("SQLCIPHER_KEY environment variable not set")
        self.ph = PasswordHasher(memory_cost=65536, time_cost=3, parallelism=4)  # Argon2 parameters
        self.init_db()
        self._setup_handlers()
        self.application.job_queue.run_repeating(self.fetch_all_user_tokens, interval=60, first=10)


    ################## WebSocket Client
    def get_client(self) -> AsyncWebsocketClient:
        """Create a WebSocket client with TLS verification."""
        connector = TCPConnector(ssl=self.ssl_context)
        session = ClientSession(connector=connector)
        return AsyncWebsocketClient(self.nodes[0], session=session)

    async def get_available_client(self) -> AsyncWebsocketClient:
        """Get a working WebSocket client with failover, initializing if needed."""
        for node in self.nodes:
            try:
                client = AsyncWebsocketClient(node, ssl_context=self.ssl_context)
                await client.open()
                return client
            except Exception as e:
                logger.warning(f"Failed to connect to {node}: {e}")
        raise ValueError("No available XRPL nodes")

    # ########## Database Setup
    def get_db_connection(self) -> sqlcipher.Connection:
        """Create an encrypted SQLCipher connection."""
        conn = sqlcipher.connect(self.DB_NAME, check_same_thread=False)
        conn.execute(f"PRAGMA key='{self.DB_KEY}'")
        return conn

    def init_db(self) -> None:
        """Initialize SQLCipher database."""
        conn = self.get_db_connection()
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS users 
                     (user_id INTEGER, wallet_name TEXT, encrypted_seed TEXT, 
                      salt TEXT, is_current INTEGER DEFAULT 0, 
                      PRIMARY KEY (user_id, wallet_name))''')
        c.execute('''CREATE TABLE IF NOT EXISTS price_history 
                     (id INTEGER PRIMARY KEY AUTOINCREMENT, currency TEXT, issuer TEXT, 
                      timestamp INTEGER, bid_price REAL, ask_price REAL)''')
        c.execute('''CREATE TABLE IF NOT EXISTS user_tokens 
                     (user_id INTEGER, currency TEXT, issuer TEXT, 
                      PRIMARY KEY (user_id, currency, issuer))''')
        c.execute('''CREATE TABLE IF NOT EXISTS user_settings 
                     (user_id INTEGER PRIMARY KEY, default_currency TEXT, 
                      default_issuer TEXT, slippage_tolerance REAL)''')
        conn.commit()
        conn.close()

    ########## Seed Encryption with Argon2
    def encrypt_seed(self, seed: str, passphrase: str) -> Tuple[str, str]:
        """Encrypt a wallet seed using Argon2-derived key and AES-256-GCM."""
        salt = os.urandom(16)
        try:
            hash = self.ph.hash(passphrase.encode(), salt=salt)
            key = base64.b64encode(hash.encode())[:32]  # First 32 bytes for AES-256
        except exceptions.HashError as e:
            raise ValueError(f"Argon2 key derivation failed: {e}")

        aesgcm = AESGCM(key)
        nonce = os.urandom(12)
        encrypted_seed = aesgcm.encrypt(nonce, seed.encode(), None)
        encrypted_data = base64.b64encode(nonce + encrypted_seed).decode()
        return encrypted_data, base64.b64encode(salt).decode()

    def decrypt_seed(self, encrypted_seed: str, salt: str, passphrase: str) -> str:
        """Decrypt a wallet seed using Argon2-derived key and AES-256-GCM."""
        salt = base64.b64decode(salt)
        encrypted_data = base64.b64decode(encrypted_seed)
        nonce, ciphertext = encrypted_data[:12], encrypted_data[12:]

        try:
            hash = self.ph.hash(passphrase.encode(), salt=salt)
            key = base64.b64encode(hash.encode())[:32]
        except exceptions.HashError as e:
            raise ValueError(f"Argon2 key derivation failed: {e}")

        try:
            aesgcm = AESGCM(key)
            return aesgcm.decrypt(nonce, ciphertext, None).decode()
        except Exception as e:
            raise ValueError(f"Decryption failed: {e}")

    ############ Wallet Management
    def get_wallet(self, user_id: int, wallet_name: str | None = None, passphrase: str | None = None) -> Optional[Wallet]:
        """Retrieve a user's current or specified wallet."""
        conn = self.get_db_connection()
        c = conn.cursor()
        if wallet_name:
            c.execute("SELECT encrypted_seed, salt FROM users WHERE user_id = ? AND wallet_name = ?",
                      (user_id, wallet_name))
        else:
            c.execute("SELECT encrypted_seed, salt FROM users WHERE user_id = ? AND is_current = 1",
                      (user_id,))
        result = c.fetchone()
        conn.close()
        if result and passphrase:
            try:
                seed = self.decrypt_seed(result[0], result[1], passphrase)
                return Wallet.from_seed(seed)
            except ValueError:
                return None
        return None

    def set_current_wallet(self, user_id: int, wallet_name: str) -> bool:
        """Set a wallet as the current one for a user."""
        conn = self.get_db_connection()
        c = conn.cursor()
        c.execute("UPDATE users SET is_current = 0 WHERE user_id = ?", (user_id,))
        c.execute("UPDATE users SET is_current = 1 WHERE user_id = ? AND wallet_name = ?",
                  (user_id, wallet_name))
        conn.commit()
        conn.close()
        return c.rowcount > 0

    async def create_multisig_wallet(self, user_id: int, passphrase: str,
                                     signers: List[Tuple[str, int]], threshold: int) -> Optional[Wallet]:
        """Create a multisig wallet with specified signers and threshold."""
        wallet = Wallet.create()
        wallet_name = f"multisig_{int(time.time())}"
        encrypted_seed, salt = self.encrypt_seed(wallet.seed, passphrase)

        signer_entries = [{"Account": signer[0], "SignerWeight": signer[1]} for signer in signers]
        tx = SignerListSet(
            account=wallet.classic_address,
            signer_quorum=threshold,
            signer_entries=signer_entries
        )

        async with await self.get_available_client() as client:
            response = await submit_and_wait(tx, client, wallet)
            if not response.is_successful():
                logger.error(f"Failed to set signer list: {response.result}")
                return None

        conn = self.get_db_connection()
        c = conn.cursor()
        c.execute("INSERT INTO users (user_id, wallet_name, encrypted_seed, salt, is_current) VALUES (?, ?, ?, ?, ?)",
                  (user_id, wallet_name, encrypted_seed, salt, 1))
        conn.commit()
        conn.close()
        return wallet

    ###### XRPL Utilities
    async def get_fee(self) -> str:
        """Get the current network fee."""
        async with await self.get_available_client() as client:
            response = await client.request(Fee())
            return response.result["drops"]["base_fee"] if response.is_successful() else "10"

    async def get_ledger_index(self) -> int:
        """Get the latest validated ledger index."""
        async with await self.get_available_client() as client:
            return await get_latest_validated_ledger_sequence(client)

    async def get_balance(self, wallet: Wallet) -> float:
        """Get XRP balance for a wallet."""
        async with await self.get_available_client() as client:
            response = await client.request(AccountInfo(account=wallet.classic_address, ledger_index="validated"))
            return drops_to_xrp(response.result["account_data"]["Balance"]) if response.is_successful() else 0.0

    ############# Trading and Price Fetching
    async def fetch_price_data(self, currency: str, issuer: str) -> Dict[str, float]:
        """Fetch real-time bid and ask prices from the DEX order book."""
        async with await self.get_available_client() as client:
            bid_request = BookOffers(taker_gets={"currency": "XRP"},
                                     taker_pays={"currency": currency, "issuer": issuer}, limit=1)
            bid_response = await client.request(bid_request)
            bid_offers = bid_response.result.get("offers", [])
            best_bid = float(bid_offers[0]["quality"]) if bid_offers else 0.0

            ask_request = BookOffers(taker_gets={"currency": currency, "issuer": issuer},
                                     taker_pays={"currency": "XRP"}, limit=1)
            ask_response = await client.request(ask_request)
            ask_offers = ask_response.result.get("offers", [])
            best_ask = 1 / float(ask_offers[0]["quality"]) if ask_offers else 0.0

            if best_bid and best_ask:
                conn = self.get_db_connection()
                c = conn.cursor()
                c.execute("INSERT INTO price_history (currency, issuer, timestamp, bid_price, ask_price) VALUES (?, ?, ?, ?, ?)",
                          (currency, issuer, int(time.time()), best_bid, best_ask))
                conn.commit()
                conn.close()
            return {"bid": best_bid, "ask": best_ask}

    async def fetch_all_user_tokens(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Fetch price data for all user tokens periodically."""
        conn = self.get_db_connection()
        c = conn.cursor()
        c.execute("SELECT DISTINCT currency, issuer FROM user_tokens")
        tokens = c.fetchall()
        conn.close()
        for currency, issuer in tokens:
            await self.fetch_price_data(currency, issuer)

    ##################################### Technical Analysis
    def calculate_sma(self, prices: List[float], period: int) -> List[float]:
        """Calculate Simple Moving Average."""
        return [mean(prices[max(0, i-period+1):i+1]) for i in range(len(prices))]

    def calculate_ema(self, prices: List[float], period: int) -> List[float]:
        """Calculate Exponential Moving Average."""
        if not prices:
            return []
        ema = [prices[0]]
        multiplier = 2 / (period + 1)
        for price in prices[1:]:
            ema.append(price * multiplier + ema[-1] * (1 - multiplier))
        return ema

    def calculate_bollinger_bands(self, prices: List[float], period: int) -> Tuple[List[float], List[float], List[float]]:
        """Calculate Bollinger Bands."""
        sma = self.calculate_sma(prices, period)
        upper, lower = [], []
        for i in range(len(prices)):
            window = prices[max(0, i-period+1):i+1]
            std = stdev(window) if len(window) > 1 else 0
            upper.append(sma[i] + 2 * std)
            lower.append(sma[i] - 2 * std)
        return upper, sma, lower

    def calculate_fibonacci_levels(self, prices: List[float]) -> Dict[str, float]:
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

    async def generate_chart(self, currency: str, issuer: str) -> Tuple[io.BytesIO, float]:
        """Generate a technical analysis chart with current price highlighted."""
        conn = self.get_db_connection()
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

    ### Input Validation
    def validate_amount(self, text: str) -> float:
        """Validate a numeric amount."""
        try:
            amount = float(text)
            if amount <= 0:
                raise ValueError("Amount must be positive")
            return amount
        except ValueError:
            raise ValueError("Invalid amount format")

    ############################################## Telegram UI - Menu System with Emojis
    def get_main_menu(self) -> InlineKeyboardMarkup:
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

    def get_wallet_menu(self) -> InlineKeyboardMarkup:
        """Wallet menu keyboard with emojis."""
        keyboard = [
            [InlineKeyboardButton("✨ Create Wallet", callback_data="create_wallet"),
             InlineKeyboardButton("🔐 Create Multisig Wallet", callback_data="create_multisig")],
            [InlineKeyboardButton("🔄 Switch Wallet", callback_data="switch_wallet"),
             InlineKeyboardButton("📥 Import Wallet", callback_data="import_wallet")],
            [InlineKeyboardButton("💰 Check Balance", callback_data="check_balance"),
             InlineKeyboardButton("⬅️ Back", callback_data="main_menu")]
        ]
        return InlineKeyboardMarkup(keyboard)

    def get_trade_menu(self) -> InlineKeyboardMarkup:
        """Trade menu keyboard with emojis."""
        keyboard = [
            [InlineKeyboardButton("📈 Buy", callback_data="buy"),
             InlineKeyboardButton("📉 Sell", callback_data="sell")],
            [InlineKeyboardButton("⬅️ Back", callback_data="main_menu")]
        ]
        return InlineKeyboardMarkup(keyboard)

    def get_amm_menu(self) -> InlineKeyboardMarkup:
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

    def get_escrow_menu(self) -> InlineKeyboardMarkup:
        """Escrow menu keyboard with emojis."""
        keyboard = [
            [InlineKeyboardButton("🔒 Create Escrow", callback_data="escrow_create"),
             InlineKeyboardButton("✅ Finish Escrow", callback_data="escrow_finish")],
            [InlineKeyboardButton("❌ Cancel Escrow", callback_data="escrow_cancel"),
             InlineKeyboardButton("⬅️ Back", callback_data="main_menu")]
        ]
        return InlineKeyboardMarkup(keyboard)

    def get_payment_channel_menu(self) -> InlineKeyboardMarkup:
        """Payment Channel menu keyboard with emojis."""
        keyboard = [
            [InlineKeyboardButton("💳 Create Payment Channel", callback_data="payment_channel_create"),
             InlineKeyboardButton("💸 Claim from Payment Channel", callback_data="payment_channel_claim")],
            [InlineKeyboardButton("⬅️ Back", callback_data="main_menu")]
        ]
        return InlineKeyboardMarkup(keyboard)

    def get_analysis_menu(self) -> InlineKeyboardMarkup:
        """Analysis menu keyboard with emojis."""
        keyboard = [
            [InlineKeyboardButton("📊 Analyze Token", callback_data="analyze_token"),
             InlineKeyboardButton("🔍 Add Token Pairing", callback_data="add_token_pairing")],
            [InlineKeyboardButton("⬅️ Back", callback_data="main_menu")]
        ]
        return InlineKeyboardMarkup(keyboard)

    def get_settings_menu(self) -> InlineKeyboardMarkup:
        """Settings menu keyboard with emojis."""
        keyboard = [
            [InlineKeyboardButton("🔗 Set Default Token Pairing", callback_data="set_default_pairing"),
             InlineKeyboardButton("📉 Set Slippage Tolerance", callback_data="set_slippage")],
            [InlineKeyboardButton("⬅️ Back", callback_data="main_menu")]
        ]
        return InlineKeyboardMarkup(keyboard)

    ### Handlers
    def _setup_handlers(self) -> None:
        """Set up Telegram handlers."""
        self.application.add_handler(CommandHandler("start", self.start))
        self.application.add_handler(CallbackQueryHandler(self.button))
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text))

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /start command."""
        user_id = update.message.from_user.id
        logger.info(f"User {user_id} started the bot")
        await update.message.reply_text("Welcome to XRPL Bot! 🌟", reply_markup=self.get_main_menu())

    async def button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
            self.pending_inputs[user_id] = ("create_wallet_passphrase", [])
            await query.edit_message_text("✨ Enter a passphrase to encrypt your new wallet:")

        elif action == "create_multisig":
            self.pending_inputs[user_id] = ("multisig_passphrase", [])
            await query.edit_message_text("🔐 Enter a passphrase for the multisig wallet:")

        elif action == "switch_wallet":
            conn = self.get_db_connection()
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
            self.pending_inputs[user_id] = ("check_balance_passphrase", [])
            await query.edit_message_text("🔐 Enter your wallet passphrase:")

        elif action == "import_wallet":
            self.pending_inputs[user_id] = ("import_wallet_passphrase", [])
            await query.edit_message_text("📥 Enter a passphrase for the imported wallet:")

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

    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle text input for all operations."""
        user_id = update.message.from_user.id
        text = update.message.text
        if user_id not in self.pending_inputs:
            return

        action, params = self.pending_inputs[user_id]
        del self.pending_inputs[user_id]

        # Wallet Creation and Import
        if action == "create_wallet_passphrase":
            passphrase = text
            wallet = Wallet.create()
            wallet_name = f"wallet_{int(time.time())}"
            try:
                encrypted_seed, salt = self.encrypt_seed(wallet.seed, passphrase)
                conn = self.get_db_connection()
                c = conn.cursor()
                c.execute("INSERT INTO users (user_id, wallet_name, encrypted_seed, salt, is_current) VALUES (?, ?, ?, ?, ?)",
                          (user_id, wallet_name, encrypted_seed, salt, 1))
                conn.commit()
                conn.close()
                await update.message.reply_text(f"✨ Wallet created: {wallet.classic_address}",
                                                reply_markup=self.get_wallet_menu())
            except ValueError as e:
                await update.message.reply_text(f"🔴 Failed to create wallet: {e}",
                                                reply_markup=self.get_wallet_menu())

        elif action == "import_wallet_passphrase":
            self.pending_inputs[user_id] = ("import_wallet_seed", [text])
            await update.message.reply_text("📥 Enter your wallet seed to import:")

        elif action == "import_wallet_seed":
            passphrase, seed = params[0], text
            try:
                wallet = Wallet.from_seed(seed)  # Validate seed
                wallet_name = f"imported_{int(time.time())}"
                encrypted_seed, salt = self.encrypt_seed(seed, passphrase)
                conn = self.get_db_connection()
                c = conn.cursor()
                c.execute("INSERT INTO users (user_id, wallet_name, encrypted_seed, salt, is_current) VALUES (?, ?, ?, ?, ?)",
                          (user_id, wallet_name, encrypted_seed, salt, 0))
                conn.commit()
                conn.close()
                await update.message.reply_text(f"📥 Wallet imported: {wallet.classic_address}",
                                                reply_markup=self.get_wallet_menu())
            except Exception as e:
                await update.message.reply_text(f"🔴 Invalid seed or encryption failed: {e}",
                                                reply_markup=self.get_wallet_menu())

        # Multisig Wallet Creation
        elif action == "multisig_passphrase":
            self.pending_inputs[user_id] = ("multisig_signers", [text])
            await update.message.reply_text("🔐 Enter signer addresses and weights (e.g., 'rAddress1,2 rAddress2,1'):")

        elif action == "multisig_signers":
            passphrase = params[0]
            try:
                signers = [tuple(s.split(",")) for s in text.split()]
                signers = [(addr, int(weight)) for addr, weight in signers]
                for addr, _ in signers:
                    if not is_valid_classic_address(addr):
                        raise ValueError("Invalid signer address")
                self.pending_inputs[user_id] = ("multisig_threshold", [passphrase, signers])
                await update.message.reply_text("🔐 Enter signature threshold (e.g., 3):")
            except ValueError as e:
                await update.message.reply_text(f"🔴 {e}", reply_markup=self.get_wallet_menu())

        elif action == "multisig_threshold":
            passphrase, signers = params[0], params[1]
            try:
                threshold = int(text)
                wallet = await self.create_multisig_wallet(user_id, passphrase, signers, threshold)
                if wallet:
                    await update.message.reply_text(f"🔐 Multisig wallet created: {wallet.classic_address}",
                                                    reply_markup=self.get_wallet_menu())
                else:
                    await update.message.reply_text("🔴 Failed to create multisig wallet.",
                                                    reply_markup=self.get_wallet_menu())
            except ValueError:
                await update.message.reply_text("🔴 Invalid threshold.", reply_markup=self.get_wallet_menu())

        # Wallet Functions
        elif action == "check_balance_passphrase":
            passphrase = text
            wallet = self.get_wallet(user_id, passphrase=passphrase)
            if wallet:
                balance = await self.get_balance(wallet)
                await update.message.reply_text(f"💰 Balance: {balance} XRP", reply_markup=self.get_wallet_menu())
            else:
                await update.message.reply_text("🔴 No wallet selected or wrong passphrase.",
                                                reply_markup=self.get_wallet_menu())

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
            try:
                amount = self.validate_amount(text)
                self.pending_inputs[user_id] = (f"{trade_type}_passphrase", [trade_type, currency, issuer, amount])
                await update.message.reply_text(f"🔐 Enter your wallet passphrase to confirm {trade_type}:")
            except ValueError as e:
                await update.message.reply_text(f"🔴 {e}", reply_markup=self.get_trade_menu())

        elif action == "buy_passphrase" or action == "sell_passphrase":
            trade_type, currency, issuer, amount = params[0], params[1], params[2], params[3]
            wallet = self.get_wallet(user_id, passphrase=text)
            if not wallet:
                await update.message.reply_text("🔴 No wallet selected or wrong passphrase.",
                                                reply_markup=self.get_trade_menu())
                return
            prices = await self.fetch_price_data(currency, issuer)
            if not prices["bid"] or not prices["ask"]:
                await update.message.reply_text("🔴 No market data available.", reply_markup=self.get_trade_menu())
                return
            try:
                if trade_type == "buy":
                    xrp_amount = amount * prices["ask"]
                    tx = OfferCreate(account=wallet.classic_address,
                                     taker_gets={"currency": currency, "issuer": issuer, "value": str(amount)},
                                     taker_pays=xrp_to_drops(xrp_amount))
                else:
                    xrp_amount = amount * prices["bid"]
                    tx = OfferCreate(account=wallet.classic_address, taker_gets=xrp_to_drops(xrp_amount),
                                     taker_pays={"currency": currency, "issuer": issuer, "value": str(amount)})
                async with await self.get_available_client() as client:
                    response = await submit_and_wait(tx, client, wallet)
                    if response.is_successful():
                        await update.message.reply_text(f"✅ {trade_type.capitalize()} successful: {amount} {currency}",
                                                        reply_markup=self.get_trade_menu())
                    else:
                        await update.message.reply_text(f"🔴 {trade_type.capitalize()} failed.",
                                                        reply_markup=self.get_trade_menu())
            except Exception as e:
                await update.message.reply_text(f"🔴 {trade_type.capitalize()} error: {e}",
                                                reply_markup=self.get_trade_menu())

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
            try:
                amount = self.validate_amount(text)
                self.pending_inputs[user_id] = ("pay_passphrase", [destination, amount])
                await update.message.reply_text("🔐 Enter your wallet passphrase to confirm payment:")
            except ValueError as e:
                await update.message.reply_text(f"🔴 {e}", reply_markup=self.get_main_menu())

        elif action == "pay_passphrase":
            destination, amount = params[0], params[1]
            wallet = self.get_wallet(user_id, passphrase=text)
            if not wallet:
                await update.message.reply_text("🔴 No wallet selected or wrong passphrase.",
                                                reply_markup=self.get_main_menu())
                return
            try:
                tx = Payment(account=wallet.classic_address, destination=destination, amount=xrp_to_drops(amount))
                async with await self.get_available_client() as client:
                    response = await submit_and_wait(tx, client, wallet)
                    if response.is_successful():
                        await update.message.reply_text(f"✅ Payment of {amount} XRP sent to {destination}",
                                                        reply_markup=self.get_main_menu())
                    else:
                        await update.message.reply_text("🔴 Payment failed.", reply_markup=self.get_main_menu())
            except Exception as e:
                await update.message.reply_text(f"🔴 Payment error: {e}", reply_markup=self.get_main_menu())

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
            try:
                xrp_amount, token_amount = map(float, text.split())
                self.pending_inputs[user_id] = ("amm_create_passphrase", [currency, issuer, xrp_amount, token_amount])
                await update.message.reply_text("🔐 Enter your wallet passphrase to create AMM pool:")
            except ValueError:
                await update.message.reply_text("🔴 Invalid amount format.", reply_markup=self.get_amm_menu())

        elif action == "amm_create_passphrase":
            currency, issuer, xrp_amount, token_amount = params
            wallet = self.get_wallet(user_id, passphrase=text)
            if not wallet:
                await update.message.reply_text("🔴 No wallet selected or wrong passphrase.",
                                                reply_markup=self.get_amm_menu())
                return
            try:
                tx = AMMCreate(account=wallet.classic_address, amount=xrp_to_drops(xrp_amount),
                               amount2={"currency": currency, "issuer": issuer, "value": str(token_amount)},
                               trading_fee=500)
                async with await self.get_available_client() as client:
                    response = await submit_and_wait(tx, client, wallet)
                    if response.is_successful():
                        await update.message.reply_text(f"✅ AMM pool created: {xrp_amount} XRP, {token_amount} {currency}",
                                                        reply_markup=self.get_amm_menu())
                    else:
                        await update.message.reply_text("🔴 AMM creation failed.", reply_markup=self.get_amm_menu())
            except Exception as e:
                await update.message.reply_text(f"🔴 AMM creation error: {e}", reply_markup=self.get_amm_menu())

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
            try:
                xrp_amount, token_amount = map(float, text.split())
                self.pending_inputs[user_id] = ("amm_deposit_passphrase", [currency, issuer, xrp_amount, token_amount])
                await update.message.reply_text("🔐 Enter your wallet passphrase to deposit to AMM:")
            except ValueError:
                await update.message.reply_text("🔴 Invalid amount format.", reply_markup=self.get_amm_menu())

        elif action == "amm_deposit_passphrase":
            currency, issuer, xrp_amount, token_amount = params
            wallet = self.get_wallet(user_id, passphrase=text)
            if not wallet:
                await update.message.reply_text("🔴 No wallet selected or wrong passphrase.",
                                                reply_markup=self.get_amm_menu())
                return
            try:
                tx = AMMDeposit(account=wallet.classic_address, amount=xrp_to_drops(xrp_amount),
                                amount2={"currency": currency, "issuer": issuer, "value": str(token_amount)})
                async with await self.get_available_client() as client:
                    response = await submit_and_wait(tx, client, wallet)
                    if response.is_successful():
                        await update.message.reply_text(f"✅ Deposited {xrp_amount} XRP and {token_amount} {currency} to AMM",
                                                        reply_markup=self.get_amm_menu())
                    else:
                        await update.message.reply_text("🔴 AMM deposit failed.", reply_markup=self.get_amm_menu())
            except Exception as e:
                await update.message.reply_text(f"🔴 AMM deposit error: {e}", reply_markup=self.get_amm_menu())

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
            try:
                lp_amount = self.validate_amount(text)
                self.pending_inputs[user_id] = ("amm_withdraw_passphrase", [currency, issuer, lp_amount])
                await update.message.reply_text("🔐 Enter your wallet passphrase to withdraw from AMM:")
            except ValueError as e:
                await update.message.reply_text(f"🔴 {e}", reply_markup=self.get_amm_menu())

        elif action == "amm_withdraw_passphrase":
            currency, issuer, lp_amount = params
            wallet = self.get_wallet(user_id, passphrase=text)
            if not wallet:
                await update.message.reply_text("🔴 No wallet selected or wrong passphrase.",
                                                reply_markup=self.get_amm_menu())
                return
            try:
                tx = AMMWithdraw(account=wallet.classic_address, amount={"currency": "XRP"},
                                 amount2={"currency": currency, "issuer": issuer},
                                 lp_token_in={"currency": "XXX", "issuer": wallet.classic_address, "value": str(lp_amount)})
                async with await self.get_available_client() as client:
                    response = await submit_and_wait(tx, client, wallet)
                    if response.is_successful():
                        await update.message.reply_text(f"✅ Withdrew {lp_amount} LP tokens from AMM",
                                                        reply_markup=self.get_amm_menu())
                    else:
                        await update.message.reply_text("🔴 AMM withdrawal failed.", reply_markup=self.get_amm_menu())
            except Exception as e:
                await update.message.reply_text(f"🔴 AMM withdrawal error: {e}", reply_markup=self.get_amm_menu())

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
            try:
                amount = self.validate_amount(text)
                self.pending_inputs[user_id] = ("amm_bid_passphrase", [currency, issuer, amount])
                await update.message.reply_text("🔐 Enter your wallet passphrase to bid on AMM:")
            except ValueError as e:
                await update.message.reply_text(f"🔴 {e}", reply_markup=self.get_amm_menu())

        elif action == "amm_bid_passphrase":
            currency, issuer, amount = params
            wallet = self.get_wallet(user_id, passphrase=text)
            if not wallet:
                await update.message.reply_text("🔴 No wallet selected or wrong passphrase.",
                                                reply_markup=self.get_amm_menu())
                return
            try:
                tx = AMMBid(account=wallet.classic_address, amount=xrp_to_drops(amount),
                            asset={"currency": "XRP"}, asset2={"currency": currency, "issuer": issuer})
                async with await self.get_available_client() as client:
                    response = await submit_and_wait(tx, client, wallet)
                    if response.is_successful():
                        await update.message.reply_text(f"✅ Bid {amount} XRP on AMM", reply_markup=self.get_amm_menu())
                    else:
                        await update.message.reply_text("🔴 AMM bid failed.", reply_markup=self.get_amm_menu())
            except Exception as e:
                await update.message.reply_text(f"🔴 AMM bid error: {e}", reply_markup=self.get_amm_menu())

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
            try:
                fee = int(text)
                self.pending_inputs[user_id] = ("amm_vote_passphrase", [currency, issuer, fee])
                await update.message.reply_text("🔐 Enter your wallet passphrase to vote on AMM fee:")
            except ValueError:
                await update.message.reply_text("🔴 Invalid fee format.", reply_markup=self.get_amm_menu())

        elif action == "amm_vote_passphrase":
            currency, issuer, fee = params
            wallet = self.get_wallet(user_id, passphrase=text)
            if not wallet:
                await update.message.reply_text("🔴 No wallet selected or wrong passphrase.",
                                                reply_markup=self.get_amm_menu())
                return
            try:
                tx = AMMVote(account=wallet.classic_address, asset={"currency": "XRP"},
                             asset2={"currency": currency, "issuer": issuer}, trading_fee=fee)
                async with await self.get_available_client() as client:
                    response = await submit_and_wait(tx, client, wallet)
                    if response.is_successful():
                        await update.message.reply_text(f"✅ Voted for {fee/10000}% fee on AMM",
                                                        reply_markup=self.get_amm_menu())
                    else:
                        await update.message.reply_text("🔴 AMM vote failed.", reply_markup=self.get_amm_menu())
            except Exception as e:
                await update.message.reply_text(f"🔴 AMM vote error: {e}", reply_markup=self.get_amm_menu())

        # Escrow Handlers
        elif action == "escrow_amount":
            try:
                amount = self.validate_amount(text)
                self.pending_inputs[user_id] = ("escrow_create_destination", [amount])
                await update.message.reply_text("🔒 Enter destination address for escrow:")
            except ValueError as e:
                await update.message.reply_text(f"🔴 {e}", reply_markup=self.get_escrow_menu())

        elif action == "escrow_create_destination":
            amount, destination = params[0], text
            if not is_valid_classic_address(destination):
                await update.message.reply_text("🔴 Invalid destination address.", reply_markup=self.get_escrow_menu())
                return
            self.pending_inputs[user_id] = ("escrow_create_condition", [amount, destination])
            await update.message.reply_text("🔒 Enter condition for escrow (e.g., time in seconds):")

        elif action == "escrow_create_condition":
            amount, destination, condition = params[0], params[1], text
            self.pending_inputs[user_id] = ("escrow_create_passphrase", [amount, destination, condition])
            await update.message.reply_text("🔐 Enter your wallet passphrase to create escrow:")

        elif action == "escrow_create_passphrase":
            amount, destination, condition = params
            wallet = self.get_wallet(user_id, passphrase=text)
            if not wallet:
                await update.message.reply_text("🔴 No wallet selected or wrong passphrase.",
                                                reply_markup=self.get_escrow_menu())
                return
            try:
                tx = EscrowCreate(account=wallet.classic_address, amount=xrp_to_drops(amount),
                                  destination=destination, condition=condition)
                async with await self.get_available_client() as client:
                    response = await submit_and_wait(tx, client, wallet)
                    if response.is_successful():
                        await update.message.reply_text(f"✅ Escrow created for {amount} XRP to {destination}",
                                                        reply_markup=self.get_escrow_menu())
                    else:
                        await update.message.reply_text("🔴 Escrow creation failed.", reply_markup=self.get_escrow_menu())
            except Exception as e:
                await update.message.reply_text(f"🔴 Escrow creation error: {e}", reply_markup=self.get_escrow_menu())

        elif action == "escrow_finish_sequence":
            try:
                sequence = int(text)
                self.pending_inputs[user_id] = ("escrow_finish_passphrase", [sequence])
                await update.message.reply_text("🔐 Enter your wallet passphrase to finish escrow:")
            except ValueError:
                await update.message.reply_text("🔴 Invalid sequence.", reply_markup=self.get_escrow_menu())

        elif action == "escrow_finish_passphrase":
            sequence = params[0]
            wallet = self.get_wallet(user_id, passphrase=text)
            if not wallet:
                await update.message.reply_text("🔴 No wallet selected or wrong passphrase.",
                                                reply_markup=self.get_escrow_menu())
                return
            try:
                tx = EscrowFinish(account=wallet.classic_address, offer_sequence=sequence)
                async with await self.get_available_client() as client:
                    response = await submit_and_wait(tx, client, wallet)
                    if response.is_successful():
                        await update.message.reply_text(f"✅ Escrow {sequence} finished",
                                                        reply_markup=self.get_escrow_menu())
                    else:
                        await update.message.reply_text("🔴 Failed to finish escrow.",
                                                        reply_markup=self.get_escrow_menu())
            except Exception as e:
                await update.message.reply_text(f"🔴 Escrow finish error: {e}", reply_markup=self.get_escrow_menu())

        elif action == "escrow_cancel_sequence":
            try:
                sequence = int(text)
                self.pending_inputs[user_id] = ("escrow_cancel_passphrase", [sequence])
                await update.message.reply_text("🔐 Enter your wallet passphrase to cancel escrow:")
            except ValueError:
                await update.message.reply_text("🔴 Invalid sequence.", reply_markup=self.get_escrow_menu())

        elif action == "escrow_cancel_passphrase":
            sequence = params[0]
            wallet = self.get_wallet(user_id, passphrase=text)
            if not wallet:
                await update.message.reply_text("🔴 No wallet selected or wrong passphrase.",
                                                reply_markup=self.get_escrow_menu())
                return
            try:
                tx = EscrowCancel(account=wallet.classic_address, offer_sequence=sequence)
                async with await self.get_available_client() as client:
                    response = await submit_and_wait(tx, client, wallet)
                    if response.is_successful():
                        await update.message.reply_text(f"✅ Escrow {sequence} canceled",
                                                        reply_markup=self.get_escrow_menu())
                    else:
                        await update.message.reply_text("🔴 Failed to cancel escrow.",
                                                        reply_markup=self.get_escrow_menu())
            except Exception as e:
                await update.message.reply_text(f"🔴 Escrow cancel error: {e}", reply_markup=self.get_escrow_menu())

        # Payment Channel Handlers
        elif action == "payment_channel_amount":
            try:
                amount = self.validate_amount(text)
                self.pending_inputs[user_id] = ("payment_channel_destination", [amount])
                await update.message.reply_text("💳 Enter destination address for payment channel:")
            except ValueError as e:
                await update.message.reply_text(f"🔴 {e}", reply_markup=self.get_payment_channel_menu())

        elif action == "payment_channel_destination":
            amount, destination = params[0], text
            if not is_valid_classic_address(destination):
                await update.message.reply_text("🔴 Invalid destination address.",
                                                reply_markup=self.get_payment_channel_menu())
                return
            self.pending_inputs[user_id] = ("payment_channel_expiration", [amount, destination])
            await update.message.reply_text("💳 Enter expiration time for payment channel (in seconds):")

        elif action == "payment_channel_expiration":
            amount, destination, expiration = params[0], params[1], text
            try:
                expiration = int(expiration)
                self.pending_inputs[user_id] = ("payment_channel_passphrase", [amount, destination, expiration])
                await update.message.reply_text("🔐 Enter your wallet passphrase to create payment channel:")
            except ValueError:
                await update.message.reply_text("🔴 Invalid expiration format.",
                                                reply_markup=self.get_payment_channel_menu())

        elif action == "payment_channel_passphrase":
            amount, destination, expiration = params
            wallet = self.get_wallet(user_id, passphrase=text)
            if not wallet:
                await update.message.reply_text("🔴 No wallet selected or wrong passphrase.",
                                                reply_markup=self.get_payment_channel_menu())
                return
            try:
                tx = PaymentChannelCreate(account=wallet.classic_address, amount=xrp_to_drops(amount),
                                          destination=destination, settle_delay=expiration)
                async with await self.get_available_client() as client:
                    response = await submit_and_wait(tx, client, wallet)
                    if response.is_successful():
                        await update.message.reply_text(f"✅ Payment channel created for {amount} XRP to {destination}",
                                                        reply_markup=self.get_payment_channel_menu())
                    else:
                        await update.message.reply_text("🔴 Payment channel creation failed.",
                                                        reply_markup=self.get_payment_channel_menu())
            except Exception as e:
                await update.message.reply_text(f"🔴 Payment channel creation error: {e}",
                                                reply_markup=self.get_payment_channel_menu())

        elif action == "payment_channel_claim_channel":
            channel_id = text
            self.pending_inputs[user_id] = ("payment_channel_claim_passphrase", [channel_id])
            await update.message.reply_text("🔐 Enter your wallet passphrase to claim from payment channel:")

        elif action == "payment_channel_claim_passphrase":
            channel_id = params[0]
            wallet = self.get_wallet(user_id, passphrase=text)
            if not wallet:
                await update.message.reply_text("🔴 No wallet selected or wrong passphrase.",
                                                reply_markup=self.get_payment_channel_menu())
                return
            try:
                tx = PaymentChannelClaim(channel=channel_id, account=wallet.classic_address)
                async with await self.get_available_client() as client:
                    response = await submit_and_wait(tx, client, wallet)
                    if response.is_successful():
                        await update.message.reply_text(f"✅ Claimed from payment channel {channel_id}",
                                                        reply_markup=self.get_payment_channel_menu())
                    else:
                        await update.message.reply_text("🔴 Failed to claim from payment channel.",
                                                        reply_markup=self.get_payment_channel_menu())
            except Exception as e:
                await update.message.reply_text(f"🔴 Payment channel claim error: {e}",
                                                reply_markup=self.get_payment_channel_menu())

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
            conn = self.get_db_connection()
            c = conn.cursor()
            c.execute("INSERT OR IGNORE INTO user_tokens (user_id, currency, issuer) VALUES (?, ?, ?)",
                      (user_id, currency, issuer))
            conn.commit()
            conn.close()
            await update.message.reply_text(f"🔍 Added {currency} from {issuer} for analysis.",
                                            reply_markup=self.get_analysis_menu())

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
            conn = self.get_db_connection()
            c = conn.cursor()
            c.execute("INSERT OR REPLACE INTO user_settings (user_id, default_currency, default_issuer) VALUES (?, ?, ?)",
                      (user_id, currency, issuer))
            conn.commit()
            conn.close()
            await update.message.reply_text(f"🔗 Default token pairing set to {currency} from {issuer}",
                                            reply_markup=self.get_settings_menu())

        elif action == "set_slippage_value":
            try:
                slippage = float(text)
                if slippage < 0:
                    raise ValueError("Slippage must be non-negative")
                conn = self.get_db_connection()
                c = conn.cursor()
                c.execute("UPDATE user_settings SET slippage_tolerance = ? WHERE user_id = ?",
                          (slippage, user_id))
                if c.rowcount == 0:
                    c.execute("INSERT INTO user_settings (user_id, slippage_tolerance) VALUES (?, ?)",
                              (user_id, slippage))
                conn.commit()
                conn.close()
                await update.message.reply_text(f"📉 Slippage tolerance set to {slippage}%",
                                                reply_markup=self.get_settings_menu())
            except ValueError:
                await update.message.reply_text("🔴 Invalid slippage value.", reply_markup=self.get_settings_menu())

    ### Run the Bot
    def run(self) -> None:
        """Start the bot."""
        logger.info("Starting XRPL Bot...")
        self.application.run_polling()

if __name__ == "__main__":
    bot = XRPLBot()
    bot.run()