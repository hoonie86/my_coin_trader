import pandas as pd
import numpy as np
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

def calculate_rsi(df, period=14):
    """지표 계산 독립화: RSI 계산"""
    delta = df['close'].diff()
    up, down = delta.clip(lower=0), -1 * delta.clip(upper=0)
    ema_up = up.ewm(com=period - 1, adjust=False).mean()
    ema_down = down.ewm(com=period - 1, adjust=False).mean()
    rs = ema_up / ema_down
    return 100 - (100 / (1 + rs))

def check_buy_signal(df, symbol, warning_list, df_1m=None, market_return=0.0):
    """
    매수 신호 판단 통합 모듈
    market_return: 현재 시장의 평균 수익률 (예: -0.03 이면 -3%)
    """
    data_dict = {}
    
    # [방어선 1] 시장 상황 필터: 시장 수익률이 -3% 이하면 모든 매수 중단
    if market_return <= -0.03:
        return False, f"🚫 [시장잠금] 전체시장 하락폭 과다({market_return*100:.1f}%)", "", data_dict

    if len(df) < 185:
        return False, "데이터부족", "", data_dict

    # 1. 지표 계산 (기존 로직 유지)
    df['ma5'] = df['close'].rolling(5).mean()
    df['ma20'] = df['close'].rolling(20).mean()
    df['ma40'] = df['close'].rolling(40).mean()
    df['ma90'] = df['close'].rolling(90).mean()
    df['ma185'] = df['close'].rolling(185).mean()
    df['rsi'] = calculate_rsi(df)

    curr = df.iloc[-1]
    prev = df.iloc[-2]
    curr_price = float(curr['close'])
    
    # 2. 기초 가격/유의종목 필터 (기존 유지)
    if curr_price < 10 or curr_price >= 10000:
        return False, "가격필터(BTC마켓)", "", data_dict

    if symbol.split('/')[0] in warning_list:
        return False, "투자유의", "F", data_dict

    # 윗꼬리 저항 체크 (기존 유지)
    upper_wick_dist_pct = (curr['high'] - curr_price) / curr_price * 100
    if upper_wick_dist_pct >= 2.0:
        return False, f"🚫 [저항과다] 윗꼬리:{upper_wick_dist_pct:.2f}%", "F", data_dict

    # 3. 스테이블 코인 및 185일선 고점 차단 (기존 유지)
    exclude_symbols = ['USDC', 'USDT', 'DAI', 'BUSD']
    if symbol.split('/')[0] in exclude_symbols:
        return False, "제외종목(스테이블)", "", data_dict

    lookback_range = 200
    recent_185 = df['ma185'].iloc[-lookback_range:] if len(df) >= lookback_range else df['ma185']
    if recent_185.max() > recent_185.min():
        pos_185 = (curr['ma185'] - recent_185.min()) / (recent_185.max() - recent_185.min())
        if pos_185 >= 0.7:
            return False, f"🚫 [제외] 185선 고점 구간({pos_185*100:.1f}%)", "B", data_dict

    # 4. 수급 돌파 로직 (S/S+ 급) - 1분봉 및 30분봉 연동
    # (사용자님이 주신 S/S+ 판정 로직이 여기에 그대로 포함됩니다)
    if df_1m is not None and len(df_1m) >= 21:
        vol_avg_20 = df_1m['vol'].tail(20).mean()
        vol_cur = float(df_1m.iloc[-1]['vol'])
        price_3bars_ago_1m = float(df_1m.iloc[-4]['close']) if len(df_1m) >= 4 else 0
        surge_3pct_1m = (price_3bars_ago_1m > 0 and (curr_price - price_3bars_ago_1m) / price_3bars_ago_1m >= 0.02)
        rsi_1m = calculate_rsi(df_1m).iloc[-1]
        
        if vol_avg_20 > 0 and vol_cur >= vol_avg_20 * 3 and surge_3pct_1m:
            if rsi_1m < 70:
                return True, f"💎 [S] 수급 돌파(RSI:{int(rsi_1m)})", "S", data_dict

    # 5. 밥그릇 패턴 및 변동성 필터 (TYPE 3)
    ma40_val = float(curr['ma40'])
    ma185_val = float(curr['ma185'])
    rsi_val = float(curr['rsi'])
    
    # [방어선 2] 골든크로스 및 변동성 체크
    gold_index = -1
    for i in range(1, 97):
        if len(df) > i+1:
            if df['ma40'].iloc[-i-1] < df['ma185'].iloc[-i-1] and df['ma40'].iloc[-i] > df['ma185'].iloc[-i]:
                gold_index = len(df) - i
                break

    if gold_index != -1:
        check_start_idx = max(0, gold_index - 20)
        dynamic_window = df.iloc[check_start_idx:]
        win_low, win_high = dynamic_window['low'].min(), dynamic_window['high'].max()
        dynamic_rise = ((win_high - win_low) / win_low * 100) if win_low > 0 else 0
        
        # [신규 필터] 골크 전후 변동성 5% 이상이면 탈락
        if dynamic_rise >= 5.0:
            return False, f"🚫 [제외] 골크 전후 변동성 과다({dynamic_rise:.1f}%)", "B", data_dict

    # TYPE 3 바닥낚시 (이격도 -8.5% 로직)
    disparity_5_185 = (curr['ma5'] - ma185_val) / ma185_val * 100 if ma185_val > 0 else 0
    candle_body_pct = ((curr_price - curr['open']) / curr['open'] * 100) if curr['open'] > 0 else 0

    if ma40_val < ma185_val and rsi_val <= 40:
        if disparity_5_185 <= -8.5 and 0.5 <= candle_body_pct <= 2.0:
            return True, f"💎 [Type 3] 바닥낚시(이격:{disparity_5_185:.1f}%)", "A", data_dict

    # 최종 기본 반환 (기존 로직 흐름 유지)
    if curr_price <= ma40_val:
        return False, "현재가 <= 40일선", "", data_dict
        
    return False, "조건 미부합", "", data_dict