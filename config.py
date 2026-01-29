import ccxt
import logging
from logging.handlers import TimedRotatingFileHandler
import datetime
from datetime import datetime, timedelta  # 여기에 timedelta 추가!

# [사용자 설정]
API_KEY = '710241e6e5ddfbbf963d779786db685d'
SECRET_KEY = '48066fdefa0d0aed512e704d8e70db7e'
TELEGRAM_TOKEN = '8579499626:AAFiZe0WdKjZNfzRRj76clMP2lDD4Xdo7ls'
CHAT_ID = '6766537196'

exchange = ccxt.bithumb({'apiKey': API_KEY, 'secret': SECRET_KEY, 'enableRateLimit': True})

# [로깅 설정] - 사용자 원본 로직 100% 복구
logger = logging.getLogger("TradingBot")
logger.setLevel(logging.INFO)
file_handler = TimedRotatingFileHandler("trading_bot.log", when="midnight", interval=1, backupCount=30, encoding='utf-8')
file_handler.setFormatter(logging.Formatter('[%(asctime)s] %(message)s'))
logger.addHandler(file_handler)

console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter('[%(asctime)s] %(message)s'))
logger.addHandler(console_handler)

# 기타 설정
DEFAULT_TEST_BUY = 20000
REPORT_INTERVAL = 3600  # 1시간
WARNING_LIST = []



def is_sleeping_time():
    """한국 시간(KST) 기준 야간 판정"""
    kst_offset = timedelta(hours=9)
    now_kst = datetime.utcnow() + kst_offset
    now_time = now_kst.time()

    start = datetime.strptime("23:30", "%H:%M").time()
    end = datetime.strptime("07:30", "%H:%M").time()

    return now_time >= start or now_time <= end