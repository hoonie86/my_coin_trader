import csv
import os
from datetime import datetime
from config import logger


CSV_FILE = "missed_opportunities.csv"


def ensure_csv_exists():
    """CSV 파일이 없으면 헤더와 함께 생성"""
    if not os.path.exists(CSV_FILE):
        with open(CSV_FILE, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['시간', '심볼', '탈락사유', '현재가'])


def record_missed_opportunity(symbol, reason, current_price):
    """
    매수 신호가 오지 않은 종목의 정보를 CSV에 기록
    
    Args:
        symbol: 종목 심볼 (예: BTC/KRW)
        reason: 탈락 사유 (예: "RSI 과열(67.5 > 65)")
        current_price: 현재가
    """
    try:
        ensure_csv_exists()
        
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        with open(CSV_FILE, 'a', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow([timestamp, symbol, reason, f"{current_price:,.0f}"])
        
        logger.info(f"[분석기록] {symbol} | 사유: {reason} | 현재가: {current_price:,.0f}원")
        
    except Exception as e:
        logger.error(f"Missed Opportunity Record Error ({symbol}): {e}")
