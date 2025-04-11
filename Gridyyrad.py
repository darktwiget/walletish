import logging
import os
import ssl
import time
from typing import Dict, Any, Optional, List, Tuple
from datetime import datetime
from dotenv import load_dotenv
from argon2 import PasswordHasher, exceptions
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlcipher3 import dbapi2 as sqlcipher
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from xrpl.asyncio.clients import AsyncWebsocketClient
from xrpl.asyncio.transaction import submit_and_wait
from xrpl.asyncio.ledger import get_latest_validated_ledger_sequence
from xrpl.models.requests import BookOffers, AccountInfo
from xrpl.models.transactions import OfferCreate, OfferCancel
from xrpl.wallet import Wallet
from xrpl.utils import xrp_to_drops, drops_to_xrp
from xrpl.core.addresscodec import is_valid_classic_address
from aiohttp import ClientSession, TCPConnector

# Load environment variables
load_dotenv()

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class XRPLGridBot:
    def __init__(self):
        self.nodes = ["wss://s.altnet.rippletest.net:51233"]  # Testnet for safety; change to wss://s1.ripple.com for mainnet
        self.ssl_context = ssl.create_default_context()
        self.ssl_context.verify_mode = ssl.CERT_REQUIRED
        self.ssl_context.check_hostname = True
        self.client: Optional[AsyncWebsocketClient] = None
        self.application = Application.builder().token(os.getenv("raddwall")).build()
        if not os.getenv("raddwall"):
            raise ValueError("raddwall environment variable not set")
        self.pending_inputs: Dict[int, Tuple[str, List[Any]]] = {}
        self.DB_NAME = "xrpl_grid_bot.db"
        self.DB_KEY = os.getenv("SQLCIPHER_KEY")
        if not self.DB_KEY:
            raise ValueError("SQLCIPHER_KEY environment variable not set")
        self.ph = PasswordHasher(memory_cost=65536, time_cost=3, parallelism=4)
        self.init_db()
        self._setup_handlers()
        self.application.job_queue.run_repeating(self.monitor_grid_bots, interval=30, first=10)

    # Database Setup
    def get_db_connection(self) -> sqlcipher.Connection:
        conn = sqlcipher.connect(self.DB_NAME, check_same_thread=False)
        conn.execute(f"PRAGMA key='{self.DB_KEY}'")
        return conn

    def init_db(self) -> None:
        conn = self.get_db_connection()
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS users 
                     (user_id INTEGER, wallet_name TEXT, encrypted_seed TEXT, 
                      salt TEXT, is_current INTEGER DEFAULT 0, 
                      PRIMARY KEY (user_id, wallet_name))''')
        c.execute('''CREATE TABLE IF NOT EXISTS grid_bots 
                     (user_id INTEGER, bot_id INTEGER PRIMARY KEY AUTOINCREMENT, 
                      currency TEXT, issuer TEXT, lower_price REAL, upper_price REAL, 
                      grid_levels INTEGER, order_size REAL, is_active INTEGER DEFAULT 1)''')
        c.execute('''CREATE TABLE IF NOT EXISTS grid_orders 
                     (bot_id INTEGER, order_sequence INTEGER, is_buy INTEGER, 
                      price REAL, amount REAL, status TEXT, 
                      PRIMARY KEY (bot_id, order_sequence))''')
        conn.commit()
        conn.close()

    # Seed Encryption
    def encrypt_seed(self, seed: str, passphrase: str) -> Tuple[str, str]:
        salt = os.urandom(16)
        try:
            hash = self.ph.hash(passphrase.encode(), salt=salt)
            key = base64.b64encode(hash.encode())[:32]
        except exceptions.HashError as e:
            raise ValueError(f"Argon2 key derivation failed: {e}")
        aesgcm = AESGCM(key)
        nonce = os.urandom(12)
        encrypted_seed = aesgcm.encrypt(nonce, seed.encode(), None)
        encrypted_data = base64.b64encode(nonce + encrypted_seed).decode()
        return encrypted_data, base64.b64encode(salt).decode()

    def decrypt_seed(self, encrypted_seed: str, salt: str, passphrase: str) -> str:
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

    # Wallet Management
    def get_wallet(self, user_id: int, passphrase: str | None = None) -> Optional[Wallet]:
        conn = self.get_db_connection()
        c = conn.cursor()
        c.execute("SELECT encrypted_seed, salt FROM users WHERE user_id = ? AND is_current = 1", (user_id,))
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
        conn = self.get_db_connection()
        c = conn.cursor()
        c.execute("UPDATE users SET is_current = 0 WHERE user_id = ?", (user_id,))
        c.execute("UPDATE users SET is_current = 1 WHERE user_id = ? AND wallet_name = ?", (user_id, wallet_name))
        conn.commit()
        conn.close()
        return c.rowcount > 0

    # XRPL Utilities
    async def get_available_client(self) -> AsyncWebsocketClient:
        for node in self.nodes:
            try:
                client = AsyncWebsocketClient(node, ssl_context=self.ssl_context)
                await client.open()
                return client
            except Exception as e:
                logger.warning(f"Failed to connect to {node}: {e}")
        raise ValueError("No available XRPL nodes")

    async def get_balance(self, wallet: Wallet) -> float:
        async with await self.get_available_client() as client:
            response = await client.request(AccountInfo(account=wallet.classic_address, ledger_index="validated"))
            return drops_to_xrp(response.result["account_data"]["Balance"]) if response.is_successful() else 0.0

    # Price Fetching for USD/XRP and Meme Tokens
    async def fetch_price_data(self, currency: str, issuer: str) -> Dict[str, float]:
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

            return {"bid": best_bid, "ask": best_ask}

    # Grid Bot Logic
    async def start_grid_bot(self, user_id: int, bot_id: int, passphrase: str) -> str:
        conn = self.get_db_connection()
        c = conn.cursor()
        c.execute("""SELECT currency, issuer, lower_price, upper_price, grid_levels, order_size 
                     FROM grid_bots WHERE user_id = ? AND bot_id = ?""", (user_id, bot_id))
        bot = c.fetchone()
        if not bot:
            conn.close()
            return "🔴 Grid bot not found."
        currency, issuer, lower_price, upper_price, grid_levels, order_size = bot

        wallet = self.get_wallet(user_id, passphrase)
        if not wallet:
            conn.close()
            return "🔴 Invalid passphrase or no wallet selected."

        # Check balance
        balance = await self.get_balance(wallet)
        required_xrp = order_size * grid_levels / 2 + 1  # Approx for buy orders + reserve
        if balance < required_xrp:
            conn.close()
            return f"🔴 Insufficient balance: Need ~{required_xrp:.2f} XRP, have {balance:.2f} XRP."

        # Get current price for validation
        prices = await self.fetch_price_data(currency, issuer)
        current_price = (prices["bid"] + prices["ask"]) / 2 if prices["bid"] and prices["ask"] else 0
        if not current_price:
            conn.close()
            return "🔴 No market data available."
        if not (lower_price <= current_price <= upper_price):
            conn.close()
            return f"🔴 Current price ({current_price:.4f} XRP) outside grid range ({lower_price:.4f}–{upper_price:.4f})."

        # Update bot status
        c.execute("UPDATE grid_bots SET is_active = 1 WHERE bot_id = ?", (bot_id,))
        conn.commit()
        conn.close()

        # Calculate grid levels
        price_step = (upper_price - lower_price) / (grid_levels - 1)
        grid_prices = [lower_price + i * price_step for i in range(grid_levels)]

        async with await self.get_available_client() as client:
            for price in grid_prices:
                try:
                    if price < current_price:  # Buy order
                        token_amount = order_size / price
                        tx = OfferCreate(
                            account=wallet.classic_address,
                            taker_gets={"currency": currency, "issuer": issuer, "value": str(token_amount)},
                            taker_pays=xrp_to_drops(order_size),
                            flags=0x00080000  # Passive
                        )
                        is_buy = 1
                    else:  # Sell order
                        token_amount = order_size / price
                        tx = OfferCreate(
                            account=wallet.classic_address,
                            taker_gets=xrp_to_drops(order_size),
                            taker_pays={"currency": currency, "issuer": issuer, "value": str(token_amount)}
                        )
                        is_buy = 0

                    response = await submit_and_wait(tx, client, wallet)
                    if response.is_successful():
                        sequence = response.result["Sequence"]
                        conn = self.get_db_connection()
                        c = conn.cursor()
                        c.execute("""INSERT INTO grid_orders (bot_id, order_sequence, is_buy, price, amount, status) 
                                     VALUES (?, ?, ?, ?, ?, ?)""",
                                  (bot_id, sequence, is_buy, price, order_size, "open"))
                        conn.commit()
                        conn.close()
                    else:
                        logger.error(f"Failed to place order at {price}: {response.result}")
                except Exception as e:
                    logger.error(f"Error placing order at {price}: {e}")
        return f"✅ Grid bot {bot_id} started for {currency}/XRP."

    async def stop_grid_bot(self, user_id: int, bot_id: int, passphrase: str) -> str:
        conn = self.get_db_connection()
        c = conn.cursor()
        c.execute("SELECT order_sequence FROM grid_orders WHERE bot_id = ? AND status = 'open'", (bot_id,))
        orders = c.fetchall()

        wallet = self.get_wallet(user_id, passphrase)
        if not wallet:
            conn.close()
            return "🔴 Invalid passphrase or no wallet selected."

        async with await self.get_available_client() as client:
            for order in orders:
                sequence = order[0]
                try:
                    tx = OfferCancel(account=wallet.classic_address, offer_sequence=sequence)
                    response = await submit_and_wait(tx, client, wallet)
                    if response.is_successful():
                        c.execute("UPDATE grid_orders SET status = 'canceled' WHERE bot_id = ? AND order_sequence = ?",
                                  (bot_id, sequence))
                except Exception as e:
                    logger.error(f"Failed to cancel order {sequence}: {e}")

        c.execute("UPDATE grid_bots SET is_active = 0 WHERE bot_id = ?", (bot_id,))
        conn.commit()
        conn.close()
        return f"🛑 Grid bot {bot_id} stopped."

    async def monitor_grid_bots(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        conn = self.get_db_connection()
        c = conn.cursor()
        c.execute("SELECT bot_id, user_id, currency, issuer, lower_price, upper_price, grid_levels, order_size FROM grid_bots WHERE is_active = 1")
        bots = c.fetchall()
        conn.close()

        for bot in bots:
            bot_id, user_id, currency, issuer, lower_price, upper_price, grid_levels, order_size = bot
            # Placeholder passphrase; in production, prompt user or use secure storage
            wallet = self.get_wallet(user_id, "default_passphrase")  # Replace with secure passphrase handling
            if not wallet:
                continue

            async with await self.get_available_client() as client:
                conn = self.get_db_connection()
                c = conn.cursor()
                c.execute("SELECT order_sequence, is_buy, price FROM grid_orders WHERE bot_id = ? AND status = 'open'", (bot_id,))
                orders = c.fetchall()

                for order in orders:
                    sequence, is_buy, price = order
                    try:
                        response = await client.request(AccountInfo(account=wallet.classic_address, ledger_index="validated"))
                        offers = response.result.get("account_data", {}).get("Offers", [])
                        if not any(o["seq"] == sequence for o in offers):
                            c.execute("UPDATE grid_orders SET status = 'filled' WHERE bot_id = ? AND order_sequence = ?",
                                      (bot_id, sequence))
                            price_step = (upper_price - lower_price) / (grid_levels - 1)
                            grid_prices = [lower_price + i * price_step for i in range(grid_levels)]
                            next_price = min([p for p in grid_prices if p > price]) if is_buy else max([p for p in grid_prices if p < price])

                            new_tx = OfferCreate(
                                account=wallet.classic_address,
                                taker_gets=xrp_to_drops(order_size) if is_buy else {"currency": currency, "issuer": issuer, "value": str(order_size / next_price)},
                                taker_pays={"currency": currency, "issuer": issuer, "value": str(order_size / next_price)} if is_buy else xrp_to_drops(order_size)
                            )
                            response = await submit_and_wait(new_tx, client, wallet)
                            if response.is_successful():
                                new_sequence = response.result["Sequence"]
                                c.execute("""INSERT INTO grid_orders (bot_id, order_sequence, is_buy, price, amount, status) 
                                             VALUES (?, ?, ?, ?, ?, ?)""",
                                          (bot_id, new_sequence, 0 if is_buy else 1, next_price, order_size, "open"))
                                await context.bot.send_message(user_id, f"✅ Order at {price:.4f} filled, new order placed at {next_price:.4f}.")
                    except Exception as e:
                        logger.error(f"Error monitoring order {sequence}: {e}")
                conn.commit()
                conn.close()

    # Input Validation
    def validate_amount(self, text: str) -> float:
        try:
            amount = float(text)
            if amount <= 0:
                raise ValueError("Amount must be positive")
            return amount
        except ValueError:
            raise ValueError("Invalid amount format")

    # Telegram UI
    def get_main_menu(self) -> InlineKeyboardMarkup:
        keyboard = [
            [InlineKeyboardButton("👛 Wallet", callback_data="wallet_menu"),
             InlineKeyboardButton("📈 Grid Bot", callback_data="grid_bot_menu")]
        ]
        return InlineKeyboardMarkup(keyboard)

    def get_wallet_menu(self) -> InlineKeyboardMarkup:
        keyboard = [
            [InlineKeyboardButton("✨ Create Wallet", callback_data="create_wallet"),
             InlineKeyboardButton("🔄 Switch Wallet", callback_data="switch_wallet")],
            [InlineKeyboardButton("⬅️ Back", callback_data="main_menu")]
        ]
        return InlineKeyboardMarkup(keyboard)

    def get_grid_bot_menu(self) -> InlineKeyboardMarkup:
        keyboard = [
            [InlineKeyboardButton("✨ Start Grid Bot", callback_data="start_grid_bot"),
             InlineKeyboardButton("🛑 Stop Grid Bot", callback_data="stop_grid_bot")],
            [InlineKeyboardButton("⚙️ Configure Grid Bot", callback_data="configure_grid_bot"),
             InlineKeyboardButton("📋 List Grid Bots", callback_data="list_grid_bots")],
            [InlineKeyboardButton("⬅️ Back", callback_data="main_menu")]
        ]
        return InlineKeyboardMarkup(keyboard)

    # Handlers
    def _setup_handlers(self) -> None:
        self.application.add_handler(CommandHandler("start", self.start))
        self.application.add_handler(CallbackQueryHandler(self.button))
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text))

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = update.message.from_user.id
        logger.info(f"User {user_id} started the bot")
        await update.message.reply_text("Welcome to XRPL Grid Bot! 🌟", reply_markup=self.get_main_menu())

    async def button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        user_id = query.from_user.id
        data = query.data.split("|")
        action = data[0]

        if action == "main_menu":
            await query.edit_message_text("🌟 Main Menu", reply_markup=self.get_main_menu())
        elif action == "wallet_menu":
            await query.edit_message_text("👛 Wallet Options", reply_markup=self.get_wallet_menu())
        elif action == "grid_bot_menu":
            await query.edit_message_text("📈 Grid Bot Options", reply_markup=self.get_grid_bot_menu())

        # Wallet Handlers
        elif action == "create_wallet":
            self.pending_inputs[user_id] = ("create_wallet_passphrase", [])
            await query.edit_message_text("✨ Enter a passphrase to encrypt your new wallet:")

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

        # Grid Bot Handlers
        elif action == "start_grid_bot":
            conn = self.get_db_connection()
            c = conn.cursor()
            c.execute("SELECT bot_id, currency, issuer FROM grid_bots WHERE user_id = ? AND is_active = 0", (user_id,))
            bots = c.fetchall()
            conn.close()
            if not bots:
                await query.edit_message_text("🔴 No inactive grid bots found.", reply_markup=self.get_grid_bot_menu())
            else:
                keyboard = [[InlineKeyboardButton(f"{b[1]}/{b[2]}", callback_data=f"start_grid_select|{b[0]}")] for b in bots]
                keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="grid_bot_menu")])
                await query.edit_message_text("📈 Select a grid bot to start:", reply_markup=InlineKeyboardMarkup(keyboard))

        elif action == "start_grid_select":
            bot_id = int(data[1])
            self.pending_inputs[user_id] = ("start_grid_passphrase", [bot_id])
            await query.edit_message_text("🔐 Enter your wallet passphrase to start the grid bot:")

        elif action == "stop_grid_bot":
            conn = self.get_db_connection()
            c = conn.cursor()
            c.execute("SELECT bot_id, currency, issuer FROM grid_bots WHERE user_id = ? AND is_active = 1", (user_id,))
            bots = c.fetchall()
            conn.close()
            if not bots:
                await query.edit_message_text("🔴 No active grid bots found.", reply_markup=self.get_grid_bot_menu())
            else:
                keyboard = [[InlineKeyboardButton(f"{b[1]}/{b[2]}", callback_data=f"stop_grid_select|{b[0]}")] for b in bots]
                keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="grid_bot_menu")])
                await query.edit_message_text("🛑 Select a grid bot to stop:", reply_markup=InlineKeyboardMarkup(keyboard))

        elif action == "stop_grid_select":
            bot_id = int(data[1])
            self.pending_inputs[user_id] = ("stop_grid_passphrase", [bot_id])
            await query.edit_message_text("🔐 Enter your wallet passphrase to stop the grid bot:")

        elif action == "configure_grid_bot":
            self.pending_inputs[user_id] = ("grid_currency", [])
            await query.edit_message_text("📈 Enter token currency code for grid bot (e.g., USD or DOG):")

        elif action == "list_grid_bots":
            conn = self.get_db_connection()
            c = conn.cursor()
            c.execute("SELECT bot_id, currency, issuer, is_active FROM grid_bots WHERE user_id = ?", (user_id,))
            bots = c.fetchall()
            conn.close()
            if not bots:
                await query.edit_message_text("🔴 No grid bots found.", reply_markup=self.get_grid_bot_menu())
            else:
                message = "📋 Your Grid Bots:\n"
                for bot in bots:
                    status = "Active" if bot[3] else "Inactive"
                    message += f"Bot ID: {bot[0]}, Pair: {bot[1]}/{bot[2]}, Status: {status}\n"
                await query.edit_message_text(message, reply_markup=self.get_grid_bot_menu())

    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = update.message.from_user.id
        text = update.message.text
        if user_id not in self.pending_inputs:
            return

        action, params = self.pending_inputs[user_id]
        del self.pending_inputs[user_id]

        # Wallet Creation
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
                await update.message.reply_text(f"✨ Wallet created: {wallet.classic_address}", reply_markup=self.get_wallet_menu())
            except ValueError as e:
                await update.message.reply_text(f"🔴 Failed to create wallet: {e}", reply_markup=self.get_wallet_menu())

        # Grid Bot Configuration
        elif action == "grid_currency":
            currency = text.upper()
            self.pending_inputs[user_id] = ("grid_issuer", [currency])
            await update.message.reply_text(f"📈 Enter issuer address for {currency}:")

        elif action == "grid_issuer":
            currency, issuer = params[0], text
            if not is_valid_classic_address(issuer):
                await update.message.reply_text("🔴 Invalid issuer address.", reply_markup=self.get_grid_bot_menu())
                return
            self.pending_inputs[user_id] = ("grid_lower_price", [currency, issuer])
            await update.message.reply_text("📈 Enter lower price for grid (in XRP):")

        elif action == "grid_lower_price":
            currency, issuer = params[0], params[1]
            try:
                lower_price = self.validate_amount(text)
                self.pending_inputs[user_id] = ("grid_upper_price", [currency, issuer, lower_price])
                await update.message.reply_text("📈 Enter upper price for grid (in XRP):")
            except ValueError as e:
                await update.message.reply_text(f"🔴 {e}", reply_markup=self.get_grid_bot_menu())

        elif action == "grid_upper_price":
            currency, issuer, lower_price = params[0], params[1], params[2]
            try:
                upper_price = self.validate_amount(text)
                if upper_price <= lower_price:
                    raise ValueError("Upper price must be greater than lower price")
                self.pending_inputs[user_id] = ("grid_levels", [currency, issuer, lower_price, upper_price])
                await update.message.reply_text("📈 Enter number of grid levels (e.g., 5):")
            except ValueError as e:
                await update.message.reply_text(f"🔴 {e}", reply_markup=self.get_grid_bot_menu())

        elif action == "grid_levels":
            currency, issuer, lower_price, upper_price = params[0], params[1], params[2], params[3]
            try:
                levels = int(text)
                if levels < 2:
                    raise ValueError("Grid levels must be at least 2")
                self.pending_inputs[user_id] = ("grid_order_size", [currency, issuer, lower_price, upper_price, levels])
                await update.message.reply_text("📈 Enter order size (in XRP):")
            except ValueError as e:
                await update.message.reply_text(f"🔴 {e}", reply_markup=self.get_grid_bot_menu())

        elif action == "grid_order_size":
            currency, issuer, lower_price, upper_price, levels = params[0], params[1], params[2], params[3], params[4]
            try:
                order_size = self.validate_amount(text)
                conn = self.get_db_connection()
                c = conn.cursor()
                c.execute("""INSERT INTO grid_bots (user_id, currency, issuer, lower_price, upper_price, grid_levels, order_size, is_active) 
                             VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                          (user_id, currency, issuer, lower_price, upper_price, levels, order_size, 0))
                bot_id = c.lastrowid
                conn.commit()
                conn.close()
                await update.message.reply_text(f"✅ Grid bot configured (ID: {bot_id}). Start it from the menu.",
                                                reply_markup=self.get_grid_bot_menu())
            except ValueError as e:
                await update.message.reply_text(f"🔴 {e}", reply_markup=self.get_grid_bot_menu())

        # Grid Bot Start/Stop
        elif action == "start_grid_passphrase":
            bot_id = params[0]
            message = await self.start_grid_bot(user_id, bot_id, text)
            await update.message.reply_text(message, reply_markup=self.get_grid_bot_menu())

        elif action == "stop_grid_passphrase":
            bot_id = params[0]
            message = await self.stop_grid_bot(user_id, bot_id, text)
            await update.message.reply_text(message, reply_markup=self.get_grid_bot_menu())

    # Run the Bot
    def run(self) -> None:
        logger.info("Starting XRPL Grid Bot...")
        self.application.run_polling()

if __name__ == "__main__":
    bot = XRPLGridBot()
    bot.run()