import pandas as pd
import numpy as np
from datetime import datetime
from utils.inventory_manager import load_inventory, save_inventory  ###### 인벤토리 관리 함수 가정

async def check_sell_signal(exchange, df, symbol, purchase_price, symbol_inventory_age, status, realtime_p, emergency_mode_dict=None):
    """
    [최종 정교화 매도 전략]
    1. 수익 구간별 트레일링 스탑 (최고가 대비 하락)
    2. 30분봉 급등 판정 시 비상 체제 가동
    3. 비상 시 3분봉 패턴 분석 (음봉 킬러, 50% 붕괴)
    4. TYPE3 전용 방어막 유지
    """
    this_profit = ((realtime_p - purchase_price) / purchase_price * 100) if purchase_price > 0 else 0
    is_urgent = False
    sell_reason = ""
    
    # [추가] 인벤토리 내 최고가(max_price) 관리
    inv_data = load_inventory()
    symbol_key = symbol.split('/')[0] if '/' in symbol else symbol
    item = inv_data.get(symbol_key, {})
    this_buy_type = item.get('buy_type', 1)
    
    current_max = item.get('max_price', 0)
    if realtime_p > current_max:
        item['max_price'] = realtime_p
        save_inventory(inv_data)
        current_max = realtime_p

    drop_from_max = ((current_max - realtime_p) / current_max * 100) if current_max > 0 else 0

    # ---------------------------------------------------------
    # 0단계: 수익 구간별 트레일링 스탑 (공통 적용)
    # ---------------------------------------------------------
    if 1.0 <= this_profit < 1.2:
        return True, "💰 [수익방어] 최소 수익(1%) 달성 및 본전 확보", False
    
    elif 1.2 <= this_profit < 2.0 and drop_from_max >= 1.0:
        return True, f"📉 [트레일링] 1.2%구간 고점대비 1% 하락", False
            
    elif 2.0 <= this_profit < 3.5 and drop_from_max >= 1.5:
        return True, f"📉 [트레일링] 2%구간 고점대비 1.5% 하락", False
            
    elif this_profit >= 3.5 and drop_from_max >= 3.0:
        return True, f"📉 [트레일링] 3.5%이상 구간 고점대비 3% 하락", False

    # ---------------------------------------------------------
    # 1단계: 유예 기간 및 급등 판정
    # ---------------------------------------------------------
    curr = df.iloc[-1]
    soaring_rate = (realtime_p - curr['open']) / curr['open'] * 100
    
    # 30분봉 시가 대비 3% 이상 급등 시 비상 모드 강제 진입
    is_emergency_mode = True if soaring_rate >= 3.0 or this_buy_type == 3 else False

    # 매수 초기(6봉 미만) 유예 로직 (단, 긴급 상황 제외)
    if symbol_inventory_age < 6 and not is_emergency_mode:
        return False, "", False

    # ---------------------------------------------------------
    # 2단계: 상황별 매도 신호 (비상/일반)
    # ---------------------------------------------------------
    if is_emergency_mode:
        # [비상체제: 3분봉 데이터 기반 분석]
        # (주의: exchange 객체를 통해 3분봉 별도 호출)
        ohlcv_3m = await asyncio.to_thread(exchange.fetch_ohlcv, symbol, '3m', limit=10)
        df_3m = pd.DataFrame(ohlcv_3m, columns=['time', 'open', 'high', 'low', 'close', 'vol'])
        
        # 패턴 A: 최근 3개 캔들 중 2개가 음봉 (거래량이 최고점 양봉의 10% 이상 실림)
        recent_3 = df_3m.iloc[-3:]
        max_vol_in_3m = df_3m['vol'].max()
        bear_candles = recent_3[(recent_3['close'] < recent_3['open']) & (recent_3['vol'] >= max_vol_in_3m * 0.1)]
        
        if len(bear_candles) >= 2:
            return True, "🚨 [비상] 3분봉 음봉 킬러 패턴 발생", True

        # 패턴 B: 최고점 거래량 돌파 음봉 & 직전 양봉 몸통 50% 이탈
        prev_3m = df_3m.iloc[-2]
        curr_3m = df_3m.iloc[-1]
        body_mid = prev_3m['open'] + (prev_3m['close'] - prev_3m['open']) * 0.5
        if curr_3m['close'] < body_mid and curr_3m['vol'] > prev_3m['vol']:
            return True, "🚨 [비상] 3분봉 직전 양봉 몸통 50% 붕괴", True

        # 비상 수익률 가변 대응
        if 3.0 <= this_profit < 10.0 and drop_from_max >= 2.0:
            return True, "🚨 [비상] 3%~10%구간 -2% 하락 매도", True
        elif this_profit >= 10.0 and (drop_from_max >= 3.0 or this_profit >= 13.0):
            return True, "🚨 [비상] 10%이상구간 -3% 하락 또는 마지노선(13%) 매도", True

    else:
        # [일반체제: 30분봉 기준]
        ma40 = df['close'].rolling(40).mean().iloc[-1]
        ma90 = df['close'].rolling(90).mean().iloc[-1]
        
        if realtime_p < ma40:
            sell_reason = "📉 [일반] 40선 지지 이탈"
        elif pd.isna(ma40) and realtime_p < ma90:
            sell_reason = "📉 [일반] 90선 하향 돌파"
        elif drop_from_max >= 3.0:
            sell_reason = "📉 [일반] 최고점 대비 3% 하락"

        if sell_reason and this_buy_type != 3: # TYPE3는 40/90선 무시
            return True, sell_reason, False

    # ---------------------------------------------------------
    # 3단계: 절대 손절선 (TYPE3 포함 공통)
    # ---------------------------------------------------------
    if this_profit <= -3.0:
        return True, "📉 [절대손절] 진입가 대비 -3% 도달", True

    return False, "", False

async def get_aggressive_sell_price(exchange, symbol, current_price):
    """
    [공격적 매도 호가 산출]
    매도 호가 상위 3개 중 최고가 선택, 이격 0.3% 이상 시 시장가급 던짐
    """
    orderbook = await asyncio.to_thread(exchange.fetch_order_book, symbol)
    # 상위 3개 매도호가 중 최고가
    top_3_asks = [ask[0] for ask in orderbook['asks'][:3]]
    target_p = max(top_3_asks)
    
    diff_rate = (target_p - current_price) / current_price * 100
    if diff_rate >= 0.3:
        # 이격이 크면 현재가보다 한 호가 아래(즉시 체결 유도)
        return orderbook['bids'][0][0] 
    
    return target_p