import requests
import logging

logger = logging.getLogger(__name__)

def get_bithumb_tick_size(price):
    """호가 단위 계산 (빗썸 기준)"""
    if price < 10: return 0.001
    if price < 100: return 0.01
    if price < 1000: return 0.1
    if price < 5000: return 1
    if price < 10000: return 5
    if price < 50000: return 10
    if price < 100000: return 50
    return 100

def get_warning_list():
    """유의종목/거래지원 종료 리스트 가져오기"""
    try:
        url = "https://api.bithumb.com/public/assetsstatus/ALL"
        res = requests.get(url, timeout=5).json()
        data = res.get('data', {})
        # halt_status가 0이 아니면 거래 중지 또는 유의 종목
        return [coin for coin, info in data.items() if info.get('halt_status', 0) != 0]
    except Exception as e:
        logger.error(f"Warning List Fetch Error: {e}")
        return []