import csv
import os
import shutil
from datetime import datetime
from config import logger


CSV_FILE = "missed_opportunities.csv"
LOSS_REVIEW_FILE = "loss_review.csv"
MAX_FILE_SIZE_MB = 50


def ensure_csv_exists():
    """CSV 파일이 없으면 헤더와 함께 생성 (패턴태그·등급 등 확장 컬럼 포함)"""
    if not os.path.exists(CSV_FILE):
        with open(CSV_FILE, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow([
                '시간', '심볼', '탈락사유', '현재가',
                'RSI', '거래량배수', 'MA40이격도(%)', 'MA185이격도(%)',
                'MA40값', 'MA185값', '185선기울기(%)', '골든크로스봉수', '등급',
                '패턴태그'
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
                        'MA40값', 'MA185값', '185선기울기(%)', '골든크로스봉수', '등급',
                        '패턴태그'
                    ])
                logger.info(f"[파일재생성] {CSV_FILE} 새로 생성됨")
    except Exception as e:
        logger.error(f"File Backup Error: {e}")


def record_missed_opportunity(symbol, reason, current_price, data_dict=None):
    """
    매수 신호가 오지 않은 종목 또는 미지 패턴(조건 1개라도 만족/3분 내 3% 급등) 정보를 CSV에 기록.
    조건 탈락 여부와 관계없이 계산된 모든 수치(RSI, 이격도, 기울기 등)를 빈칸 없이 기록.
    
    Args:
        symbol: 종목 심볼 (예: BTC/KRW)
        reason: 탈락 사유 또는 패턴 요약
        current_price: 현재가
        data_dict: 판단 근거 수치 딕셔너리 (rsi, vol_ratio, disparity_40_pct, pattern_labels 등)
    """
    try:
        ensure_csv_exists()
        check_and_backup_file()
        
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        if data_dict is None:
            data_dict = {}
        
        # 수치 데이터 추출 (없으면 빈 문자열로 채워 빈칸 제거)
        rsi = data_dict.get('rsi', '')
        vol_ratio = data_dict.get('vol_ratio', '')
        disparity_40_pct = data_dict.get('disparity_40_pct', '')
        disparity_185_pct = data_dict.get('disparity_185_pct', '')
        ma40_val = data_dict.get('ma40_val', '')
        ma185_val = data_dict.get('ma185_val', '')
        slope_rate = data_dict.get('slope_rate', '')
        bars_since_gold = data_dict.get('bars_since_gold', '')
        grade = data_dict.get('grade', '')
        # [신규] 패턴 태그: 정배열 / 단기역습 / 바닥탈출
        pattern_labels = data_dict.get('pattern_labels', [])
        pattern_tag = '|'.join(pattern_labels) if isinstance(pattern_labels, (list, tuple)) else str(pattern_labels or '')
        
        # 수치 포맷팅 (조건 탈락 여부와 관계없이 끝까지 계산된 값 사용)
        rsi_str = f"{rsi:.2f}" if isinstance(rsi, (int, float)) else str(rsi)
        vol_ratio_str = f"{vol_ratio:.3f}" if isinstance(vol_ratio, (int, float)) else str(vol_ratio)
        disparity_40_str = f"{disparity_40_pct:.4f}" if isinstance(disparity_40_pct, (int, float)) else str(disparity_40_pct)
        disparity_185_str = f"{disparity_185_pct:.4f}" if isinstance(disparity_185_pct, (int, float)) else str(disparity_185_pct)
        ma40_str = f"{ma40_val:,.0f}" if isinstance(ma40_val, (int, float)) else str(ma40_val)
        ma185_str = f"{ma185_val:,.0f}" if isinstance(ma185_val, (int, float)) else str(ma185_val)
        slope_str = f"{slope_rate:.4f}" if isinstance(slope_rate, (int, float)) else str(slope_rate)
        bars_str = str(bars_since_gold) if bars_since_gold != '' and bars_since_gold is not None else ''
        
        with open(CSV_FILE, 'a', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow([
                timestamp, symbol, reason, f"{current_price:,.0f}",
                rsi_str, vol_ratio_str, disparity_40_str, disparity_185_str,
                ma40_str, ma185_str, slope_str, bars_str, grade,
                pattern_tag
            ])
        
        logger.info(f"[분석기록] {symbol} | 사유: {reason} | RSI: {rsi_str} | 거래량배수: {vol_ratio_str} | 태그: {pattern_tag}")
        
    except Exception as e:
        logger.error(f"Missed Opportunity Record Error ({symbol}): {e}")


def ensure_loss_review_exists():
    """loss_review.csv 헤더 생성 (손절 슬리피지·직전 1분 하락속도)"""
    if not os.path.exists(LOSS_REVIEW_FILE):
        with open(LOSS_REVIEW_FILE, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow([
                '시간', '심볼', '손절가', '목표손절가', '슬리피지(%)', '슬리피지_2pct이상',
                '직전1분_시가', '직전1분_종가', '직전1분_하락속도(%)'
            ])


def record_loss_review(symbol, sell_price, target_stop_price, slippage_pct, last_1m_open, last_1m_close):
    """
    손절 시 복기 데이터: -2% 이상 슬리피지 여부, 손절 직전 1분간 하락 속도를 loss_review.csv에 기록.
    """
    try:
        ensure_loss_review_exists()
        slip_over_2 = 'Y' if slippage_pct <= -2.0 else 'N'
        drop_speed_1m = ((last_1m_close - last_1m_open) / last_1m_open * 100) if last_1m_open and last_1m_open != 0 else ''
        with open(LOSS_REVIEW_FILE, 'a', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow([
                datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                symbol, f"{sell_price:,.0f}", f"{target_stop_price:,.0f}",
                f"{slippage_pct:.2f}", slip_over_2,
                f"{last_1m_open:,.0f}" if last_1m_open else '', f"{last_1m_close:,.0f}" if last_1m_close else '',
                f"{drop_speed_1m:.4f}" if isinstance(drop_speed_1m, (int, float)) else drop_speed_1m
            ])
        logger.info(f"[손절복기] {symbol} | 슬리피지: {slippage_pct:.2f}% | 직전1분하락: {drop_speed_1m}")
    except Exception as e:
        logger.error(f"Loss Review Record Error ({symbol}): {e}")


def update_missed_opportunity_return(symbol, record_time_str, price_at_record, price_60m_later):
    """
    기록된 종목의 60분 후 가격을 조회하여 실제 수익률을 로그에 업데이트.
    (CSV 행을 직접 수정하지 않고, 로그로 남겨 analyzer가 나중에 매칭할 수 있도록 함)
    """
    try:
        if not price_at_record or price_at_record == 0:
            return
        ret_60m = (price_60m_later - price_at_record) / price_at_record * 100
        logger.info(f"[60분수익률] {symbol} | 기록시점: {record_time_str} | 기록가: {price_at_record:,.0f} | 60분후: {price_60m_later:,.0f} | 수익률: {ret_60m:+.2f}%")
    except Exception as e:
        logger.error(f"Update Missed Return Error ({symbol}): {e}")
