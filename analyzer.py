import csv
import os
import shutil
from datetime import datetime
from config import logger


CSV_FILE = "missed_opportunities.csv"
MAX_FILE_SIZE_MB = 50


def ensure_csv_exists():
    """CSV 파일이 없으면 헤더와 함께 생성"""
    if not os.path.exists(CSV_FILE):
        with open(CSV_FILE, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow([
                '시간', '심볼', '탈락사유', '현재가', 
                'RSI', '거래량배수', 'MA40이격도(%)', 'MA185이격도(%)',
                'MA40값', 'MA185값', '185선기울기(%)', '골든크로스봉수', '등급'
            ])


def check_and_backup_file():
    """파일 크기가 50MB를 초과하면 백업하고 새 파일 생성"""
    try:
        if os.path.exists(CSV_FILE):
            file_size_mb = os.path.getsize(CSV_FILE) / (1024 * 1024)
            
            if file_size_mb > MAX_FILE_SIZE_MB:
                # 백업 파일명 생성 (타임스탬프 포함)
                backup_filename = f"missed_opportunities_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
                shutil.copy2(CSV_FILE, backup_filename)
                logger.info(f"[파일백업] {CSV_FILE} ({file_size_mb:.2f}MB) -> {backup_filename}")
                
                # 새 파일 생성 (헤더만)
                with open(CSV_FILE, 'w', newline='', encoding='utf-8') as f:
                    writer = csv.writer(f)
                    writer.writerow([
                        '시간', '심볼', '탈락사유', '현재가', 
                        'RSI', '거래량배수', 'MA40이격도(%)', 'MA185이격도(%)',
                        'MA40값', 'MA185값', '185선기울기(%)', '골든크로스봉수', '등급'
                    ])
                logger.info(f"[파일재생성] {CSV_FILE} 새로 생성됨")
    except Exception as e:
        logger.error(f"File Backup Error: {e}")


def record_missed_opportunity(symbol, reason, current_price, data_dict=None):
    """
    매수 신호가 오지 않은 종목의 정보를 CSV에 기록 (상세 수치 포함)
    
    Args:
        symbol: 종목 심볼 (예: BTC/KRW)
        reason: 탈락 사유 (예: "RSI 과열(67.5 > 65)")
        current_price: 현재가
        data_dict: 판단 근거 수치 딕셔너리 (RSI, 거래량배수, MA40이격도, MA185이격도 등)
    """
    try:
        ensure_csv_exists()
        check_and_backup_file()
        
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        # data_dict가 없으면 기본값 사용
        if data_dict is None:
            data_dict = {}
        
        # 수치 데이터 추출 (없으면 빈 문자열)
        rsi = data_dict.get('rsi', '')
        vol_ratio = data_dict.get('vol_ratio', '')
        disparity_40_pct = data_dict.get('disparity_40_pct', '')
        disparity_185_pct = data_dict.get('disparity_185_pct', '')
        ma40_val = data_dict.get('ma40_val', '')
        ma185_val = data_dict.get('ma185_val', '')
        slope_rate = data_dict.get('slope_rate', '')
        bars_since_gold = data_dict.get('bars_since_gold', '')
        grade = data_dict.get('grade', '')
        
        # 수치 포맷팅
        rsi_str = f"{rsi:.2f}" if isinstance(rsi, (int, float)) else str(rsi)
        vol_ratio_str = f"{vol_ratio:.3f}" if isinstance(vol_ratio, (int, float)) else str(vol_ratio)
        disparity_40_str = f"{disparity_40_pct:.4f}" if isinstance(disparity_40_pct, (int, float)) else str(disparity_40_pct)
        disparity_185_str = f"{disparity_185_pct:.4f}" if isinstance(disparity_185_pct, (int, float)) else str(disparity_185_pct)
        ma40_str = f"{ma40_val:,.0f}" if isinstance(ma40_val, (int, float)) else str(ma40_val)
        ma185_str = f"{ma185_val:,.0f}" if isinstance(ma185_val, (int, float)) else str(ma185_val)
        slope_str = f"{slope_rate:.4f}" if isinstance(slope_rate, (int, float)) else str(slope_rate)
        bars_str = str(bars_since_gold) if bars_since_gold != '' else ''
        
        with open(CSV_FILE, 'a', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow([
                timestamp, symbol, reason, f"{current_price:,.0f}",
                rsi_str, vol_ratio_str, disparity_40_str, disparity_185_str,
                ma40_str, ma185_str, slope_str, bars_str, grade
            ])
        
        logger.info(f"[분석기록] {symbol} | 사유: {reason} | RSI: {rsi_str} | 거래량배수: {vol_ratio_str}")
        
    except Exception as e:
        logger.error(f"Missed Opportunity Record Error ({symbol}): {e}")
